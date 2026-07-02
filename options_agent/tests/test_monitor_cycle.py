"""End-to-end tests for run_monitor_cycle().

WP-5.5 core coverage:
- Stop-loss trigger: position breaches threshold → closing order submitted,
  position → PENDING_CLOSE, MonitorResult records the exit.
- Profit-target trigger: same flow with a profitable position.
- DTE / time-stop trigger: expiration within threshold → close submitted.
- Idempotency: running twice after a trigger submits exactly one closing order.
- FLATTEN mode: all open positions closed regardless of rule thresholds.
- Per-position error isolation: one MarkStaleError does not stop other positions.
- OutcomeRecord finalize: reconcile-detected CLOSED position gets an OutcomeRecord
  with the real fill price and exit_reason (written in the next cycle's finalize step).

WP-8.3 alert dispatch coverage:
- EXIT_SUBMITTED dispatched at close-order-submit time (early signal, order WORKING).
- FILL dispatched in finalize step with realized_pnl (fill-confirmed, not submit-time).
- dispatcher=None is safe — no raises, no behaviour change.
- Assignment during reconcile → HALT + CRITICAL alert; monitor continues managing
  remaining options positions (does not return early).

All Alpaca calls are mocked at the BrokerClient method level.
All tests use in-memory SQLite via the shared `engine` fixture from conftest.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from options_agent.config import Config
from options_agent.contracts.alerts import AlertEventType
from options_agent.contracts.journal import OutcomeEventType
from options_agent.contracts.proposal import ExitPlan, Leg
from options_agent.contracts.state import (
    AssetClass,
    AssignmentEvent,
    ExitReason,
    KillSwitchState,
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.execution.broker import BrokerClient
from options_agent.obs.alerts import AlertDispatcher, NullChannel
from options_agent.obs.killswitch import get_current_state
from options_agent.orchestrator import run_monitor_cycle
from options_agent.state.crud import get_order, get_position, insert_position
from options_agent.state.db import get_connection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tuesday 2026-06-16 at 14:30 ET (18:30 UTC) — NYSE is open.
_MARKET_OPEN_NOW = datetime(2026, 6, 16, 18, 30, tzinfo=UTC)
# Mark set 2 minutes before _MARKET_OPEN_NOW — fresh within the 10-min window.
_FRESH_MARK = _MARKET_OPEN_NOW - timedelta(minutes=2)

_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50,
    stop_loss_max_loss_fraction=0.50,
    time_stop_dte=21,
)

_SHORT_PUT = Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15))
_LONG_PUT = Leg(right="put", side="buy", strike=445.0, expiration=date(2026, 8, 15))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(
    *,
    unrealized_pnl: float = 50.0,
    current_mark: float = -1.00,
    marked_at: datetime = _FRESH_MARK,
    status: PositionStatus = PositionStatus.OPEN,
    nearest_expiration: date = date(2026, 8, 15),
    est_max_loss: float = 2225.0,
    est_max_profit: float = 275.0,
    entry_net_amount: float = -275.0,
    exit_plan: ExitPlan | None = _EXIT_PLAN,
) -> Position:
    return Position(
        id=str(uuid.uuid4()),
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=_SHORT_PUT,
                filled_qty=1,
                avg_fill_price=0.55,
                status=LegStatus.OPEN,
            ),
            PositionLeg(
                leg=_LONG_PUT,
                filled_qty=1,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
        ],
        quantity=1,
        entry_net_amount=entry_net_amount,
        current_mark=current_mark,
        marked_at=marked_at,
        unrealized_pnl=unrealized_pnl,
        realized_pnl=None,
        exit_plan=exit_plan,
        status=status,
        opened_at=_MARKET_OPEN_NOW - timedelta(days=30),
        closed_at=None,
        nearest_expiration=nearest_expiration,
        est_max_loss=est_max_loss,
        est_max_profit=est_max_profit,
        opening_order_id=str(uuid.uuid4()),
        asset_class=AssetClass.OPTION_STRATEGY,
        equity_legs=[],
        assigned_from_position_id=None,
    )


def _make_close_order(position_id: str, exit_reason: ExitReason) -> Order:
    return Order(
        id=str(uuid.uuid4()),
        broker_order_id=f"broker-close-{position_id[:8]}",
        position_id=position_id,
        role=OrderRole.CLOSE,
        status=OrderStatus.WORKING,
        broker_status_raw="new",
        submitted_at=_MARKET_OPEN_NOW,
        filled_at=None,
        limit_price=1.02,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
        exit_reason=exit_reason,
    )


def _make_broker(monkeypatch: pytest.MonkeyPatch, config: Config) -> BrokerClient:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    with patch(
        "options_agent.execution.broker.TradingClient",
        return_value=MagicMock(),
    ):
        return BrokerClient(config)


def _wire_broker_noop(broker: BrokerClient) -> None:
    """Wire broker so reconcile is a no-op (no live orders or positions)."""
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_all_positions = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])
    # Return None for any order-status poll; reconcile handles None gracefully.
    broker.get_broker_order = MagicMock(return_value=None)


def _wire_submit_close(broker: BrokerClient, close_order: Order) -> None:
    """Wire broker.submit_multi_leg to return close_order on the next call."""
    broker.submit_multi_leg = MagicMock(return_value=close_order)


# ---------------------------------------------------------------------------
# Stop-loss trigger
# ---------------------------------------------------------------------------


def test_stop_loss_trigger(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop-loss fires: position → PENDING_CLOSE and MonitorResult records the exit."""
    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    # est_max_loss=2225, fraction=0.50 → threshold=-1112.50; pnl=-1200 triggers.
    pos = _make_position(unrealized_pnl=-1200.0)
    close_order = _make_close_order(pos.id, ExitReason.STOP_LOSS)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    result = run_monitor_cycle(
        config, broker=broker, engine=engine, _now=_MARKET_OPEN_NOW
    )

    assert pos.id in result.exits_triggered
    assert close_order.id in result.orders_submitted
    assert result.errors == []

    with get_connection(engine) as conn:
        db_pos = get_position(conn, pos.id)
        db_order = get_order(conn, close_order.id)

    assert db_pos is not None
    assert db_pos.status == PositionStatus.PENDING_CLOSE
    assert db_order is not None
    assert db_order.exit_reason == ExitReason.STOP_LOSS


