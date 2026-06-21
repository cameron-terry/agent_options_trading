"""Tests for WP-8.2: orchestrator.py::run_entry_cycle() — full 10-step pipeline.

Happy-path exercises the full pipeline:
  reconcile → state-integrity (no anomalies) → temporal gates → assemble
  → portfolio gates → reason (mocked) → validate → size → execute → journal

All Alpaca network calls are mocked at the BrokerClient method level.
reason() is mocked with stub_reasoner() so tests never call the Anthropic API.
The DB layer uses the shared in-memory SQLite engine fixture from conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from options_agent.agent.stub_reasoner import stub_reasoner
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
    AssignmentEvent,
    Order,
    OrderRef,
    OrderRole,
    OrderStatus,
    PositionStatus,
    StateDiff,
)
from options_agent.execution.broker import BrokerClient
from options_agent.orchestrator import run_entry_cycle, run_monitor_cycle
from options_agent.risk.limits import Limits
from options_agent.state.crud import (
    get_order,
    get_position,
    insert_order,
    insert_position,
)
from options_agent.state.db import get_connection
from options_agent.state.journal import read_journal_record

# Patch target for the real reasoner — mocked in all tests that reach step 7.
_REASON_PATCH = "options_agent.orchestrator.reason"

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
        # WP-8.2 additions (state-integrity short-circuits)
        ShortCircuitReason.WORKING_CANCEL_FAILED,
        ShortCircuitReason.ORPHAN_UNRESOLVED,
        ShortCircuitReason.STATE_INTEGRITY,
        ShortCircuitReason.ASSIGNMENT_HALT,
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
# run_monitor_cycle — market-closed no-op (does not require a broker)
# ---------------------------------------------------------------------------

# Juneteenth (2026-06-19) is an exchange holiday; NYSE is closed.
_MARKET_CLOSED_NOW = datetime(2026, 6, 19, 18, 0, tzinfo=UTC)


def test_run_monitor_cycle_market_closed_returns_empty(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Market-closed: returns empty MonitorResult without evaluating positions."""
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    with patch(
        "options_agent.execution.broker.TradingClient",
        return_value=MagicMock(),
    ):
        broker = BrokerClient(Config())

    result = run_monitor_cycle(
        Config(), broker=broker, engine=engine, _now=_MARKET_CLOSED_NOW
    )

    assert result.positions_evaluated == 0
    assert result.exits_triggered == []
    assert result.orders_submitted == []
    assert result.errors == []


# ---------------------------------------------------------------------------
# Helpers for run_entry_cycle tests
# ---------------------------------------------------------------------------

# Timestamps for internal mock data (submitted_at, filled_at, etc.)
_NOW = datetime(2026, 6, 16, 14, 30, tzinfo=UTC)

# Clock injected into run_entry_cycle — must be a NYSE trading day during open hours,
# outside blackout windows. Tuesday June 16, 2026 at 14:30 UTC = 10:30 AM ET.
_MARKET_HOURS_NOW = datetime(2026, 6, 16, 14, 30, tzinfo=UTC)

# Within the 30-minute open blackout (13:45 UTC = 9:45 AM ET, 15 min after NYSE open).
_BLACKOUT_NOW = datetime(2026, 6, 16, 13, 45, tzinfo=UTC)

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

    submit_multi_leg  → WORKING Order (position_id captured from args)
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


def _wire_reconcile_only(broker: BrokerClient) -> None:
    """Configure only the reconcile-required mocks (clean empty state)."""
    broker.list_open_orders = MagicMock(return_value=[])
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

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

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

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

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

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

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

    This is the primary integration assertion: the FK chain
    JournalRecord → Order → Position must exist and the status must have
    advanced through the real lifecycle, not been written as OPEN directly.
    """
    broker = _make_broker(monkeypatch)
    _wire_happy_broker(broker)

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)
        assert jr is not None
        order = get_order(conn, jr.order_ids[0])
        assert order is not None
        pos = get_position(conn, order.position_id)

    assert pos is not None
    assert pos.status == PositionStatus.OPEN
    assert pos.id == jr.position_ids[0]


# ---------------------------------------------------------------------------
# run_entry_cycle — validation failure (REJECTED)
# ---------------------------------------------------------------------------


def test_run_entry_cycle_validation_failure_result(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REJECTED CycleResult returned when strategy is not in allowed_strategies."""
    broker = _make_broker(monkeypatch)
    _wire_reconcile_only(broker)
    config = Config(limits=Limits(allowed_strategies=frozenset()))

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            config, broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.action_taken == ActionTaken.REJECTED
    assert result.validation is not None
    assert result.validation.passed is False
    assert result.journal_record_id is not None
    assert result.sizing is None


