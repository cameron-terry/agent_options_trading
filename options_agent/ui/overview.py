"""Overview API aggregation — WP-9.2.

Builds the console's landing-screen data from existing read interfaces only:
positions/orders (state/crud.py), journal_records + outcome_records
(state/journal.py), and kill_switch_log (obs/killswitch.py). No broker or
market-data call anywhere in this module — marks are whatever the monitor
last cached on Position.current_mark/unrealized_pnl.

Portfolio Greeks are deliberately absent from get_tiles(). Computing them
(context.portfolio.aggregate_portfolio_greeks) requires a live FilteredChain
per underlying — a market-data fetch this module must not make. There is no
cached per-leg Greeks store on Position/PositionLeg today; adding one is a
WP-0 contract change, out of scope for this card. (WP-9 epic decision,
2026-07-03.)

Distance-to-trigger reuses monitor.exits.stop_loss_threshold/
profit_target_threshold directly rather than re-deriving the formula, so the
meter can never drift from the monitor's actual trigger math.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.engine import Connection

from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeEventType,
    OutcomeRecord,
)
from options_agent.contracts.state import ActionTaken, AssetClass, Position
from options_agent.monitor.exits import profit_target_threshold, stop_loss_threshold
from options_agent.obs.killswitch import get_current_state
from options_agent.state.crud import get_position, list_open_positions
from options_agent.state.journal import query_journal, query_outcome_records

_ACTIVITY_LIMIT = 20

# A position is "closed" for hit/realized-pnl counting when one of these
# fires — same definition as obs/review.py's _TERMINAL (kept local: that
# module's constant is private and this is a small, stable enum set).
_TERMINAL_OUTCOME_TYPES = frozenset(
    {OutcomeEventType.FULL_CLOSE, OutcomeEventType.EXPIRED, OutcomeEventType.ASSIGNED}
)


class KillSwitchTile(BaseModel):
    state: str


class EquityTile(BaseModel):
    """None fields mean "insufficient data" — no JournalRecord has been written yet."""

    value: float | None
    as_of: datetime | None


class RealizedPnlTile(BaseModel):
    total: float
    closed_count: int
    hit_count: int


class UnrealizedPnlTile(BaseModel):
    total: float
    open_position_count: int


class CyclesTodayTile(BaseModel):
    total: int
    by_action: dict[str, int]


class Tiles(BaseModel):
    account_equity: EquityTile
    realized_pnl: RealizedPnlTile
    unrealized_pnl: UnrealizedPnlTile
    cycles_today: CyclesTodayTile


class EquityCurvePoint(BaseModel):
    timestamp: datetime
    cumulative_realized_pnl: float
    # Anchored so the last point equals the current account_equity tile value
    # (offset = latest_equity - total_realized_pnl); None when no equity
    # reading exists yet (pre-first-cycle).
    equity: float | None


class ActivityItem(BaseModel):
    timestamp: datetime
    kind: Literal["journal", "outcome"]
    action: str
    headline: str
    cycle_id: str | None = None
    position_id: str | None = None


class DistanceToTrigger(BaseModel):
    direction: Literal["stop", "target"]
    pct: float


class PositionSummary(BaseModel):
    id: str
    underlying: str
    strategy: str
    strikes: str
    quantity: int
    entry_net_amount: float
    current_mark: float
    marked_at: datetime
    unrealized_pnl: float
    dte: int | None
    distance_to_trigger: DistanceToTrigger | None


class OverviewResponse(BaseModel):
    kill_switch: KillSwitchTile
    tiles: Tiles
    equity_curve: list[EquityCurvePoint]
    activity: list[ActivityItem]
    mode: Literal["paper", "live"]


def distance_to_trigger(pos: Position, *, today: datetime) -> DistanceToTrigger | None:
    """Normalize pos.unrealized_pnl between 0 (entry) and 1.0 (trigger reached).

    Direction follows the sign of unrealized_pnl: non-negative P&L measures
    distance toward the profit target, negative P&L measures distance toward
    the stop-loss — a position is only ever "heading toward" the threshold on
    its own side. Returns None for EQUITY positions or positions with no
    exit_plan (same guard monitor.exits applies before it will read either
    threshold function).

    today is accepted for signature symmetry with monitor.exits.check_time_stop
    but unused here — the meter is P&L-based, not DTE-based.
    """
    del today
    if pos.asset_class != AssetClass.OPTION_STRATEGY or pos.exit_plan is None:
        return None

    if pos.unrealized_pnl >= 0:
        threshold = profit_target_threshold(pos)
        if threshold <= 0:
            return None
        return DistanceToTrigger(direction="target", pct=pos.unrealized_pnl / threshold)

    threshold = stop_loss_threshold(pos)
    if threshold >= 0:
        return None
    return DistanceToTrigger(direction="stop", pct=pos.unrealized_pnl / threshold)


def _position_dte(pos: Position, *, now: datetime) -> int | None:
    if pos.asset_class != AssetClass.OPTION_STRATEGY:
        return None
    return (pos.nearest_expiration - now.date()).days


def _strikes_summary(pos: Position) -> str:
    """Compact strike string, e.g. "530/525" or "485/480 · 560/565" (iron condor).

    Groups legs by option right (put/call), preserving pos.legs order within
    each group — which is short-leg-then-long-leg by construction (every
    strategy in this codebase opens the short leg first per right). Groups
    are joined in the order they first appear, matching the put-then-call
    convention the design reference uses. A single-leg position (e.g. a cash-
    secured put) renders as just its strike.
    """
    groups: dict[str, list[str]] = {}
    for pos_leg in pos.legs:
        strike = pos_leg.leg.strike
        strike_str = str(int(strike)) if strike == int(strike) else str(strike)
        groups.setdefault(pos_leg.leg.right, []).append(strike_str)
    return " · ".join("/".join(strikes) for strikes in groups.values())


def get_positions(conn: Connection, *, now: datetime) -> list[PositionSummary]:
    """Open positions with distance-to-trigger meters — GET /api/positions."""
    positions = list_open_positions(conn)
    return [
        PositionSummary(
            id=pos.id,
            underlying=pos.underlying,
            strategy=pos.strategy,
            strikes=_strikes_summary(pos),
            quantity=pos.quantity,
            entry_net_amount=pos.entry_net_amount,
            current_mark=pos.current_mark,
            marked_at=pos.marked_at,
            unrealized_pnl=pos.unrealized_pnl,
            dte=_position_dte(pos, now=now),
            distance_to_trigger=distance_to_trigger(pos, today=now),
        )
        for pos in positions
    ]


def _latest_account_equity(conn: Connection) -> tuple[float | None, datetime | None]:
    """Most recent JournalRecord's assembled portfolio.account_equity.

    assembled_context["portfolio"] is PortfolioState.model_dump(mode="json")
    (see context/assembler.py:to_context_snapshot) — a stable, documented
    key, not an arbitrary blob lookup. Only updates once per entry cycle (a
    few times/day), so this tile can lag real account state between cycles;
    there is no live-broker alternative available to this read-only service.
    """
    records = query_journal(conn)
    if not records:
        return None, None
    latest = records[-1]
    portfolio = latest.context_snapshot.assembled_context.get("portfolio")
    if not isinstance(portfolio, dict) or "account_equity" not in portfolio:
        return None, None
    return float(portfolio["account_equity"]), latest.timestamp


def get_tiles(conn: Connection, *, now: datetime) -> Tiles:
    equity_value, equity_as_of = _latest_account_equity(conn)

    outcomes = query_outcome_records(conn)
    closed_outcomes = [o for o in outcomes if o.event_type in _TERMINAL_OUTCOME_TYPES]
    realized_total = sum(o.realized_pnl for o in outcomes)
    hit_count = sum(1 for o in closed_outcomes if o.realized_pnl > 0)

    open_positions = list_open_positions(conn)
    unrealized_total = sum(p.unrealized_pnl for p in open_positions)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_records = query_journal(conn, date_from=today_start, date_to=now)
    by_action: dict[str, int] = {}
    for record in today_records:
        by_action[record.action_taken.value] = (
            by_action.get(record.action_taken.value, 0) + 1
        )

    return Tiles(
        account_equity=EquityTile(value=equity_value, as_of=equity_as_of),
        realized_pnl=RealizedPnlTile(
            total=realized_total,
            closed_count=len(closed_outcomes),
            hit_count=hit_count,
        ),
        unrealized_pnl=UnrealizedPnlTile(
            total=unrealized_total, open_position_count=len(open_positions)
        ),
        cycles_today=CyclesTodayTile(total=len(today_records), by_action=by_action),
    )


def get_equity_curve(conn: Connection) -> list[EquityCurvePoint]:
    """Cumulative realized P&L over time, anchored to the latest known equity.

    Anchor: offset = latest_account_equity - total_realized_pnl, so the last
    point equals the account_equity tile exactly (both read the same
    underlying reality — cumulative realized gains/losses since inception).
    equity is None on every point when no account_equity reading exists yet;
    cumulative_realized_pnl is always populated (needs only outcome_records).
    """
    outcomes = query_outcome_records(conn)
    equity_value, _ = _latest_account_equity(conn)
    total_realized = sum(o.realized_pnl for o in outcomes)
    offset = (equity_value - total_realized) if equity_value is not None else None

    points: list[EquityCurvePoint] = []
    running = 0.0
    for outcome in outcomes:
        running += outcome.realized_pnl
        points.append(
            EquityCurvePoint(
                timestamp=outcome.recorded_at,
                cumulative_realized_pnl=running,
                equity=(offset + running) if offset is not None else None,
            )
        )
    return points


def _journal_headline(record: JournalRecord) -> str:
    action = record.action_taken
    underlying = record.underlying or "—"

    if action == ActionTaken.OPENED:
        return f"OPENED {underlying} {record.strategy or ''}".strip()
    if action == ActionTaken.CLOSED:
        return f"CLOSED {underlying} {record.strategy or ''}".strip()
    if action == ActionTaken.ROLLED:
        return f"ROLLED {underlying} {record.strategy or ''}".strip()
    if action == ActionTaken.NO_ACTION_AGENT:
        return (
            f"NO_ACTION_AGENT {underlying}" if record.underlying else "NO_ACTION_AGENT"
        )
    if action == ActionTaken.NO_ACTION_GATED:
        # No gate-reason field is persisted on gated cycles (decision.proposal
        # and decision.validation_result are both None) — nothing to append.
        return "NO_ACTION_GATED"
    if action == ActionTaken.SIZED_TO_ZERO:
        return f"SIZED_TO_ZERO {underlying}"
    if action == ActionTaken.EXECUTION_FAILED:
        return f"EXECUTION_FAILED {underlying}"
    if action == ActionTaken.REJECTED:
        reasons = ", ".join(r.value for r in record.rejection_rule_ids)
        return (
            f"REJECTED {underlying} — {reasons}"
            if reasons
            else f"REJECTED {underlying}"
        )
    return f"{action.value} {underlying}"


def _outcome_headline(outcome: OutcomeRecord, *, underlying: str | None) -> str:
    label = underlying or outcome.position_id
    return f"{outcome.event_type.value} {label} — realized {outcome.realized_pnl:+.2f}"


def get_activity(
    conn: Connection, *, limit: int = _ACTIVITY_LIMIT
) -> list[ActivityItem]:
    """Most recent journal + outcome events, newest first, capped at *limit*.

    Both query_journal and query_outcome_records return ascending-by-time,
    unfiltered result sets (no LIMIT/OFFSET support today) — this merges and
    slices in Python. Fine at console scale; would need a DB-side limit if
    the journal grows past what's comfortable to fetch in full per request.
    """
    records = query_journal(conn)
    outcomes = query_outcome_records(conn)

    # Resolve underlying for outcome rows via Position — cached per position_id
    # to avoid a query per outcome row.
    underlying_cache: dict[str, str | None] = {}

    def _underlying_for(position_id: str) -> str | None:
        if position_id not in underlying_cache:
            pos = get_position(conn, position_id)
            underlying_cache[position_id] = pos.underlying if pos else None
        return underlying_cache[position_id]

    items = [
        ActivityItem(
            timestamp=r.timestamp,
            kind="journal",
            action=r.action_taken.value,
            headline=_journal_headline(r),
            cycle_id=r.cycle_id,
            position_id=r.position_ids[0] if r.position_ids else None,
        )
        for r in records
    ] + [
        ActivityItem(
            timestamp=o.recorded_at,
            kind="outcome",
            action=o.event_type.value,
            headline=_outcome_headline(o, underlying=_underlying_for(o.position_id)),
            position_id=o.position_id,
        )
        for o in outcomes
    ]
    items.sort(key=lambda item: item.timestamp, reverse=True)
    return items[:limit]


def get_overview(
    conn: Connection,
    *,
    now: datetime | None = None,
    mode: Literal["paper", "live"] = "paper",
) -> OverviewResponse:
    now = now or datetime.now(UTC)
    return OverviewResponse(
        kill_switch=KillSwitchTile(state=get_current_state(conn).value),
        tiles=get_tiles(conn, now=now),
        equity_curve=get_equity_curve(conn),
        activity=get_activity(conn),
        mode=mode,
    )
