from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

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

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_LEG = Leg(right="call", side="sell", strike=500.0, expiration=date(2026, 8, 15))
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)
_NOW = datetime(2026, 6, 7, 15, 0, tzinfo=UTC)


def _make_proposal() -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[_LEG],
        thesis="Neutral-to-bullish near support",
        iv_rationale="IV rank 72nd pct — credit spread favourable",
        catalyst_check="No earnings within 35 days",
        conviction=0.65,
        est_max_loss=1750.0,
        est_max_profit=250.0,
        breakevens=[497.50],
        net_delta=0.10,
        net_theta=7.20,
        net_vega=-0.25,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )


def _make_context_snapshot(**overrides: object) -> ContextSnapshot:
    defaults: dict = {
        "assembled_context": {"iv_rank": 72, "regime": "neutral"},
        "context_hash": "sha256:deadbeef12345678",
        "model_id": "claude-sonnet-4-6",
        "prompt_version": "v1.0.0",
        "assembled_at": _NOW,
    }
    defaults.update(overrides)
    return ContextSnapshot(**defaults)


def _make_decision(**overrides: object) -> Decision:
    defaults: dict = {
        "proposal": _make_proposal(),
        "validation_result": ValidationResult(passed=True),
        "sizing_result": SizingResult(
            contracts=2,
            sized_max_loss=1750.0,
            sized_max_profit=250.0,
            risk_budget_used=0.018,
            binding_constraint=SizingConstraint.RISK_BUDGET,
        ),
        "action_taken": ActionTaken.OPENED,
    }
    defaults.update(overrides)
    return Decision(**defaults)


def _make_journal_record(**overrides: object) -> JournalRecord:
    defaults: dict = {
        "cycle_id": "cycle-2026-06-07-001",
        "timestamp": _NOW,
        "action_taken": ActionTaken.OPENED,
        "decision": _make_decision(),
        "context_snapshot": _make_context_snapshot(),
        "position_ids": ["pos-001"],
        "order_ids": ["ord-001"],
        "strategy": "bull_put_spread",
        "underlying": "SPY",
        "net_delta_at_open": 0.10,
        "earnings_within_dte": False,
        "conviction": 0.65,
        "iv_rank_at_open": 72.0,
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
        "realized_pnl": 175.0,
        "fill_price": -0.62,
        "closing_order_id": "ord-close-001",
    }
    defaults.update(overrides)
    return OutcomeRecord(**defaults)


# ---------------------------------------------------------------------------
# ActionTaken — enum catalog
# ---------------------------------------------------------------------------


def test_action_taken_all_values_present() -> None:
    assert set(ActionTaken) == {
        ActionTaken.OPENED,
        ActionTaken.CLOSED,
        ActionTaken.ROLLED,
        ActionTaken.NO_ACTION_GATED,
        ActionTaken.NO_ACTION_AGENT,
        ActionTaken.SIZED_TO_ZERO,
        ActionTaken.REJECTED,
        ActionTaken.EXECUTION_FAILED,
    }


def test_action_taken_is_str() -> None:
    assert ActionTaken.OPENED == "OPENED"
    assert ActionTaken.NO_ACTION_GATED == "NO_ACTION_GATED"
    assert ActionTaken.REJECTED == "REJECTED"


# ---------------------------------------------------------------------------
# OutcomeEventType — enum catalog
# ---------------------------------------------------------------------------


def test_outcome_event_type_all_values_present() -> None:
    assert set(OutcomeEventType) == {
        OutcomeEventType.PARTIAL_CLOSE,
        OutcomeEventType.FULL_CLOSE,
        OutcomeEventType.ROLL,
        OutcomeEventType.EXPIRED,
        OutcomeEventType.ASSIGNED,
    }


# ---------------------------------------------------------------------------
# OutcomeRecord
# ---------------------------------------------------------------------------


