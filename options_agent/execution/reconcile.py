"""Fill-detection and expiry/assignment reconcile (WP-1.4 + WP-1.5).

reconcile() is the system's safety net: it pulls live order state from Alpaca,
diffs against the local DB, records immutable FillEvents, and transitions Order
and Position records to match broker reality.  WP-1.5 extends it to detect two
externally-initiated state transitions that have no corresponding fill event:
option expiry and option assignment.

Design invariants
-----------------
Broker is source of truth for fills.  DB is source of truth for intent and
rationale.  reconcile() never overwrites intent fields (position_id, role,
limit_price) — it only updates status, fill counts, and timestamps.

The caller must call reconcile() inside a single transaction so that all writes
either commit together or roll back as a unit.  Use state.db.get_connection():

    with get_connection(engine) as conn:
        diff = reconcile(broker, conn)

Idempotency (fill path): enforced at the FillEvent level via broker_exec_id.
Idempotency (expiry/assignment path): enforced by status checks — only OPEN
positions are candidates; a position already marked EXPIRED or ASSIGNED is
never re-processed.

Expiry/assignment detection strategy (WP-1.5)
----------------------------------------------
Primary: activity feed — query /v2/account/activities for OPEXP and OPASN
events in the last 48 h.  Event-driven; gives precise occurrence timestamps.

Backstop: absence check — any OPEN position whose nearest_expiration is
_EXPIRY_GRACE_DAYS or more in the past and whose option legs are absent from
get_all_positions() is marked EXPIRED.  Fires regardless of whether the
activity feed succeeded, catching events the feed missed (paper-env gaps,
API failures, weekend settlement lag).

NOTE on paper trading: Alpaca paper may not emit OPEXP/OPASN activities with
the same reliability as live.  If the activity feed consistently returns empty
for known expirations, the absence backstop is the operative path.  Verify
this empirically before the first real-money run.

WP-5 / WP-8 contracts
-----------------------
- WP-5 MUST skip exit-rule evaluation for asset_class == EQUITY positions.
- WP-8 owns the assigned-equity disposition policy (auto-liquidate vs. halt).
  An equity position in the DB is an unhandled state for an options bot; WP-8
  must decide and implement that policy.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.engine import Connection

from options_agent.contracts.state import (
    EQUITY_NEVER_EXPIRES,
    AssetClass,
    AssignmentEvent,
    EquityLeg,
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
from options_agent.risk.structure import apply_fill_metrics
from options_agent.state.crud import (
    get_order,
    get_position,
    insert_fill_event_if_new,
    insert_position,
    list_fill_events_for_order,
    list_open_option_positions_expiring_on_or_before,
    list_open_positions,
    list_pending_orders,
    patch_order,
    update_position,
)

logger = logging.getLogger(__name__)

# Absence backstop: only treat a position as expired if its nearest_expiration
# is at least this many days in the past.  A grace period avoids false positives
# from broker settlement lag on expiration afternoon.
_EXPIRY_GRACE_DAYS: int = 1

# Activity feed lookback window.  48 h catches weekend expirations that reconcile
# runs on Monday, and any same-day processing lag.
_ACTIVITY_LOOKBACK_HOURS: int = 48


def _occ_symbol(underlying: str, expiration: date, right: str, strike: float) -> str:
    """Build the OCC option symbol used by Alpaca to identify a single leg.

    Format: {underlying}{YYMMDD}{P|C}{strike_in_thousandths_zero_padded_to_8}
    Example: QQQ260724P00691000
    """
    right_char = "P" if right == "put" else "C"
    strike_thou = round(strike * 1000)
    return f"{underlying}{expiration.strftime('%y%m%d')}{right_char}{strike_thou:08d}"


def _refresh_position_marks(
    broker: BrokerClient,
    conn: Connection,
    now: datetime,
) -> None:
    """Update current_mark, marked_at, and unrealized_pnl for all open option positions.

    Fetches current leg prices from the broker's open-positions list, computes the
    net combo mark per position, and writes it back so the monitor's exit evaluators
    have a fresh basis for stop-loss and profit-target checks.

    Called at the end of every reconcile() pass so that mark age never exceeds
    2 × monitor interval (240 s) before MarkStaleError would fire.
    """
    try:
        broker_positions = broker.get_all_positions()
    except Exception as exc:
        logger.warning(
            "reconcile: mark refresh — broker.get_all_positions() failed: %s", exc
        )
        return

    # Build OCC symbol → current per-share option price (always positive magnitude).
    price_by_occ: dict[str, float] = {}
    for bp in broker_positions:
        if bp.current_price is not None:
            try:
                price_by_occ[str(bp.symbol)] = abs(float(bp.current_price))
            except (ValueError, TypeError):
                pass

    for pos in list_open_positions(conn):
        if pos.asset_class != AssetClass.OPTION_STRATEGY:
            continue
        if pos.status != PositionStatus.OPEN:
            continue

        net_mark = 0.0
        skip = False
        for pl in pos.legs:
            occ = _occ_symbol(
                pos.underlying, pl.leg.expiration, pl.leg.right, pl.leg.strike
            )
            price = price_by_occ.get(occ)
            if price is None:
                logger.warning(
                    "reconcile: mark refresh — leg %s not found in broker positions"
                    " (position %s); skipping mark update",
                    occ,
                    pos.id,
                )
                skip = True
                break
            sign = -1.0 if pl.leg.side == "sell" else 1.0
            net_mark += sign * price * pl.leg.ratio

        if skip:
            continue

        unrealized_pnl = round(
            (net_mark - pos.entry_net_amount) * pos.quantity * 100, 4
        )
        update_position(
            conn,
            pos.model_copy(
                update={
                    "current_mark": round(net_mark, 4),
                    "marked_at": now,
                    "unrealized_pnl": unrealized_pnl,
                }
            ),
        )
        logger.debug(
            "reconcile: mark refresh — %s pos=%s mark=%.4f unrealized_pnl=%.2f",
            pos.underlying,
            pos.id,
            net_mark,
            unrealized_pnl,
        )


def reconcile(
    broker: BrokerClient,
    conn: Connection,
    *,
    _clock: datetime | None = None,
) -> StateDiff:
    """Diff broker state against local DB and sync fill updates.

    Returns a StateDiff describing every status transition observed this pass.
    All DB writes are executed on the supplied connection; the caller owns the
    transaction boundary.

    _clock is a test-only override for the current time. Production callers
    must not pass it.
    """
    now = _clock if _clock is not None else datetime.now(UTC)

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
            # "pending_cancel" appears in the open-order index while a cancel
            # request is in-flight.  Alpaca paper (and occasionally live) lags
            # in removing the order from the index after the fill or cancel is
            # finalised, so the index may show "pending_cancel" long after the
            # primary store holds "filled" or "canceled".  Re-fetch via
            # get_order_by_id to get the authoritative terminal state so that a
            # fill that races a cancel is not silently missed.
            if str(alpaca_order.status.value) == "pending_cancel":
                try:
                    fresh = broker.get_broker_order(broker_id)
                    if fresh is not None:
                        alpaca_order = fresh
                except Exception as exc:
                    logger.warning(
                        "reconcile: error re-fetching pending_cancel order %s: %s",
                        broker_id,
                        exc,
                    )
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
            # WP-1: net_fill_price is Alpaca's signed mleg price (negative =
            # net credit), matching Position.entry_net_amount's convention —
            # see execution/broker.py's two other fill-mapping sites, which
            # correctly gate on fill_price_raw/filled_qty presence, not price
            # sign. The old `fill_price > 0` guard here silently dropped
            # net_fill_price for every credit fill completed asynchronously
            # (i.e. not within the synchronous cycle-top poll window).
            patch_kwargs["net_fill_price"] = (
                fill_price if fill_price_raw is not None else None
            )

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
                updated_order,
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

    # ------------------------------------------------------------------
    # WP-1.5: detect expiry and assignment events.
    # ------------------------------------------------------------------
    expired_positions, assignment_events, ext_anomalies = (
        _detect_expiry_and_assignments(broker, conn, now)
    )
    anomalies.extend(ext_anomalies)

    # ── Mark refresh: update current_mark for all open option positions ───────
    _refresh_position_marks(broker, conn, now)

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
        expired_option_positions=expired_positions,
        assigned_positions=assignment_events,
        reconciled_at=now,
    )


def _apply_fill_to_position(
    conn: Connection,
    filled_order: Order,
    filled_at: datetime,
    new_positions: list[Position],
    closed_positions: list[Position],
) -> None:
    """Transition the position linked to a newly-filled order.

    filled_order must be the freshly-patched Order row (net_fill_price set) —
    a pre-patch copy has no fill price and the WP-1 recompute below would
    silently no-op.
    """
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
        update: dict[str, object] = {"status": PositionStatus.OPEN}
        # WP-1: this order filled asynchronously (after cycle-top), so the
        # est_max_loss/profit baked in at Position creation still reflects
        # the pre-trade chain-mid estimate. Correct it against the real fill.
        if filled_order.net_fill_price is not None:
            fill_max_loss, fill_max_profit = apply_fill_metrics(
                [pos_leg.leg for pos_leg in pos.legs],
                filled_order.net_fill_price,
                prior_est_max_loss=pos.est_max_loss,
                prior_est_max_profit=pos.est_max_profit,
                log_context=f"reconcile fill order {filled_order.id}",
            )
            update["est_max_loss"] = fill_max_loss
            update["est_max_profit"] = fill_max_profit
        updated = pos.model_copy(update=update)
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


# ---------------------------------------------------------------------------
# WP-1.5: expiry and assignment detection
# ---------------------------------------------------------------------------


def _detect_expiry_and_assignments(
    broker: BrokerClient,
    conn: Connection,
    now: datetime,
) -> tuple[list[Position], list[AssignmentEvent], list[ReconcileAnomaly]]:
    """Detect option expiry and assignment events for all open positions.

    Strategy
    --------
    Primary path: query /v2/account/activities for OPEXP and OPASN events in
    the last _ACTIVITY_LOOKBACK_HOURS.  Each matched event closes the option
    position (EXPIRED or ASSIGNED) and, for assignments, creates an EQUITY
    Position row.

    Backstop path: compare get_all_positions() against DB positions past their
    nearest_expiration + _EXPIRY_GRACE_DAYS.  Catches any expirations the
    activity feed missed (paper-env gaps, API failures, etc.).

    Idempotency: only OPEN positions are candidates; a position already marked
    EXPIRED or ASSIGNED is skipped on every subsequent pass.
    """
    expired: list[Position] = []
    assignments: list[AssignmentEvent] = []
    anomalies: list[ReconcileAnomaly] = []
    today = now.date()

    # Build OCC-symbol → Position index for all open option positions.
    # Each open option position has FillEvents whose leg_symbol is the OCC string.
    open_positions = list_open_positions(conn)
    occ_to_pos: dict[str, Position] = {}
    for pos in open_positions:
        if pos.asset_class != AssetClass.OPTION_STRATEGY:
            continue
        try:
            fill_events = list_fill_events_for_order(conn, pos.opening_order_id)
        except Exception:
            continue
        for fe in fill_events:
            if fe.leg_symbol:
                occ_to_pos[fe.leg_symbol] = pos

    # Track which positions were handled by the activity feed so the backstop
    # does not double-process them.
    activity_handled: set[str] = set()

    # ------------------------------------------------------------------
    # Primary: activity feed
    # ------------------------------------------------------------------
    try:
        after = now - timedelta(hours=_ACTIVITY_LOOKBACK_HOURS)
        activities: list[dict[str, Any]] = broker.get_account_activities(
            ["OPEXP", "OPASN"], after=after
        )
        for act in activities:
            act_type = str(act.get("activity_type", ""))
            occ_symbol = str(act.get("symbol", ""))
            if not occ_symbol:
                continue

            pos = occ_to_pos.get(occ_symbol)
            if pos is None:
                # Activity for an OCC symbol not in our open positions — already
                # processed in a prior pass or belongs to a position we don't own.
                logger.debug(
                    "reconcile: activity %s for unknown/closed OCC %s — skipping",
                    act_type,
                    occ_symbol,
                )
                continue

            if pos.id in activity_handled:
                continue

            occurred_at = _parse_activity_datetime(act.get("date"), now)

            if act_type == "OPEXP":
                updated = pos.model_copy(
                    update={"status": PositionStatus.EXPIRED, "closed_at": occurred_at}
                )
                update_position(conn, updated)
                expired.append(updated)
                activity_handled.add(pos.id)
                logger.info(
                    "reconcile: OPEXP — position %s (%s) marked EXPIRED",
                    pos.id,
                    occ_symbol,
                )

            elif act_type == "OPASN":
                assigned_qty = int(float(act.get("qty") or 0))
                if assigned_qty == 0:
                    logger.warning(
                        "reconcile: OPASN — zero qty for %s, skipping", occ_symbol
                    )
                    continue
                assignment_price = float(act.get("price") or 0)

                equity_pos = _build_equity_position_from_assignment(
                    option_pos=pos,
                    assigned_qty=assigned_qty,
                    assignment_price=assignment_price,
                    occurred_at=occurred_at,
                )
                insert_position(conn, equity_pos)

                updated_option = pos.model_copy(
                    update={"status": PositionStatus.ASSIGNED, "closed_at": occurred_at}
                )
                update_position(conn, updated_option)

                assignments.append(
                    AssignmentEvent(
                        closed_option_position_id=pos.id,
                        created_equity_position=equity_pos,
                        assigned_qty=assigned_qty,
                        assignment_price=assignment_price,
                        occurred_at=occurred_at,
                    )
                )
                activity_handled.add(pos.id)
                logger.info(
                    "reconcile: OPASN — %s (%s) assigned; equity %s created",
                    pos.id,
                    occ_symbol,
                    equity_pos.id,
                )

    except Exception as exc:
        logger.warning(
            "reconcile: activity feed unavailable (%s); absence backstop will run", exc
        )
        anomalies.append(
            ReconcileAnomaly(
                order_id=None,
                broker_order_id=None,
                description=f"Activity feed unavailable: {exc}",
            )
        )

    # ------------------------------------------------------------------
    # Backstop: absence check for positions past expiration
    # ------------------------------------------------------------------
    try:
        live_symbols: set[str] = {
            ap.symbol for ap in broker.get_all_positions() if ap.symbol
        }
        cutoff = today - timedelta(days=_EXPIRY_GRACE_DAYS)
        candidates = list_open_option_positions_expiring_on_or_before(conn, cutoff)

        for cand in candidates:
            if cand.id in activity_handled:
                continue  # already handled above

            # Check if any leg is still present at the broker.
            try:
                fill_events = list_fill_events_for_order(conn, cand.opening_order_id)
            except Exception:
                fill_events = []
            leg_symbols = {fe.leg_symbol for fe in fill_events if fe.leg_symbol}

            if leg_symbols & live_symbols:
                # At least one leg still open — not expired yet.
                continue

            updated = cand.model_copy(
                update={"status": PositionStatus.EXPIRED, "closed_at": now}
            )
            update_position(conn, updated)
            expired.append(updated)
            logger.info(
                "reconcile: absence backstop — position %s marked EXPIRED "
                "(nearest_expiration=%s, all legs absent from broker)",
                cand.id,
                cand.nearest_expiration,
            )

    except Exception as exc:
        logger.error("reconcile: absence backstop failed: %s", exc)
        anomalies.append(
            ReconcileAnomaly(
                order_id=None,
                broker_order_id=None,
                description=f"Absence backstop failed: {exc}",
            )
        )

    return expired, assignments, anomalies


def _build_equity_position_from_assignment(
    option_pos: Position,
    assigned_qty: int,
    assignment_price: float,
    occurred_at: datetime,
) -> Position:
    """Build an EQUITY Position record for shares received/delivered via assignment.

    qty in EquityLeg is the number of shares implied by the assignment
    (assigned_qty contracts × 100 shares/contract).  The sign follows Alpaca's
    activity qty field (positive = long shares received; negative = short shares
    delivered).  Using the raw Alpaca qty preserves the directional information
    without us having to re-derive it from the original leg side.

    nearest_expiration is set to EQUITY_NEVER_EXPIRES (9999-12-31) — equity
    has no expiration.  WP-5 must guard on asset_class == EQUITY before
    computing DTE or evaluating exit rules.

    WP-8 owns the disposition policy for this position: auto-liquidate vs. halt.
    """
    shares = assigned_qty * 100
    return Position(
        id=str(uuid.uuid4()),
        underlying=option_pos.underlying,
        strategy="assigned_equity",
        legs=[],
        equity_legs=[
            EquityLeg(
                symbol=option_pos.underlying,
                qty=shares,
                avg_price=assignment_price,
            )
        ],
        asset_class=AssetClass.EQUITY,
        assigned_from_position_id=option_pos.id,
        quantity=assigned_qty,
        entry_net_amount=shares * assignment_price,
        current_mark=shares * assignment_price,
        marked_at=occurred_at,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=None,
        status=PositionStatus.OPEN,
        opened_at=occurred_at,
        closed_at=None,
        nearest_expiration=EQUITY_NEVER_EXPIRES,
        est_max_loss=0.0,
        est_max_profit=0.0,
        opening_order_id=f"asn:{option_pos.id}",
    )


def _parse_activity_datetime(raw: Any, fallback: datetime) -> datetime:
    """Parse an Alpaca activity date string; return fallback on any failure.

    Handles ISO 8601 datetime strings with and without timezone info, and
    date-only strings ("2026-06-10") which Alpaca may return for some events.
    All returned datetimes are UTC-aware.
    """
    if not raw:
        return fallback
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return fallback
