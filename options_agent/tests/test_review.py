"""Tests for obs/review.py — hit_rate_by_strategy, pnl_attribution, cycle_funnel.

All tests are pure (no DB); data is built from fixture factory functions.

Coverage
--------
  hit_rate_by_strategy:
    - empty journal
    - all wins
    - all losses
    - mixed wins/losses
    - multi-strategy grouping
    - open (partially-closed) positions excluded from closed stats
    - partial-close then full-close: both events sum into one position P&L
    - since filter
    - prompt_version filter

  pnl_attribution:
    - by-underlying and by-strategy totals match
    - open positions in open_summary, not in totals
    - since filter

  cycle_funnel:
    - all action_taken values tallied correctly
    - since filter
    - gated vs reasoned split
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest

from options_agent.contracts import (
    ActionTaken,
    ContextSnapshot,
    Decision,
    ExitPlan,
    JournalRecord,
    Leg,
    OutcomeEventType,
    OutcomeRecord,
    RejectionReason,
    Severity,
    SizingConstraint,
    SizingResult,
    TradeProposal,
    ValidationResult,
    ValidationRuleId,
)
from options_agent.obs.review import (
    cycle_funnel,
    hit_rate_by_strategy,
    pnl_attribution,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_T2 = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)

_LEG = Leg(right="call", side="sell", strike=500.0, expiration=date(2026, 8, 15))
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.50, time_stop_dte=21
)


def _proposal(
    strategy: str = "bull_put_spread", underlying: str = "SPY"
) -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying=underlying,
        strategy=strategy,
        legs=[_LEG],
        thesis="test",
        iv_rationale="IV rank high",
        catalyst_check="No earnings",
        conviction=0.65,
        est_max_loss=1000.0,
        est_max_profit=200.0,
        breakevens=[490.0],
        net_delta=0.10,
        net_theta=5.0,
        net_vega=-0.20,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )


def _decision(
    action_taken: ActionTaken = ActionTaken.OPENED,
    strategy: str = "bull_put_spread",
    underlying: str = "SPY",
) -> Decision:
    has_proposal = action_taken not in {
        ActionTaken.NO_ACTION_GATED,
        ActionTaken.NO_ACTION_AGENT,
    }
    if action_taken == ActionTaken.REJECTED:
        vr = ValidationResult(
            passed=False,
            reasons=[
                RejectionReason(
                    rule_id=ValidationRuleId.MAX_LOSS_CAP,
                    severity=Severity.ERROR,
                    human_message="test rejection",
                )
            ],
        )
    else:
        vr = ValidationResult(passed=True)

    return Decision(
        proposal=_proposal(strategy, underlying) if has_proposal else None,
        validation_result=vr,
        sizing_result=SizingResult(
            contracts=2,
            sized_max_loss=1000.0,
            sized_max_profit=200.0,
            risk_budget_used=0.01,
            binding_constraint=SizingConstraint.RISK_BUDGET,
        )
        if action_taken == ActionTaken.OPENED
        else None,
        action_taken=action_taken,
    )


def _snapshot(prompt_version: str = "v1") -> ContextSnapshot:
    return ContextSnapshot(
        assembled_context={"iv_rank": 72},
        context_hash="sha256:abc",
        model_id="claude-sonnet-4-6",
        prompt_version=prompt_version,
        assembled_at=_T0,
    )


_CYCLE_COUNTER = [0]


def _jr(
    *,
    action_taken: ActionTaken = ActionTaken.OPENED,
    position_ids: list[str] | None = None,
    strategy: str = "bull_put_spread",
    underlying: str = "SPY",
    timestamp: datetime = _T0,
    prompt_version: str = "v1",
    cycle_id: str | None = None,
) -> JournalRecord:
    _CYCLE_COUNTER[0] += 1
    cid = cycle_id or f"cycle-{_CYCLE_COUNTER[0]:04d}"
    rejection_rule_ids = (
        [ValidationRuleId.MAX_LOSS_CAP] if action_taken == ActionTaken.REJECTED else []
    )
    return JournalRecord(
        cycle_id=cid,
        timestamp=timestamp,
        action_taken=action_taken,
        decision=_decision(action_taken, strategy, underlying),
        context_snapshot=_snapshot(prompt_version),
        position_ids=position_ids
        or (["pos-" + cid] if action_taken == ActionTaken.OPENED else []),
        order_ids=[],
        strategy=strategy if action_taken == ActionTaken.OPENED else None,
        underlying=underlying if action_taken == ActionTaken.OPENED else None,
        limits_version="v1",
        prompt_version=prompt_version,
        model_id="claude-sonnet-4-6",
        rejection_rule_ids=rejection_rule_ids,
    )


_OUTCOME_COUNTER = [0]


def _outcome(
    position_id: str,
    realized_pnl: float,
    event_type: OutcomeEventType = OutcomeEventType.FULL_CLOSE,
    recorded_at: datetime = _T1,
) -> OutcomeRecord:
    _OUTCOME_COUNTER[0] += 1
    return OutcomeRecord(
        id=f"outcome-{_OUTCOME_COUNTER[0]:04d}",
        position_id=position_id,
        event_type=event_type,
        recorded_at=recorded_at,
        contracts_closed=2,
        realized_pnl=realized_pnl,
    )


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — empty
# ---------------------------------------------------------------------------


def test_hit_rate_empty_journal() -> None:
    report = hit_rate_by_strategy([], [])
    assert report.by_strategy == {}
    assert report.overall.trade_count == 0
    assert math.isnan(report.overall.hit_rate)
    assert math.isnan(report.overall.expectancy)
    assert report.overall.total_pnl == 0.0
    assert report.open_summary.open_position_count == 0


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — all wins
# ---------------------------------------------------------------------------


def test_hit_rate_all_wins() -> None:
    r = _jr(position_ids=["p1"])
    o = _outcome("p1", 150.0)
    report = hit_rate_by_strategy([r], [o])

    assert report.overall.trade_count == 1
    assert report.overall.hit_count == 1
    assert report.overall.miss_count == 0
    assert report.overall.hit_rate == pytest.approx(1.0)
    assert report.overall.avg_win == pytest.approx(150.0)
    assert math.isnan(report.overall.avg_loss)
    assert report.overall.expectancy == pytest.approx(150.0)
    assert report.overall.total_pnl == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — all losses
# ---------------------------------------------------------------------------


def test_hit_rate_all_losses() -> None:
    r = _jr(position_ids=["p2"])
    o = _outcome("p2", -400.0)
    report = hit_rate_by_strategy([r], [o])

    assert report.overall.trade_count == 1
    assert report.overall.hit_count == 0
    assert report.overall.miss_count == 1
    assert report.overall.hit_rate == pytest.approx(0.0)
    assert math.isnan(report.overall.avg_win)
    assert report.overall.avg_loss == pytest.approx(-400.0)
    assert report.overall.expectancy == pytest.approx(-400.0)
    assert report.overall.total_pnl == pytest.approx(-400.0)


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — mixed wins/losses + expectancy
# ---------------------------------------------------------------------------


def test_hit_rate_mixed() -> None:
    r1 = _jr(position_ids=["p3"])
    r2 = _jr(position_ids=["p4"])
    r3 = _jr(position_ids=["p5"])
    o1 = _outcome("p3", 200.0)
    o2 = _outcome("p4", 200.0)
    o3 = _outcome("p5", -800.0)
    report = hit_rate_by_strategy([r1, r2, r3], [o1, o2, o3])

    overall = report.overall
    assert overall.trade_count == 3
    assert overall.hit_count == 2
    assert overall.miss_count == 1
    assert overall.hit_rate == pytest.approx(2 / 3)
    assert overall.avg_win == pytest.approx(200.0)
    assert overall.avg_loss == pytest.approx(-800.0)
    # expectancy = 200 * (2/3) + (-800) * (1/3)
    assert overall.expectancy == pytest.approx(200 * (2 / 3) + (-800) * (1 / 3))
    assert overall.total_pnl == pytest.approx(-400.0)


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — multi-strategy grouping
# ---------------------------------------------------------------------------


def test_hit_rate_multi_strategy() -> None:
    r_bps = _jr(strategy="bull_put_spread", position_ids=["p6"])
    r_ic = _jr(strategy="iron_condor", position_ids=["p7"])
    r_ic2 = _jr(strategy="iron_condor", position_ids=["p8"])
    o1 = _outcome("p6", 100.0)  # bull_put_spread win
    o2 = _outcome("p7", -300.0)  # iron_condor loss
    o3 = _outcome("p8", 150.0)  # iron_condor win
    report = hit_rate_by_strategy([r_bps, r_ic, r_ic2], [o1, o2, o3])

    assert "bull_put_spread" in report.by_strategy
    assert "iron_condor" in report.by_strategy

    bps = report.by_strategy["bull_put_spread"]
    assert bps.trade_count == 1
    assert bps.hit_rate == pytest.approx(1.0)
    assert bps.total_pnl == pytest.approx(100.0)

    ic = report.by_strategy["iron_condor"]
    assert ic.trade_count == 2
    assert ic.hit_count == 1
    assert ic.miss_count == 1
    assert ic.hit_rate == pytest.approx(0.5)
    assert ic.total_pnl == pytest.approx(-150.0)

    # Overall aggregates all three
    assert report.overall.trade_count == 3


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — open positions in open_summary, not closed stats
# ---------------------------------------------------------------------------


def test_hit_rate_open_position_excluded_from_closed_stats() -> None:
    r_closed = _jr(position_ids=["p9"])
    r_open = _jr(position_ids=["p10"])
    o_closed = _outcome("p9", 100.0, OutcomeEventType.FULL_CLOSE)
    # p10 only has a partial close — still open
    o_partial = _outcome("p10", 50.0, OutcomeEventType.PARTIAL_CLOSE)

    report = hit_rate_by_strategy([r_closed, r_open], [o_closed, o_partial])

    # Closed stats: only p9
    assert report.overall.trade_count == 1
    assert report.overall.total_pnl == pytest.approx(100.0)

    # Open summary: p10
    assert report.open_summary.open_position_count == 1
    assert report.open_summary.realized_to_date == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — partial then full close: pnls sum correctly
# ---------------------------------------------------------------------------


def test_hit_rate_partial_then_full_close_sums_pnl() -> None:
    r = _jr(position_ids=["p11"])
    o_partial = _outcome("p11", 60.0, OutcomeEventType.PARTIAL_CLOSE)
    o_full = _outcome("p11", -20.0, OutcomeEventType.FULL_CLOSE)

    report = hit_rate_by_strategy([r], [o_partial, o_full])

    # Total realized P&L for p11 = 60 + (-20) = 40 → hit
    assert report.overall.trade_count == 1
    assert report.overall.hit_count == 1
    assert report.overall.total_pnl == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — CLOSED/ROLLED records sharing a position_id must not
# overwrite the opening OPENED record in _build_position_map
# ---------------------------------------------------------------------------


def test_hit_rate_closed_record_does_not_shadow_opening_record() -> None:
    # p12 was opened (bull_put_spread / SPY) by r_open, then closed by an agent
    # CLOSE cycle r_close (which also carries position_ids=["p12"] but has
    # strategy=None, underlying=None). If _build_position_map includes non-OPENED
    # records, r_close overwrites r_open and the position is attributed to
    # "_unknown". The fix (filter to OPENED only) preserves the opening metadata.
    r_open = _jr(
        action_taken=ActionTaken.OPENED,
        position_ids=["p12"],
        strategy="bull_put_spread",
        underlying="SPY",
    )
    r_close = _jr(
        action_taken=ActionTaken.CLOSED,
        position_ids=["p12"],
    )
    o = _outcome("p12", 175.0)

    report = hit_rate_by_strategy([r_open, r_close], [o])

    # Must be attributed to bull_put_spread, not "_unknown"
    assert "bull_put_spread" in report.by_strategy
    assert "_unknown" not in report.by_strategy
    assert report.by_strategy["bull_put_spread"].trade_count == 1
    assert report.by_strategy["bull_put_spread"].total_pnl == pytest.approx(175.0)

    # Closing cycle should not create a second trade entry
    assert report.overall.trade_count == 1


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — since filter
# ---------------------------------------------------------------------------


def test_hit_rate_since_filter() -> None:
    r_old = _jr(position_ids=["p12"], timestamp=_T0)
    r_new = _jr(position_ids=["p13"], timestamp=_T2)
    o_old = _outcome("p12", 100.0)
    o_new = _outcome("p13", 200.0)

    report = hit_rate_by_strategy([r_old, r_new], [o_old, o_new], since=_T1)

    # Only r_new (at _T2 >= _T1) is in scope; p12's outcome excluded
    assert report.overall.trade_count == 1
    assert report.overall.total_pnl == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# hit_rate_by_strategy — prompt_version filter
# ---------------------------------------------------------------------------


def test_hit_rate_prompt_version_filter() -> None:
    r_v1 = _jr(position_ids=["p14"], prompt_version="v1")
    r_v2 = _jr(position_ids=["p15"], prompt_version="v2")
    o1 = _outcome("p14", 100.0)
    o2 = _outcome("p15", -500.0)

    report_v2 = hit_rate_by_strategy([r_v1, r_v2], [o1, o2], prompt_version="v2")

    assert report_v2.overall.trade_count == 1
    assert report_v2.overall.total_pnl == pytest.approx(-500.0)


# ---------------------------------------------------------------------------
# pnl_attribution — by underlying and by strategy
# ---------------------------------------------------------------------------


def test_pnl_attribution_by_underlying_and_strategy() -> None:
    r_spy = _jr(underlying="SPY", strategy="bull_put_spread", position_ids=["p16"])
    r_qqq = _jr(underlying="QQQ", strategy="iron_condor", position_ids=["p17"])
    r_spy2 = _jr(underlying="SPY", strategy="iron_condor", position_ids=["p18"])
    o1 = _outcome("p16", 100.0)
    o2 = _outcome("p17", -200.0)
    o3 = _outcome("p18", 300.0)

    report = pnl_attribution([r_spy, r_qqq, r_spy2], [o1, o2, o3])

    assert report.by_underlying["SPY"].net_pnl == pytest.approx(400.0)
    assert report.by_underlying["SPY"].trade_count == 2
    assert report.by_underlying["QQQ"].net_pnl == pytest.approx(-200.0)
    assert report.by_underlying["QQQ"].trade_count == 1

    assert report.by_strategy["bull_put_spread"].net_pnl == pytest.approx(100.0)
    assert report.by_strategy["iron_condor"].net_pnl == pytest.approx(100.0)

    assert report.total_realized_pnl == pytest.approx(200.0)


def test_pnl_attribution_open_position_not_in_total() -> None:
    r_closed = _jr(position_ids=["p19"])
    r_open = _jr(position_ids=["p20"])
    o_closed = _outcome("p19", 100.0, OutcomeEventType.FULL_CLOSE)
    o_partial = _outcome("p20", 40.0, OutcomeEventType.PARTIAL_CLOSE)

    report = pnl_attribution([r_closed, r_open], [o_closed, o_partial])

    assert report.total_realized_pnl == pytest.approx(100.0)
    assert report.open_summary.open_position_count == 1
    assert report.open_summary.realized_to_date == pytest.approx(40.0)


def test_pnl_attribution_since_filter() -> None:
    r_old = _jr(position_ids=["p21"], timestamp=_T0)
    r_new = _jr(position_ids=["p22"], timestamp=_T2)
    o_old = _outcome("p21", 100.0)
    o_new = _outcome("p22", 250.0)

    report = pnl_attribution([r_old, r_new], [o_old, o_new], since=_T1)

    assert report.total_realized_pnl == pytest.approx(250.0)
    assert len(report.by_underlying) == 1


# ---------------------------------------------------------------------------
# cycle_funnel — all action_taken values
# ---------------------------------------------------------------------------


def test_cycle_funnel_all_action_types() -> None:
    records = [
        _jr(action_taken=ActionTaken.OPENED),
        _jr(action_taken=ActionTaken.OPENED),
        _jr(action_taken=ActionTaken.NO_ACTION_GATED),
        _jr(action_taken=ActionTaken.NO_ACTION_GATED),
        _jr(action_taken=ActionTaken.NO_ACTION_GATED),
        _jr(action_taken=ActionTaken.NO_ACTION_AGENT),
        _jr(action_taken=ActionTaken.REJECTED),
        _jr(action_taken=ActionTaken.SIZED_TO_ZERO),
        _jr(action_taken=ActionTaken.EXECUTION_FAILED),
    ]
    report = cycle_funnel(records)

    assert report.total == 9
    assert report.gated == 3
    assert report.reasoned == 6  # total - gated
    assert report.no_action_agent == 1
    assert report.proposed == 5  # reasoned - no_action_agent
    assert report.rejected == 1
    assert report.sized_to_zero == 1
    assert report.execution_failed == 1
    assert report.opened == 2


def test_cycle_funnel_all_gated() -> None:
    records = [_jr(action_taken=ActionTaken.NO_ACTION_GATED) for _ in range(5)]
    report = cycle_funnel(records)

    assert report.total == 5
    assert report.gated == 5
    assert report.reasoned == 0
    assert report.opened == 0


def test_cycle_funnel_empty() -> None:
    report = cycle_funnel([])
    assert report.total == 0
    assert report.gated == 0
    assert report.opened == 0


def test_cycle_funnel_since_filter() -> None:
    records = [
        _jr(action_taken=ActionTaken.OPENED, timestamp=_T0),
        _jr(action_taken=ActionTaken.NO_ACTION_GATED, timestamp=_T0),
        _jr(action_taken=ActionTaken.OPENED, timestamp=_T2),
    ]
    report = cycle_funnel(records, since=_T1)

    assert report.total == 1
    assert report.opened == 1
    assert report.gated == 0
