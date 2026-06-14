"""Tests for WP-1.5: expiry and assignment detection in execution/reconcile.py.

All broker calls are mocked.  The DB layer uses the shared in-memory SQLite
engine fixture from conftest.py so we exercise real SQL round-trips.

Detection paths covered
-----------------------
Expiry (activity feed):   OPEXP activity → position marked EXPIRED
Expiry (backstop):        absence + past nearest_expiration → EXPIRED
Assignment (activity):    OPASN activity → option ASSIGNED, equity pos created
Idempotency:              second reconcile pass produces no new events
Activity feed failure:    backstop still fires; anomaly recorded
Scope guard:              EQUITY positions are never expiry candidates
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from options_agent.contracts.proposal import Leg
from options_agent.contracts.state import (
    EQUITY_NEVER_EXPIRES,
    AssetClass,
    FillEvent,
    LegStatus,
    Order,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.execution.reconcile import reconcile
from options_agent.state.crud import (
    get_position,
    insert_fill_event_if_new,
    insert_order,
    insert_position,
)
from options_agent.state.db import get_connection

# Re-use helper types from the fill-detection test module to keep fixtures DRY.
from options_agent.tests.test_reconcile import (
    _EXIT_PLAN,
    _NOW,
    _OCC,
    _broker,
    _order,
)

# ---------------------------------------------------------------------------
# Additional shared helpers
# ---------------------------------------------------------------------------

# A date in the past so absence-backstop candidates are visible
_PAST_EXPIRY = date(2026, 6, 10)  # 3 days before _NOW (2026-06-13)
_FAR_EXPIRY = date(2026, 7, 18)


def _pos_expired_candidate(
    pos_id: str = "pos-exp",
    expiry: date = _PAST_EXPIRY,
) -> Position:
    """OPEN position with nearest_expiration in the past (backstop candidate)."""
    return Position(
        id=pos_id,
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=Leg(right="put", side="sell", strike=450.0, expiration=expiry),
                filled_qty=5,
                avg_fill_price=1.25,
                status=LegStatus.OPEN,
            )
        ],
        quantity=5,
        entry_net_amount=-312.50,
        current_mark=-312.50,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=PositionStatus.OPEN,
        opened_at=_NOW - timedelta(days=30),
        closed_at=None,
        nearest_expiration=expiry,
        est_max_loss=500.0,
        est_max_profit=250.0,
        opening_order_id="ord-exp",
    )


def _fill_event_for(
    order_id: str,
    leg_symbol: str = _OCC,
    fe_id: str = "fe-001",
) -> FillEvent:
    return FillEvent(
        id=fe_id,
        order_id=order_id,
        broker_exec_id=f"{order_id}@5",
        leg_symbol=leg_symbol,
        filled_qty=5,
        fill_price=1.25,
        occurred_at=_NOW - timedelta(days=30),
        observed_at=_NOW - timedelta(days=30),
    )


def _seed_with_fill(engine, pos: Position, ord_: Order, occ: str = _OCC) -> None:
    """Insert position, order, and a fill event so OCC→position index is populated."""
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, ord_)
        insert_fill_event_if_new(conn, _fill_event_for(ord_.id, occ))


def _alpaca_position(symbol: str) -> MagicMock:
    ap = MagicMock()
    ap.symbol = symbol
    return ap


# ---------------------------------------------------------------------------
# Expiry — activity feed (primary path)
# ---------------------------------------------------------------------------


def test_opexp_activity_marks_position_expired(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        return_value=[
            {
                "activity_type": "OPEXP",
                "symbol": _OCC,
                "date": "2026-06-10T21:00:00+00:00",
                "qty": "5",
                "price": "0",
            }
        ]
    )
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.expired_option_positions) == 1
    assert diff.expired_option_positions[0].id == "pos-exp"
    assert diff.expired_option_positions[0].status == PositionStatus.EXPIRED

    with get_connection(engine) as conn:
        fetched = get_position(conn, "pos-exp")
    assert fetched is not None
    assert fetched.status == PositionStatus.EXPIRED
    assert fetched.closed_at is not None


def test_opexp_activity_sets_closed_at_from_event_date(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    event_time = datetime(2026, 6, 10, 21, 0, 0, tzinfo=UTC)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        return_value=[
            {
                "activity_type": "OPEXP",
                "symbol": _OCC,
                "date": "2026-06-10T21:00:00+00:00",
                "qty": "5",
                "price": "0",
            },
        ]
    )
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        reconcile(broker, conn)

    with get_connection(engine) as conn:
        pos_after = get_position(conn, "pos-exp")
    assert pos_after is not None
    assert pos_after.closed_at == event_time


def test_unknown_occ_in_activity_is_ignored(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        return_value=[
            {
                "activity_type": "OPEXP",
                "symbol": "AAPL999999P00100000",
                "date": "2026-06-10T21:00:00+00:00",
            },
        ]
    )
    # Our leg is still live at the broker — the backstop must not fire.
    broker.get_all_positions = MagicMock(return_value=[_alpaca_position(_OCC)])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.expired_option_positions) == 0

    with get_connection(engine) as conn:
        pos_after = get_position(conn, "pos-exp")
    assert pos_after is not None
    assert pos_after.status == PositionStatus.OPEN


# ---------------------------------------------------------------------------
# Expiry — absence backstop
# ---------------------------------------------------------------------------


def test_absence_backstop_marks_expired_when_all_legs_gone(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()  # nearest_expiration = 2026-06-10, _NOW = 2026-06-13
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    # Activity feed returns nothing — backstop must fire
    broker.get_account_activities = MagicMock(return_value=[])
    # The option leg is absent from live positions
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.expired_option_positions) == 1
    assert diff.expired_option_positions[0].status == PositionStatus.EXPIRED

    with get_connection(engine) as conn:
        fetched = get_position(conn, "pos-exp")
    assert fetched is not None
    assert fetched.status == PositionStatus.EXPIRED


def test_absence_backstop_does_not_mark_when_leg_still_live(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])
    # The OCC leg is STILL live at the broker
    broker.get_all_positions = MagicMock(return_value=[_alpaca_position(_OCC)])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.expired_option_positions) == 0

    with get_connection(engine) as conn:
        fetched = get_position(conn, "pos-exp")
    assert fetched is not None
    assert fetched.status == PositionStatus.OPEN


def test_absence_backstop_grace_period_not_yet_passed(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position that expired TODAY should not be caught by the backstop yet."""
    broker = _broker(monkeypatch)
    # nearest_expiration = today; cutoff = today - EXPIRY_GRACE_DAYS (yesterday)
    # → position is not past the grace window yet
    today = _NOW.date()  # 2026-06-13
    pos = _pos_expired_candidate(expiry=today)
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])
    broker.get_all_positions = MagicMock(return_value=[])

    # Pin clock to _NOW so the grace-period boundary is deterministic.
    with get_connection(engine) as conn:
        diff = reconcile(broker, conn, _clock=_NOW)

    # Grace period not elapsed → should NOT be expired by backstop
    assert len(diff.expired_option_positions) == 0


