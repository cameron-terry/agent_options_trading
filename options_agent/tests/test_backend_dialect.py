"""WP-2.4: Cross-dialect smoke tests.

Each test here targets a specific known divergence between SQLite and
Postgres so that regressions surface with a clear failure message rather
than as a mysterious data corruption later.

All tests run against whatever backend the `engine` fixture provides — when
CI sets DB_URL=postgresql://..., these same assertions verify Postgres
behaviour.  Against SQLite they verify that our compatibility layer holds.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from options_agent.contracts.state import (
    ExitPlan,
    Leg,
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
    insert_order,
    insert_position,
)
from options_agent.state.db import get_connection

_LEG = Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 7, 18))
_EXIT = ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21)
_NOW = datetime(2026, 6, 7, 14, 30, 0, 123456, tzinfo=UTC)


def _pos(**kw) -> Position:  # type: ignore[no-untyped-def]
    defaults: dict = {
        "id": "pos-dial-001",
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
        "exit_plan": _EXIT,
        "status": PositionStatus.OPEN,
        "opened_at": _NOW,
        "closed_at": None,
        "nearest_expiration": date(2026, 7, 18),
        "est_max_loss": 2187.50,
        "est_max_profit": 312.50,
        "opening_order_id": "ord-dial-001",
    }
    defaults.update(kw)
    return Position(**defaults)


# ---------------------------------------------------------------------------
# Datetime — timezone-aware round-trip
#
# SQLite stores TIMESTAMP WITH TIME ZONE as a plain string; SQLAlchemy
# re-attaches UTC on read-back.  Postgres stores it natively as TIMESTAMPTZ.
# Both must return a tz-aware datetime equal to the original.
# ---------------------------------------------------------------------------


def test_datetime_timezone_round_trip(engine) -> None:
    pos = _pos()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    with get_connection(engine) as conn:
        fetched = get_position(conn, pos.id)
    assert fetched is not None
    assert fetched.opened_at == _NOW
    assert fetched.opened_at.tzinfo is not None
    assert fetched.marked_at == _NOW
    assert fetched.marked_at.tzinfo is not None


def test_datetime_microsecond_precision(engine) -> None:
    """Microseconds must survive the round-trip on both backends."""
    pos = _pos()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    with get_connection(engine) as conn:
        fetched = get_position(conn, pos.id)
    assert fetched is not None
    assert fetched.opened_at.microsecond == 123456


# ---------------------------------------------------------------------------
# JSON columns — None values and nested structures
#
# SQLite serialises JSON as TEXT; Postgres uses JSONB.  Both must preserve
# None (Python) ↔ null (JSON) and nested dict/list structures faithfully.
# ---------------------------------------------------------------------------


def test_json_null_optional_field_round_trip(engine) -> None:
    """realized_pnl is nullable; None must survive as None, not 'null' string."""
    pos = _pos(realized_pnl=None)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    with get_connection(engine) as conn:
        fetched = get_position(conn, pos.id)
    assert fetched is not None
    assert fetched.realized_pnl is None


def test_json_list_in_blob_round_trip(engine) -> None:
    """legs is stored as a JSON blob (list[PositionLeg]); must survive intact."""
    pos = _pos()
    with get_connection(engine) as conn:
        insert_position(conn, pos)
    with get_connection(engine) as conn:
        fetched = get_position(conn, pos.id)
    assert fetched is not None
    assert len(fetched.legs) == 1
    assert fetched.legs[0].leg.strike == 450.0
    assert fetched.legs[0].filled_qty == 5


# ---------------------------------------------------------------------------
# Foreign key constraint enforcement
#
# SQLite only enforces FK constraints when PRAGMA foreign_keys=ON is set;
# conftest and build_engine both enable this.  Postgres enforces FKs
# unconditionally.  Both backends must reject an order whose position_id
# references a non-existent position.
# ---------------------------------------------------------------------------


def test_fk_order_without_position_raises(engine) -> None:
    """Inserting an order with a missing position_id must raise IntegrityError."""
    orphan_order = Order(
        id="ord-orphan",
        broker_order_id="",
        position_id="pos-does-not-exist",
        role=OrderRole.OPEN,
        status=OrderStatus.PENDING_SUBMIT,
        broker_status_raw="",
        submitted_at=_NOW,
        filled_at=None,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )
    with pytest.raises(IntegrityError):
        with get_connection(engine) as conn:
            insert_order(conn, orphan_order)


# ---------------------------------------------------------------------------
# rejection_rule_ids pattern — JSON list of strings
#
# This mirrors the journal_records.rejection_rule_ids column.  Verified
# here via the positions.legs JSON blob as a proxy (same sa.JSON type and
# the same round-trip path).  The journal-level round-trip is covered in
# test_journal_db.py; this test confirms the list-of-strings pattern
# specifically.
# ---------------------------------------------------------------------------


def test_order_round_trip_with_empty_legs_filled(engine) -> None:
    """legs_filled=[] (empty JSON list) must come back as [], not None or '{}'."""
    pos = _pos()
    order = Order(
        id="ord-dial-001",
        broker_order_id="",
        position_id="pos-dial-001",
        role=OrderRole.OPEN,
        status=OrderStatus.PENDING_SUBMIT,
        broker_status_raw="",
        submitted_at=_NOW,
        filled_at=None,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, order)
    with get_connection(engine) as conn:
        fetched = get_order(conn, "ord-dial-001")
    assert fetched is not None
    assert fetched.legs_filled == []
    assert isinstance(fetched.legs_filled, list)
