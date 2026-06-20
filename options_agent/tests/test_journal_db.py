"""Tests for WP-2.3: Journal writer + context-snapshot storage strategy."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from options_agent.contracts import (
    ActionTaken,
    ContextSnapshot,
    Decision,
    ExitPlan,
    JournalRecord,
    Leg,
    OutcomeEventType,
    OutcomeRecord,
    Position,
    PositionStatus,
    RejectionReason,
    Severity,
    SizingConstraint,
    SizingResult,
    TradeProposal,
    ValidationResult,
    ValidationRuleId,
)
from options_agent.contracts.state import LegStatus, PositionLeg
from options_agent.state.crud import insert_position
from options_agent.state.db import get_connection
from options_agent.state.journal import (
    _coerce_for_json,
    query_journal,
    read_journal_record,
    read_outcome_record,
    write_journal_record,
    write_outcome_record,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_LEG = Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 7, 18))
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)
_NOW = datetime(2026, 6, 7, 14, 30, tzinfo=UTC)
_POS_LEG = PositionLeg(
    leg=_LEG, filled_qty=2, avg_fill_price=1.25, status=LegStatus.OPEN
)
_POS_LEG_1 = PositionLeg(
    leg=_LEG, filled_qty=1, avg_fill_price=2.0, status=LegStatus.OPEN
)


def _make_proposal(underlying: str = "SPY") -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying=underlying,
        strategy="bull_put_spread",
        legs=[_LEG],
        thesis="Bullish bias near support",
        iv_rationale="IV rank 65th pct — credit spread favourable",
        catalyst_check="No earnings within 30 days",
        conviction=0.7,
        est_max_loss=2187.50,
        est_max_profit=312.50,
        breakevens=[447.50],
        net_delta=0.12,
        net_theta=8.50,
        net_vega=-0.30,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )


def _make_context_snapshot(assembled_context: dict | None = None) -> ContextSnapshot:
    return ContextSnapshot(
        assembled_context=assembled_context or {"iv_rank": 65, "regime": "neutral"},
        context_hash="sha256:abcdef1234567890",
        model_id="claude-sonnet-4-6",
        prompt_version="v1.0.0",
        assembled_at=_NOW,
    )


def _make_decision(
    action: ActionTaken = ActionTaken.OPENED, **overrides: object
) -> Decision:
    defaults: dict = {
        "proposal": _make_proposal(),
        "validation_result": ValidationResult(passed=True),
        "sizing_result": SizingResult(
            contracts=2,
            sized_max_loss=2187.50,
            sized_max_profit=312.50,
            risk_budget_used=0.022,
            binding_constraint=SizingConstraint.RISK_BUDGET,
        ),
        "action_taken": action,
    }
    defaults.update(overrides)
    return Decision(**defaults)


def _make_journal_record(
    cycle_id: str = "cycle-001",
    timestamp: datetime = _NOW,
    action: ActionTaken = ActionTaken.OPENED,
    underlying: str | None = "SPY",
    **overrides: object,
) -> JournalRecord:
    defaults: dict = {
        "cycle_id": cycle_id,
        "timestamp": timestamp,
        "action_taken": action,
        "decision": _make_decision(action),
        "context_snapshot": _make_context_snapshot(),
        "position_ids": ["pos-001"],
        "order_ids": ["ord-001"],
        "strategy": "bull_put_spread",
        "underlying": underlying,
        "net_delta_at_open": 0.12,
        "earnings_within_dte": False,
        "conviction": 0.7,
        "iv_rank_at_open": 65.0,
        "limits_version": "v1.0.0",
        "prompt_version": "v1.0.0",
        "model_id": "claude-sonnet-4-6",
        "rejection_rule_ids": [],
    }
    defaults.update(overrides)
    return JournalRecord(**defaults)


def _make_outcome_record(**overrides: object) -> OutcomeRecord:
    defaults: dict = {
        "id": "outcome-001",
        "position_id": "pos-001",
        "event_type": OutcomeEventType.FULL_CLOSE,
        "recorded_at": _NOW,
        "contracts_closed": 2,
        "realized_pnl": 218.75,
        "fill_price": -0.47,
        "closing_order_id": "ord-close-001",
    }
    defaults.update(overrides)
    return OutcomeRecord(**defaults)


# ---------------------------------------------------------------------------
# _coerce_for_json — unit tests (no DB needed)
# ---------------------------------------------------------------------------


def test_coerce_passthrough_json_native_types() -> None:
    assert _coerce_for_json(None) is None
    assert _coerce_for_json(True) is True
    assert _coerce_for_json(42) == 42
    assert _coerce_for_json(3.14) == 3.14
    assert _coerce_for_json("hello") == "hello"


def test_coerce_dict_recursively() -> None:
    result = _coerce_for_json({"a": 1, "b": "x", "c": None})
    assert result == {"a": 1, "b": "x", "c": None}


def test_coerce_list_and_tuple() -> None:
    assert _coerce_for_json([1, 2, 3]) == [1, 2, 3]
    assert _coerce_for_json((4, 5)) == [4, 5]


def test_coerce_datetime(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="options_agent.state.journal"):
        result = _coerce_for_json(_NOW)
    assert result == _NOW.isoformat()
    assert "isoformat" in caplog.text


def test_coerce_date(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    d = date(2026, 7, 18)
    with caplog.at_level(logging.WARNING, logger="options_agent.state.journal"):
        result = _coerce_for_json(d)
    assert result == d.isoformat()
    assert "isoformat" in caplog.text


def test_coerce_decimal(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    from decimal import Decimal

    with caplog.at_level(logging.WARNING, logger="options_agent.state.journal"):
        result = _coerce_for_json(Decimal("1.25"))
    assert result == pytest.approx(1.25)
    assert "Decimal" in caplog.text


def test_coerce_unknown_type_raises() -> None:
    class _Unknown:
        pass

    with pytest.raises(TypeError, match="non-JSON-serializable"):
        _coerce_for_json(_Unknown())


def test_coerce_nested_unknown_raises() -> None:
    class _Weird:
        pass

    with pytest.raises(TypeError, match="non-JSON-serializable"):
        _coerce_for_json({"key": _Weird()})


def test_coerce_mixed_nested(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    from decimal import Decimal

    context = {
        "price": Decimal("450.25"),
        "labels": ["SPY", "QQQ"],
        "nested": {"iv_rank": 65},
    }
    with caplog.at_level(logging.WARNING, logger="options_agent.state.journal"):
        result = _coerce_for_json(context)
    assert result == {
        "price": pytest.approx(450.25),
        "labels": ["SPY", "QQQ"],
        "nested": {"iv_rank": 65},
    }


# ---------------------------------------------------------------------------
# write_journal_record + read_journal_record — lossless round-trip
# ---------------------------------------------------------------------------


def test_write_read_full_journal_record(engine) -> None:
    jr = _make_journal_record()
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored == jr


def test_write_read_preserves_context_snapshot(engine) -> None:
    jr = _make_journal_record(
        context_snapshot=_make_context_snapshot(
            {"iv_rank": 72, "chain_rows": 18, "regime": "high_iv", "score": 0.85}
        )
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored is not None
    assert restored.context_snapshot.assembled_context["iv_rank"] == 72
    assert restored.context_snapshot.assembled_context["chain_rows"] == 18
    assert restored.context_snapshot.context_hash == jr.context_snapshot.context_hash


def test_read_nonexistent_journal_record_returns_none(engine) -> None:
    with get_connection(engine) as conn:
        assert read_journal_record(conn, "cycle-does-not-exist") is None


def test_write_journal_record_duplicate_raises(engine) -> None:
    jr = _make_journal_record()
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
    with pytest.raises(IntegrityError):
        with get_connection(engine) as conn:
            write_journal_record(conn, jr)


# ---------------------------------------------------------------------------
# NO_ACTION cycles — gated and agent
# ---------------------------------------------------------------------------


def test_write_read_no_action_gated(engine) -> None:
    jr = _make_journal_record(
        cycle_id="cycle-gated-001",
        action=ActionTaken.NO_ACTION_GATED,
        decision=Decision(
            proposal=None,
            validation_result=None,
            sizing_result=None,
            action_taken=ActionTaken.NO_ACTION_GATED,
        ),
        position_ids=[],
        order_ids=[],
        strategy=None,
        underlying=None,
        net_delta_at_open=None,
        conviction=None,
        iv_rank_at_open=None,
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored == jr
    assert restored is not None
    assert restored.action_taken == ActionTaken.NO_ACTION_GATED
    assert restored.decision.proposal is None
    assert restored.position_ids == []


def test_write_read_no_action_agent(engine) -> None:
    no_action_proposal = TradeProposal(
        action="NO_ACTION",
        underlying="SPY",
        strategy="",
        legs=[],
        thesis="No compelling setup",
        iv_rationale="IV in middle band — no edge",
        catalyst_check="No events",
        conviction=0.0,
        est_max_loss=0.0,
        est_max_profit=0.0,
        breakevens=[],
        net_delta=0.0,
        net_theta=0.0,
        net_vega=0.0,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )
    jr = _make_journal_record(
        cycle_id="cycle-no-action-001",
        action=ActionTaken.NO_ACTION_AGENT,
        decision=Decision(
            proposal=no_action_proposal,
            validation_result=None,
            sizing_result=None,
            action_taken=ActionTaken.NO_ACTION_AGENT,
        ),
        position_ids=[],
        order_ids=[],
        strategy=None,
        conviction=0.0,
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored == jr
    assert restored is not None
    assert restored.action_taken == ActionTaken.NO_ACTION_AGENT
    assert restored.decision.proposal is not None
    assert restored.decision.proposal.action == "NO_ACTION"


# ---------------------------------------------------------------------------
# REJECTED cycles — structured rejection_rule_ids
# ---------------------------------------------------------------------------


def test_write_read_rejected_cycle(engine) -> None:
    rejection = ValidationResult(
        passed=False,
        reasons=[
            RejectionReason(
                rule_id=ValidationRuleId.NAKED_SHORT,
                severity=Severity.ERROR,
                human_message="Naked short leg detected",
            )
        ],
    )
    jr = _make_journal_record(
        cycle_id="cycle-rejected-001",
        action=ActionTaken.REJECTED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=rejection,
            sizing_result=None,
            action_taken=ActionTaken.REJECTED,
        ),
        position_ids=[],
        order_ids=[],
        rejection_rule_ids=[ValidationRuleId.NAKED_SHORT],
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored == jr
    assert restored is not None
    assert restored.action_taken == ActionTaken.REJECTED
    assert restored.rejection_rule_ids == [ValidationRuleId.NAKED_SHORT]
    assert restored.decision.validation_result is not None
    assert not restored.decision.validation_result.passed


def test_write_read_rejected_multiple_rules(engine) -> None:
    rejection = ValidationResult(
        passed=False,
        reasons=[
            RejectionReason(
                rule_id=ValidationRuleId.MAX_LOSS_CAP,
                severity=Severity.ERROR,
                human_message="Max loss exceeded",
                observed=5000.0,
                limit=2500.0,
            ),
            RejectionReason(
                rule_id=ValidationRuleId.PORTFOLIO_DELTA_BAND,
                severity=Severity.ERROR,
                human_message="Delta band breached",
                observed=0.55,
                limit=0.40,
            ),
        ],
    )
    jr = _make_journal_record(
        cycle_id="cycle-multi-reject-001",
        action=ActionTaken.REJECTED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=rejection,
            sizing_result=None,
            action_taken=ActionTaken.REJECTED,
        ),
        position_ids=[],
        order_ids=[],
        rejection_rule_ids=[
            ValidationRuleId.MAX_LOSS_CAP,
            ValidationRuleId.PORTFOLIO_DELTA_BAND,
        ],
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored is not None
    assert set(restored.rejection_rule_ids) == {
        ValidationRuleId.MAX_LOSS_CAP,
        ValidationRuleId.PORTFOLIO_DELTA_BAND,
    }


# ---------------------------------------------------------------------------
# DB round-trips for remaining ActionTaken variants
# (CLOSED, ROLLED, SIZED_TO_ZERO, EXECUTION_FAILED)
# ---------------------------------------------------------------------------


def test_write_read_closed_cycle(engine) -> None:
    jr = _make_journal_record(
        cycle_id="cycle-closed-001",
        action=ActionTaken.CLOSED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=None,
            action_taken=ActionTaken.CLOSED,
        ),
        position_ids=["pos-001"],
        order_ids=["ord-close-001"],
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored == jr
    assert restored is not None
    assert restored.action_taken == ActionTaken.CLOSED
    assert restored.position_ids == ["pos-001"]
    assert restored.order_ids == ["ord-close-001"]


def test_write_read_rolled_cycle(engine) -> None:
    jr = _make_journal_record(
        cycle_id="cycle-rolled-001",
        action=ActionTaken.ROLLED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=SizingResult(
                contracts=2,
                sized_max_loss=2187.50,
                sized_max_profit=312.50,
                risk_budget_used=0.022,
                binding_constraint=SizingConstraint.RISK_BUDGET,
            ),
            action_taken=ActionTaken.ROLLED,
        ),
        position_ids=["pos-001"],
        order_ids=["ord-roll-001"],
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored == jr
    assert restored is not None
    assert restored.action_taken == ActionTaken.ROLLED
    assert restored.decision.sizing_result is not None
    assert restored.decision.sizing_result.contracts == 2


def test_write_read_sized_to_zero_cycle(engine) -> None:
    jr = _make_journal_record(
        cycle_id="cycle-sized-zero-001",
        action=ActionTaken.SIZED_TO_ZERO,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=SizingResult(
                contracts=0,
                sized_max_loss=0.0,
                sized_max_profit=0.0,
                risk_budget_used=0.0,
                binding_constraint=SizingConstraint.CONVICTION_FLOOR,
                capped_to_zero=True,
            ),
            action_taken=ActionTaken.SIZED_TO_ZERO,
        ),
        position_ids=[],
        order_ids=[],
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored == jr
    assert restored is not None
    assert restored.action_taken == ActionTaken.SIZED_TO_ZERO
    assert restored.decision.sizing_result is not None
    assert restored.decision.sizing_result.contracts == 0
    assert restored.decision.sizing_result.capped_to_zero is True
    assert restored.position_ids == []


def test_write_read_execution_failed_cycle(engine) -> None:
    jr = _make_journal_record(
        cycle_id="cycle-exec-failed-001",
        action=ActionTaken.EXECUTION_FAILED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=SizingResult(
                contracts=2,
                sized_max_loss=2187.50,
                sized_max_profit=312.50,
                risk_budget_used=0.022,
                binding_constraint=SizingConstraint.RISK_BUDGET,
            ),
            action_taken=ActionTaken.EXECUTION_FAILED,
        ),
        position_ids=[],
        order_ids=[],
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, jr)
        restored = read_journal_record(conn, jr.cycle_id)
    assert restored == jr
    assert restored is not None
    assert restored.action_taken == ActionTaken.EXECUTION_FAILED
    assert restored.decision.validation_result is not None
    assert restored.decision.validation_result.passed
    assert restored.position_ids == []


# ---------------------------------------------------------------------------
# write_outcome_record + read_outcome_record
# ---------------------------------------------------------------------------


def test_write_read_outcome_record_full_close(engine) -> None:
    # Insert the position the outcome references so the FK is satisfied.
    # OutcomeRecord is written independently (no JournalRecord required).
    pos = Position(
        id="pos-001",
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[_POS_LEG],
        quantity=2,
        entry_net_amount=-312.50,
        current_mark=-100.0,
        marked_at=_NOW,
        unrealized_pnl=212.50,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=PositionStatus.OPEN,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=date(2026, 7, 18),
        est_max_loss=2187.50,
        est_max_profit=312.50,
        opening_order_id="ord-001",
    )

    outcome = _make_outcome_record()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        write_outcome_record(conn, outcome)
        restored = read_outcome_record(conn, outcome.id)
    assert restored == outcome
    assert restored is not None
    assert restored.event_type == OutcomeEventType.FULL_CLOSE
    assert restored.realized_pnl == pytest.approx(218.75)


def test_write_outcome_without_journal_record(engine) -> None:
    """Monitor-driven closes: OutcomeRecord written with no entry-cycle record."""
    pos = Position(
        id="pos-monitor-001",
        underlying="QQQ",
        strategy="iron_condor",
        legs=[_POS_LEG_1],
        quantity=1,
        entry_net_amount=-200.0,
        current_mark=-50.0,
        marked_at=_NOW,
        unrealized_pnl=150.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=PositionStatus.OPEN,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=date(2026, 7, 18),
        est_max_loss=800.0,
        est_max_profit=200.0,
        opening_order_id="ord-monitor-001",
    )

    monitor_outcome = _make_outcome_record(
        id="outcome-monitor-001",
        position_id="pos-monitor-001",
        event_type=OutcomeEventType.PARTIAL_CLOSE,
        contracts_closed=1,
        realized_pnl=150.0,
        closing_order_id="ord-close-monitor-001",
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        # No write_journal_record call — monitor-driven close has no entry-cycle
        write_outcome_record(conn, monitor_outcome)
        restored = read_outcome_record(conn, monitor_outcome.id)
    assert restored is not None
    assert restored.position_id == "pos-monitor-001"
    assert restored.event_type == OutcomeEventType.PARTIAL_CLOSE


def test_write_outcome_record_duplicate_raises(engine) -> None:
    pos = Position(
        id="pos-dup-001",
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[_POS_LEG],
        quantity=2,
        entry_net_amount=-312.50,
        current_mark=-100.0,
        marked_at=_NOW,
        unrealized_pnl=212.50,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=PositionStatus.OPEN,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=date(2026, 7, 18),
        est_max_loss=2187.50,
        est_max_profit=312.50,
        opening_order_id="ord-001",
    )
    outcome = _make_outcome_record(id="outcome-dup-001", position_id="pos-dup-001")
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        write_outcome_record(conn, outcome)
    with pytest.raises(IntegrityError):
        with get_connection(engine) as conn:
            write_outcome_record(conn, outcome)


def test_read_nonexistent_outcome_record_returns_none(engine) -> None:
    with get_connection(engine) as conn:
        assert read_outcome_record(conn, "outcome-does-not-exist") is None


# ---------------------------------------------------------------------------
# query_journal — indexed filters
# ---------------------------------------------------------------------------


def _write_three_records(
    conn, spy_ts: datetime, qqq_ts: datetime, later_ts: datetime
) -> None:
    """Seed three distinct journal records for filter tests."""
    write_journal_record(
        conn,
        _make_journal_record(
            cycle_id="cycle-spy-001",
            timestamp=spy_ts,
            underlying="SPY",
            action=ActionTaken.OPENED,
        ),
    )
    write_journal_record(
        conn,
        _make_journal_record(
            cycle_id="cycle-qqq-001",
            timestamp=qqq_ts,
            underlying="QQQ",
            action=ActionTaken.OPENED,
        ),
    )
    rejection = ValidationResult(
        passed=False,
        reasons=[
            RejectionReason(
                rule_id=ValidationRuleId.MAX_LOSS_CAP,
                severity=Severity.ERROR,
                human_message="Max loss exceeded",
                observed=5000.0,
                limit=2500.0,
            )
        ],
    )
    write_journal_record(
        conn,
        _make_journal_record(
            cycle_id="cycle-spy-rejected-001",
            timestamp=later_ts,
            underlying="SPY",
            action=ActionTaken.REJECTED,
            decision=Decision(
                proposal=_make_proposal("SPY"),
                validation_result=rejection,
                sizing_result=None,
                action_taken=ActionTaken.REJECTED,
            ),
            position_ids=[],
            order_ids=[],
            rejection_rule_ids=[ValidationRuleId.MAX_LOSS_CAP],
        ),
    )


def test_query_journal_no_filter_returns_all(engine) -> None:
    t1 = _NOW
    t2 = _NOW + timedelta(hours=1)
    t3 = _NOW + timedelta(hours=2)
    with get_connection(engine) as conn:
        _write_three_records(conn, t1, t2, t3)
        results = query_journal(conn)
    assert len(results) == 3


def test_query_journal_by_symbol(engine) -> None:
    t1 = _NOW
    t2 = _NOW + timedelta(hours=1)
    t3 = _NOW + timedelta(hours=2)
    with get_connection(engine) as conn:
        _write_three_records(conn, t1, t2, t3)
        spy_results = query_journal(conn, symbol="SPY")
        qqq_results = query_journal(conn, symbol="QQQ")
    assert len(spy_results) == 2
    assert all(r.underlying == "SPY" for r in spy_results)
    assert len(qqq_results) == 1
    assert qqq_results[0].underlying == "QQQ"


def test_query_journal_by_action_type(engine) -> None:
    t1 = _NOW
    t2 = _NOW + timedelta(hours=1)
    t3 = _NOW + timedelta(hours=2)
    with get_connection(engine) as conn:
        _write_three_records(conn, t1, t2, t3)
        opened = query_journal(conn, action_type=ActionTaken.OPENED)
        rejected = query_journal(conn, action_type=ActionTaken.REJECTED)
    assert len(opened) == 2
    assert all(r.action_taken == ActionTaken.OPENED for r in opened)
    assert len(rejected) == 1
    assert rejected[0].action_taken == ActionTaken.REJECTED


def test_query_journal_by_date_range(engine) -> None:
    t1 = _NOW
    t2 = _NOW + timedelta(hours=1)
    t3 = _NOW + timedelta(hours=2)
    with get_connection(engine) as conn:
        _write_three_records(conn, t1, t2, t3)
        # date_from and date_to are inclusive
        results = query_journal(conn, date_from=t2, date_to=t2)
    assert len(results) == 1
    assert results[0].cycle_id == "cycle-qqq-001"


def test_query_journal_date_from_only(engine) -> None:
    t1 = _NOW
    t2 = _NOW + timedelta(hours=1)
    t3 = _NOW + timedelta(hours=2)
    with get_connection(engine) as conn:
        _write_three_records(conn, t1, t2, t3)
        results = query_journal(conn, date_from=t2)
    assert len(results) == 2
    assert {r.cycle_id for r in results} == {"cycle-qqq-001", "cycle-spy-rejected-001"}


def test_query_journal_combined_symbol_and_action(engine) -> None:
    t1 = _NOW
    t2 = _NOW + timedelta(hours=1)
    t3 = _NOW + timedelta(hours=2)
    with get_connection(engine) as conn:
        _write_three_records(conn, t1, t2, t3)
        results = query_journal(conn, symbol="SPY", action_type=ActionTaken.REJECTED)
    assert len(results) == 1
    assert results[0].cycle_id == "cycle-spy-rejected-001"
    assert results[0].underlying == "SPY"
    assert results[0].rejection_rule_ids == [ValidationRuleId.MAX_LOSS_CAP]


def test_query_journal_returns_ordered_by_timestamp(engine) -> None:
    t1 = _NOW
    t2 = _NOW + timedelta(hours=2)
    t3 = _NOW + timedelta(hours=1)
    # Insert out of chronological order
    with get_connection(engine) as conn:
        write_journal_record(conn, _make_journal_record(cycle_id="c-t1", timestamp=t1))
        write_journal_record(
            conn, _make_journal_record(cycle_id="c-t2", timestamp=t2, underlying="QQQ")
        )
        write_journal_record(
            conn, _make_journal_record(cycle_id="c-t3", timestamp=t3, underlying="AAPL")
        )
        results = query_journal(conn)
    assert [r.cycle_id for r in results] == ["c-t1", "c-t3", "c-t2"]


def test_query_journal_empty_result(engine) -> None:
    with get_connection(engine) as conn:
        results = query_journal(conn, symbol="UNKNOWN")
    assert results == []


def test_query_journal_no_position_id_param() -> None:
    """query_journal intentionally has no position_id parameter.

    Filtering by position_id requires a JSON array scan on SQLite (no index).
    WP-7 should use: OutcomeRecord.position_id → Position → JournalRecord.cycle_id.
    cycle_id is the primary key and is always indexed.
    """
    import inspect

    sig = inspect.signature(query_journal)
    assert "position_id" not in sig.parameters, (
        "position_id filter is intentionally deferred — see WP-2.3 PR description"
    )
