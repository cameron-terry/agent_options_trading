"""Position and Order CRUD primitives (WP-2.2).

All functions accept a SQLAlchemy Core ``Connection`` from
``state.db.get_connection`` and operate synchronously.

Mutation policy:
- Position and Order are mutable-in-place (status transitions, fill updates).
  update_position / patch_order may be called multiple times over a record's
  lifecycle — this is by design.
- JournalRecord and OutcomeRecord are append-only; their writes belong to
  WP-2.3 and must never call UPDATE on those tables.

Two-phase Order flow (crash-safety invariant):
  insert_order writes a PENDING_SUBMIT row *before* submitting to the broker
  so that a crash between "broker accepted" and "row written" leaves a
  local breadcrumb that reconcile (WP-1) can diff against broker state.
  patch_order then fills in broker_order_id and transitions status as
  broker confirmations arrive.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from options_agent.contracts.state import (
    AssetClass,
    FillEvent,
    LegFill,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionStatus,
)
from options_agent.state.db import fill_events_table, orders_table, positions_table

# ---------------------------------------------------------------------------
# Serialization helpers — Pydantic → DB row and back
# ---------------------------------------------------------------------------

_TERMINAL_POSITION_STATUSES = {
    PositionStatus.CLOSED,
    PositionStatus.EXPIRED,
    PositionStatus.ASSIGNED,
}

_TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}

# Roles that represent "this exposure is being closed / already being handled".
# ROLL is included: a working roll is mechanically closing the position and
# re-opening a replacement, so submitting a CLOSE on top of it is a double-exit.
_EXPOSURE_CLOSING_ROLES = frozenset({OrderRole.CLOSE, OrderRole.ROLL})


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime returned by SQLite's DateTime column.

    SQLite stores datetimes as strings and strips timezone info on read-back.
    All datetimes in this system are UTC; restoring tz-awareness ensures
    Pydantic model equality holds across DB round-trips.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _pos_to_row(pos: Position) -> dict[str, Any]:
    return {
        "id": pos.id,
        "underlying": pos.underlying,
        "strategy": pos.strategy,
        "legs": [leg.model_dump(mode="json") for leg in pos.legs],
        "quantity": pos.quantity,
        "entry_net_amount": pos.entry_net_amount,
        "current_mark": pos.current_mark,
        "marked_at": pos.marked_at,
        "unrealized_pnl": pos.unrealized_pnl,
        "realized_pnl": pos.realized_pnl,
        "exit_plan": (
            pos.exit_plan.model_dump(mode="json") if pos.exit_plan is not None else None
        ),
        "status": pos.status.value,
        "opened_at": pos.opened_at,
        "closed_at": pos.closed_at,
        "nearest_expiration": pos.nearest_expiration,
        "est_max_loss": pos.est_max_loss,
        "est_max_profit": pos.est_max_profit,
        "opening_order_id": pos.opening_order_id,
        "asset_class": pos.asset_class.value,
        "equity_legs": (
            [el.model_dump(mode="json") for el in pos.equity_legs]
            if pos.equity_legs
            else None
        ),
        "assigned_from_position_id": pos.assigned_from_position_id,
    }


def _row_to_pos(row: Any) -> Position:
    d = dict(row._mapping)
    d["marked_at"] = _ensure_utc(d["marked_at"])
    d["opened_at"] = _ensure_utc(d["opened_at"])
    d["closed_at"] = _ensure_utc(d["closed_at"])
    # WP-1.5 fields: default to option_strategy / empty for pre-migration rows
    if not d.get("asset_class"):
        d["asset_class"] = AssetClass.OPTION_STRATEGY.value
    d["equity_legs"] = d.get("equity_legs") or []
    # assigned_from_position_id may be absent from older rows → None
    d.setdefault("assigned_from_position_id", None)
    return Position.model_validate(d)


def _order_to_row(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "broker_order_id": order.broker_order_id,
        "position_id": order.position_id,
        "role": order.role.value,
        "status": order.status.value,
        "broker_status_raw": order.broker_status_raw,
        "submitted_at": order.submitted_at,
        "filled_at": order.filled_at,
        "legs_filled": [lf.model_dump(mode="json") for lf in order.legs_filled],
        "net_fill_price": order.net_fill_price,
        "filled_qty": order.filled_qty,
        "exit_reason": order.exit_reason.value
        if order.exit_reason is not None
        else None,
    }


def _row_to_order(row: Any) -> Order:
    d = dict(row._mapping)
    d["submitted_at"] = _ensure_utc(d["submitted_at"])
    d["filled_at"] = _ensure_utc(d["filled_at"])
    return Order.model_validate(d)


# ---------------------------------------------------------------------------
# Position CRUD
# ---------------------------------------------------------------------------


def insert_position(conn: Connection, position: Position) -> None:
    """Insert a new Position row. Raises IntegrityError if id already exists."""
    conn.execute(positions_table.insert().values(**_pos_to_row(position)))


def get_position(conn: Connection, position_id: str) -> Position | None:
    """Return the Position with the given id, or None if not found."""
    row = conn.execute(
        sa.select(positions_table).where(positions_table.c.id == position_id)
    ).first()
    return _row_to_pos(row) if row is not None else None


def update_position(conn: Connection, position: Position) -> None:
    """Overwrite all mutable fields of an existing Position row.

    Callers own the Position object and pass the updated version here; this
    function does not validate that the transition is legal — that is the
    responsibility of the caller (WP-1 reconcile, WP-5 monitor).

    Raises KeyError if no row with position.id exists — a missing row means
    either a bug in the caller or a data-integrity violation that should not
    be silently swallowed.
    """
    row = _pos_to_row(position)
    pk = row.pop("id")
    result = conn.execute(
        positions_table.update().where(positions_table.c.id == pk).values(**row)
    )
    if result.rowcount == 0:
        raise KeyError(f"Position {pk!r} not found")


def list_open_positions(conn: Connection) -> list[Position]:
    """Return all non-terminal positions (PENDING_OPEN, OPEN, PENDING_CLOSE).

    Results are ordered by opened_at ascending for stable iteration across
    both SQLite and Postgres.
    """
    terminal_values = [s.value for s in _TERMINAL_POSITION_STATUSES]
    rows = conn.execute(
        sa.select(positions_table)
        .where(positions_table.c.status.notin_(terminal_values))
        .order_by(positions_table.c.opened_at)
    ).fetchall()
    return [_row_to_pos(r) for r in rows]


def list_open_option_positions_expiring_on_or_before(
    conn: Connection,
    cutoff: date,
) -> list[Position]:
    """Return OPEN option-strategy positions whose nearest_expiration <= cutoff.

    Used by the WP-1.5 absence backstop to identify candidates for expiry
    detection: positions that are still OPEN in our DB but are past (or on)
    their expiration date, indicating the broker may have expired them.
    Only OPTION_STRATEGY positions are returned — equity positions have the
    EQUITY_NEVER_EXPIRES sentinel date and are never candidates.
    """
    rows = conn.execute(
        sa.select(positions_table)
        .where(
            positions_table.c.status == PositionStatus.OPEN.value,
            sa.or_(
                positions_table.c.asset_class == AssetClass.OPTION_STRATEGY.value,
                positions_table.c.asset_class.is_(None),  # pre-migration rows
            ),
            positions_table.c.nearest_expiration <= cutoff,
        )
        .order_by(positions_table.c.nearest_expiration)
    ).fetchall()
    return [_row_to_pos(r) for r in rows]


# ---------------------------------------------------------------------------
# Order CRUD — two-phase flow
# ---------------------------------------------------------------------------


def insert_order(conn: Connection, order: Order) -> None:
    """Insert a new Order row (phase 1 of the two-phase flow).

    Callers should pass an Order with:
      - status=PENDING_SUBMIT
      - broker_order_id="" (not yet assigned)
      - broker_status_raw="" (not yet known)

    The row must exist in the DB *before* the order is submitted to the broker
    so that a crash in the submission window leaves a PENDING_SUBMIT breadcrumb
    that WP-1 reconcile can detect and resolve against broker state.
    """
    conn.execute(orders_table.insert().values(**_order_to_row(order)))


def get_order(conn: Connection, order_id: str) -> Order | None:
    """Return the Order with the given id, or None if not found."""
    row = conn.execute(
        sa.select(orders_table).where(orders_table.c.id == order_id)
    ).first()
    return _row_to_order(row) if row is not None else None


def patch_order(
    conn: Connection,
    order_id: str,
    *,
    broker_order_id: str | None = None,
    status: OrderStatus | None = None,
    broker_status_raw: str | None = None,
    filled_at: datetime | None = None,
    legs_filled: list[LegFill] | None = None,
    net_fill_price: float | None = None,
    filled_qty: int | None = None,
) -> None:
    """Update named fields on an existing Order row (phase 2 of the two-phase flow).

    Only the fields explicitly passed (not None) are written; others are left
    unchanged. This makes the call idempotent: patching with the same values
    twice produces the same DB state — a requirement for WP-1 reconcile and
    WP-5 monitor, which may both see the same broker update before local state
    settles.

    legs_filled accepts LegFill Pydantic objects (same as insert_order), not
    pre-serialized dicts — serialization is handled internally.

    broker_status_raw should always accompany a status update so that Alpaca's
    exact status string is preserved alongside the mapped enum; this keeps
    any mapping bugs recoverable from the DB.

    Raises KeyError if no row with order_id exists.
    """
    updates: dict[str, Any] = {}
    if broker_order_id is not None:
        updates["broker_order_id"] = broker_order_id
    if status is not None:
        updates["status"] = status.value
    if broker_status_raw is not None:
        updates["broker_status_raw"] = broker_status_raw
    if filled_at is not None:
        updates["filled_at"] = filled_at
    if legs_filled is not None:
        updates["legs_filled"] = [lf.model_dump(mode="json") for lf in legs_filled]
    if net_fill_price is not None:
        updates["net_fill_price"] = net_fill_price
    if filled_qty is not None:
        updates["filled_qty"] = filled_qty

    if not updates:
        return

    result = conn.execute(
        orders_table.update().where(orders_table.c.id == order_id).values(**updates)
    )
    if result.rowcount == 0:
        raise KeyError(f"Order {order_id!r} not found")


def list_pending_orders(conn: Connection) -> list[Order]:
    """Return all non-terminal orders (PENDING_SUBMIT, WORKING, PARTIALLY_FILLED).

    Results are ordered by submitted_at ascending for stable iteration across
    both SQLite and Postgres.
    """
    terminal_values = [s.value for s in _TERMINAL_ORDER_STATUSES]
    rows = conn.execute(
        sa.select(orders_table)
        .where(orders_table.c.status.notin_(terminal_values))
        .order_by(orders_table.c.submitted_at)
    ).fetchall()
    return [_row_to_order(r) for r in rows]


def has_pending_close(conn: Connection, position_id: str) -> bool:
    """Return True if any non-terminal CLOSE or ROLL order exists for position_id.

    This is the Order-table layer of idempotency, complementing the position-status
    layer (_SKIPPABLE_STATUSES in monitor/exits.py). It guards the desync window
    where a closing order was inserted (insert_order) but the position status was
    not yet updated to PENDING_CLOSE (update_position) — e.g., a crash between the
    two writes.

    ROLL is included alongside CLOSE because a working roll is mechanically closing
    the position; submitting a CLOSE on top of a working ROLL is a double-exit.

    Pending = non-terminal = {PENDING_SUBMIT, WORKING, PARTIALLY_FILLED}.
    PARTIALLY_FILLED is the most important case: a partial close means the position
    still shows open exposure but a closing order is actively filling — stacking a
    second close would double the closing quantity.

    Callers must guarantee reconcile ran before this check (the same freshness
    contract enforced by MarkStaleError for the P&L-based exit evaluators).
    For check_time_stop (mark-independent), the monitor cycle is still expected
    to run reconcile at cycle-top.
    """
    terminal_values = [s.value for s in _TERMINAL_ORDER_STATUSES]
    role_values = [r.value for r in _EXPOSURE_CLOSING_ROLES]
    row = conn.execute(
        sa.select(orders_table.c.id)
        .where(
            orders_table.c.position_id == position_id,
            orders_table.c.role.in_(role_values),
            orders_table.c.status.notin_(terminal_values),
        )
        .limit(1)
    ).first()
    return row is not None


def count_close_orders(conn: Connection, position_id: str) -> int:
    """Return the number of CLOSE-role orders ever submitted for position_id.

    Used by the monitor's reprice path as the escalation counter: each
    cancel-and-replace inserts a new CLOSE order, so this count grows by one
    per reprice and determines how far the replacement limit price is widened
    toward the market.
    """
    row = conn.execute(
        sa.select(sa.func.count())
        .select_from(orders_table)
        .where(
            orders_table.c.position_id == position_id,
            orders_table.c.role == OrderRole.CLOSE.value,
        )
    ).scalar()
    return int(row or 0)


def get_closing_order(conn: Connection, position_id: str) -> Order | None:
    """Return the most recently filled CLOSE order for position_id, or None.

    Used by the monitor cycle finalize step to retrieve the actual fill price
    and exit_reason when writing OutcomeRecords for newly-closed positions.

    Returns the FILLED CLOSE order with the latest filled_at timestamp. If
    multiple filled CLOSE orders exist (e.g., partial closes), returns the
    most recent — the one whose fill completed the close.

    Returns None if no filled CLOSE order exists (e.g., position was closed by
    expiry or assignment, which do not produce a CLOSE order).
    """
    row = conn.execute(
        sa.select(orders_table)
        .where(
            orders_table.c.position_id == position_id,
            orders_table.c.role == OrderRole.CLOSE.value,
            orders_table.c.status == OrderStatus.FILLED.value,
        )
        .order_by(orders_table.c.filled_at.desc())
        .limit(1)
    ).first()
    return _row_to_order(row) if row is not None else None


# ---------------------------------------------------------------------------
# FillEvent CRUD — append-only; idempotency via broker_exec_id
# ---------------------------------------------------------------------------


def _fill_event_to_row(fe: FillEvent) -> dict[str, Any]:
    return {
        "id": fe.id,
        "order_id": fe.order_id,
        "broker_exec_id": fe.broker_exec_id,
        "leg_symbol": fe.leg_symbol,
        "filled_qty": fe.filled_qty,
        "fill_price": fe.fill_price,
        "occurred_at": fe.occurred_at,
        "observed_at": fe.observed_at,
    }


def _row_to_fill_event(row: Any) -> FillEvent:
    d = dict(row._mapping)
    d["occurred_at"] = _ensure_utc(d["occurred_at"])
    d["observed_at"] = _ensure_utc(d["observed_at"])
    return FillEvent.model_validate(d)


def insert_fill_event_if_new(conn: Connection, fill_event: FillEvent) -> bool:
    """Insert a FillEvent only if its broker_exec_id has not been seen before.

    Returns True if the row was inserted, False if broker_exec_id already exists.
    This is the idempotency guard for reconcile: the same fill observed on
    multiple consecutive passes is recorded exactly once.
    """
    existing = conn.execute(
        sa.select(fill_events_table.c.id).where(
            fill_events_table.c.broker_exec_id == fill_event.broker_exec_id
        )
    ).first()
    if existing is not None:
        return False
    conn.execute(fill_events_table.insert().values(**_fill_event_to_row(fill_event)))
    return True


def list_fill_events_for_order(conn: Connection, order_id: str) -> list[FillEvent]:
    """Return all FillEvents recorded for a given order, ordered by occurred_at."""
    rows = conn.execute(
        sa.select(fill_events_table)
        .where(fill_events_table.c.order_id == order_id)
        .order_by(fill_events_table.c.occurred_at)
    ).fetchall()
    return [_row_to_fill_event(r) for r in rows]