# ---------------------------------------------------------------------------
# Assignment — activity feed
# ---------------------------------------------------------------------------


def test_opasn_activity_marks_option_assigned_and_creates_equity_position(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        return_value=[
            {
                "activity_type": "OPASN",
                "symbol": _OCC,
                "date": "2026-06-10T21:00:00+00:00",
                "qty": "5",
                "price": "450.00",
            }
        ]
    )
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    # Option position is ASSIGNED
    assert len(diff.assigned_positions) == 1
    event = diff.assigned_positions[0]
    assert event.closed_option_position_id == "pos-exp"
    assert event.assigned_qty == 5
    assert event.assignment_price == 450.0

    with get_connection(engine) as conn:
        option_pos = get_position(conn, "pos-exp")
    assert option_pos is not None
    assert option_pos.status == PositionStatus.ASSIGNED
    assert option_pos.closed_at is not None

    # Equity position was created
    equity_pos = event.created_equity_position
    assert equity_pos is not None
    assert equity_pos.asset_class == AssetClass.EQUITY
    assert equity_pos.assigned_from_position_id == "pos-exp"
    assert equity_pos.underlying == "SPY"
    assert equity_pos.status == PositionStatus.OPEN
    assert equity_pos.exit_plan is None
    assert equity_pos.nearest_expiration == EQUITY_NEVER_EXPIRES
    assert len(equity_pos.equity_legs) == 1
    assert equity_pos.equity_legs[0].qty == 500  # 5 contracts × 100
    assert equity_pos.equity_legs[0].avg_price == 450.0

    # Equity position persisted in DB
    with get_connection(engine) as conn:
        db_equity = get_position(conn, equity_pos.id)
    assert db_equity is not None
    assert db_equity.asset_class == AssetClass.EQUITY
    assert db_equity.assigned_from_position_id == "pos-exp"


