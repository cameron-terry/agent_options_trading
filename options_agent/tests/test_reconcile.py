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
    FillEvent,
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.execution.broker import BrokerClient
from options_agent.execution.reconcile import _record_fill_events, reconcile
from options_agent.state.crud import (
    get_order,
    get_position,
    insert_fill_event_if_new,
    insert_order,
    insert_position,
    list_fill_events_for_order,
    list_open_positions,
)
from options_agent.state.db import get_connection

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 13, 14, 30, tzinfo=UTC)
_LEG = Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 7, 18))
_OCC = "SPY260718P00450000"
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)
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
# WP-1: two-leg credit-spread fixtures for the async fill-time recompute.
# ---------------------------------------------------------------------------

_CS_EXPIRY = date(2026, 7, 18)
_CS_LEG_SHORT = Leg(right="put", side="sell", strike=560.0, expiration=_CS_EXPIRY)
_CS_LEG_LONG = Leg(right="put", side="buy", strike=555.0, expiration=_CS_EXPIRY)


def _credit_spread_pos(
    pos_id: str = "pos-cs-001",
    status: PositionStatus = PositionStatus.PENDING_OPEN,
) -> Position:
    return Position(
        id=pos_id,
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=_CS_LEG_SHORT,
                filled_qty=0,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
            PositionLeg(
                leg=_CS_LEG_LONG,
                filled_qty=0,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
        ],
        quantity=1,
        entry_net_amount=-1.50,
        current_mark=-1.50,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=status,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=date(2026, 7, 18),
        est_max_loss=350.0,
        est_max_profit=150.0,
        opening_order_id="ord-cs-001",
    )


def _credit_spread_order(
    order_id: str = "ord-cs-001",
    broker_order_id: str = "broker-cs-001",
    status: OrderStatus = OrderStatus.WORKING,
    filled_qty: int = 0,
    position_id: str = "pos-cs-001",
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
        limit_price=-1.50,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=filled_qty,
    )


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
        # _clock pins reconcile()'s notion of "now" to the fixture's _NOW
        # (2026-06-13), which is safely before _pos()'s nearest_expiration
        # (2026-07-18). Without it, reconcile() falls back to the real
        # wall-clock date; once that passes 2026-07-18 the position transitions
        # PENDING_OPEN -> OPEN and is immediately caught by the same-pass
        # expiry backstop and flipped to EXPIRED before this test can assert
        # on it.
        diff = reconcile(broker, conn, _clock=_NOW)

    assert len(diff.new_positions) == 1
    assert diff.new_positions[0].id == "pos-001"
    assert diff.new_positions[0].status == PositionStatus.OPEN

    with get_connection(engine) as conn:
        pos = get_position(conn, "pos-001")
    assert pos is not None
    assert pos.status == PositionStatus.OPEN


