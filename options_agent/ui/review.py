"""Performance & bias API — WP-9.5.

Thin wrappers over the four pure functions in obs/review.py (cycle_funnel,
hit_rate_by_strategy, pnl_attribution, detect_bias): fetch records/outcomes,
call the function, serialize the report. No new metrics, no new hit
definition — see obs/review.py's module docstring for the analytics design.

Record/outcome fetching mirrors obs/__main__.py's cmd_review/cmd_bias
exactly (query_journal(date_from=since), then query_outcome_records scoped
to the position_ids touched by OPENED/CLOSED/ROLLED cycles in that window)
so these endpoints stay numerically identical to `python -m options_agent.obs
review`/`bias` on the same DB and filters — the parity invariant the WP-9
epic's definition of done requires.

NaN handling: obs/review.py's dataclasses use math.nan for undefined stats
(e.g. avg_win with zero wins). Raw JSON has no NaN literal — json.dumps
would emit an invalid `NaN` token that JSON.parse rejects in the browser —
so every float that can be NaN is converted to None here before leaving
this module.

Insufficient-sample display gating: hit_rate_by_strategy() and
pnl_attribution() have no min-sample-size floor of their own (StrategyStats
only goes NaN at trade_count == 0). The card's "insufficient (n<10)" cell
requirement is a display-layer concern, so this module applies
bias_min_sample_size as a presentation gate on top of the unmodified
StrategyStats — nulling hit_rate/avg_win/avg_loss/expectancy (never
trade_count or total_pnl) when trade_count is below the floor. detect_bias()
already enforces its own min_sample_size internally; its `sufficient` flags
are passed through unchanged.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.engine import Connection

from options_agent.contracts.results import ValidationRuleId
from options_agent.contracts.state import ActionTaken
from options_agent.obs.review import (
    StrategyStats,
    cycle_funnel,
    detect_bias,
    hit_rate_by_strategy,
    pnl_attribution,
)
from options_agent.state.journal import query_journal, query_outcome_records

_OPENED_ACTION_TYPES = frozenset(
    {ActionTaken.OPENED, ActionTaken.CLOSED, ActionTaken.ROLLED}
)


def _nn(value: float) -> float | None:
    """NaN → None; every other float (including 0.0) passes through unchanged."""
    return None if math.isnan(value) else value


def _fetch(conn: Connection, *, since: datetime | None):
    """Records + outcomes for the review window — same shape as the CLI's fetch.

    Returns (records, outcomes). outcomes are scoped to position_ids touched
    by an OPENED/CLOSED/ROLLED record in the window, matching
    obs/__main__.py's cmd_review/cmd_bias exactly.
    """
    records = query_journal(conn, date_from=since)
    position_ids = [
        pid
        for r in records
        if r.action_taken in _OPENED_ACTION_TYPES
        for pid in r.position_ids
    ]
    outcomes = query_outcome_records(conn, position_ids=position_ids or None)
    return records, outcomes


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------


class RejectionRuleCount(BaseModel):
    rule_id: ValidationRuleId
    count: int


class FunnelResponse(BaseModel):
    total: int
    gated: int
    reasoned: int
    no_action_agent: int
    proposed: int
    rejected: int
    sized_to_zero: int
    execution_failed: int
    opened: int
    # Not part of cycle_funnel()'s output — a simple count over the same
    # already-fetched records' rejection_rule_ids, included here rather than
    # as a fifth endpoint since it's a trivial aggregation, not a new metric.
    rejections_by_rule: list[RejectionRuleCount]


def get_funnel(
    conn: Connection,
    *,
    since: datetime | None = None,
    prompt_version: str | None = None,
) -> FunnelResponse:
    """GET /api/review/funnel.

    prompt_version is accepted for a uniform query-param interface across
    the four /api/review/* endpoints (useful to WP-9.6's compare view) but
    is not applied here — cycle_funnel() has no prompt_version filter, and
    neither does the CLI's cmd_review call into it. Applying one locally
    would break the parity invariant against `python -m options_agent.obs
    review`.
    """
    del prompt_version
    records = query_journal(conn, date_from=since)
    report = cycle_funnel(records, since=since)

    rule_counts = Counter(rule_id for r in records for rule_id in r.rejection_rule_ids)
    rejections_by_rule = [
        RejectionRuleCount(rule_id=rule_id, count=count)
        for rule_id, count in sorted(rule_counts.items(), key=lambda kv: -kv[1])
    ]

    return FunnelResponse(
        total=report.total,
        gated=report.gated,
        reasoned=report.reasoned,
        no_action_agent=report.no_action_agent,
        proposed=report.proposed,
        rejected=report.rejected,
        sized_to_zero=report.sized_to_zero,
        execution_failed=report.execution_failed,
        opened=report.opened,
        rejections_by_rule=rejections_by_rule,
    )


# ---------------------------------------------------------------------------
# Hit rate
# ---------------------------------------------------------------------------


class StrategyStatsOut(BaseModel):
    strategy: str
    trade_count: int
    hit_count: int
    miss_count: int
    hit_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    expectancy: float | None
    total_pnl: float
    sufficient: bool


class OpenSummaryOut(BaseModel):
    open_position_count: int
    realized_to_date: float


class HitRateResponse(BaseModel):
    by_strategy: dict[str, StrategyStatsOut]
    overall: StrategyStatsOut
    open_summary: OpenSummaryOut
    min_sample_size: int


def _strategy_stats_out(
    stats: StrategyStats, *, min_sample_size: int
) -> StrategyStatsOut:
    sufficient = stats.trade_count >= min_sample_size

    def rate_field(value: float) -> float | None:
        return None if not sufficient else _nn(value)

    return StrategyStatsOut(
        strategy=stats.strategy,
        trade_count=stats.trade_count,
        hit_count=stats.hit_count,
        miss_count=stats.miss_count,
        hit_rate=rate_field(stats.hit_rate),
        avg_win=rate_field(stats.avg_win),
        avg_loss=rate_field(stats.avg_loss),
        expectancy=rate_field(stats.expectancy),
        total_pnl=stats.total_pnl,
        sufficient=sufficient,
    )


def get_hit_rate(
    conn: Connection,
    *,
    since: datetime | None = None,
    prompt_version: str | None = None,
    min_sample_size: int,
) -> HitRateResponse:
    """GET /api/review/hit-rate."""
    records, outcomes = _fetch(conn, since=since)
    report = hit_rate_by_strategy(
        records, outcomes, since=since, prompt_version=prompt_version
    )

    by_strategy = {
        strategy: _strategy_stats_out(stats, min_sample_size=min_sample_size)
        for strategy, stats in report.by_strategy.items()
    }
    return HitRateResponse(
        by_strategy=by_strategy,
        overall=_strategy_stats_out(report.overall, min_sample_size=min_sample_size),
        open_summary=OpenSummaryOut(
            open_position_count=report.open_summary.open_position_count,
            realized_to_date=report.open_summary.realized_to_date,
        ),
        min_sample_size=min_sample_size,
    )


# ---------------------------------------------------------------------------
# P&L attribution
# ---------------------------------------------------------------------------


class UnderlyingPnLOut(BaseModel):
    underlying: str
    net_pnl: float
    trade_count: int


class StrategyPnLOut(BaseModel):
    strategy: str
    net_pnl: float
    trade_count: int


class AttributionResponse(BaseModel):
    by_underlying: dict[str, UnderlyingPnLOut]
    by_strategy: dict[str, StrategyPnLOut]
    total_realized_pnl: float
    open_summary: OpenSummaryOut


def get_attribution(
    conn: Connection,
    *,
    since: datetime | None = None,
    prompt_version: str | None = None,
) -> AttributionResponse:
    """GET /api/review/attribution."""
    records, outcomes = _fetch(conn, since=since)
    report = pnl_attribution(
        records, outcomes, since=since, prompt_version=prompt_version
    )

    return AttributionResponse(
        by_underlying={
            u: UnderlyingPnLOut(
                underlying=v.underlying, net_pnl=v.net_pnl, trade_count=v.trade_count
            )
            for u, v in report.by_underlying.items()
        },
        by_strategy={
            s: StrategyPnLOut(
                strategy=v.strategy, net_pnl=v.net_pnl, trade_count=v.trade_count
            )
            for s, v in report.by_strategy.items()
        },
        total_realized_pnl=report.total_realized_pnl,
        open_summary=OpenSummaryOut(
            open_position_count=report.open_summary.open_position_count,
            realized_to_date=report.open_summary.realized_to_date,
        ),
    )


# ---------------------------------------------------------------------------
# Bias
# ---------------------------------------------------------------------------


class DeltaSkewOut(BaseModel):
    sample_size: int
    mean_net_delta: float | None
    sufficient: bool
    direction: str


class DirectionWinRateOut(BaseModel):
    direction: str
    sample_size: int
    sufficient: bool
    hit_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    expectancy: float | None
    total_pnl: float


class EventProximityOut(BaseModel):
    near_catalyst: DirectionWinRateOut
    baseline: DirectionWinRateOut


class BiasResponse(BaseModel):
    min_sample_size: int
    window_start: datetime | None
    delta_skew: DeltaSkewOut
    by_direction: dict[str, DirectionWinRateOut]
    event_proximity: EventProximityOut


def _direction_stats_out(stats) -> DirectionWinRateOut:  # type: ignore[no-untyped-def]
    return DirectionWinRateOut(
        direction=stats.direction,
        sample_size=stats.sample_size,
        sufficient=stats.sufficient,
        hit_rate=_nn(stats.hit_rate),
        avg_win=_nn(stats.avg_win),
        avg_loss=_nn(stats.avg_loss),
        expectancy=_nn(stats.expectancy),
        total_pnl=stats.total_pnl,
    )


def get_bias(
    conn: Connection,
    *,
    since: datetime | None = None,
    prompt_version: str | None = None,
    min_sample_size: int,
) -> BiasResponse:
    """GET /api/review/bias."""
    records, outcomes = _fetch(conn, since=since)
    report = detect_bias(
        records,
        outcomes,
        since=since,
        prompt_version=prompt_version,
        min_sample_size=min_sample_size,
    )

    return BiasResponse(
        min_sample_size=report.min_sample_size,
        window_start=report.window_start,
        delta_skew=DeltaSkewOut(
            sample_size=report.delta_skew.sample_size,
            mean_net_delta=_nn(report.delta_skew.mean_net_delta),
            sufficient=report.delta_skew.sufficient,
            direction=report.delta_skew.direction,
        ),
        by_direction={
            d: _direction_stats_out(stats) for d, stats in report.by_direction.items()
        },
        event_proximity=EventProximityOut(
            near_catalyst=_direction_stats_out(report.event_proximity.near_catalyst),
            baseline=_direction_stats_out(report.event_proximity.baseline),
        ),
    )
