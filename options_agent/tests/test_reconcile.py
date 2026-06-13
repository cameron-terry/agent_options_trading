"""Tests for WP-1.4: execution/reconcile.py fill-detection path.

All Alpaca network calls are mocked via BrokerClient.list_open_orders() and
BrokerClient.get_broker_order().  The DB layer uses the shared in-memory
SQLite engine fixture from conftest.py so we test real SQL round-trips.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest

from options_agent.config import Config
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.state import (
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.execution.broker import BrokerClient
from options_agent.execution.reconcile import reconcile
from options_agent.state.crud import (
    get_order,
    get_position,
    insert_order,
    insert_position,
    list_fill_events_for_order,
)
from options_agent.state.db import get_connection

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 13, 14, 30, tzinfo=UTC)
_LEG = Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 7, 18))
_OCC = "SPY260718P00450000"
_EXIT_PLAN = ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21)
_PROPOSAL = TradeProposal(
    action="OPEN",
    underlying="SPY",
    strategy="bull_put_spread",
    legs=[_LEG],
    thesis="test",
    iv_rationale="high IV rank",
    catalyst_check="no earnings",
    conviction=0.7,
    est_max_loss=500.0,
    est_max_profit=250.0,
    breakevens=[445.0],
    net_delta=-0.2,
    net_theta=0.5,
    net_vega=-0.3,
    exit_plan=_EXIT_PLAN,
    informed_by=[],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pos(
    pos_id: str = "pos-001",
    status: PositionStatus = PositionStatus.PENDING_OPEN,
) -> Position:
    return Position(
        id=pos_id,
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=_LEG, filled_qty=0, avg_fill_price=0.0, status=LegStatus.OPEN
            )
        ],
        quantity=5,
        entry_net_amount=-312.50,
        current_mark=-312.50,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=status,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=date(2026, 7, 18),
        est_max_loss=500.0,
        est_max_profit=250.0,
        opening_order_id="ord-001",
    )


def _order(
    order_id: str = "ord-001",
    broker_order_id: str = "broker-abc",
    status: OrderStatus = OrderStatus.WORKING,
    filled_qty: int = 0,
    position_id: str = "pos-001",
    role: OrderRole = OrderRole.OPEN,
) -> Order:
    return Order(
        id=order_id,
        broker_order_id=broker_order_id,
        position_id=position_id,
        role=role,
        status=status,
        broker_status_raw="new",
        submitted_at=_NOW,
        filled_at=None,
        limit_price=1.25,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=filled_qty,
    )


def _alpaca_order(
    broker_id: str = "broker-abc",
    status: str = "filled",
    filled_qty: int = 5,
    filled_avg_price: float | None = 1.25,
    symbol: str | None = _OCC,
    submitted_at: datetime | None = None,
    filled_at: datetime | None = None,
    legs: list | None = None,
) -> MagicMock:
    """Build a mock AlpacaOrder matching the fields reconcile reads."""
    o = MagicMock()
    o.id = broker_id
    o.status.value = status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.symbol = symbol
    o.submitted_at = submitted_at or _NOW
    o.filled_at = filled_at
    o.legs = legs
    return o


def _broker(monkeypatch: pytest.MonkeyPatch) -> BrokerClient:
    """Return a BrokerClient with a mocked TradingClient."""
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    with patch(
        "options_agent.execution.broker.TradingClient",
        return_value=MagicMock(),
    ):
        return BrokerClient(Config())


def _seed(engine, pos: Position, order: Order) -> None:
    """Insert one position and one order into the DB."""
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, order)


# ---------------------------------------------------------------------------
# Full fill: WORKING -> FILLED
# ---------------------------------------------------------------------------


def test_full_fill_transitions_order_status(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    filled_alpaca = _alpaca_order(
        status="filled",
        filled_qty=5,
        filled_avg_price=1.25,
        filled_at=_NOW,
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.newly_filled) == 1
    assert diff.newly_filled[0].id == "ord-001"
    assert diff.newly_filled[0].status == OrderStatus.FILLED
    assert diff.newly_filled[0].filled_qty == 5

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-001")
    assert fetched is not None
    assert fetched.status == OrderStatus.FILLED
    assert fetched.filled_qty == 5
    assert fetched.net_fill_price == 1.25


def test_full_fill_transitions_position_to_open(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(status=PositionStatus.PENDING_OPEN), _order())

    filled_alpaca = _alpaca_order(status="filled", filled_qty=5, filled_at=_NOW)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.new_positions) == 1
    assert diff.new_positions[0].id == "pos-001"
    assert diff.new_positions[0].status == PositionStatus.OPEN

    with get_connection(engine) as conn:
        pos = get_position(conn, "pos-001")
    assert pos is not None
    assert pos.status == PositionStatus.OPEN


def test_full_fill_records_fill_event(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    filled_alpaca = _alpaca_order(status="filled", filled_qty=5, filled_at=_NOW)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        reconcile(broker, conn)

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-001")
    assert len(events) == 1
    assert events[0].broker_exec_id == "broker-abc@5"
    assert events[0].filled_qty == 5
    assert events[0].leg_symbol == _OCC


def test_full_fill_close_role_transitions_position_to_closed(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos(status=PositionStatus.PENDING_CLOSE)
    ord_ = _order(role=OrderRole.CLOSE)
    _seed(engine, pos, ord_)

    filled_alpaca = _alpaca_order(status="filled", filled_qty=5, filled_at=_NOW)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.closed_positions) == 1
    assert diff.closed_positions[0].status == PositionStatus.CLOSED

    with get_connection(engine) as conn:
        pos_after = get_position(conn, "pos-001")
    assert pos_after is not None
    assert pos_after.status == PositionStatus.CLOSED
    assert pos_after.closed_at is not None


def test_full_fill_roll_role_transitions_position_to_closed(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROLL-role fills must close the position just like CLOSE-role fills."""
    broker = _broker(monkeypatch)
    pos = _pos(status=PositionStatus.PENDING_CLOSE)
    ord_ = _order(role=OrderRole.ROLL)
    _seed(engine, pos, ord_)

    filled_alpaca = _alpaca_order(status="filled", filled_qty=5, filled_at=_NOW)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.closed_positions) == 1
    assert diff.closed_positions[0].status == PositionStatus.CLOSED

    with get_connection(engine) as conn:
        pos_after = get_position(conn, "pos-001")
    assert pos_after is not None
    assert pos_after.status == PositionStatus.CLOSED