def test_async_fill_recomputes_est_max_loss_from_fill_price(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP-1: an order that fills asynchronously (not at cycle-top, so it's
    picked up by a later reconcile() pass) must still correct
    Position.est_max_loss/profit against the real fill, not leave the
    pre-fill chain-mid estimate (350/150) stranded.
    """
    broker = _broker(monkeypatch)
    _seed(
        engine,
        _credit_spread_pos(status=PositionStatus.PENDING_OPEN),
        _credit_spread_order(),
    )

    # Filled at a better credit (2.00) than the 1.50 mid the position was
    # created with -> max loss (5 - 2.00) * 100 = 300, not 350.
    filled_alpaca = _alpaca_order(
        broker_id="broker-cs-001",
        status="filled",
        filled_qty=1,
        filled_avg_price=-2.00,
        filled_at=_NOW,
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        reconcile(broker, conn, _clock=_NOW)

    with get_connection(engine) as conn:
        pos = get_position(conn, "pos-cs-001")
    assert pos is not None
    assert pos.status == PositionStatus.OPEN
    assert pos.est_max_loss == 300.0
    assert pos.est_max_profit == 200.0


def test_async_credit_fill_stores_signed_net_fill_price(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: Alpaca's mleg filled_avg_price is signed (negative =
    credit); a prior `fill_price > 0` guard silently dropped net_fill_price
    to None for every async credit fill.
    """
    broker = _broker(monkeypatch)
    _seed(
        engine,
        _credit_spread_pos(status=PositionStatus.PENDING_OPEN),
        _credit_spread_order(),
    )

    filled_alpaca = _alpaca_order(
        broker_id="broker-cs-001",
        status="filled",
        filled_qty=1,
        filled_avg_price=-1.50,
        filled_at=_NOW,
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        reconcile(broker, conn, _clock=_NOW)

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-cs-001")
    assert fetched is not None
    assert fetched.net_fill_price == -1.50


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


def test_cancellation_closes_pending_open_position(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a PENDING_OPEN position whose only order is CANCELLED with
    zero fill must not be left stranded — reconcile() must close it out so it
    stops counting as an "active" position (e.g. risk/validator.py's
    duplicate/conflict check treats PENDING_OPEN as active).
    """
    broker = _broker(monkeypatch)
    _seed(engine, _pos(status=PositionStatus.PENDING_OPEN), _order())

    cancelled_alpaca = _alpaca_order(
        status="canceled", filled_qty=0, filled_avg_price=None, filled_at=None
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=cancelled_alpaca)

    with get_connection(engine) as conn:
        reconcile(broker, conn, _clock=_NOW)

    with get_connection(engine) as conn:
        pos = get_position(conn, "pos-001")
        open_positions = list_open_positions(conn)

    assert pos is not None
    assert pos.status == PositionStatus.CLOSED
    assert pos.closed_at is not None
    assert pos.realized_pnl == 0.0
    assert pos.id not in [p.id for p in open_positions]


def test_cancellation_after_partial_fill_leaves_position_alone(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial fill followed by cancellation of the remainder represents
    real, unclosed exposure — the position must NOT be closed out as if
    nothing filled.
    """
    broker = _broker(monkeypatch)
    _seed(engine, _pos(status=PositionStatus.PENDING_OPEN), _order())

    cancelled_alpaca = _alpaca_order(
        status="canceled", filled_qty=2, filled_avg_price=1.25, filled_at=_NOW
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=cancelled_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn, _clock=_NOW)

    assert len(diff.newly_cancelled) == 1
    assert diff.newly_cancelled[0].filled_qty == 2

    with get_connection(engine) as conn:
        pos = get_position(conn, "pos-001")
    assert pos is not None
    assert pos.status == PositionStatus.PENDING_OPEN


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


def test_rejection_closes_pending_open_position(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(status=PositionStatus.PENDING_OPEN), _order())

    rejected_alpaca = _alpaca_order(
        status="rejected", filled_qty=0, filled_avg_price=None, filled_at=None
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=rejected_alpaca)

    with get_connection(engine) as conn:
        reconcile(broker, conn, _clock=_NOW)

    with get_connection(engine) as conn:
        pos = get_position(conn, "pos-001")
    assert pos is not None
    assert pos.status == PositionStatus.CLOSED


def test_cancellation_of_close_role_order_does_not_touch_position(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled CLOSE-role order (e.g. a failed exit attempt) must not
    trigger the PENDING_OPEN cleanup path — that guard is role==OPEN only.
    The position (still OPEN, mid-exit-attempt) must be left untouched.
    """
    broker = _broker(monkeypatch)
    pos = _pos(status=PositionStatus.PENDING_CLOSE)
    ord_ = _order(role=OrderRole.CLOSE)
    _seed(engine, pos, ord_)

    cancelled_alpaca = _alpaca_order(
        status="canceled", filled_qty=0, filled_avg_price=None, filled_at=None
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=cancelled_alpaca)

    with get_connection(engine) as conn:
        reconcile(broker, conn, _clock=_NOW)

    with get_connection(engine) as conn:
        pos_after = get_position(conn, "pos-001")
    assert pos_after is not None
    assert pos_after.status == PositionStatus.PENDING_CLOSE


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expiry_detected(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _pos(), _order())

    expired_alpaca = _alpaca_order(
        status="expired", filled_qty=0, filled_avg_price=None, filled_at=None
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=expired_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.newly_expired) == 1
    assert diff.newly_expired[0].status == OrderStatus.EXPIRED
    assert len(diff.newly_filled) == 0

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-001")
    assert fetched is not None
    assert fetched.status == OrderStatus.EXPIRED


def test_expiry_closes_pending_open_position(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a PENDING_OPEN position whose only order expires worthless
    with zero fill must not be left stranded — reconcile() must close it out
    so it stops counting as an "active" position (risk/validator.py's
    duplicate/conflict check treats PENDING_OPEN as active).
    """
    broker = _broker(monkeypatch)
    _seed(engine, _pos(status=PositionStatus.PENDING_OPEN), _order())

    expired_alpaca = _alpaca_order(
        status="expired", filled_qty=0, filled_avg_price=None, filled_at=None
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=expired_alpaca)

    with get_connection(engine) as conn:
        reconcile(broker, conn, _clock=_NOW)

    with get_connection(engine) as conn:
        pos = get_position(conn, "pos-001")
        open_positions = list_open_positions(conn)

    assert pos is not None
    assert pos.status == PositionStatus.CLOSED
    assert pos.closed_at is not None
    assert pos.realized_pnl == 0.0
    assert pos.id not in [p.id for p in open_positions]


def test_expiry_after_partial_fill_leaves_position_alone(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial fill followed by expiration of the remainder represents
    real, unclosed exposure — the position must NOT be closed out as if
    nothing filled.
    """
    broker = _broker(monkeypatch)
    _seed(engine, _pos(status=PositionStatus.PENDING_OPEN), _order())

    expired_alpaca = _alpaca_order(
        status="expired", filled_qty=2, filled_avg_price=1.25, filled_at=_NOW
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=expired_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn, _clock=_NOW)

    assert len(diff.newly_expired) == 1
    assert diff.newly_expired[0].filled_qty == 2

    with get_connection(engine) as conn:
        pos = get_position(conn, "pos-001")
    assert pos is not None
    assert pos.status == PositionStatus.PENDING_OPEN


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


# ---------------------------------------------------------------------------
# WP-1: per-leg fill audit trail — backfill for orders that reached the DB
# already at a terminal filled_qty (the synchronous-fill path). These orders
# are invisible to the main loop above because list_pending_orders() excludes
# terminal orders by design; reconcile() must still record their fills via
# _backfill_missing_fill_events().
# ---------------------------------------------------------------------------

_LEG_SELL_PUT = Leg(
    right="put", side="sell", strike=691.0, expiration=date(2026, 7, 24)
)
_LEG_BUY_PUT = Leg(right="put", side="buy", strike=687.0, expiration=date(2026, 7, 24))
_LEG_SELL_CALL = Leg(
    right="call", side="sell", strike=751.0, expiration=date(2026, 7, 24)
)
_LEG_BUY_CALL = Leg(
    right="call", side="buy", strike=755.0, expiration=date(2026, 7, 24)
)

_OCC_SELL_PUT = "QQQ260724P00691000"
_OCC_BUY_PUT = "QQQ260724P00687000"
_OCC_SELL_CALL = "QQQ260724C00751000"
_OCC_BUY_CALL = "QQQ260724C00755000"


def _multi_leg_pos(
    pos_id: str = "pos-ic", status: PositionStatus = PositionStatus.OPEN
) -> Position:
    return Position(
        id=pos_id,
        underlying="QQQ",
        strategy="iron_condor",
        legs=[
            PositionLeg(
                leg=_LEG_SELL_PUT,
                filled_qty=6,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
            PositionLeg(
                leg=_LEG_BUY_PUT,
                filled_qty=6,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
            PositionLeg(
                leg=_LEG_SELL_CALL,
                filled_qty=6,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
            PositionLeg(
                leg=_LEG_BUY_CALL,
                filled_qty=6,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
        ],
        quantity=6,
        entry_net_amount=-2.09,
        current_mark=-2.09,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=status,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=date(2026, 7, 24),
        est_max_loss=1000.0,
        est_max_profit=800.0,
        opening_order_id="ord-ic",
    )


def _multi_leg_order(
    order_id: str = "ord-ic",
    broker_order_id: str = "broker-ic",
    status: OrderStatus = OrderStatus.FILLED,
    filled_qty: int = 6,
    net_fill_price: float | None = -2.09,
    position_id: str = "pos-ic",
) -> Order:
    return Order(
        id=order_id,
        broker_order_id=broker_order_id,
        position_id=position_id,
        role=OrderRole.OPEN,
        status=status,
        broker_status_raw="filled",
        submitted_at=_NOW,
        filled_at=_NOW,
        limit_price=-2.10,
        legs_filled=[],
        net_fill_price=net_fill_price,
        filled_qty=filled_qty,
    )


def _mock_leg(symbol: str, filled_qty: int, filled_avg_price: float) -> MagicMock:
    leg = MagicMock()
    leg.symbol = symbol
    leg.filled_qty = filled_qty
    leg.filled_avg_price = filled_avg_price
    return leg


def _multi_leg_alpaca_order(
    broker_id: str = "broker-ic",
    status: str = "filled",
    filled_qty: int = 6,
    filled_avg_price: float | None = -2.09,
    filled_at: datetime | None = None,
    leg_prices: tuple[float, float, float, float] = (13.09, 12.20, 9.84, 8.64),
) -> MagicMock:
    """Build a mock multi-leg AlpacaOrder with real, distinct per-leg fill data.

    leg_prices default matches an empirically-observed QQQ iron condor fill
    against Alpaca paper (verified 2026-07-19): signed sum
    -13.09+12.20-9.84+8.64 == -2.09, matching the combo net_fill_price.
    """
    o = MagicMock()
    o.id = broker_id
    o.status.value = status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.symbol = None
    o.submitted_at = _NOW
    o.filled_at = filled_at
    o.legs = [
        _mock_leg(_OCC_SELL_PUT, filled_qty, leg_prices[0]),
        _mock_leg(_OCC_BUY_PUT, filled_qty, leg_prices[1]),
        _mock_leg(_OCC_SELL_CALL, filled_qty, leg_prices[2]),
        _mock_leg(_OCC_BUY_CALL, filled_qty, leg_prices[3]),
    ]
    return o


def test_synchronous_single_leg_fill_is_backfilled(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the WP-1 bug: an order that fills inside
    broker.submit()'s synchronous poll window is inserted directly at
    status=FILLED, filled_qty=N (see orchestrator.py's PENDING_OPEN->OPEN
    comment). list_pending_orders() excludes it from the main loop above, so
    a fix confined to that loop alone would still silently produce zero
    fill_events — the failure mode this ticket was filed against.
    """
    broker = _broker(monkeypatch)
    already_filled = _order(status=OrderStatus.FILLED, filled_qty=5)
    _seed(engine, _pos(), already_filled)

    filled_alpaca = _alpaca_order(
        status="filled", filled_qty=5, filled_avg_price=1.25, filled_at=_NOW
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    # Not a "newly filled" transition — it was already terminal when seeded.
    assert diff.newly_filled == []

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-001")
    assert len(events) == 1
    assert events[0].filled_qty == 5
    assert events[0].fill_price == 1.25
    assert events[0].leg_symbol == _OCC


def test_synchronous_fill_backfill_is_idempotent(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    already_filled = _order(status=OrderStatus.FILLED, filled_qty=5)
    _seed(engine, _pos(), already_filled)

    filled_alpaca = _alpaca_order(
        status="filled", filled_qty=5, filled_avg_price=1.25, filled_at=_NOW
    )
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=filled_alpaca)

    with get_connection(engine) as conn:
        reconcile(broker, conn)
    with get_connection(engine) as conn:
        reconcile(broker, conn)

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-001")
    assert len(events) == 1
    # Second pass should not need to re-fetch an order that's fully recorded.
    assert broker.get_broker_order.call_count == 1


def test_synchronous_multi_leg_fill_backfills_per_leg_fill_events(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _multi_leg_pos(), _multi_leg_order())

    alp = _multi_leg_alpaca_order()
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=alp)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert diff.anomalies == []

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-ic")
    assert len(events) == 4
    by_symbol = {e.leg_symbol: e for e in events}
    assert by_symbol[_OCC_SELL_PUT].fill_price == 13.09
    assert by_symbol[_OCC_BUY_PUT].fill_price == 12.20
    assert by_symbol[_OCC_SELL_CALL].fill_price == 9.84
    assert by_symbol[_OCC_BUY_CALL].fill_price == 8.64
    assert all(e.filled_qty == 6 for e in events)

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-ic")
    assert fetched is not None
    assert len(fetched.legs_filled) == 4
    prices_by_strike = {lf.leg.strike: lf.fill_price for lf in fetched.legs_filled}
    assert prices_by_strike[691.0] == 13.09
    assert prices_by_strike[687.0] == 12.20
    assert prices_by_strike[751.0] == 9.84
    assert prices_by_strike[755.0] == 8.64


def test_multi_leg_fill_sum_matches_net_price_no_anomaly(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _multi_leg_pos(), _multi_leg_order())

    # -13.09 (sell) + 12.20 (buy) - 9.84 (sell) + 8.64 (buy) == -2.09
    alp = _multi_leg_alpaca_order(filled_avg_price=-2.09)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=alp)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert diff.anomalies == []


def test_multi_leg_fill_sum_mismatch_produces_anomaly(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _multi_leg_pos(), _multi_leg_order(net_fill_price=-9.00))

    # Combo net_fill_price disagrees with what the legs actually sum to.
    alp = _multi_leg_alpaca_order(filled_avg_price=-9.00)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=alp)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.anomalies) == 1
    assert "Leg-fill sum mismatch" in diff.anomalies[0].description
    assert diff.anomalies[0].order_id == "ord-ic"


def test_multi_leg_backfill_idempotent_no_duplicate_events(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    _seed(engine, _multi_leg_pos(), _multi_leg_order())

    alp = _multi_leg_alpaca_order()
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=alp)

    with get_connection(engine) as conn:
        reconcile(broker, conn)
    with get_connection(engine) as conn:
        reconcile(broker, conn)

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-ic")
    assert len(events) == 4


def test_multi_leg_mixed_old_and_new_legs_in_one_pass(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two of four legs already have recorded FillEvents (e.g. observed on an
    earlier reconcile pass while the order was still non-terminal); reconcile()
    must record only the two new legs — not duplicate the existing two — and
    still end up with a fully-populated legs_filled across all four.
    """
    broker = _broker(monkeypatch)
    # Order still non-terminal in the DB so it's visible via list_pending_orders
    # (the main loop's per-leg gating is what's under test here, not backfill).
    _seed(
        engine,
        _multi_leg_pos(),
        _multi_leg_order(status=OrderStatus.WORKING, net_fill_price=None),
    )

    with get_connection(engine) as conn:
        insert_fill_event_if_new(
            conn,
            FillEvent(
                id="fe-preexisting-1",
                order_id="ord-ic",
                broker_exec_id=f"broker-ic:{_OCC_SELL_PUT}@6",
                leg_symbol=_OCC_SELL_PUT,
                filled_qty=6,
                fill_price=13.09,
                occurred_at=_NOW,
                observed_at=_NOW,
            ),
        )
        insert_fill_event_if_new(
            conn,
            FillEvent(
                id="fe-preexisting-2",
                order_id="ord-ic",
                broker_exec_id=f"broker-ic:{_OCC_BUY_PUT}@6",
                leg_symbol=_OCC_BUY_PUT,
                filled_qty=6,
                fill_price=12.20,
                occurred_at=_NOW,
                observed_at=_NOW,
            ),
        )

    alp = _multi_leg_alpaca_order(filled_at=_NOW)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_broker_order = MagicMock(return_value=alp)

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert diff.anomalies == []

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-ic")
    assert len(events) == 4
    by_symbol = {e.leg_symbol: e for e in events}
    # Pre-existing rows are untouched, not duplicated.
    assert by_symbol[_OCC_SELL_PUT].id == "fe-preexisting-1"
    assert by_symbol[_OCC_BUY_PUT].id == "fe-preexisting-2"
    # The two previously-unrecorded legs are newly written.
    assert by_symbol[_OCC_SELL_CALL].fill_price == 9.84
    assert by_symbol[_OCC_BUY_CALL].fill_price == 8.64

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-ic")
    assert fetched is not None
    assert len(fetched.legs_filled) == 4


def test_record_fill_events_with_no_position_still_writes_fill_events(
    engine,
) -> None:
    """If the position lookup misses (e.g. a data inconsistency), fill_events
    must still be recorded per leg — the audit trail should not depend on the
    position record being resolvable. legs_filled cannot be rebuilt without a
    position to pair legs against, so it is left alone (no patch, no crash).
    """
    _seed(engine, _multi_leg_pos(), _multi_leg_order())

    alp = _multi_leg_alpaca_order()

    with get_connection(engine) as conn:
        local_order = get_order(conn, "ord-ic")
        assert local_order is not None
        legs_filled, anomaly = _record_fill_events(
            conn, local_order, alp, None, "broker-ic", _NOW
        )

    assert anomaly is None
    assert legs_filled is None

    with get_connection(engine) as conn:
        events = list_fill_events_for_order(conn, "ord-ic")
    assert len(events) == 4
    assert {e.leg_symbol for e in events} == {
        _OCC_SELL_PUT,
        _OCC_BUY_PUT,
        _OCC_SELL_CALL,
        _OCC_BUY_CALL,
    }