def test_outcome_record_full_close() -> None:
    o = _make_outcome_record()
    assert o.position_id == "pos-001"
    assert o.event_type == OutcomeEventType.FULL_CLOSE
    assert o.realized_pnl == 175.0
    assert o.contracts_closed == 2
    assert o.closing_order_id == "ord-close-001"


def test_outcome_record_partial_close() -> None:
    o = _make_outcome_record(
        event_type=OutcomeEventType.PARTIAL_CLOSE,
        contracts_closed=1,
        realized_pnl=80.0,
    )
    assert o.event_type == OutcomeEventType.PARTIAL_CLOSE
    assert o.contracts_closed == 1


def test_outcome_record_expired_no_fill() -> None:
    o = _make_outcome_record(
        event_type=OutcomeEventType.EXPIRED,
        realized_pnl=250.0,
        fill_price=None,
        closing_order_id=None,
    )
    assert o.event_type == OutcomeEventType.EXPIRED
    assert o.fill_price is None
    assert o.closing_order_id is None


def test_outcome_record_round_trip_dict() -> None:
    o = _make_outcome_record()
    assert OutcomeRecord.model_validate(o.model_dump()) == o


def test_outcome_record_round_trip_json() -> None:
    o = _make_outcome_record()
    assert OutcomeRecord.model_validate_json(o.model_dump_json()) == o


# ---------------------------------------------------------------------------
# JournalRecord — OPENED cycle (full golden path)
# ---------------------------------------------------------------------------


def test_journal_record_opened_construction() -> None:
    jr = _make_journal_record()
    assert jr.cycle_id == "cycle-2026-06-07-001"
    assert jr.action_taken == ActionTaken.OPENED
    assert jr.position_ids == ["pos-001"]
    assert jr.order_ids == ["ord-001"]
    assert jr.strategy == "bull_put_spread"
    assert jr.underlying == "SPY"
    assert jr.conviction == 0.65
    assert jr.iv_rank_at_open == 72.0
    assert jr.earnings_within_dte is False
    assert jr.rejection_rule_ids == []


def test_journal_record_opened_round_trip_dict() -> None:
    jr = _make_journal_record()
    assert JournalRecord.model_validate(jr.model_dump()) == jr


def test_journal_record_opened_round_trip_json() -> None:
    jr = _make_journal_record()
    assert JournalRecord.model_validate_json(jr.model_dump_json()) == jr


# ---------------------------------------------------------------------------
# JournalRecord — NO_ACTION_GATED (short-circuit before LLM)
# ---------------------------------------------------------------------------