# ---------------------------------------------------------------------------
# Partial fill: WORKING -> PARTIALLY_FILLED
# ---------------------------------------------------------------------------


def test_partial_fill_transitions_order_to_partially_filled(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    # Order is still open at broker (partially filled)
    partial_alpaca = _alpaca_order(
        broker_id="broker-abc",
        status="partially_filled",
        filled_qty=3,
        filled_avg_price=1.24,
    )
    broker.list_open_orders = MagicMock(return_value=[partial_alpaca])
    broker.get_broker_order = MagicMock()

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.newly_partial) == 1
    assert diff.newly_partial[0].filled_qty == 3
    assert diff.newly_partial[0].status == OrderStatus.PARTIALLY_FILLED
    assert len(diff.newly_filled) == 0

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-001")
    assert fetched is not None
    assert fetched.status == OrderStatus.PARTIALLY_FILLED
    assert fetched.filled_qty == 3


def test_partial_fill_records_fill_event(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    partial_alpaca = _alpaca_order(
        status="partially_filled", filled_qty=3, filled_avg_price=1.24
    )
    broker.list_open_orders = MagicMock(return_value=[partial_alpaca])
    broker.get_broker_order = MagicMock()

    with get_connection(engine) as conn:
        reconcile(broker, conn)

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-001")
    assert len(events) == 1
    assert events[0].broker_exec_id == "broker-abc@3"
    assert events[0].filled_qty == 3


def test_incremental_partial_then_full_records_two_fill_events(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    # Pass 1: partial fill of 3
    partial_alpaca = _alpaca_order(
        status="partially_filled", filled_qty=3, filled_avg_price=1.24
    )
    broker.list_open_orders = MagicMock(return_value=[partial_alpaca])
    broker.get_broker_order = MagicMock()

    with get_connection(engine) as conn:
        reconcile(broker, conn)

    # Pass 2: fully filled at 5 (incremental 2 more)
    filled_alpaca = _alpaca_order(
        status="filled", filled_qty=5, filled_avg_price=1.25, filled_at=_NOW
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        diff2 = reconcile(broker, conn)

    assert len(diff2.newly_filled) == 1

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-001")
    assert len(events) == 2
    incremental_qtys = {e.broker_exec_id: e.filled_qty for e in events}
    assert incremental_qtys["broker-abc@3"] == 3
    assert incremental_qtys["broker-abc@5"] == 2  # 5 - 3


# ---------------------------------------------------------------------------
# Idempotency: running the same pass twice changes nothing on second pass
# ---------------------------------------------------------------------------


def test_idempotent_full_fill_second_pass_is_empty(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    filled_alpaca = _alpaca_order(status="filled", filled_qty=5, filled_at=_NOW)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        diff1 = reconcile(broker, conn)
    with get_connection(engine) as conn:
        diff2 = reconcile(broker, conn)

    assert len(diff1.newly_filled) == 1
    # Second pass: order is now terminal, list_pending_orders returns nothing
    assert len(diff2.newly_filled) == 0
    assert len(diff2.newly_partial) == 0

    # FillEvent inserted exactly once
    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-001")
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancellation_detected(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    cancelled_alpaca = _alpaca_order(
        status="canceled", filled_qty=0, filled_avg_price=None, filled_at=None
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=cancelled_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.newly_cancelled) == 1
    assert diff.newly_cancelled[0].status == OrderStatus.CANCELLED
    assert len(diff.newly_filled) == 0

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-001")
    assert fetched is not None
    assert fetched.status == OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


def test_rejection_detected(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    rejected_alpaca = _alpaca_order(
        status="rejected", filled_qty=0, filled_avg_price=None, filled_at=None
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=rejected_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.newly_rejected) == 1
    assert diff.newly_rejected[0].status == OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# Orphans: open at broker, no local record
# ---------------------------------------------------------------------------


def test_orphan_detected_when_broker_has_unknown_order(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    # No local orders — empty DB for orders

    orphan_alpaca = _alpaca_order(broker_id="orphan-xyz", status="new", filled_qty=0)
    broker.list_open_orders = MagicMock(return_value=[orphan_alpaca])
    broker.get_broker_order = MagicMock()

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.orphans) == 1
    assert diff.orphans[0].broker_order_id == "orphan-xyz"
    assert len(diff.newly_filled) == 0


def test_known_order_is_not_flagged_as_orphan(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order(status=OrderStatus.WORKING))

    # Broker has the matching order open — not an orphan
    open_alpaca = _alpaca_order(broker_id="broker-abc", status="new", filled_qty=0)
    broker.list_open_orders = MagicMock(return_value=[open_alpaca])
    broker.get_broker_order = MagicMock()

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.orphans) == 0


# ---------------------------------------------------------------------------
# Unmatched local: PENDING_SUBMIT with no broker_order_id (crash breadcrumb)
# ---------------------------------------------------------------------------


def test_unmatched_local_pending_submit_with_no_broker_id(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    # Insert position + a PENDING_SUBMIT order with empty broker_order_id
    crash_order = _order(
        order_id="ord-crash",
        broker_order_id="",  # crash before submit reached broker
        status=OrderStatus.PENDING_SUBMIT,
    )
    _seed(engine, _pos(), crash_order)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock()

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.unmatched_local) == 1
    assert diff.unmatched_local[0].id == "ord-crash"
    assert len(diff.anomalies) == 0
    assert len(diff.orphans) == 0


# ---------------------------------------------------------------------------
# Anomaly: filled_qty went backwards
# ---------------------------------------------------------------------------


def test_anomaly_filled_qty_backwards(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _broker(monkeypatch)
    # Local order already has filled_qty=5 (was partially filled previously)
    _seed(
        engine,
        _pos(),
        _order(status=OrderStatus.PARTIALLY_FILLED, filled_qty=5),
    )

    # Broker reports filled_qty=3 — went backwards
    bad_alpaca = _alpaca_order(status="partially_filled", filled_qty=3)
    broker.list_open_orders = MagicMock(return_value=[bad_alpaca])
    broker.get_broker_order = MagicMock()

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.anomalies) == 1
    assert "backwards" in diff.anomalies[0].description
    assert diff.anomalies[0].order_id == "ord-001"
    assert len(diff.newly_partial) == 0
    assert len(diff.newly_filled) == 0


# ---------------------------------------------------------------------------
# Broker fetch failure
# ---------------------------------------------------------------------------


def test_broker_fetch_failure_returns_anomaly(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    broker.list_open_orders = MagicMock(side_effect=RuntimeError("network error"))
    broker.get_broker_order = MagicMock()

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.anomalies) == 1
    assert "Broker fetch failed" in diff.anomalies[0].description


def test_individual_order_fetch_failure_records_anomaly(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(side_effect=RuntimeError("timeout"))

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.anomalies) == 1
    assert diff.anomalies[0].order_id == "ord-001"


# ---------------------------------------------------------------------------
# StateDiff completeness
# ---------------------------------------------------------------------------


def test_statediff_reconciled_at_is_populated(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    broker.list_open_orders = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert diff.reconciled_at is not None
    assert diff.reconciled_at.tzinfo is not None


def test_statediff_is_loggable_as_dict(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """StateDiff.model_dump() must not raise — required for WP-7/WP-8 logging."""
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    filled_alpaca = _alpaca_order(status="filled", filled_qty=5, filled_at=_NOW)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    serialised = diff.model_dump(mode="json")
    assert "newly_filled" in serialised
    assert "reconciled_at" in serialised


def test_no_local_orders_produces_empty_diff(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    broker.list_open_orders = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert diff.newly_filled == []
    assert diff.newly_partial == []
    assert diff.newly_cancelled == []
    assert diff.orphans == []
    assert diff.unmatched_local == []
    assert diff.anomalies == []