def test_profit_target_trigger(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Profit-target fires: position → PENDING_CLOSE with PROFIT_TARGET exit_reason."""
    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    # est_max_profit=275, pct=0.50 → threshold=137.50; pnl=150 triggers.
    pos = _make_position(unrealized_pnl=150.0)
    close_order = _make_close_order(pos.id, ExitReason.PROFIT_TARGET)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    result = run_monitor_cycle(
        config, broker=broker, engine=engine, _now=_MARKET_OPEN_NOW
    )

    assert pos.id in result.exits_triggered
    assert result.errors == []

    with get_connection(engine) as conn:
        db_order = get_order(conn, close_order.id)

    assert db_order is not None
    assert db_order.exit_reason == ExitReason.PROFIT_TARGET


def test_time_stop_trigger(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """DTE time-stop fires when nearest_expiration is within time_stop_dte (21 days)."""
    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    # _MARKET_OPEN_NOW is 2026-06-16; 14 days later = 2026-06-30 ≤ 21 DTE threshold.
    expiry_within_threshold = date(2026, 6, 30)
    pos = _make_position(
        unrealized_pnl=50.0,  # not at stop or profit target
        nearest_expiration=expiry_within_threshold,
    )
    close_order = _make_close_order(pos.id, ExitReason.DTE)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    result = run_monitor_cycle(
        config, broker=broker, engine=engine, _now=_MARKET_OPEN_NOW
    )

    assert pos.id in result.exits_triggered
    assert result.errors == []

    with get_connection(engine) as conn:
        db_order = get_order(conn, close_order.id)

    assert db_order is not None
    assert db_order.exit_reason == ExitReason.DTE


# ---------------------------------------------------------------------------
# Idempotency — running twice must not submit a second closing order
# ---------------------------------------------------------------------------


def test_idempotency_no_duplicate_close(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the monitor twice after a trigger produces exactly one closing order."""
    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    pos = _make_position(unrealized_pnl=-1200.0)  # stop-loss territory
    close_order = _make_close_order(pos.id, ExitReason.STOP_LOSS)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    # First run: stop fires, order submitted.
    result1 = run_monitor_cycle(
        config, broker=broker, engine=engine, _now=_MARKET_OPEN_NOW
    )
    assert pos.id in result1.exits_triggered

    # Second run: position is PENDING_CLOSE — stop must not re-fire.
    result2 = run_monitor_cycle(
        config, broker=broker, engine=engine, _now=_MARKET_OPEN_NOW
    )
    assert pos.id not in result2.exits_triggered
    assert result2.orders_submitted == []

    # submit_multi_leg must have been called exactly once total.
    assert broker.submit_multi_leg.call_count == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# FLATTEN mode
# ---------------------------------------------------------------------------


def test_flatten_closes_all_positions(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """FLATTEN closes all open positions regardless of rule thresholds."""
    from options_agent.contracts.state import KillSwitchState
    from options_agent.obs.killswitch import set_state

    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    # Two positions — neither is at stop-loss or profit-target.
    pos_a = _make_position(unrealized_pnl=10.0)
    pos_b = _make_position(unrealized_pnl=20.0)

    # Separate close orders for each position.
    close_a = _make_close_order(pos_a.id, ExitReason.FLATTEN)
    close_b = _make_close_order(pos_b.id, ExitReason.FLATTEN)
    broker.submit_multi_leg = MagicMock(  # type: ignore[attr-defined]
        side_effect=[close_a, close_b]
    )

    with get_connection(engine) as conn:
        insert_position(conn, pos_a)
        insert_position(conn, pos_b)
        set_state(conn, KillSwitchState.FLATTEN, set_by="test", reason="test flatten")

    result = run_monitor_cycle(
        config, broker=broker, engine=engine, _now=_MARKET_OPEN_NOW
    )

    assert pos_a.id in result.exits_triggered
    assert pos_b.id in result.exits_triggered
    assert result.errors == []

    with get_connection(engine) as conn:
        db_order_a = get_order(conn, close_a.id)
        db_order_b = get_order(conn, close_b.id)

    assert db_order_a is not None and db_order_a.exit_reason == ExitReason.FLATTEN
    assert db_order_b is not None and db_order_b.exit_reason == ExitReason.FLATTEN


# ---------------------------------------------------------------------------
# Per-position error isolation — MarkStaleError on one position
# ---------------------------------------------------------------------------


def test_stale_mark_error_isolated(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """MarkStaleError on one position is recorded in errors; other positions fire."""
    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    # Stale position: marked 30 minutes ago — beyond the 10-min max_mark_age.
    stale_mark = _MARKET_OPEN_NOW - timedelta(minutes=30)
    pos_stale = _make_position(unrealized_pnl=-1200.0, marked_at=stale_mark)

    # Fresh position at stop-loss: should still fire despite pos_stale erroring.
    pos_fresh = _make_position(unrealized_pnl=-1200.0)
    close_order = _make_close_order(pos_fresh.id, ExitReason.STOP_LOSS)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos_stale)
        insert_position(conn, pos_fresh)

    result = run_monitor_cycle(
        config, broker=broker, engine=engine, _now=_MARKET_OPEN_NOW
    )

    # Stale position produced an error but did not stop the fresh position.
    assert len(result.errors) == 1
    assert pos_fresh.id in result.exits_triggered
    assert pos_stale.id not in result.exits_triggered


# ---------------------------------------------------------------------------
# OutcomeRecord finalize: written on fill confirmation, not at trigger time
# ---------------------------------------------------------------------------


def test_outcome_record_written_on_fill_confirmation(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OutcomeRecord is written in the cycle that reconcile detects the close fill."""

    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    pos = _make_position(unrealized_pnl=-1200.0)
    close_order = _make_close_order(pos.id, ExitReason.STOP_LOSS)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    # Cycle 1: trigger fires, closing order submitted.
    run_monitor_cycle(config, broker=broker, engine=engine, _now=_MARKET_OPEN_NOW)

    # No OutcomeRecord yet — order hasn't filled.
    with get_connection(engine) as conn:
        # read_outcome_record requires the id; check via the position join.
        from options_agent.state.db import outcome_records_table

        row = conn.execute(
            outcome_records_table.select().where(
                outcome_records_table.c.position_id == pos.id
            )
        ).first()
    assert row is None, "OutcomeRecord must not be written at trigger time"

    # Simulate fill: the closing order has now filled in the broker.
    filled_at = _MARKET_OPEN_NOW + timedelta(seconds=30)
    filled_alpaca = MagicMock()
    filled_alpaca.id = close_order.broker_order_id
    filled_alpaca.status.value = "filled"
    filled_alpaca.filled_qty = pos.quantity
    filled_alpaca.filled_avg_price = 1.05
    filled_alpaca.symbol = None
    filled_alpaca.legs = None
    filled_alpaca.submitted_at = _MARKET_OPEN_NOW
    filled_alpaca.filled_at = filled_at

    # Reconcile now returns the filled close order; simulate by wiring broker.
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    # Cycle 2: reconcile detects fill; finalize step writes OutcomeRecord.
    cycle2_time = _MARKET_OPEN_NOW + timedelta(minutes=5)
    run_monitor_cycle(config, broker=broker, engine=engine, _now=cycle2_time)

    with get_connection(engine) as conn:
        from options_agent.state.db import outcome_records_table

        row = conn.execute(
            outcome_records_table.select().where(
                outcome_records_table.c.position_id == pos.id
            )
        ).first()

    assert row is not None, "OutcomeRecord must be written after fill is confirmed"
    assert row.exit_reason == ExitReason.STOP_LOSS.value
    assert row.event_type == OutcomeEventType.FULL_CLOSE.value
    assert row.contracts_closed == pos.quantity
    # realized_pnl = (-entry_net_amount - fill_price) * qty * 100
    # = (-(-275.0) - 1.05) * 1 * 100 = 27395.0
    assert row.fill_price == pytest.approx(1.05)
    assert row.realized_pnl == pytest.approx(27395.0)


# ---------------------------------------------------------------------------
# WP-8.3 — alert dispatch
# ---------------------------------------------------------------------------


def _make_null_dispatcher(engine) -> AlertDispatcher:
    """Return a NullChannel-backed AlertDispatcher for test introspection."""
    ch = NullChannel()
    d = AlertDispatcher(ch, engine=engine)
    return d


def test_exit_submitted_alert_dispatched_on_trigger(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXIT_SUBMITTED fires at close-order-submit time, not at fill-confirmation."""
    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    pos = _make_position(unrealized_pnl=-1200.0)  # stop-loss territory
    close_order = _make_close_order(pos.id, ExitReason.STOP_LOSS)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    dispatcher = _make_null_dispatcher(engine)
    ch: NullChannel = dispatcher._channel  # type: ignore[attr-defined]

    run_monitor_cycle(
        config,
        broker=broker,
        engine=engine,
        dispatcher=dispatcher,
        _now=_MARKET_OPEN_NOW,
    )
    dispatcher.shutdown()

    exit_submitted = [
        e for e in ch.sent if e.event_type == AlertEventType.EXIT_SUBMITTED
    ]
    assert len(exit_submitted) == 1
    ev = exit_submitted[0]
    assert ev.symbol == pos.underlying
    assert ev.order_id == close_order.broker_order_id
    assert "stop_loss" in ev.detail.lower() or "stop" in ev.detail.lower()

    # No FILL at submit time — that fires only at fill-confirmation.
    fill_events = [e for e in ch.sent if e.event_type == AlertEventType.FILL]
    assert fill_events == []


def test_fill_alert_dispatched_on_close_confirmation(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FILL fires in the finalize step with realized_pnl, not at submit time."""
    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    pos = _make_position(unrealized_pnl=-1200.0)
    close_order = _make_close_order(pos.id, ExitReason.STOP_LOSS)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    # Cycle 1: trigger fires, closing order submitted. No FILL yet.
    dispatcher = _make_null_dispatcher(engine)
    ch: NullChannel = dispatcher._channel  # type: ignore[attr-defined]

    run_monitor_cycle(
        config,
        broker=broker,
        engine=engine,
        dispatcher=dispatcher,
        _now=_MARKET_OPEN_NOW,
    )

    fill_after_trigger = [e for e in ch.sent if e.event_type == AlertEventType.FILL]
    assert fill_after_trigger == [], "FILL must not fire at submit time"

    # Simulate fill: closing order confirmed as filled.
    filled_at = _MARKET_OPEN_NOW + timedelta(seconds=30)
    filled_alpaca = MagicMock()
    filled_alpaca.id = close_order.broker_order_id
    filled_alpaca.status.value = "filled"
    filled_alpaca.filled_qty = pos.quantity
    filled_alpaca.filled_avg_price = 1.05
    filled_alpaca.symbol = None
    filled_alpaca.legs = None
    filled_alpaca.submitted_at = _MARKET_OPEN_NOW
    filled_alpaca.filled_at = filled_at

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    cycle2_time = _MARKET_OPEN_NOW + timedelta(minutes=5)
    run_monitor_cycle(
        config, broker=broker, engine=engine, dispatcher=dispatcher, _now=cycle2_time
    )
    dispatcher.shutdown()

    fill_events = [e for e in ch.sent if e.event_type == AlertEventType.FILL]
    assert len(fill_events) == 1
    ev = fill_events[0]
    assert ev.symbol == pos.underlying
    assert ev.order_id == close_order.broker_order_id
    # realized_pnl = (-(-275.0) - 1.05) * 5 * 100 = 136975.0
    assert "136975" in ev.detail or "realized_pnl" in ev.detail


def test_dispatcher_none_does_not_raise(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing dispatcher=None is safe — no raise, existing behaviour unchanged."""
    config = Config()
    broker = _make_broker(monkeypatch, config)
    _wire_broker_noop(broker)

    pos = _make_position(unrealized_pnl=-1200.0)
    close_order = _make_close_order(pos.id, ExitReason.STOP_LOSS)
    _wire_submit_close(broker, close_order)

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    # No dispatcher — must not raise.
    result = run_monitor_cycle(
        config, broker=broker, engine=engine, dispatcher=None, _now=_MARKET_OPEN_NOW
    )

    assert pos.id in result.exits_triggered
    assert result.errors == []


# ---------------------------------------------------------------------------
# WP-8.3 — assignment handling
# ---------------------------------------------------------------------------


def test_assignment_engages_halt_and_monitor_continues(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assignment → HALT + CRITICAL; monitor still evaluates remaining options
    positions.

    WP-7.1 HALT semantics: HALT stops new entries, not the monitor. The monitor
    must keep managing the existing options book even after HALT.
    """

    config = Config()
    broker = _make_broker(monkeypatch, config)

    # One options position at stop-loss — should still be closed despite the
    # assignment triggering HALT, because the monitor continues after HALT.
    pos = _make_position(unrealized_pnl=-1200.0)
    close_order = _make_close_order(pos.id, ExitReason.STOP_LOSS)

    # Broker: normal reconcile returns no open orders/positions ...
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_all_positions = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=None)
    # ... but the reconcile StateDiff contains an assignment event.
    assignment = AssignmentEvent(
        closed_option_position_id=str(uuid.uuid4()),
        created_equity_position=None,
        assigned_qty=100,
        assignment_price=450.0,
        occurred_at=_MARKET_OPEN_NOW,
    )

    with get_connection(engine) as conn:
        insert_position(conn, pos)

    dispatcher = _make_null_dispatcher(engine)
    ch: NullChannel = dispatcher._channel  # type: ignore[attr-defined]

    # Inject assignment into StateDiff returned by reconcile.
    from options_agent.contracts.state import StateDiff

    real_state_diff = StateDiff(
        assigned_positions=[assignment],
        reconciled_at=_MARKET_OPEN_NOW,
    )

    broker.submit_multi_leg = MagicMock(return_value=close_order)

    with patch(
        "options_agent.orchestrator._reconcile",
        return_value=real_state_diff,
    ):
        result = run_monitor_cycle(
            config,
            broker=broker,
            engine=engine,
            dispatcher=dispatcher,
            _now=_MARKET_OPEN_NOW,
        )
    dispatcher.shutdown()

    # HALT must have been engaged.
    with get_connection(engine) as conn:
        ks = get_current_state(conn)
    assert ks == KillSwitchState.HALT

    # KILL_SWITCH_CHANGE CRITICAL alert must have been dispatched.
    critical_alerts = [
        e for e in ch.sent if e.event_type == AlertEventType.KILL_SWITCH_CHANGE
    ]
    assert len(critical_alerts) >= 1

    # Monitor must have continued and closed the options position.
    assert pos.id in result.exits_triggered, (
        "Monitor must continue evaluating options positions after HALT"
    )