def test_assignment_position_round_trips_through_db(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AssignmentEvent.created_equity_position must survive a DB round-trip."""
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        return_value=[
            {
                "activity_type": "OPASN",
                "symbol": _OCC,
                "date": "2026-06-10T21:00:00+00:00",
                "qty": "2",
                "price": "450.00",
            },
        ]
    )
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    eq = diff.assigned_positions[0].created_equity_position
    assert eq is not None

    with get_connection(engine) as conn:
        reloaded = get_position(conn, eq.id)
    assert reloaded is not None
    assert reloaded.equity_legs[0].qty == 200  # 2 × 100
    assert reloaded.equity_legs[0].avg_price == 450.0
    assert reloaded.assigned_from_position_id == "pos-exp"
    assert reloaded.exit_plan is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_expiry_idempotent_second_pass_produces_no_events(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        return_value=[
            {
                "activity_type": "OPEXP",
                "symbol": _OCC,
                "date": "2026-06-10T21:00:00+00:00",
            },
        ]
    )
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff1 = reconcile(broker, conn)
    with get_connection(engine) as conn:
        diff2 = reconcile(broker, conn)

    assert len(diff1.expired_option_positions) == 1
    # Second pass: position is already EXPIRED → not OPEN → not a candidate
    assert len(diff2.expired_option_positions) == 0
    assert len(diff2.assigned_positions) == 0


def test_assignment_idempotent_second_pass_produces_no_events(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        return_value=[
            {
                "activity_type": "OPASN",
                "symbol": _OCC,
                "date": "2026-06-10T21:00:00+00:00",
                "qty": "5",
                "price": "450",
            },
        ]
    )
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff1 = reconcile(broker, conn)
    with get_connection(engine) as conn:
        diff2 = reconcile(broker, conn)

    assert len(diff1.assigned_positions) == 1
    assert len(diff2.assigned_positions) == 0
    assert len(diff2.expired_option_positions) == 0


# ---------------------------------------------------------------------------
# Activity feed failure — backstop fires; anomaly recorded
# ---------------------------------------------------------------------------


def test_activity_feed_failure_still_runs_backstop(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        side_effect=RuntimeError("network timeout")
    )
    broker.get_all_positions = MagicMock(return_value=[])  # all legs absent

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    # Backstop should have fired
    assert len(diff.expired_option_positions) == 1
    assert diff.expired_option_positions[0].status == PositionStatus.EXPIRED

    # Anomaly should be recorded for the activity feed failure
    activity_anomalies = [a for a in diff.anomalies if "Activity feed" in a.description]
    assert len(activity_anomalies) == 1


def test_both_backstop_and_absence_fail_records_anomalies(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(side_effect=RuntimeError("feed error"))
    broker.get_all_positions = MagicMock(side_effect=RuntimeError("positions error"))

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    # No positions changed, but two anomalies (feed + backstop)
    anomaly_descs = [a.description for a in diff.anomalies]
    assert any("Activity feed" in d for d in anomaly_descs)
    assert any(
        "Absence backstop" in d or "backstop" in d.lower() for d in anomaly_descs
    )


# ---------------------------------------------------------------------------
# Scope guard: EQUITY positions are never expiry candidates
# ---------------------------------------------------------------------------


def test_equity_position_not_processed_as_expiry_candidate(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EQUITY position with EQUITY_NEVER_EXPIRES should never be returned
    by list_open_option_positions_expiring_on_or_before regardless of date."""
    broker = _broker(monkeypatch)

    # Insert an EQUITY position directly (as would be created by assignment)
    equity_pos = Position(
        id="pos-equity",
        underlying="SPY",
        strategy="assigned_equity",
        legs=[],
        equity_legs=[],
        asset_class=AssetClass.EQUITY,
        assigned_from_position_id="pos-exp",
        quantity=5,
        entry_net_amount=225000.0,
        current_mark=225000.0,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=None,
        status=PositionStatus.OPEN,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=EQUITY_NEVER_EXPIRES,
        est_max_loss=0.0,
        est_max_profit=0.0,
        opening_order_id="asn:pos-exp",
    )
    with get_connection(engine) as conn:
        insert_position(conn, equity_pos)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    assert len(diff.expired_option_positions) == 0
    assert len(diff.assigned_positions) == 0

    with get_connection(engine) as conn:
        fetched = get_position(conn, "pos-equity")
    assert fetched is not None
    assert fetched.status == PositionStatus.OPEN  # untouched


# ---------------------------------------------------------------------------
# StateDiff completeness for new fields
# ---------------------------------------------------------------------------


def test_statediff_new_fields_serialise_cleanly(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(monkeypatch)
    pos = _pos_expired_candidate()
    ord_ = _order(
        order_id="ord-exp",
        status=OrderStatus.FILLED,
        filled_qty=5,
        position_id="pos-exp",
    )
    _seed_with_fill(engine, pos, ord_)

    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(
        return_value=[
            {
                "activity_type": "OPASN",
                "symbol": _OCC,
                "date": "2026-06-10T21:00:00+00:00",
                "qty": "5",
                "price": "450",
            },
        ]
    )
    broker.get_all_positions = MagicMock(return_value=[])

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

    serialised = diff.model_dump(mode="json")
    assert "expired_option_positions" in serialised
    assert "assigned_positions" in serialised
    assert len(serialised["assigned_positions"]) == 1
    event = serialised["assigned_positions"][0]
    assert event["assignment_price"] == 450.0
    assert event["created_equity_position"] is not None
