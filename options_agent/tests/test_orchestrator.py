"""Tests for WP-0.5.2: orchestrator.py::run_entry_cycle().

Happy-path test exercises the full pipeline:
  stub_reasoner → validate_structural → size → submit_multi_leg
  → reconcile → write_journal_record
and asserts the PENDING_OPEN → OPEN position transition driven by reconcile.

All Alpaca network calls are mocked at the BrokerClient method level.
The DB layer uses the shared in-memory SQLite engine fixture from conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

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
from options_agent.contracts.state import (
    Order,
    OrderRole,
    OrderStatus,
    PositionStatus,
)
from options_agent.execution.broker import BrokerClient
from options_agent.orchestrator import run_entry_cycle, run_monitor_cycle
from options_agent.risk.limits import Limits
from options_agent.state.crud import get_order, get_position
from options_agent.state.db import get_connection
from options_agent.state.journal import read_journal_record

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
# run_monitor_cycle still raises NotImplementedError (WP-8)
# ---------------------------------------------------------------------------


def test_run_monitor_cycle_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        run_monitor_cycle(positions=[], config=Config())


# ---------------------------------------------------------------------------
# Helpers for run_entry_cycle tests
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 14, 14, 30, tzinfo=UTC)
_BROKER_ORDER_ID = "broker-test-abc-123"


def _make_broker(monkeypatch: pytest.MonkeyPatch) -> BrokerClient:
    """Return a BrokerClient backed by a MagicMock TradingClient."""
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    with patch(
        "options_agent.execution.broker.TradingClient",
        return_value=MagicMock(),
    ):
        return BrokerClient(Config())


def _wire_happy_broker(broker: BrokerClient, qty: int = 2) -> None:
    """Configure broker mocks for the standard happy-path scenario.

    get_account()     → mock account with $100,000 equity
    submit_multi_leg  → returns WORKING Order (position_id captured from args)
    list_open_orders  → [] (order already dropped off open list)
    get_broker_order  → FILLED AlpacaOrder
    get_all_positions → [] (no open equity positions for expiry backstop)
    get_account_activities → [] (no assignment/expiry events)
    """
    mock_account = MagicMock()
    mock_account.equity = "100000.00"
    mock_account.buying_power = "100000.00"
    mock_account.options_buying_power = "100000.00"
    mock_account.options_approved_level = 3
    broker.get_account = MagicMock(return_value=mock_account)

    def _submit(proposal, qty_arg, limit_price, position_id, role=OrderRole.OPEN):
        return Order(
            id="order-slice-001",
            broker_order_id=_BROKER_ORDER_ID,
            position_id=position_id,
            role=role,
            status=OrderStatus.WORKING,
            broker_status_raw="new",
            submitted_at=_NOW,
            filled_at=None,
            limit_price=limit_price,
            legs_filled=[],
            net_fill_price=None,
            filled_qty=0,
        )

    broker.submit_multi_leg = _submit  # type: ignore[method-assign]

    # reconcile: order is not in open list → fetched individually as filled
    broker.list_open_orders = MagicMock(return_value=[])

    filled_alpaca = MagicMock()
    filled_alpaca.id = _BROKER_ORDER_ID
    filled_alpaca.status.value = "filled"
    filled_alpaca.filled_qty = qty
    filled_alpaca.filled_avg_price = -1.50
    filled_alpaca.symbol = None
    filled_alpaca.legs = None
    filled_alpaca.submitted_at = _NOW
    filled_alpaca.filled_at = _NOW
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    broker.get_all_positions = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])


# ---------------------------------------------------------------------------
# run_entry_cycle — happy path (OPENED)
# ---------------------------------------------------------------------------


def test_run_entry_cycle_happy_path_result(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CycleResult fields are fully populated on the happy path."""
    broker = _make_broker(monkeypatch)
    _wire_happy_broker(broker)

    result = run_entry_cycle(Config(), broker=broker, engine=engine)

    assert result.action_taken == ActionTaken.OPENED
    assert result.proposal is not None
    assert result.validation is not None
    assert result.validation.passed is True
    assert result.sizing is not None
    assert result.sizing.contracts > 0
    assert result.journal_record_id is not None
    assert result.short_circuit_reason is None
    assert result.error is None


def test_run_entry_cycle_journal_written(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JournalRecord is written and contains position_ids and order_ids."""
    broker = _make_broker(monkeypatch)
    _wire_happy_broker(broker)

    result = run_entry_cycle(Config(), broker=broker, engine=engine)

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)

    assert jr is not None
    assert jr.action_taken == ActionTaken.OPENED
    assert len(jr.position_ids) == 1
    assert len(jr.order_ids) == 1
    assert jr.strategy == "bull_put_spread"
    assert jr.underlying == "SPY"


def test_run_entry_cycle_broker_order_id_traceable(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JournalRecord.order_ids → Order.broker_order_id is resolvable (criterion 3)."""
    broker = _make_broker(monkeypatch)
    _wire_happy_broker(broker)

    result = run_entry_cycle(Config(), broker=broker, engine=engine)

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)
        assert jr is not None
        order = get_order(conn, jr.order_ids[0])

    assert order is not None
    assert order.broker_order_id == _BROKER_ORDER_ID