def test_run_entry_cycle_validation_failure_journal_written(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REJECTED cycles write a JournalRecord with rejection_rule_ids populated."""
    broker = _make_broker(monkeypatch)
    _wire_reconcile_only(broker)
    config = Config(limits=Limits(allowed_strategies=frozenset()))

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            config, broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

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
    # reconcile mocks — needed by step 2 (called before execute)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_all_positions = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])


def test_run_entry_cycle_broker_rejection_result(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker REJECTED → CycleResult with EXECUTION_FAILED and CycleError(EXECUTE)."""
    broker = _make_broker(monkeypatch)
    _wire_rejected_broker(broker)

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

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

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)

    assert jr is not None
    assert jr.action_taken == ActionTaken.EXECUTION_FAILED
    assert jr.strategy == "bull_put_spread"
    assert jr.underlying == "SPY"
    assert jr.rejection_rule_ids == []


def test_run_entry_cycle_broker_rejection_no_dangling_position(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broker REJECTED → no Position row created (no orphaned PENDING_OPEN)."""
    broker = _make_broker(monkeypatch)
    _wire_rejected_broker(broker)

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)
        assert jr is not None
        assert jr.position_ids == []
        if jr.position_ids:
            pos = get_position(conn, jr.position_ids[0])
            assert pos is None


# ---------------------------------------------------------------------------
# run_entry_cycle — temporal gate short-circuits
# ---------------------------------------------------------------------------


def test_run_entry_cycle_market_closed_short_circuits(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entry cycle returns MARKET_CLOSED when NYSE is not open."""
    broker = _make_broker(monkeypatch)
    _wire_reconcile_only(broker)

    result = run_entry_cycle(
        Config(), broker=broker, engine=engine, _now=_MARKET_CLOSED_NOW
    )

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.MARKET_CLOSED
    assert result.journal_record_id is None  # gated before journal step


def test_run_entry_cycle_blackout_window_short_circuits(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entry cycle returns BLACKOUT_WINDOW within 30 min of session open."""
    broker = _make_broker(monkeypatch)
    _wire_reconcile_only(broker)

    result = run_entry_cycle(Config(), broker=broker, engine=engine, _now=_BLACKOUT_NOW)

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.BLACKOUT_WINDOW
    assert result.journal_record_id is None


# ---------------------------------------------------------------------------
# run_entry_cycle — state-integrity short-circuits (WP-8.7 / WP-8.8 / WP-8.6)
# ---------------------------------------------------------------------------

_CLEAN_DIFF = StateDiff(reconciled_at=_NOW)


def test_run_entry_cycle_orphan_unresolved(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORPHAN_UNRESOLVED returned when reconcile surfaces unknown broker orders."""
    broker = _make_broker(monkeypatch)
    orphan_diff = StateDiff(
        orphans=[
            OrderRef(
                broker_order_id="orphan-001",
                broker_status_raw="new",
                submitted_at=_NOW,
            )
        ],
        reconciled_at=_NOW,
    )

    with patch("options_agent.orchestrator._reconcile", return_value=orphan_diff):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.ORPHAN_UNRESOLVED
    assert result.journal_record_id == result.cycle_id


def test_run_entry_cycle_state_integrity_unmatched_local(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STATE_INTEGRITY + HALT when reconcile surfaces unmatched-local orders."""
    from options_agent.contracts.state import KillSwitchState
    from options_agent.obs.killswitch import get_current_state

    broker = _make_broker(monkeypatch)

    fake_order = Order(
        id="unmatched-001",
        broker_order_id="",
        position_id="pos-unmatched",
        role=OrderRole.OPEN,
        status=OrderStatus.PENDING_SUBMIT,
        broker_status_raw="",
        submitted_at=_NOW,
        filled_at=None,
        limit_price=-1.50,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )
    unmatched_diff = StateDiff(unmatched_local=[fake_order], reconciled_at=_NOW)

    with patch("options_agent.orchestrator._reconcile", return_value=unmatched_diff):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.STATE_INTEGRITY
    assert result.journal_record_id == result.cycle_id

    # Confirm kill-switch was engaged.
    with get_connection(engine) as conn:
        ks = get_current_state(conn)
    assert ks == KillSwitchState.HALT


def test_run_entry_cycle_assignment_halt(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASSIGNMENT_HALT + HALT when reconcile surfaces an option assignment."""
    from options_agent.contracts.state import KillSwitchState
    from options_agent.obs.killswitch import get_current_state

    broker = _make_broker(monkeypatch)

    assignment = AssignmentEvent(
        closed_option_position_id="pos-option-001",
        created_equity_position=None,
        assigned_qty=100,
        assignment_price=580.0,
        occurred_at=_NOW,
    )
    assigned_diff = StateDiff(assigned_positions=[assignment], reconciled_at=_NOW)

    with patch("options_agent.orchestrator._reconcile", return_value=assigned_diff):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.ASSIGNMENT_HALT
    assert result.journal_record_id == result.cycle_id

    with get_connection(engine) as conn:
        ks = get_current_state(conn)
    assert ks == KillSwitchState.HALT


def test_run_entry_cycle_working_cancel_failed(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKING_CANCEL_FAILED returned when a stale entry order cannot be cancelled."""
    from datetime import date

    from options_agent.contracts.proposal import Leg
    from options_agent.contracts.state import (
        AssetClass,
        LegStatus,
        Position,
        PositionLeg,
        PositionStatus,
    )

    broker = _make_broker(monkeypatch)
    broker.cancel = MagicMock(side_effect=Exception("broker timeout"))
    # Reconcile sees an empty broker (order not visible in open list)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_all_positions = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])

    _expiry = date(2026, 9, 18)
    _leg = Leg(right="put", side="sell", strike=560.0, expiration=_expiry)

    # Pre-insert a Position and WORKING OPEN Order so list_pending_orders finds it.
    pos = Position(
        id="pos-stale-001",
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=_leg, filled_qty=0, avg_fill_price=0.0, status=LegStatus.OPEN
            )
        ],
        quantity=1,
        entry_net_amount=-1.50,
        current_mark=-1.50,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=None,
        status=PositionStatus.PENDING_OPEN,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=_expiry,
        est_max_loss=-500.0,
        est_max_profit=150.0,
        opening_order_id="order-stale-001",
        asset_class=AssetClass.OPTION_STRATEGY,
        equity_legs=[],
        assigned_from_position_id=None,
    )
    stale_order = Order(
        id="order-stale-001",
        broker_order_id="broker-stale-001",
        position_id="pos-stale-001",
        role=OrderRole.OPEN,
        status=OrderStatus.WORKING,
        broker_status_raw="new",
        submitted_at=_NOW,
        filled_at=None,
        limit_price=-1.50,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, stale_order)

    # Reconcile sees no changes (order still WORKING at broker side).
    with patch("options_agent.orchestrator._reconcile", return_value=_CLEAN_DIFF):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.WORKING_CANCEL_FAILED
    assert result.journal_record_id == result.cycle_id


# ---------------------------------------------------------------------------
# run_entry_cycle — ReasonerError → CycleError
# ---------------------------------------------------------------------------


def test_run_entry_cycle_reasoner_error_is_cycle_error(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ReasonerError from reason() is converted to CycleError(stage=REASON)."""
    from options_agent.agent.reasoner import ReasonerError

    broker = _make_broker(monkeypatch)
    _wire_reconcile_only(broker)

    with patch(_REASON_PATCH, side_effect=ReasonerError("schema retries exhausted")):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.error is not None
    assert result.error.stage == CycleStage.REASON
    assert result.error.recoverable is True
    assert result.journal_record_id is not None

    # JournalRecord is written even on REASON failure.
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)
    assert jr is not None


# ---------------------------------------------------------------------------
# run_entry_cycle — NO_ACTION_AGENT path
# ---------------------------------------------------------------------------


def test_run_entry_cycle_no_action_agent(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reasoner returning NO_ACTION journals NO_ACTION_AGENT and skips validate/size."""
    broker = _make_broker(monkeypatch)
    _wire_reconcile_only(broker)

    no_action_proposal = stub_reasoner().model_copy(update={"action": "NO_ACTION"})

    with patch(_REASON_PATCH, return_value=no_action_proposal):
        result = run_entry_cycle(
            Config(), broker=broker, engine=engine, _now=_MARKET_HOURS_NOW
        )

    assert result.action_taken == ActionTaken.NO_ACTION_AGENT
    assert result.proposal is not None
    assert result.proposal.action == "NO_ACTION"
    assert result.short_circuit_reason is None
    assert result.validation is None
    assert result.sizing is None
    assert result.journal_record_id == result.cycle_id

    assert result.journal_record_id is not None
    with get_connection(engine) as conn:
        jr = read_journal_record(conn, result.journal_record_id)
    assert jr is not None
    assert jr.action_taken == ActionTaken.NO_ACTION_AGENT


# ---------------------------------------------------------------------------
# run_entry_cycle — alert dispatch semantics (ENTRY_SUBMITTED before FILL)
# ---------------------------------------------------------------------------


def test_run_entry_cycle_entry_submitted_and_fill_alerts_dispatched(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENTRY_SUBMITTED fires at submit; FILL fires after reconcile confirms fill."""
    from options_agent.contracts.alerts import AlertEventType
    from options_agent.obs.alerts import AlertDispatcher, NullChannel

    broker = _make_broker(monkeypatch)
    _wire_happy_broker(broker)

    channel = NullChannel()
    dispatcher = AlertDispatcher(channel, engine, retry_delay_s=0.0)

    with patch(_REASON_PATCH, return_value=stub_reasoner()):
        result = run_entry_cycle(
            Config(),
            broker=broker,
            engine=engine,
            dispatcher=dispatcher,
            _now=_MARKET_HOURS_NOW,
        )

    dispatcher.shutdown()

    assert result.action_taken == ActionTaken.OPENED

    event_types = [e.event_type for e in channel.sent]
    assert AlertEventType.ENTRY_SUBMITTED in event_types, (
        f"Expected ENTRY_SUBMITTED in dispatched events; got {event_types}"
    )
    assert AlertEventType.FILL in event_types, (
        f"Expected FILL in dispatched events; got {event_types}"
    )

    submitted_idx = event_types.index(AlertEventType.ENTRY_SUBMITTED)
    fill_idx = event_types.index(AlertEventType.FILL)
    assert submitted_idx < fill_idx, (
        "ENTRY_SUBMITTED must precede FILL (submit happens before fill confirmation)"
    )


# ---------------------------------------------------------------------------
# run_entry_cycle — FILL alert idempotency (prior-cycle fills)
# ---------------------------------------------------------------------------


def test_fill_alert_dispatched_exactly_once_on_double_reconcile(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prior-cycle fill fires exactly one FILL alert regardless of how many cycles run.

    Mechanism: list_pending_orders() excludes terminal orders (FILLED, CANCELLED…),
    so reconcile's newly_filled is empty on every pass after the first confirmation.
    This test uses the real _reconcile() (not patched) to exercise that DB guard.
    """
    from datetime import date

    from options_agent.contracts.alerts import AlertEventType
    from options_agent.contracts.proposal import Leg
    from options_agent.contracts.state import (
        AssetClass,
        LegStatus,
        Position,
        PositionLeg,
        PositionStatus,
    )
    from options_agent.obs.alerts import AlertDispatcher, NullChannel

    _expiry = date(2026, 9, 18)
    _prior_broker_id = "broker-prior-fill-001"

    # Insert a WORKING OPEN order left over from a prior cycle (not yet confirmed).
    prior_pos = Position(
        id="pos-prior-fill-001",
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=Leg(right="put", side="sell", strike=560.0, expiration=_expiry),
                filled_qty=0,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            )
        ],
        quantity=2,
        entry_net_amount=-1.50,
        current_mark=-1.50,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=None,
        status=PositionStatus.PENDING_OPEN,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=_expiry,
        est_max_loss=-500.0,
        est_max_profit=150.0,
        opening_order_id="ord-prior-fill-001",
        asset_class=AssetClass.OPTION_STRATEGY,
        equity_legs=[],
        assigned_from_position_id=None,
    )
    prior_order = Order(
        id="ord-prior-fill-001",
        broker_order_id=_prior_broker_id,
        position_id="pos-prior-fill-001",
        role=OrderRole.OPEN,
        status=OrderStatus.WORKING,
        broker_status_raw="new",
        submitted_at=_NOW,
        filled_at=None,
        limit_price=-1.50,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )
    with get_connection(engine) as conn:
        insert_position(conn, prior_pos)
        insert_order(conn, prior_order)

    # Broker reports the order as filled (same response on every call).
    filled_alpaca = MagicMock()
    filled_alpaca.id = _prior_broker_id
    filled_alpaca.status.value = "filled"
    filled_alpaca.filled_qty = 2
    filled_alpaca.filled_avg_price = -1.50
    filled_alpaca.symbol = None
    filled_alpaca.legs = None
    filled_alpaca.submitted_at = _NOW
    filled_alpaca.filled_at = _NOW

    broker = _make_broker(monkeypatch)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)
    broker.get_all_positions = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])

    channel = NullChannel()
    dispatcher = AlertDispatcher(channel, engine, retry_delay_s=0.0)

    no_action_proposal = stub_reasoner().model_copy(update={"action": "NO_ACTION"})

    # Cycle 1: step-2 reconcile sees the prior-cycle fill → one FILL dispatched.
    with patch(_REASON_PATCH, return_value=no_action_proposal):
        run_entry_cycle(
            Config(),
            broker=broker,
            engine=engine,
            dispatcher=dispatcher,
            _now=_MARKET_HOURS_NOW,
        )

    # Cycle 2: prior order is now terminal in DB; list_pending_orders excludes it
    # → newly_filled is empty → no second FILL.
    with patch(_REASON_PATCH, return_value=no_action_proposal):
        run_entry_cycle(
            Config(),
            broker=broker,
            engine=engine,
            dispatcher=dispatcher,
            _now=_MARKET_HOURS_NOW,
        )

    dispatcher.shutdown()

    fill_events = [e for e in channel.sent if e.event_type == AlertEventType.FILL]
    assert len(fill_events) == 1, (
        f"Expected exactly 1 FILL alert across two cycles; got {len(fill_events)}"
    )
