"""Tests for WP-2.2: Position and Order CRUD primitives."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from options_agent.contracts.state import (
    ExitPlan,
    FillEvent,
    Leg,
    LegFill,
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.state.crud import (
    get_order,
    get_position,
    has_pending_close,
    insert_fill_event_if_new,
    insert_order,
    insert_position,
    list_open_positions,
    list_orders_with_unrecorded_fills,
    list_pending_orders,
    patch_order,
    update_position,
)
from options_agent.state.db import get_connection

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_LEG = Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 7, 18))
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)
_NOW = datetime(2026, 6, 7, 14, 30, tzinfo=UTC)


def _pos(**overrides: object) -> Position:
    defaults: dict = {
        "id": "pos-001",
        "underlying": "SPY",
        "strategy": "bull_put_spread",
        "legs": [
            PositionLeg(
                leg=_LEG, filled_qty=5, avg_fill_price=1.25, status=LegStatus.OPEN
            )
        ],
        "quantity": 5,
        "entry_net_amount": -312.50,
        "current_mark": -200.00,
        "marked_at": _NOW,
        "unrealized_pnl": 112.50,
        "realized_pnl": None,
        "exit_plan": _EXIT_PLAN,
        "status": PositionStatus.OPEN,
        "opened_at": _NOW,
        "closed_at": None,
        "nearest_expiration": date(2026, 7, 18),
        "est_max_loss": 2187.50,
        "est_max_profit": 312.50,
        "opening_order_id": "ord-001",
    }
    defaults.update(overrides)
    return Position(**defaults)


def _order(**overrides: object) -> Order:
    defaults: dict = {
        "id": "ord-001",
        "broker_order_id": "",
        "position_id": "pos-001",
        "role": OrderRole.OPEN,
        "status": OrderStatus.PENDING_SUBMIT,
        "broker_status_raw": "",
        "submitted_at": _NOW,
        "filled_at": None,
        "legs_filled": [],
        "net_fill_price": None,
        "filled_qty": 0,
    }
    defaults.update(overrides)
    return Order(**defaults)


# ---------------------------------------------------------------------------
# Position — insert + get
# ---------------------------------------------------------------------------


def test_insert_get_position_round_trips(engine):
    pos = _pos()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    with get_connection(engine) as conn:
        fetched = get_position(conn, "pos-001")
    assert fetched == pos


def test_get_position_missing_returns_none(engine):
    with get_connection(engine) as conn:
        assert get_position(conn, "nonexistent") is None


def test_position_all_fields_survive_round_trip(engine):
    """Every WP-0 Position field must come back unchanged after DB round-trip."""
    pos = _pos(
        realized_pnl=50.0,
        status=PositionStatus.PENDING_CLOSE,
        closed_at=_NOW,
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    with get_connection(engine) as conn:
        fetched = get_position(conn, pos.id)
    assert fetched == pos


def test_position_credit_sign_survives_round_trip(engine):
    pos = _pos(entry_net_amount=-500.0)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    with get_connection(engine) as conn:
        fetched = get_position(conn, pos.id)
    assert fetched is not None
    assert fetched.entry_net_amount == -500.0


# ---------------------------------------------------------------------------
# Position — update
# ---------------------------------------------------------------------------


def test_update_position_status_transition(engine):
    pos = _pos()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    closed = _pos(
        status=PositionStatus.CLOSED,
        closed_at=_NOW,
        realized_pnl=150.0,
    )
    with get_connection(engine) as conn:
        update_position(conn, closed)
    with get_connection(engine) as conn:
        fetched = get_position(conn, pos.id)
    assert fetched is not None
    assert fetched.status == PositionStatus.CLOSED
    assert fetched.realized_pnl == 150.0
    assert fetched.closed_at == _NOW


def test_update_position_mark_snapshot(engine):
    pos = _pos()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    updated = _pos(current_mark=-150.00, unrealized_pnl=162.50)
    with get_connection(engine) as conn:
        update_position(conn, updated)
    with get_connection(engine) as conn:
        fetched = get_position(conn, pos.id)
    assert fetched is not None
    assert fetched.current_mark == -150.00
    assert fetched.unrealized_pnl == 162.50


# ---------------------------------------------------------------------------
# Position — list_open_positions
# ---------------------------------------------------------------------------


def test_list_open_positions_excludes_terminal(engine):
    open_pos = _pos(id="pos-open", status=PositionStatus.OPEN)
    pending_open = _pos(id="pos-pending-open", status=PositionStatus.PENDING_OPEN)
    pending_close = _pos(id="pos-pending-close", status=PositionStatus.PENDING_CLOSE)
    closed_pos = _pos(
        id="pos-closed",
        status=PositionStatus.CLOSED,
        closed_at=_NOW,
        realized_pnl=0.0,
    )
    expired_pos = _pos(id="pos-expired", status=PositionStatus.EXPIRED)
    assigned_pos = _pos(id="pos-assigned", status=PositionStatus.ASSIGNED)

    with get_connection(engine) as conn:
        all_pos = [
            open_pos,
            pending_open,
            pending_close,
            closed_pos,
            expired_pos,
            assigned_pos,
        ]
        for p in all_pos:
            insert_position(conn, p)

    with get_connection(engine) as conn:
        result = list_open_positions(conn)

    ids = {p.id for p in result}
    assert ids == {"pos-open", "pos-pending-open", "pos-pending-close"}


def test_list_open_positions_empty(engine):
    with get_connection(engine) as conn:
        assert list_open_positions(conn) == []


# ---------------------------------------------------------------------------
# Order — insert + get (two-phase flow)
# ---------------------------------------------------------------------------


def test_insert_get_order_round_trips(engine):
    pos = _pos()
    ord_ = _order()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-001")
    assert fetched == ord_


def test_get_order_missing_returns_none(engine):
    with get_connection(engine) as conn:
        assert get_order(conn, "nonexistent") is None


def test_insert_order_pending_submit_breadcrumb(engine):
    """Two-phase invariant: PENDING_SUBMIT row exists before broker confirmation.

    This is the window that patch_order closes after broker responds.
    Reconcile (WP-1) relies on detecting this row during a crash window.
    """
    pos = _pos()
    pending = _order(
        id="ord-crash",
        broker_order_id="",
        status=OrderStatus.PENDING_SUBMIT,
        broker_status_raw="",
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, pending)

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-crash")
    assert fetched is not None
    assert fetched.status == OrderStatus.PENDING_SUBMIT
    assert fetched.broker_order_id == ""


# ---------------------------------------------------------------------------
# Order — patch_order
# ---------------------------------------------------------------------------


def test_patch_order_broker_id_and_status(engine):
    pos = _pos()
    ord_ = _order()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
    with get_connection(engine) as conn:
        patch_order(
            conn,
            "ord-001",
            broker_order_id="alpaca-abc-123",
            status=OrderStatus.WORKING,
            broker_status_raw="accepted",
        )
    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-001")
    assert fetched is not None
    assert fetched.broker_order_id == "alpaca-abc-123"
    assert fetched.status == OrderStatus.WORKING
    assert fetched.broker_status_raw == "accepted"


def test_patch_order_fill(engine):
    pos = _pos()
    ord_ = _order(id="ord-fill")
    fill = LegFill(leg=_LEG, filled_qty=5, fill_price=1.25)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
    with get_connection(engine) as conn:
        patch_order(
            conn,
            "ord-fill",
            status=OrderStatus.FILLED,
            broker_status_raw="filled",
            filled_at=_NOW,
            legs_filled=[fill],
            net_fill_price=-1.25,
            filled_qty=5,
        )
    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-fill")
    assert fetched is not None
    assert fetched.status == OrderStatus.FILLED
    assert fetched.filled_qty == 5
    assert fetched.net_fill_price == -1.25
    assert fetched.filled_at == _NOW
    assert len(fetched.legs_filled) == 1


def test_patch_order_idempotent_double_patch(engine):
    """Patching twice with the same data must produce a single effect.

    This is the idempotency guarantee required by WP-1 reconcile and
    WP-5 monitor, which may both observe the same broker update before
    local state settles.
    """
    pos = _pos()
    ord_ = _order(id="ord-idem")
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)

    # Call patch_order twice with identical fill data
    for _ in range(2):
        with get_connection(engine) as conn:
            patch_order(
                conn,
                "ord-idem",
                broker_order_id="alpaca-idem-456",
                status=OrderStatus.FILLED,
                broker_status_raw="filled",
                filled_qty=5,
                net_fill_price=-1.25,
            )

    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-idem")
    assert fetched is not None
    assert fetched.status == OrderStatus.FILLED
    assert fetched.filled_qty == 5
    assert fetched.broker_order_id == "alpaca-idem-456"


def test_patch_order_no_fields_is_noop(engine):
    pos = _pos()
    ord_ = _order(id="ord-noop")
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
    with get_connection(engine) as conn:
        patch_order(conn, "ord-noop")  # no kwargs → early return, no DB write
    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-noop")
    assert fetched is not None
    assert fetched.status == OrderStatus.PENDING_SUBMIT


# ---------------------------------------------------------------------------
# Order — list_pending_orders
# ---------------------------------------------------------------------------


def test_list_pending_orders_excludes_terminal(engine):
    pos = _pos()
    pending = _order(id="ord-pending", status=OrderStatus.PENDING_SUBMIT)
    working = _order(id="ord-working", status=OrderStatus.WORKING, broker_order_id="b1")
    partial = _order(
        id="ord-partial", status=OrderStatus.PARTIALLY_FILLED, broker_order_id="b2"
    )
    filled = _order(id="ord-filled", status=OrderStatus.FILLED, broker_order_id="b3")
    cancelled = _order(
        id="ord-cancelled", status=OrderStatus.CANCELLED, broker_order_id="b4"
    )
    rejected = _order(
        id="ord-rejected", status=OrderStatus.REJECTED, broker_order_id="b5"
    )
    expired = _order(id="ord-expired", status=OrderStatus.EXPIRED, broker_order_id="b6")

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        for o in [pending, working, partial, filled, cancelled, rejected, expired]:
            insert_order(conn, o)

    with get_connection(engine) as conn:
        result = list_pending_orders(conn)

    ids = {o.id for o in result}
    assert ids == {"ord-pending", "ord-working", "ord-partial"}


def test_list_pending_orders_empty(engine):
    with get_connection(engine) as conn:
        assert list_pending_orders(conn) == []


# ---------------------------------------------------------------------------
# Order — list_orders_with_unrecorded_fills (WP-1: per-leg fill audit trail)
# ---------------------------------------------------------------------------


def test_list_orders_with_unrecorded_fills_finds_terminal_order_with_no_events(
    engine,
):
    """An order inserted already FILLED (the synchronous-fill path) with zero
    fill_events rows must be surfaced — it is invisible to list_pending_orders
    since it's terminal, so this is the only query that can find it.
    """
    pos = _pos()
    filled = _order(
        id="ord-filled", status=OrderStatus.FILLED, broker_order_id="b1", filled_qty=5
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, filled)

    with get_connection(engine) as conn:
        result = list_orders_with_unrecorded_fills(conn)

    assert {o.id for o in result} == {"ord-filled"}


def test_list_orders_with_unrecorded_fills_excludes_fully_recorded_order(
    engine,
):
    pos = _pos()
    filled = _order(
        id="ord-filled", status=OrderStatus.FILLED, broker_order_id="b1", filled_qty=5
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, filled)
        insert_fill_event_if_new(
            conn,
            FillEvent(
                id="fe-1",
                order_id="ord-filled",
                broker_exec_id="b1@5",
                leg_symbol="SPY260718P00450000",
                filled_qty=5,
                fill_price=1.25,
                occurred_at=_NOW,
                observed_at=_NOW,
            ),
        )

    with get_connection(engine) as conn:
        result = list_orders_with_unrecorded_fills(conn)

    assert result == []


def test_list_orders_with_unrecorded_fills_excludes_unfilled_order(engine):
    pos = _pos()
    working = _order(id="ord-working", status=OrderStatus.WORKING, filled_qty=0)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, working)

    with get_connection(engine) as conn:
        result = list_orders_with_unrecorded_fills(conn)

    assert result == []


def test_list_orders_with_unrecorded_fills_finds_partially_recorded_order(
    engine,
):
    """An order with some, but not all, of its filled_qty already recorded in
    fill_events (e.g. an interrupted prior reconcile pass) must still be
    surfaced — the gap is what matters, not whether any rows exist at all.
    """
    pos = _pos()
    filled = _order(
        id="ord-001", status=OrderStatus.FILLED, broker_order_id="b1", filled_qty=6
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, filled)
        insert_fill_event_if_new(
            conn,
            FillEvent(
                id="fe-1",
                order_id="ord-001",
                broker_exec_id="b1@3",
                leg_symbol="SPY260718P00450000",
                filled_qty=3,
                fill_price=1.25,
                occurred_at=_NOW,
                observed_at=_NOW,
            ),
        )

    with get_connection(engine) as conn:
        result = list_orders_with_unrecorded_fills(conn)

    assert {o.id for o in result} == {"ord-001"}


# ---------------------------------------------------------------------------
# Order — all fields survive round trip
# ---------------------------------------------------------------------------


def test_order_all_fields_round_trip(engine):
    """Every WP-0 Order field must come back unchanged after DB round-trip."""
    pos = _pos()
    fill = LegFill(leg=_LEG, filled_qty=5, fill_price=1.25)
    ord_ = _order(
        id="ord-full",
        broker_order_id="alpaca-full-789",
        status=OrderStatus.FILLED,
        broker_status_raw="filled",
        filled_at=_NOW,
        legs_filled=[fill],
        net_fill_price=-1.25,
        filled_qty=5,
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-full")
    assert fetched == ord_


# ---------------------------------------------------------------------------
# Error paths — missing rows and constraint violations
# ---------------------------------------------------------------------------


def test_insert_position_duplicate_id_raises(engine):
    """insert_position must raise IntegrityError on duplicate id."""
    pos = _pos()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    with pytest.raises(IntegrityError):
        with get_connection(engine) as conn:
            insert_position(conn, pos)


def test_update_position_missing_id_raises(engine):
    """update_position must raise KeyError when the row does not exist."""
    pos = _pos(id="pos-ghost")
    with pytest.raises(KeyError, match="pos-ghost"):
        with get_connection(engine) as conn:
            update_position(conn, pos)


def test_patch_order_missing_id_raises(engine):
    """patch_order must raise KeyError when the order row does not exist."""
    with pytest.raises(KeyError, match="ord-ghost"):
        with get_connection(engine) as conn:
            patch_order(conn, "ord-ghost", status=OrderStatus.WORKING)


# ---------------------------------------------------------------------------
# has_pending_close — WP-5.4 idempotency guard (Order-table layer)
# ---------------------------------------------------------------------------


def test_has_pending_close_false_when_no_orders(engine) -> None:
    """No orders at all → has_pending_close returns False."""
    pos = _pos(id="pos-hpc-empty")
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        assert has_pending_close(conn, "pos-hpc-empty") is False


def test_has_pending_close_true_for_pending_submit_close(engine) -> None:
    """PENDING_SUBMIT CLOSE order → returns True."""
    pos = _pos(id="pos-hpc-pending")
    ord_ = _order(
        id="ord-hpc-pending",
        position_id="pos-hpc-pending",
        role=OrderRole.CLOSE,
        status=OrderStatus.PENDING_SUBMIT,
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
        assert has_pending_close(conn, "pos-hpc-pending") is True


def test_has_pending_close_true_for_working_close(engine) -> None:
    """WORKING CLOSE order → returns True."""
    pos = _pos(id="pos-hpc-working")
    ord_ = _order(
        id="ord-hpc-working",
        position_id="pos-hpc-working",
        role=OrderRole.CLOSE,
        status=OrderStatus.WORKING,
        broker_order_id="alpaca-w1",
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
        assert has_pending_close(conn, "pos-hpc-working") is True


def test_has_pending_close_true_for_partially_filled_close(engine) -> None:
    """PARTIALLY_FILLED CLOSE order → returns True.

    This is the primary case: partial fill means the position still shows open
    exposure, but a closing order is actively filling. Stacking a second close
    would double the closing quantity.
    """
    pos = _pos(id="pos-hpc-partial")
    ord_ = _order(
        id="ord-hpc-partial",
        position_id="pos-hpc-partial",
        role=OrderRole.CLOSE,
        status=OrderStatus.PARTIALLY_FILLED,
        broker_order_id="alpaca-p1",
        filled_qty=2,
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
        assert has_pending_close(conn, "pos-hpc-partial") is True


def test_has_pending_close_true_for_working_roll(engine) -> None:
    """WORKING ROLL order → returns True.

    A roll is mechanically closing the position; submitting a CLOSE on top of a
    working ROLL is a double-exit.
    """
    pos = _pos(id="pos-hpc-roll")
    ord_ = _order(
        id="ord-hpc-roll",
        position_id="pos-hpc-roll",
        role=OrderRole.ROLL,
        status=OrderStatus.WORKING,
        broker_order_id="alpaca-r1",
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
        assert has_pending_close(conn, "pos-hpc-roll") is True


def test_has_pending_close_false_for_filled_close(engine) -> None:
    """FILLED CLOSE order is terminal → returns False (position should be CLOSED)."""
    pos = _pos(id="pos-hpc-filled")
    ord_ = _order(
        id="ord-hpc-filled",
        position_id="pos-hpc-filled",
        role=OrderRole.CLOSE,
        status=OrderStatus.FILLED,
        broker_order_id="alpaca-f1",
        filled_qty=5,
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
        assert has_pending_close(conn, "pos-hpc-filled") is False


def test_has_pending_close_false_for_cancelled_close(engine) -> None:
    """CANCELLED CLOSE order is terminal → returns False."""
    pos = _pos(id="pos-hpc-cancelled")
    ord_ = _order(
        id="ord-hpc-cancelled",
        position_id="pos-hpc-cancelled",
        role=OrderRole.CLOSE,
        status=OrderStatus.CANCELLED,
        broker_order_id="alpaca-c1",
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
        assert has_pending_close(conn, "pos-hpc-cancelled") is False


def test_has_pending_close_false_for_open_order_different_role(engine) -> None:
    """WORKING OPEN order on the same position → returns False.

    Only CLOSE and ROLL roles indicate a pending exit; an OPEN order does not.
    """
    pos = _pos(id="pos-hpc-open-role")
    ord_ = _order(
        id="ord-hpc-open-role",
        position_id="pos-hpc-open-role",
        role=OrderRole.OPEN,
        status=OrderStatus.WORKING,
        broker_order_id="alpaca-o1",
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
        assert has_pending_close(conn, "pos-hpc-open-role") is False


def test_has_pending_close_false_for_different_position(engine) -> None:
    """WORKING CLOSE order on a different position → returns False for the target."""
    pos_a = _pos(id="pos-hpc-a")
    pos_b = _pos(id="pos-hpc-b")
    ord_ = _order(
        id="ord-hpc-other-pos",
        position_id="pos-hpc-a",
        role=OrderRole.CLOSE,
        status=OrderStatus.WORKING,
        broker_order_id="alpaca-x1",
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos_a)
        insert_position(conn, pos_b)
        insert_order(conn, ord_)
        # The CLOSE order is for pos-hpc-a, not pos-hpc-b
        assert has_pending_close(conn, "pos-hpc-b") is False
        assert has_pending_close(conn, "pos-hpc-a") is True