def test_run_entry_cycle_pending_open_to_open_transition(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reconcile() transitions the Position from PENDING_OPEN to OPEN.

    This is the primary integration assertion for the slice: the FK chain
    JournalRecord → Order → Position must exist and the status must have
    advanced through the real lifecycle, not been written as OPEN directly.
    """
    broker = _make_broker(monkeypatch)
    _wire_happy_broker(broker)

    result = run_entry_cycle(Config(), broker=broker, engine=engine)

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)
        assert jr is not None
        order = get_order(conn, jr.order_ids[0])
        assert order is not None
        pos = get_position(conn, order.position_id)

    assert pos is not None
    # reconcile() must have detected the fill and applied the PENDING_OPEN → OPEN
    # transition. Writing OPEN directly would defeat the purpose of the slice.
    assert pos.status == PositionStatus.OPEN
    assert pos.id == jr.position_ids[0]


# ---------------------------------------------------------------------------
# run_entry_cycle — validation failure (REJECTED)
# ---------------------------------------------------------------------------


def test_run_entry_cycle_validation_failure_result(
    engine,
) -> None:
    """REJECTED CycleResult returned when strategy is not in allowed_strategies."""
    # Empty playbook forces UNKNOWN_STRATEGY rejection on any proposal.
    config = Config(limits=Limits(allowed_strategies=frozenset()))

    result = run_entry_cycle(config, engine=engine)

    assert result.action_taken == ActionTaken.REJECTED
    assert result.validation is not None
    assert result.validation.passed is False
    assert result.journal_record_id is not None
    assert result.sizing is None


def test_run_entry_cycle_validation_failure_journal_written(
    engine,
) -> None:
    """REJECTED cycles write a JournalRecord with rejection_rule_ids populated."""
    config = Config(limits=Limits(allowed_strategies=frozenset()))

    result = run_entry_cycle(config, engine=engine)

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)

    assert jr is not None
    assert jr.action_taken == ActionTaken.REJECTED
    assert len(jr.rejection_rule_ids) > 0


# ---------------------------------------------------------------------------
# run_entry_cycle — broker rejection (EXECUTION_FAILED)
# ---------------------------------------------------------------------------


def _wire_rejected_broker(broker: BrokerClient) -> None:
    """Configure broker mocks so submit_multi_leg returns a REJECTED order."""
    mock_account = MagicMock()
    mock_account.equity = "100000.00"
    mock_account.buying_power = "100000.00"
    mock_account.options_buying_power = "100000.00"
    mock_account.options_approved_level = 3
    broker.get_account = MagicMock(return_value=mock_account)

    def _submit_rejected(
        proposal, qty_arg, limit_price, position_id, role=OrderRole.OPEN
    ):
        return Order(
            id="order-rejected-001",
            broker_order_id="broker-rejected-abc",
            position_id=position_id,
            role=role,
            status=OrderStatus.REJECTED,
            broker_status_raw="rejected",
            submitted_at=_NOW,
            filled_at=None,
            limit_price=limit_price,
            legs_filled=[],
            net_fill_price=None,
            filled_qty=0,
        )

    broker.submit_multi_leg = _submit_rejected  # type: ignore[method-assign]


def test_run_entry_cycle_broker_rejection_result(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker REJECTED → CycleResult with EXECUTION_FAILED and CycleError(EXECUTE)."""
    broker = _make_broker(monkeypatch)
    _wire_rejected_broker(broker)

    result = run_entry_cycle(Config(), broker=broker, engine=engine)

    assert result.action_taken == ActionTaken.EXECUTION_FAILED
    assert result.error is not None
    assert result.error.stage == CycleStage.EXECUTE
    assert result.error.recoverable is False
    assert result.proposal is not None
    assert result.validation is not None
    assert result.validation.passed is True
    assert result.sizing is not None
    assert result.journal_record_id is not None
    assert result.short_circuit_reason is None


def test_run_entry_cycle_broker_rejection_journal_written(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker REJECTED → JournalRecord written with EXECUTION_FAILED action."""
    broker = _make_broker(monkeypatch)
    _wire_rejected_broker(broker)

    result = run_entry_cycle(Config(), broker=broker, engine=engine)

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)

    assert jr is not None
    assert jr.action_taken == ActionTaken.EXECUTION_FAILED
    assert jr.strategy == "bull_put_spread"
    assert jr.underlying == "SPY"
    # EXECUTION_FAILED is distinct from validation REJECTED — no rule_ids
    assert jr.rejection_rule_ids == []


def test_run_entry_cycle_broker_rejection_no_dangling_position(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker REJECTED → no Position row created (no orphaned PENDING_OPEN)."""
    broker = _make_broker(monkeypatch)
    _wire_rejected_broker(broker)

    result = run_entry_cycle(Config(), broker=broker, engine=engine)

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)
        assert jr is not None
        # No position_ids on an EXECUTION_FAILED journal record.
        assert jr.position_ids == []
        # No Position row should exist for this cycle.
        if jr.position_ids:  # defensive — already asserted empty above
            pos = get_position(conn, jr.position_ids[0])
            assert pos is None