def test_journal_record_no_action_gated() -> None:
    jr = _make_journal_record(
        action_taken=ActionTaken.NO_ACTION_GATED,
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
    assert jr.action_taken == ActionTaken.NO_ACTION_GATED
    assert jr.decision.proposal is None
    assert jr.position_ids == []
    assert jr.strategy is None


def test_journal_record_no_action_gated_round_trip() -> None:
    jr = _make_journal_record(
        action_taken=ActionTaken.NO_ACTION_GATED,
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
    assert JournalRecord.model_validate_json(jr.model_dump_json()) == jr


# ---------------------------------------------------------------------------
# JournalRecord — NO_ACTION_AGENT (LLM chose not to act)
# ---------------------------------------------------------------------------


def test_journal_record_no_action_agent() -> None:
    no_action_proposal = TradeProposal(
        action="NO_ACTION",
        underlying="SPY",
        strategy="",
        legs=[],
        thesis="No compelling setup today",
        iv_rationale="IV rank in middle band — no edge",
        catalyst_check="No nearby events",
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
        action_taken=ActionTaken.NO_ACTION_AGENT,
        decision=Decision(
            proposal=no_action_proposal,
            validation_result=None,
            sizing_result=None,
            action_taken=ActionTaken.NO_ACTION_AGENT,
        ),
        position_ids=[],
        order_ids=[],
        strategy=None,
        underlying="SPY",
        conviction=0.0,
    )
    assert jr.action_taken == ActionTaken.NO_ACTION_AGENT
    assert jr.decision.proposal is not None
    assert jr.decision.proposal.action == "NO_ACTION"


# ---------------------------------------------------------------------------
# JournalRecord — REJECTED (validator said no)
# ---------------------------------------------------------------------------


def test_journal_record_rejected() -> None:
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
        action_taken=ActionTaken.REJECTED,
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
    assert jr.action_taken == ActionTaken.REJECTED
    assert jr.rejection_rule_ids == [ValidationRuleId.NAKED_SHORT]
    assert jr.position_ids == []
    assert jr.decision.validation_result is not None
    assert not jr.decision.validation_result.passed


def test_journal_record_rejected_multiple_rules() -> None:
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
        action_taken=ActionTaken.REJECTED,
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
    assert set(jr.rejection_rule_ids) == {
        ValidationRuleId.MAX_LOSS_CAP,
        ValidationRuleId.PORTFOLIO_DELTA_BAND,
    }


def test_journal_record_rejected_round_trip() -> None:
    rejection = ValidationResult(
        passed=False,
        reasons=[
            RejectionReason(
                rule_id=ValidationRuleId.EVENT_BLACKOUT,
                severity=Severity.ERROR,
                human_message="Earnings within blackout window",
            )
        ],
    )
    jr = _make_journal_record(
        action_taken=ActionTaken.REJECTED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=rejection,
            sizing_result=None,
            action_taken=ActionTaken.REJECTED,
        ),
        position_ids=[],
        order_ids=[],
        rejection_rule_ids=[ValidationRuleId.EVENT_BLACKOUT],
    )
    assert JournalRecord.model_validate_json(jr.model_dump_json()) == jr


# ---------------------------------------------------------------------------
# JournalRecord — SIZED_TO_ZERO (passed validation, but 0 contracts)
# ---------------------------------------------------------------------------


def test_journal_record_sized_to_zero() -> None:
    jr = _make_journal_record(
        action_taken=ActionTaken.SIZED_TO_ZERO,
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
    assert jr.action_taken == ActionTaken.SIZED_TO_ZERO
    assert jr.decision.sizing_result is not None
    assert jr.decision.sizing_result.capped_to_zero is True
    assert jr.position_ids == []


# ---------------------------------------------------------------------------
# JournalRecord — EXECUTION_FAILED
# ---------------------------------------------------------------------------


def test_journal_record_execution_failed() -> None:
    jr = _make_journal_record(
        action_taken=ActionTaken.EXECUTION_FAILED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=SizingResult(
                contracts=2,
                sized_max_loss=1750.0,
                sized_max_profit=250.0,
                risk_budget_used=0.018,
                binding_constraint=SizingConstraint.RISK_BUDGET,
            ),
            action_taken=ActionTaken.EXECUTION_FAILED,
        ),
        position_ids=[],
        order_ids=[],
    )
    assert jr.action_taken == ActionTaken.EXECUTION_FAILED
    assert jr.position_ids == []
    assert jr.decision.validation_result is not None
    assert jr.decision.validation_result.passed


# ---------------------------------------------------------------------------
# JournalRecord — versioning fields
# ---------------------------------------------------------------------------


def test_journal_record_versioning_fields() -> None:
    jr = _make_journal_record(
        limits_version="v2.1.0",
        prompt_version="v1.3.0",
        model_id="claude-opus-4-8",
    )
    assert jr.limits_version == "v2.1.0"
    assert jr.prompt_version == "v1.3.0"
    assert jr.model_id == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# JournalRecord — CLOSED and ROLLED
# ---------------------------------------------------------------------------


def test_journal_record_closed() -> None:
    jr = _make_journal_record(
        action_taken=ActionTaken.CLOSED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=None,
            action_taken=ActionTaken.CLOSED,
        ),
        position_ids=["pos-001"],
        order_ids=["ord-close-001"],
        strategy="bull_put_spread",
        underlying="SPY",
    )
    assert jr.action_taken == ActionTaken.CLOSED
    assert jr.position_ids == ["pos-001"]
    assert jr.order_ids == ["ord-close-001"]


