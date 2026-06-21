"""Journal analytics: hit rate, P&L attribution, cycle funnel (WP-7.3).

Three pure functions — no DB calls, no live-data dependencies:

    hit_rate_by_strategy(records, outcomes, *, since, prompt_version)
    pnl_attribution(records, outcomes, *, since, prompt_version)
    cycle_funnel(records, *, since)

All three operate on pre-fetched JournalRecord and OutcomeRecord objects
(fetched by the caller via state.journal query functions). They are
deterministic, fixture-testable, and safe to call from any context.

Design notes
------------
Hit definition: realized_pnl > 0 (mechanism-agnostic). ExitReason is NOT
used to define a hit — it measures exit plumbing, not trade quality.

Hit rate is never presented without P&L context (avg win, avg loss,
expectancy) because premium-selling strategies are designed for asymmetric
win rates. A standalone hit rate actively misleads.

Open positions: only fully-closed positions (FULL_CLOSE / EXPIRED /
ASSIGNED) count toward headline metrics. Partial-close proceeds from
still-open positions are reported separately in open_summary.

Funnel: kept separate from hit rate. It counts all cycles by action_taken
and is the primary diagnostic during warm-up (when the agent is gated or
inactive more often than it opens positions).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeEventType,
    OutcomeRecord,
)
from options_agent.contracts.state import ActionTaken

# ---------------------------------------------------------------------------
# Terminal outcome types — a position is "closed" when one of these fires
# ---------------------------------------------------------------------------

_TERMINAL = frozenset(
    {OutcomeEventType.FULL_CLOSE, OutcomeEventType.EXPIRED, OutcomeEventType.ASSIGNED}
)

# action_taken values where the LLM was NOT called (short-circuited before it)
_GATED = frozenset({ActionTaken.NO_ACTION_GATED})

# action_taken values where the LLM was called but returned no specific proposal
_NO_PROPOSAL = frozenset({ActionTaken.NO_ACTION_GATED, ActionTaken.NO_ACTION_AGENT})


# ---------------------------------------------------------------------------
# Return types — dataclasses (not Pydantic) because these are computed
# outputs, not validated inputs. Use dataclasses.asdict() for serialization.
# ---------------------------------------------------------------------------


@dataclass
class StrategyStats:
    """Hit rate + P&L summary for one strategy bucket (or "_all" for overall)."""

    strategy: str
    trade_count: int
    hit_count: int
    miss_count: int
    # NaN when trade_count == 0
    hit_rate: float
    # NaN when the bucket has no wins / no losses
    avg_win: float
    avg_loss: float
    # avg_win * hit_rate + avg_loss * miss_rate; NaN when trade_count == 0
    expectancy: float
    total_pnl: float


@dataclass
class OpenSummary:
    """Realized proceeds from still-open positions (partial closes only)."""

    open_position_count: int
    realized_to_date: float


@dataclass
class HitRateReport:
    """Output of hit_rate_by_strategy()."""

    by_strategy: dict[str, StrategyStats]
    overall: StrategyStats
    open_summary: OpenSummary


@dataclass
class UnderlyingPnL:
    underlying: str
    net_pnl: float
    trade_count: int


@dataclass
class StrategyPnL:
    strategy: str
    net_pnl: float
    trade_count: int


@dataclass
class PnLAttributionReport:
    """Output of pnl_attribution()."""

    by_underlying: dict[str, UnderlyingPnL]
    by_strategy: dict[str, StrategyPnL]
    total_realized_pnl: float
    open_summary: OpenSummary


@dataclass
class CycleFunnelReport:
    """Output of cycle_funnel() — full entry-cycle breakdown by action_taken."""

    total: int
    gated: int
    reasoned: int
    no_action_agent: int
    proposed: int
    rejected: int
    sized_to_zero: int
    execution_failed: int
    opened: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _apply_filters(
    records: Sequence[JournalRecord],
    *,
    since: datetime | None,
    prompt_version: str | None,
) -> list[JournalRecord]:
    out = list(records)
    if since is not None:
        out = [r for r in out if r.timestamp >= since]
    if prompt_version is not None:
        out = [r for r in out if r.prompt_version == prompt_version]
    return out


def _build_position_map(records: Sequence[JournalRecord]) -> dict[str, JournalRecord]:
    """Map each position_id → its opening JournalRecord."""
    pos_map: dict[str, JournalRecord] = {}
    for record in records:
        for pid in record.position_ids:
            pos_map[pid] = record
    return pos_map


def _split_outcomes(
    outcomes: Sequence[OutcomeRecord],
    pos_map: dict[str, JournalRecord],
) -> tuple[dict[str, tuple[JournalRecord, list[OutcomeRecord]]], OpenSummary]:
    """Partition outcomes into closed trades and open-position addendum.

    A position is "closed" when any of its OutcomeRecords has a terminal
    event type (FULL_CLOSE, EXPIRED, ASSIGNED). Positions with only
    PARTIAL_CLOSE events are still open — their partial P&L goes into
    open_summary.

    Only positions present in pos_map (i.e. with an associated opening
    JournalRecord) are included. Monitor-driven closes on positions opened
    before WP-2 was live have no journal record and are silently skipped.
    """
    by_pos: dict[str, list[OutcomeRecord]] = {}
    for o in outcomes:
        by_pos.setdefault(o.position_id, []).append(o)

    closed_trades: dict[str, tuple[JournalRecord, list[OutcomeRecord]]] = {}
    open_pos_count = 0
    open_pnl = 0.0

    for pid, pos_outcomes in by_pos.items():
        jr = pos_map.get(pid)
        if jr is None:
            continue

        has_terminal = any(o.event_type in _TERMINAL for o in pos_outcomes)
        if has_terminal:
            closed_trades[pid] = (jr, pos_outcomes)
        else:
            open_pos_count += 1
            open_pnl += sum(o.realized_pnl for o in pos_outcomes)

    return closed_trades, OpenSummary(
        open_position_count=open_pos_count,
        realized_to_date=open_pnl,
    )


def _compute_stats(strategy: str, pnls: list[float]) -> StrategyStats:
    """Build StrategyStats from per-position total realized P&Ls."""
    if not pnls:
        nan = math.nan
        return StrategyStats(
            strategy=strategy,
            trade_count=0,
            hit_count=0,
            miss_count=0,
            hit_rate=nan,
            avg_win=nan,
            avg_loss=nan,
            expectancy=nan,
            total_pnl=0.0,
        )

    hits = [p for p in pnls if p > 0]
    misses = [p for p in pnls if p <= 0]

    hit_rate = len(hits) / len(pnls)
    miss_rate = 1.0 - hit_rate
    avg_win = sum(hits) / len(hits) if hits else math.nan
    avg_loss = sum(misses) / len(misses) if misses else math.nan

    if hits and misses:
        expectancy = avg_win * hit_rate + avg_loss * miss_rate
    elif hits:
        expectancy = avg_win
    else:
        expectancy = avg_loss

    return StrategyStats(
        strategy=strategy,
        trade_count=len(pnls),
        hit_count=len(hits),
        miss_count=len(misses),
        hit_rate=hit_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        expectancy=expectancy,
        total_pnl=sum(pnls),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def hit_rate_by_strategy(
    records: Sequence[JournalRecord],
    outcomes: Sequence[OutcomeRecord],
    *,
    since: datetime | None = None,
    prompt_version: str | None = None,
) -> HitRateReport:
    """Per-strategy hit rate paired with P&L context.

    A "hit" is any fully-closed trade with total realized_pnl > 0. The
    definition is mechanism-agnostic — a win via stop-loss, profit-target,
    DTE exit, or expiry all count equally.

    Hit rate is always accompanied by avg win, avg loss, and expectancy so
    the asymmetric win-rate structure of credit strategies cannot mislead.

    Args:
        records:        All JournalRecords (any action_taken).
        outcomes:       All OutcomeRecords for the positions to analyze.
        since:          Filter to records with timestamp >= since.
        prompt_version: Filter to a specific prompt version (for before/after
                        comparison after a prompt change).
    """
    filtered = _apply_filters(records, since=since, prompt_version=prompt_version)
    pos_map = _build_position_map(filtered)
    closed_trades, open_summary = _split_outcomes(outcomes, pos_map)

    strategy_pnls: dict[str, list[float]] = {}
    for jr, pos_outcomes in closed_trades.values():
        strategy = jr.strategy or "_unknown"
        total_pnl = sum(o.realized_pnl for o in pos_outcomes)
        strategy_pnls.setdefault(strategy, []).append(total_pnl)

    by_strategy = {
        s: _compute_stats(s, pnls) for s, pnls in sorted(strategy_pnls.items())
    }
    all_pnls = [p for pnls in strategy_pnls.values() for p in pnls]
    overall = _compute_stats("_all", all_pnls)

    return HitRateReport(
        by_strategy=by_strategy,
        overall=overall,
        open_summary=open_summary,
    )


def pnl_attribution(
    records: Sequence[JournalRecord],
    outcomes: Sequence[OutcomeRecord],
    *,
    since: datetime | None = None,
    prompt_version: str | None = None,
) -> PnLAttributionReport:
    """P&L attribution broken down by underlying and by strategy.

    Only fully-closed positions contribute to the headline figures.
    Partial-close proceeds from still-open positions appear separately
    in open_summary and are never mixed into the closed-trade totals.

    Args:
        records:        All JournalRecords (any action_taken).
        outcomes:       All OutcomeRecords for the positions to analyze.
        since:          Filter to records with timestamp >= since.
        prompt_version: Filter to a specific prompt version.
    """
    filtered = _apply_filters(records, since=since, prompt_version=prompt_version)
    pos_map = _build_position_map(filtered)
    closed_trades, open_summary = _split_outcomes(outcomes, pos_map)

    underlying_pnls: dict[str, list[float]] = {}
    strategy_pnls: dict[str, list[float]] = {}

    for jr, pos_outcomes in closed_trades.values():
        total_pnl = sum(o.realized_pnl for o in pos_outcomes)
        underlying = jr.underlying or "_unknown"
        strategy = jr.strategy or "_unknown"
        underlying_pnls.setdefault(underlying, []).append(total_pnl)
        strategy_pnls.setdefault(strategy, []).append(total_pnl)

    by_underlying = {
        u: UnderlyingPnL(underlying=u, net_pnl=sum(pnls), trade_count=len(pnls))
        for u, pnls in sorted(underlying_pnls.items())
    }
    by_strategy = {
        s: StrategyPnL(strategy=s, net_pnl=sum(pnls), trade_count=len(pnls))
        for s, pnls in sorted(strategy_pnls.items())
    }
    total_realized_pnl = sum(
        sum(o.realized_pnl for o in pos_outcomes)
        for _, pos_outcomes in closed_trades.values()
    )

    return PnLAttributionReport(
        by_underlying=by_underlying,
        by_strategy=by_strategy,
        total_realized_pnl=total_realized_pnl,
        open_summary=open_summary,
    )


def cycle_funnel(
    records: Sequence[JournalRecord],
    *,
    since: datetime | None = None,
) -> CycleFunnelReport:
    """Full entry-cycle funnel from JournalRecord.action_taken values.

    Kept separate from hit_rate / pnl_attribution — the funnel counts all
    cycles, not just those that opened positions. During the warm-up phase
    (before enough journal data exists for meaningful hit rates), the funnel
    is the primary diagnostic.

    Stage definitions:
      total            All cycles in the window.
      gated            NO_ACTION_GATED — short-circuited before the LLM call.
      reasoned         total - gated — LLM was called.
      no_action_agent  LLM returned action=NO_ACTION.
      proposed         reasoned - no_action_agent — agent returned a specific proposal.
      rejected         Proposal failed deterministic validation.
      sized_to_zero    Passed validation but sizing returned 0 contracts.
      execution_failed Passed validation+sizing but broker rejected the order.
      opened           Position successfully opened.

    Args:
        records: All JournalRecords (any action_taken).
        since:   Filter to records with timestamp >= since.
    """
    filtered = (
        records if since is None else [r for r in records if r.timestamp >= since]
    )

    total = len(filtered)
    gated = sum(1 for r in filtered if r.action_taken == ActionTaken.NO_ACTION_GATED)
    no_action_agent = sum(
        1 for r in filtered if r.action_taken == ActionTaken.NO_ACTION_AGENT
    )
    proposed = sum(1 for r in filtered if r.action_taken not in _NO_PROPOSAL)
    rejected = sum(1 for r in filtered if r.action_taken == ActionTaken.REJECTED)
    sized_to_zero = sum(
        1 for r in filtered if r.action_taken == ActionTaken.SIZED_TO_ZERO
    )
    execution_failed = sum(
        1 for r in filtered if r.action_taken == ActionTaken.EXECUTION_FAILED
    )
    opened = sum(1 for r in filtered if r.action_taken == ActionTaken.OPENED)

    return CycleFunnelReport(
        total=total,
        gated=gated,
        reasoned=total - gated,
        no_action_agent=no_action_agent,
        proposed=proposed,
        rejected=rejected,
        sized_to_zero=sized_to_zero,
        execution_failed=execution_failed,
        opened=opened,
    )
