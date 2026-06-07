import pytest
from pydantic import ValidationError

from options_agent.config import Config
from options_agent.contracts import (
    ActionTaken,
    CycleError,
    CycleResult,
    CycleStage,
    MonitorResult,
    ShortCircuitReason,
    SizingResult,
    ValidationResult,
)
from options_agent.orchestrator import run_entry_cycle, run_monitor_cycle

# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


def test_orchestrator_types_importable() -> None:
    assert ActionTaken
    assert ShortCircuitReason
    assert CycleStage
    assert CycleError
    assert CycleResult
    assert MonitorResult


def test_orchestrator_functions_importable() -> None:
    assert callable(run_entry_cycle)
    assert callable(run_monitor_cycle)


# ---------------------------------------------------------------------------
# ActionTaken — catalog completeness
# ---------------------------------------------------------------------------


def test_action_taken_all_values_present() -> None:
    expected = {
        ActionTaken.OPENED,
        ActionTaken.CLOSED,
        ActionTaken.ROLLED,
        ActionTaken.NO_ACTION_GATED,
        ActionTaken.NO_ACTION_AGENT,
        ActionTaken.SIZED_TO_ZERO,
        ActionTaken.REJECTED,
        ActionTaken.EXECUTION_FAILED,
    }
    assert expected == set(ActionTaken)


# ---------------------------------------------------------------------------
# ShortCircuitReason — catalog completeness
# ---------------------------------------------------------------------------


def test_short_circuit_reason_all_values_present() -> None:
    expected = {
        ShortCircuitReason.KILL_SWITCH_HALT,
        ShortCircuitReason.KILL_SWITCH_FLATTEN,
        ShortCircuitReason.MARKET_CLOSED,
        ShortCircuitReason.BLACKOUT_WINDOW,
        ShortCircuitReason.NO_BUYING_POWER,
        ShortCircuitReason.MAX_POSITIONS,
        ShortCircuitReason.EMPTY_ACTION_SPACE,
    }
    assert expected == set(ShortCircuitReason)


# ---------------------------------------------------------------------------
# CycleStage — catalog completeness
# ---------------------------------------------------------------------------


def test_cycle_stage_all_values_present() -> None:
    expected = {
        # Entry cycle
        CycleStage.RECONCILE,
        CycleStage.GATES,
        CycleStage.ASSEMBLE,
        CycleStage.REASON,
        CycleStage.VALIDATE,
        CycleStage.SIZE,
        CycleStage.EXECUTE,
        CycleStage.JOURNAL,
        # Monitor cycle
        CycleStage.STOP_EVAL,
        CycleStage.PROFIT_EVAL,
        CycleStage.TIME_EVAL,
    }
    assert expected == set(CycleStage)


# ---------------------------------------------------------------------------
# CycleError — construction and round-trip
# ---------------------------------------------------------------------------


def test_cycle_error_recoverable() -> None:
    e = CycleError(
        stage=CycleStage.EXECUTE,
        message="Broker rate-limited",
        recoverable=True,
    )
    assert e.stage == CycleStage.EXECUTE
    assert e.recoverable is True


def test_cycle_error_not_recoverable() -> None:
    e = CycleError(
        stage=CycleStage.REASON,
        message="Model returned invalid JSON",
        recoverable=False,
    )
    assert e.stage == CycleStage.REASON
    assert e.recoverable is False


def test_cycle_error_round_trip() -> None:
    e = CycleError(stage=CycleStage.GATES, message="Market closed", recoverable=True)
    assert CycleError.model_validate(e.model_dump()) == e


# ---------------------------------------------------------------------------
# CycleResult — construction
# ---------------------------------------------------------------------------


