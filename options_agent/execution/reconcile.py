"""Fill-detection reconcile pass (WP-1.4).

reconcile() is the system's safety net: it pulls live order state from Alpaca,
diffs against the local DB, records immutable FillEvents, and transitions Order
and Position records to match broker reality.

Design invariants
-----------------
Broker is source of truth for fills.  DB is source of truth for intent and
rationale.  reconcile() never overwrites intent fields (position_id, role,
limit_price) — it only updates status, fill counts, and timestamps.

The caller must call reconcile() inside a single transaction so that all writes
either commit together or roll back as a unit.  Use state.db.get_connection():

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

Idempotency is enforced at the FillEvent level via broker_exec_id.  Running
reconcile() twice in the same state produces a StateDiff with all newly_*
lists empty on the second pass (the fill events already exist).

Scope
-----
WP-1.4 covers the fill-detection path only.  Expirations and assignments are
WP-1.5.  Orphan and unmatched_local detection is implemented here but their
resolution (cancel the orphan? alert?) belongs to WP-5 / WP-8.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.engine import Connection

from options_agent.contracts.state import (
    FillEvent,
    LegFill,
    Order,
    OrderRef,
    OrderRole,
    OrderStatus,
    Position,
    PositionStatus,
    ReconcileAnomaly,
    StateDiff,
)
from options_agent.execution.broker import STATUS_MAP, BrokerClient
from options_agent.state.crud import (
    get_order,
    get_position,
    insert_fill_event_if_new,
    list_pending_orders,
    patch_order,
    update_position,
)

logger = logging.getLogger(__name__)


def reconcile(broker: BrokerClient, conn: Connection) -> StateDiff:
    """Diff broker state against local DB and sync fill updates.

    Returns a StateDiff describing every status transition observed this pass.
    All DB writes are executed on the supplied connection; the caller owns the
    transaction boundary.
    """
    now = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Fetch broker open orders — one call to minimise round-trips.
    # ------------------------------------------------------------------
    try:
        broker_orders = broker.list_open_orders()
    except Exception as exc:
        logger.error("reconcile: failed to fetch open orders from broker: %s", exc)
        return StateDiff(
            anomalies=[
                ReconcileAnomaly(
                    order_id=None,
                    broker_order_id=None,
                    description=f"Broker fetch failed: {exc}",
                )
            ],
            reconciled_at=now,
        )

    broker_open = {str(o.id): o for o in broker_orders}

    # ------------------------------------------------------------------
    # Fetch local pending orders and split by whether they have a broker ID.
    # ------------------------------------------------------------------
    local_pending = list_pending_orders(conn)
    local_broker_ids: set[str] = {
        o.broker_order_id for o in local_pending if o.broker_order_id
    }

    local_with_id = [o for o in local_pending if o.broker_order_id]
    # PENDING_SUBMIT rows with no broker_order_id — crash breadcrumbs
    local_without_id = [o for o in local_pending if not o.broker_order_id]

    # ------------------------------------------------------------------
    # Diff each local order against broker state.
    # ------------------------------------------------------------------
    newly_filled: list[Order] = []
    newly_partial: list[Order] = []
    newly_cancelled: list[Order] = []
    newly_rejected: list[Order] = []
    newly_expired: list[Order] = []
    new_positions: list[Position] = []
    closed_positions: list[Position] = []
    anomalies: list[ReconcileAnomaly] = []

    for local_order in local_with_id:
        broker_id = local_order.broker_order_id

        if broker_id in broker_open:
            alpaca_order = broker_open[broker_id]
        else:
            # Order dropped off the open list — fetch its terminal status.
            try:
                alpaca_order = broker.get_broker_order(broker_id)
            except Exception as exc:
                logger.warning(
                    "reconcile: error fetching order %s from broker: %s", broker_id, exc
                )
                anomalies.append(
                    ReconcileAnomaly(
                        order_id=local_order.id,
                        broker_order_id=broker_id,
                        description=f"Broker fetch error: {exc}",
                    )
                )
                continue

            if alpaca_order is None:
                logger.warning(
                    "reconcile: order %s not found at broker (local id=%s)",
                    broker_id,
                    local_order.id,
                )
                anomalies.append(
                    ReconcileAnomaly(
                        order_id=local_order.id,
                        broker_order_id=broker_id,
                        description="Order not found at broker",
                    )
                )
                continue

        status_str = str(alpaca_order.status.value)
        new_status = STATUS_MAP.get(status_str, OrderStatus.WORKING)
        broker_filled_qty = int(alpaca_order.filled_qty or 0)
        fill_price_raw = alpaca_order.filled_avg_price
        fill_price = float(fill_price_raw) if fill_price_raw is not None else 0.0
        broker_filled_at = alpaca_order.filled_at

        # Guard: filled_qty must be monotonically non-decreasing.
        if broker_filled_qty < local_order.filled_qty:
            logger.error(
                "reconcile: filled_qty went backwards for order %s "
                "(local=%d broker=%d) — skipping",
                local_order.id,
                local_order.filled_qty,
                broker_filled_qty,
            )
            anomalies.append(
                ReconcileAnomaly(
                    order_id=local_order.id,
                    broker_order_id=broker_id,
                    description=(
                        f"filled_qty went backwards: "
                        f"local={local_order.filled_qty}, broker={broker_filled_qty}"
                    ),
                    raw={"broker_status": status_str},
                )
            )
            continue

        # Record a FillEvent for any new incremental fill.
        if broker_filled_qty > local_order.filled_qty:
            incremental_qty = broker_filled_qty - local_order.filled_qty
            occurred_at = broker_filled_at or now

            # OCC symbol: single-leg uses order.symbol; multi-leg uses first leg.
            leg_symbol = str(alpaca_order.symbol or "")
            if not leg_symbol and alpaca_order.legs:
                leg_symbol = str(alpaca_order.legs[0].symbol or "")

            fill_event = FillEvent(
                id=str(uuid.uuid4()),
                order_id=local_order.id,
                broker_exec_id=f"{broker_id}@{broker_filled_qty}",
                leg_symbol=leg_symbol,
                filled_qty=incremental_qty,
                fill_price=fill_price,
                occurred_at=occurred_at,
                observed_at=now,
            )
            insert_fill_event_if_new(conn, fill_event)

        # Skip the DB write if nothing actually changed.
        status_unchanged = new_status == local_order.status
        qty_unchanged = broker_filled_qty == local_order.filled_qty
        if status_unchanged and qty_unchanged:
            continue

        # Build patch kwargs.
        patch_kwargs: dict = {
            "status": new_status,
            "broker_status_raw": status_str,
        }
        if broker_filled_qty > local_order.filled_qty:
            patch_kwargs["filled_qty"] = broker_filled_qty
            patch_kwargs["net_fill_price"] = fill_price if fill_price > 0 else None

            # Rebuild legs_filled from the position's legs so the Order record
            # is self-contained for WP-7 slippage analysis.  For single-leg
            # orders this is straightforward; for multi-leg (WP-1.3) each
            # broker leg carries its own fill data.
            position = get_position(conn, local_order.position_id)
            if position is not None:
                if alpaca_order.legs:
                    # Multi-leg: pair by positional index.
                    # WP-1.3 must confirm Alpaca returns combo legs in
                    # submission order before this path handles real orders.
                    legs_filled: list[LegFill] = []
                    for alp_leg, pos_leg in zip(alpaca_order.legs, position.legs):
                        lf_qty = int(alp_leg.filled_qty or 0)
                        lf_price = float(alp_leg.filled_avg_price or 0)
                        if lf_qty > 0:
                            legs_filled.append(
                                LegFill(
                                    leg=pos_leg.leg,
                                    filled_qty=lf_qty,
                                    fill_price=lf_price,
                                )
                            )
                    if legs_filled:
                        patch_kwargs["legs_filled"] = legs_filled
                else:
                    # Single-leg
                    if position.legs:
                        patch_kwargs["legs_filled"] = [
                            LegFill(
                                leg=position.legs[0].leg,
                                filled_qty=broker_filled_qty,
                                fill_price=fill_price,
                            )
                        ]

        if broker_filled_at is not None and local_order.filled_at is None:
            patch_kwargs["filled_at"] = broker_filled_at

        patch_order(conn, local_order.id, **patch_kwargs)

        updated_order = get_order(conn, local_order.id)
        assert updated_order is not None  # row must exist; we just wrote it

        # Categorise the transition.
        prev_status = local_order.status
        if new_status == OrderStatus.FILLED and prev_status != OrderStatus.FILLED:
            newly_filled.append(updated_order)
            _apply_fill_to_position(
                conn,
                local_order,
                broker_filled_at or now,
                new_positions,
                closed_positions,
            )
        elif new_status == OrderStatus.PARTIALLY_FILLED:
            newly_partial.append(updated_order)
        elif (
            new_status == OrderStatus.CANCELLED
            and local_order.status != OrderStatus.CANCELLED
        ):
            newly_cancelled.append(updated_order)
        elif (
            new_status == OrderStatus.REJECTED
            and local_order.status != OrderStatus.REJECTED
        ):
            newly_rejected.append(updated_order)
        elif (
            new_status == OrderStatus.EXPIRED
            and local_order.status != OrderStatus.EXPIRED
        ):
            newly_expired.append(updated_order)

    # ------------------------------------------------------------------
    # Orphans: broker open orders with no matching local record.
    # ------------------------------------------------------------------
    orphans: list[OrderRef] = []
    for bid, alpaca_order in broker_open.items():
        if bid not in local_broker_ids:
            orphans.append(
                OrderRef(
                    broker_order_id=bid,
                    broker_status_raw=str(alpaca_order.status.value),
                    submitted_at=alpaca_order.submitted_at,
                )
            )
            logger.warning(
                "reconcile: orphan order at broker (broker_order_id=%s status=%s)",
                bid,
                alpaca_order.status.value,
            )

    return StateDiff(
        newly_filled=newly_filled,
        newly_partial=newly_partial,
        newly_cancelled=newly_cancelled,
        newly_rejected=newly_rejected,
        newly_expired=newly_expired,
        new_positions=new_positions,
        closed_positions=closed_positions,
        orphans=orphans,
        unmatched_local=local_without_id,
        anomalies=anomalies,
        reconciled_at=now,
    )


def _apply_fill_to_position(
    conn: Connection,
    filled_order: Order,
    filled_at: datetime,
    new_positions: list[Position],
    closed_positions: list[Position],
) -> None:
    """Transition the position linked to a newly-filled order."""
    pos = get_position(conn, filled_order.position_id)
    if pos is None:
        logger.warning(
            "reconcile: position %s not found for filled order %s",
            filled_order.position_id,
            filled_order.id,
        )
        return

    opening = filled_order.role == OrderRole.OPEN
    if opening and pos.status == PositionStatus.PENDING_OPEN:
        updated = pos.model_copy(update={"status": PositionStatus.OPEN})
        update_position(conn, updated)
        new_positions.append(updated)

    elif filled_order.role in (OrderRole.CLOSE, OrderRole.ROLL) and pos.status in (
        PositionStatus.PENDING_CLOSE,
        PositionStatus.OPEN,
    ):
        updated = pos.model_copy(
            update={"status": PositionStatus.CLOSED, "closed_at": filled_at}
        )
        update_position(conn, updated)
        closed_positions.append(updated)
