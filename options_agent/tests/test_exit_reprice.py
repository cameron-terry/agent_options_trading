"""Tests for reprice_stale_close_orders (monitor step 3b)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

from options_agent.contracts.proposal import ExitPlan, Leg
from options_agent.contracts.state import (
    AssetClass,
    ExitReason,
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.monitor.exits import reprice_stale_close_orders
from options_agent.state.crud import (
    get_order,
    get_position,
    insert_order,
    insert_position,
)
from options_agent.state.db import get_connection

_NOW = datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC)
_STALE_AFTER = timedelta(minutes=10)

_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50,
    stop_loss_max_loss_fraction=0.5,
    time_stop_dte=21,
)


def _make_position(pos_id: str, status: PositionStatus) -> Position:
    legs = [
        PositionLeg(
            leg=Leg(
                right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15)
            ),
            filled_qty=1,
            avg_fill_price=2.75,
            status=LegStatus.OPEN,
        ),
        PositionLeg(
            leg=Leg(
                right="put", side="buy", strike=445.0, expiration=date(2026, 8, 15)
            ),
            filled_qty=1,
            avg_fill_price=1.20,
            status=LegStatus.OPEN,
        ),
    ]
    return Position(
        id=pos_id,
        underlying="SPY",
        strategy="bull_put_spread",
        legs=legs,
        quantity=1,
        entry_net_amount=-1.55,
        current_mark=-2.40,
        marked_at=_NOW,
        unrealized_pnl=-85.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=status,
        opened_at=_NOW - timedelta(days=5),
        closed_at=None,
        nearest_expiration=date(2026, 8, 15),
        est_max_loss=345.0,
        est_max_profit=155.0,
        opening_order_id="open-ord-001",
        asset_class=AssetClass.OPTION_STRATEGY,
    )


def _make_close_order(
    position_id: str,
    *,
    status: OrderStatus = OrderStatus.WORKING,
    submitted_at: datetime = _NOW - timedelta(minutes=20),
) -> Order:
    return Order(
        id=str(uuid.uuid4()),
        broker_order_id=str(uuid.uuid4()),
        position_id=position_id,
        role=OrderRole.CLOSE,
        status=status,
        broker_status_raw="new",
        submitted_at=submitted_at,
        filled_at=None,
        limit_price=2.41,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
        exit_reason=ExitReason.STOP_LOSS,
    )


def _fresh_working_order(position_id: str) -> Order:
    return Order(
        id=str(uuid.uuid4()),
        broker_order_id=str(uuid.uuid4()),
        position_id=position_id,
        role=OrderRole.CLOSE,
        status=OrderStatus.WORKING,
        broker_status_raw="new",
        submitted_at=_NOW,
        filled_at=None,
        limit_price=2.46,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )


def _run(
    conn,
    broker,
    *,
    stale_after: timedelta = _STALE_AFTER,
    offset_step: float = 0.05,
    max_widenings: int = 5,
):
    return reprice_stale_close_orders(
        conn,
        broker,
        _NOW,
        stale_after=stale_after,
        offset_step=offset_step,
        max_widenings=max_widenings,
    )


def test_fresh_order_left_alone(engine) -> None:
    pos = _make_position("pos-1", PositionStatus.PENDING_CLOSE)
    order = _make_close_order("pos-1", submitted_at=_NOW - timedelta(minutes=5))
    broker = MagicMock()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, order)
        repriced, race_filled = _run(conn, broker)
    assert repriced == []
    assert race_filled == []
    broker.cancel.assert_not_called()


def test_stale_order_cancelled_and_repriced(engine) -> None:
    pos = _make_position("pos-1", PositionStatus.PENDING_CLOSE)
    order = _make_close_order("pos-1")
    broker = MagicMock()
    broker.cancel.return_value = order.model_copy(
        update={"status": OrderStatus.CANCELLED, "broker_status_raw": "canceled"}
    )
    broker.submit_multi_leg.return_value = _fresh_working_order("pos-1")

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, order)
        repriced, race_filled = _run(conn, broker)

        assert len(repriced) == 1
        assert race_filled == []
        # exit_reason carried onto the replacement.
        assert repriced[0].exit_reason == ExitReason.STOP_LOSS
        # Old order patched to CANCELLED in the DB.
        old = get_order(conn, order.id)
        assert old is not None and old.status == OrderStatus.CANCELLED
        # Replacement persisted.
        new = get_order(conn, repriced[0].id)
        assert new is not None and new.status == OrderStatus.WORKING
        # Position remains PENDING_CLOSE (live replacement exists).
        p = get_position(conn, "pos-1")
        assert p is not None and p.status == PositionStatus.PENDING_CLOSE

    # Limit widened: one prior CLOSE order → offset = 0.01 + 1 × 0.05 = 0.06.
    # Credit position closing: limit = −current_mark + offset = 2.40 + 0.06.
    _, _, limit_price, _ = broker.submit_multi_leg.call_args.args
    assert limit_price == 2.46


def test_widening_capped_at_max(engine) -> None:
    pos = _make_position("pos-1", PositionStatus.PENDING_CLOSE)
    # Nine prior CLOSE orders already terminal, plus the stale one → 10 total.
    broker = MagicMock()
    stale = _make_close_order("pos-1")
    broker.cancel.return_value = stale.model_copy(
        update={"status": OrderStatus.CANCELLED, "broker_status_raw": "canceled"}
    )
    broker.submit_multi_leg.return_value = _fresh_working_order("pos-1")

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        for _ in range(9):
            insert_order(conn, _make_close_order("pos-1", status=OrderStatus.CANCELLED))
        insert_order(conn, stale)
        repriced, _ = _run(conn, broker, max_widenings=5)

    assert len(repriced) == 1
    # Offset capped: 0.01 + 5 × 0.05 = 0.26 → limit 2.40 + 0.26.
    _, _, limit_price, _ = broker.submit_multi_leg.call_args.args
    assert limit_price == 2.66


def test_fill_race_closes_position(engine) -> None:
    pos = _make_position("pos-1", PositionStatus.PENDING_CLOSE)
    order = _make_close_order("pos-1")
    broker = MagicMock()
    broker.cancel.return_value = order.model_copy(
        update={
            "status": OrderStatus.FILLED,
            "broker_status_raw": "filled",
            "filled_at": _NOW,
            "net_fill_price": 2.41,
            "filled_qty": 1,
        }
    )

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, order)
        repriced, race_filled = _run(conn, broker)

        assert repriced == []
        assert len(race_filled) == 1
        assert race_filled[0].status == PositionStatus.CLOSED
        p = get_position(conn, "pos-1")
        assert p is not None and p.status == PositionStatus.CLOSED
    broker.submit_multi_leg.assert_not_called()


def test_resubmit_failure_reverts_position_to_open(engine) -> None:
    pos = _make_position("pos-1", PositionStatus.PENDING_CLOSE)
    order = _make_close_order("pos-1")
    broker = MagicMock()
    broker.cancel.return_value = order.model_copy(
        update={"status": OrderStatus.CANCELLED, "broker_status_raw": "canceled"}
    )
    broker.submit_multi_leg.side_effect = RuntimeError("broker down")

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, order)
        repriced, race_filled = _run(conn, broker)

        assert repriced == []
        assert race_filled == []
        # Reverted to OPEN so the next cycle's exit evaluation re-triggers.
        p = get_position(conn, "pos-1")
        assert p is not None and p.status == PositionStatus.OPEN


def test_partially_filled_close_skipped(engine) -> None:
    pos = _make_position("pos-1", PositionStatus.PENDING_CLOSE)
    order = _make_close_order("pos-1", status=OrderStatus.PARTIALLY_FILLED)
    broker = MagicMock()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, order)
        repriced, race_filled = _run(conn, broker)
    assert repriced == []
    assert race_filled == []
    broker.cancel.assert_not_called()


def test_cancel_failure_skips_and_keeps_order(engine) -> None:
    pos = _make_position("pos-1", PositionStatus.PENDING_CLOSE)
    order = _make_close_order("pos-1")
    broker = MagicMock()
    broker.cancel.side_effect = RuntimeError("transport error")

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, order)
        repriced, race_filled = _run(conn, broker)

        assert repriced == []
        assert race_filled == []
        # Old order untouched — retried next cycle.
        old = get_order(conn, order.id)
        assert old is not None and old.status == OrderStatus.WORKING