def test_cycle_result_short_circuit() -> None:
    r = CycleResult(
        cycle_id="abc-123",
        action_taken=ActionTaken.NO_ACTION_GATED,
        short_circuit_reason=ShortCircuitReason.KILL_SWITCH_HALT,
    )
    assert r.action_taken == ActionTaken.NO_ACTION_GATED
    assert r.short_circuit_reason == ShortCircuitReason.KILL_SWITCH_HALT
    assert r.proposal is None
    assert r.validation is None
    assert r.sizing is None
    assert r.error is None
    assert r.journal_record_id is None


def test_cycle_result_completed_open() -> None:
    vr = ValidationResult(passed=True)
    sr = SizingResult(
        contracts=2,
        sized_max_loss=500.0,
        sized_max_profit=250.0,
        risk_budget_used=0.01,
    )
    r = CycleResult(
        cycle_id="xyz-456",
        action_taken=ActionTaken.OPENED,
        validation=vr,
        sizing=sr,
        journal_record_id="journal-789",
    )
    assert r.action_taken == ActionTaken.OPENED
    assert r.short_circuit_reason is None
    assert r.validation is not None
    assert r.sizing is not None
    assert r.journal_record_id == "journal-789"


def test_cycle_result_error_path() -> None:
    err = CycleError(stage=CycleStage.EXECUTE, message="timeout", recoverable=True)
    r = CycleResult(
        cycle_id="err-001",
        action_taken=ActionTaken.EXECUTION_FAILED,
        error=err,
        journal_record_id="journal-err-001",
    )
    assert r.action_taken == ActionTaken.EXECUTION_FAILED
    assert r.error is not None
    assert r.error.stage == CycleStage.EXECUTE


def test_cycle_result_round_trip() -> None:
    r = CycleResult(
        cycle_id="rt-001",
        action_taken=ActionTaken.NO_ACTION_GATED,
        short_circuit_reason=ShortCircuitReason.MARKET_CLOSED,
    )
    assert CycleResult.model_validate(r.model_dump()) == r


def test_cycle_result_invariant_enforced() -> None:
    with pytest.raises(ValidationError, match="NO_ACTION_GATED"):
        CycleResult(
            cycle_id="bad-001",
            action_taken=ActionTaken.OPENED,
            short_circuit_reason=ShortCircuitReason.MARKET_CLOSED,
        )


# ---------------------------------------------------------------------------
# MonitorResult — construction and round-trip
# ---------------------------------------------------------------------------


def test_monitor_result_no_exits() -> None:
    result = MonitorResult(
        positions_evaluated=5,
        exits_triggered=[],
        orders_submitted=[],
        errors=[],
    )
    assert result.positions_evaluated == 5
    assert result.exits_triggered == []
    assert result.errors == []


def test_monitor_result_with_exits() -> None:
    result = MonitorResult(
        positions_evaluated=3,
        exits_triggered=["pos-1", "pos-2"],
        orders_submitted=["order-a", "order-b"],
        errors=[],
    )
    assert len(result.exits_triggered) == 2
    assert len(result.orders_submitted) == 2


def test_monitor_result_with_per_position_error() -> None:
    err = CycleError(
        stage=CycleStage.EXECUTE,
        message="Position pos-3 failed to evaluate",
        recoverable=True,
    )
    result = MonitorResult(
        positions_evaluated=3,
        exits_triggered=["pos-1"],
        orders_submitted=["order-a"],
        errors=[err],
    )
    assert len(result.errors) == 1
    assert result.errors[0].stage == CycleStage.EXECUTE


def test_monitor_result_round_trip() -> None:
    err = CycleError(stage=CycleStage.RECONCILE, message="stale", recoverable=True)
    result = MonitorResult(
        positions_evaluated=2,
        exits_triggered=["pos-x"],
        orders_submitted=["ord-x"],
        errors=[err],
    )
    assert MonitorResult.model_validate(result.model_dump()) == result


# ---------------------------------------------------------------------------
# Stub functions raise NotImplementedError
# ---------------------------------------------------------------------------


def test_run_entry_cycle_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        run_entry_cycle(config=Config())


def test_run_monitor_cycle_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        run_monitor_cycle(positions=[], config=Config())