def test_journal_record_closed_round_trip() -> None:
    jr = _make_journal_record(
        action_taken=ActionTaken.CLOSED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=None,
            action_taken=ActionTaken.CLOSED,
        ),
        position_ids=["pos-001"],
        order_ids=["ord-close-001"],
    )
    assert JournalRecord.model_validate_json(jr.model_dump_json()) == jr


def test_journal_record_rolled() -> None:
    jr = _make_journal_record(
        action_taken=ActionTaken.ROLLED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=SizingResult(
                contracts=2,
                sized_max_loss=1750.0,
                sized_max_profit=250.0,
                risk_budget_used=0.018,
                binding_constraint=SizingConstraint.RISK_BUDGET,
            ),
            action_taken=ActionTaken.ROLLED,
        ),
        position_ids=["pos-001"],
        order_ids=["ord-roll-001"],
        strategy="bull_put_spread",
        underlying="SPY",
    )
    assert jr.action_taken == ActionTaken.ROLLED
    assert jr.position_ids == ["pos-001"]


def test_journal_record_rolled_round_trip() -> None:
    jr = _make_journal_record(
        action_taken=ActionTaken.ROLLED,
        decision=Decision(
            proposal=_make_proposal(),
            validation_result=ValidationResult(passed=True),
            sizing_result=SizingResult(
                contracts=2,
                sized_max_loss=1750.0,
                sized_max_profit=250.0,
                risk_budget_used=0.018,
                binding_constraint=SizingConstraint.RISK_BUDGET,
            ),
            action_taken=ActionTaken.ROLLED,
        ),
        position_ids=["pos-001"],
        order_ids=["ord-roll-001"],
    )
    assert JournalRecord.model_validate(jr.model_dump()) == jr


# ---------------------------------------------------------------------------
# JournalRecord — model_validator enforcement
# ---------------------------------------------------------------------------


def test_journal_record_mismatched_action_taken_raises() -> None:
    with pytest.raises(ValidationError, match="disagrees with"):
        _make_journal_record(
            action_taken=ActionTaken.OPENED,
            decision=Decision(
                proposal=_make_proposal(),
                validation_result=ValidationResult(passed=True),
                sizing_result=None,
                action_taken=ActionTaken.REJECTED,  # mismatch
            ),
            rejection_rule_ids=[ValidationRuleId.NAKED_SHORT],
        )


def test_journal_record_rejected_empty_rule_ids_raises() -> None:
    rejection = ValidationResult(
        passed=False,
        reasons=[
            RejectionReason(
                rule_id=ValidationRuleId.KILL_SWITCH,
                severity=Severity.ERROR,
                human_message="Kill switch engaged",
            )
        ],
    )
    with pytest.raises(ValidationError, match="rejection_rule_ids must be non-empty"):
        _make_journal_record(
            action_taken=ActionTaken.REJECTED,
            decision=Decision(
                proposal=_make_proposal(),
                validation_result=rejection,
                sizing_result=None,
                action_taken=ActionTaken.REJECTED,
            ),
            position_ids=[],
            order_ids=[],
            rejection_rule_ids=[],  # violates invariant
        )


# ---------------------------------------------------------------------------
# Immutability — frozen=True
# ---------------------------------------------------------------------------


def test_journal_record_is_frozen() -> None:
    jr = _make_journal_record()
    with pytest.raises((TypeError, ValidationError)):
        jr.cycle_id = "mutated"  # type: ignore[misc]


def test_outcome_record_is_frozen() -> None:
    o = _make_outcome_record()
    with pytest.raises((TypeError, ValidationError)):
        o.realized_pnl = 999.0  # type: ignore[misc]
