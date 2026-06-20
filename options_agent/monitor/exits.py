"""Exit rule evaluators for WP-5.1 (stop-loss), WP-5.2 (profit-target), WP-5.3 (DTE).

Public contract
---------------
check_stop_loss(pos, conn, broker, now, max_mark_age) -> Order | None
check_profit_target(pos, conn, broker, now, max_mark_age) -> Order | None
check_time_stop(pos, conn, broker, now, max_mark_age) -> Order | None

All three share the same (pos, conn, broker, now, max_mark_age, limit_offset)
signature so WP-5.5 / WP-8 can call them uniformly through one interface.

Callers (WP-5.5 / WP-8) MUST run reconcile before calling this module so
that pos.marked_at reflects the current cycle. check_stop_loss and
check_profit_target enforce this via MarkStaleError. check_time_stop is
mark-independent (DTE is calendar arithmetic) and does not raise MarkStaleError,
but still benefits from fresh reconcile state for accurate status reads.

Stop-loss formula (WP-0 amendment, WP-5.1)
-------------------------------------------
  trigger when: unrealized_pnl <= -(stop_loss_max_loss_fraction × est_max_loss)

This formula is uniform across credit and debit strategies because est_max_loss
is always positive and always represents the maximum the position can lose,
regardless of whether it was opened for a credit or a debit.

DTE formula (WP-5.3)
---------------------
  min_dte = (pos.nearest_expiration - today_ET).days
  trigger when: min_dte <= exit_plan.time_stop_dte

today is derived from now in America/New_York (market time). UTC rolls to the
next calendar day at ~7–8 pm ET; using UTC.date() would compute DTE as one day
too few from that point, firing the time-stop a full day early.
time_stop_dte is calendar days (not trading days) — consistent with how
ExitPlan emits it (e.g., the WP-0 default of 21 DTE means 21 calendar days).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Connection

from options_agent.contracts.proposal import Leg, TradeProposal
from options_agent.contracts.state import (
    AssetClass,
    Order,
    OrderRole,
    Position,
    PositionStatus,
)
from options_agent.execution.broker import BrokerClient
from options_agent.state.crud import has_pending_close, insert_order, update_position

logger = logging.getLogger(__name__)

_MARKET_TZ = ZoneInfo("America/New_York")

_SKIPPABLE_STATUSES = frozenset(
    {
        PositionStatus.PENDING_CLOSE,
        PositionStatus.PENDING_OPEN,
        PositionStatus.CLOSED,
        PositionStatus.EXPIRED,
        PositionStatus.ASSIGNED,
    }
)


class MarkStaleError(Exception):
    """Raised when a Position's mark is too old to safely evaluate exit rules.

    The monitor cycle MUST run reconcile (or a mark-refresh) before calling
    any exit evaluator. A stale mark is a surfaced error — not a silent "no
    trigger" — because the dangerous failure mode is a position blowing through
    its stop while the monitor reads an outdated, in-the-money mark and does
    nothing.

    WP-5.5 / WP-8 must treat MarkStaleError as an alertable cycle error and
    log it prominently. Running reconcile at cycle-top is the fix; per-position
    live broker calls are not the correct remedy (too many API calls for a
    1–5 min high-frequency loop).
    """


def _close_proposal(
    pos: Position, thesis: str = "Monitor exit trigger"
) -> TradeProposal:
    """Build a closing TradeProposal from pos by reversing all leg sides."""
    assert pos.exit_plan is not None  # caller must guard before calling
    reversed_legs = [
        Leg(
            right=pl.leg.right,
            side="buy" if pl.leg.side == "sell" else "sell",
            strike=pl.leg.strike,
            expiration=pl.leg.expiration,
            ratio=pl.leg.ratio,
        )
        for pl in pos.legs
    ]
    return TradeProposal(
        action="CLOSE",
        underlying=pos.underlying,
        strategy=pos.strategy,
        legs=reversed_legs,
        thesis=thesis,
        iv_rationale="n/a",
        catalyst_check="n/a",
        conviction=1.0,
        est_max_loss=pos.est_max_loss,
        est_max_profit=pos.est_max_profit,
        breakevens=[],
        net_delta=0.0,
        net_theta=0.0,
        net_vega=0.0,
        exit_plan=pos.exit_plan,
        informed_by=[pos.id],
    )


def _closing_limit_price(pos: Position, offset: float) -> float:
    """Estimate closing limit price from the cached position mark.

    current_mark uses the same sign convention as entry_net_amount:
      negative → net credit strategy (short premium, e.g. credit spread)
      positive → net debit strategy  (long premium, e.g. debit spread)

    Closing reverses the direction: -current_mark gives the opposing combo price.
    The offset nudges toward fill:
      - credit strategy close (buying back, paying a debit): add offset
      - debit strategy close  (selling to close, receiving a credit): subtract offset
    """
    offset_sign = 1.0 if pos.entry_net_amount < 0 else -1.0
    return round(-pos.current_mark + offset_sign * offset, 2)


def check_stop_loss(
    pos: Position,
    conn: Connection,
    broker: BrokerClient,
    now: datetime,
    max_mark_age: timedelta,
    limit_offset: float = 0.01,
) -> Order | None:
    """Evaluate stop-loss for pos; submit and persist a closing order if breached.

    Stop-loss trigger formula:
        unrealized_pnl <= -(stop_loss_max_loss_fraction * est_max_loss)

    The formula is identical for credit and debit strategies: est_max_loss is
    always a positive number representing the maximum the position can lose,
    so the threshold is always a reachable, negative P&L value.

    Guards (return None without submitting):
      - pos.asset_class != OPTION_STRATEGY  (EQUITY positions skip all exit rules)
      - pos.exit_plan is None               (should never happen for OPTION_STRATEGY)
      - pos.status in _SKIPPABLE_STATUSES   (idempotency: PENDING_CLOSE skips re-submit)

    Raises MarkStaleError if pos.marked_at is older than max_mark_age of now.
    A stale mark is an alertable error, not a silent no-op — the caller must
    surface it. WP-5.5 / WP-8 must guarantee reconcile ran before this call.

    On trigger:
      - Submits a closing order via broker.submit() or broker.submit_multi_leg()
      - Inserts the Order into the DB via conn
      - Transitions pos.status to PENDING_CLOSE in the DB
      - Returns the submitted Order

    Returns None if the stop-loss threshold is not breached.

    now must be UTC-aware. max_mark_age is the acceptable staleness window
    (e.g. timedelta(minutes=10) for a 5-minute monitor cycle with some slack).
    """
    if pos.asset_class != AssetClass.OPTION_STRATEGY:
        logger.info(
            "check_stop_loss: skipping equity position %s (asset_class=%s); "
            "disposition belongs to WP-8",
            pos.id,
            pos.asset_class,
        )
        return None

    if pos.exit_plan is None:
        logger.warning(
            "check_stop_loss: position %s has no exit_plan; skipping", pos.id
        )
        return None

    if pos.status in _SKIPPABLE_STATUSES:
        return None

    # Staleness check — make the reconcile-at-cycle-top ordering load-bearing.
    marked_at_utc = (
        pos.marked_at
        if pos.marked_at.tzinfo is not None
        else pos.marked_at.replace(tzinfo=UTC)
    )
    mark_age = now - marked_at_utc
    if mark_age > max_mark_age:
        raise MarkStaleError(
            f"Position {pos.id!r} mark is {mark_age.total_seconds():.0f}s old "
            f"(max allowed: {max_mark_age.total_seconds():.0f}s). "
            "WP-5.5/WP-8 must run reconcile at the top of each monitor cycle "
            "before calling exit evaluators."
        )

    # Order-table idempotency guard — catches the desync window where a closing
    # order was inserted but position status was not yet updated to PENDING_CLOSE.
    # Runs after MarkStaleError so we never consult a stale Order table.
    if has_pending_close(conn, pos.id):
        logger.debug(
            "check_stop_loss: pending close/roll order exists for %s — skipping",
            pos.id,
        )
        return None

    threshold = -(pos.exit_plan.stop_loss_max_loss_fraction * pos.est_max_loss)
    if pos.unrealized_pnl > threshold:
        return None

    logger.info(
        "stop-loss triggered: position=%s unrealized_pnl=%.4f threshold=%.4f "
        "(fraction=%.2f est_max_loss=%.4f)",
        pos.id,
        pos.unrealized_pnl,
        threshold,
        pos.exit_plan.stop_loss_max_loss_fraction,
        pos.est_max_loss,
    )

    close_proposal = _close_proposal(pos, thesis="Monitor stop-loss trigger")
    limit_price = _closing_limit_price(pos, limit_offset)

    if len(pos.legs) == 1:
        order = broker.submit(
            close_proposal, pos.quantity, limit_price, pos.id, role=OrderRole.CLOSE
        )
    else:
        order = broker.submit_multi_leg(
            close_proposal, pos.quantity, limit_price, pos.id, role=OrderRole.CLOSE
        )

    updated_pos = pos.model_copy(update={"status": PositionStatus.PENDING_CLOSE})
    insert_order(conn, order)
    update_position(conn, updated_pos)

    return order


def check_profit_target(
    pos: Position,
    conn: Connection,
    broker: BrokerClient,
    now: datetime,
    max_mark_age: timedelta,
    limit_offset: float = 0.01,
) -> Order | None:
    """Evaluate profit-target for pos; submit and persist a closing order if reached.

    Profit-target trigger formula:
        unrealized_pnl >= profit_target_pct * est_max_profit

    profit_target_pct is in (0, 1] — enforced by ExitPlan.profit_target_pct's
    Field(gt=0) constraint, so it can never produce a zero threshold on its own.
    est_max_profit carries no positivity constraint in the WP-0 contract; a zero
    or negative value would set threshold=0 and spuriously trigger for any
    break-even or profitable position. This function guards that explicitly.

    No credit/debit sign adjustment is needed (unlike the stop-loss formula).
    est_max_profit is always the maximum gain regardless of strategy direction.

    Guards (return None without submitting):
      - pos.asset_class != OPTION_STRATEGY  (EQUITY positions skip all exit rules)
      - pos.exit_plan is None               (should never happen for OPTION_STRATEGY)
      - pos.est_max_profit <= 0             (data quality: avoids spurious trigger)
      - pos.status in _SKIPPABLE_STATUSES   (idempotency: PENDING_CLOSE skips re-submit)

    Raises MarkStaleError if pos.marked_at is strictly older than max_mark_age of
    now (mark_age > max_mark_age; a position marked at exactly max_mark_age passes).
    This is consistent with check_stop_loss. WP-5.5/WP-8 must guarantee reconcile
    ran before calling this function; a stale mark is a surfaced error, not a
    silent no-op, because the dangerous direction is missing a trigger.

    On trigger:
      - Submits a closing order via broker.submit() or broker.submit_multi_leg()
      - Inserts the Order into the DB via conn
      - Transitions pos.status to PENDING_CLOSE in the DB
      - Returns the submitted Order

    insert_order and update_position are both issued on conn. Atomicity depends on
    the caller: get_connection(engine) wraps the connection in engine.begin(), so
    both writes commit or roll back together. Callers that pass an unmanaged
    Connection must wrap in a transaction themselves.

    Returns None if the profit-target threshold has not been reached.
    """
    if pos.asset_class != AssetClass.OPTION_STRATEGY:
        logger.info(
            "check_profit_target: skipping equity position %s (asset_class=%s); "
            "disposition belongs to WP-8",
            pos.id,
            pos.asset_class,
        )
        return None

    if pos.exit_plan is None:
        logger.warning(
            "check_profit_target: position %s has no exit_plan; skipping", pos.id
        )
        return None

    if pos.est_max_profit <= 0:
        logger.warning(
            "check_profit_target: position %s has est_max_profit=%.4f (expected > 0); "
            "skipping to avoid spurious trigger at break-even",
            pos.id,
            pos.est_max_profit,
        )
        return None

    if pos.status in _SKIPPABLE_STATUSES:
        return None

    marked_at_utc = (
        pos.marked_at
        if pos.marked_at.tzinfo is not None
        else pos.marked_at.replace(tzinfo=UTC)
    )
    mark_age = now - marked_at_utc
    if mark_age > max_mark_age:
        raise MarkStaleError(
            f"Position {pos.id!r} mark is {mark_age.total_seconds():.0f}s old "
            f"(max allowed: {max_mark_age.total_seconds():.0f}s). "
            "WP-5.5/WP-8 must run reconcile at the top of each monitor cycle "
            "before calling exit evaluators."
        )

    # Order-table idempotency guard — after MarkStaleError so Order table is fresh.
    if has_pending_close(conn, pos.id):
        logger.debug(
            "check_profit_target: pending close/roll order exists for %s — skipping",
            pos.id,
        )
        return None

    threshold = pos.exit_plan.profit_target_pct * pos.est_max_profit
    if pos.unrealized_pnl < threshold:
        return None

    logger.info(
        "profit-target triggered: position=%s unrealized_pnl=%.4f threshold=%.4f "
        "(pct=%.2f est_max_profit=%.4f)",
        pos.id,
        pos.unrealized_pnl,
        threshold,
        pos.exit_plan.profit_target_pct,
        pos.est_max_profit,
    )

    close_proposal = _close_proposal(pos, thesis="Monitor profit-target trigger")
    limit_price = _closing_limit_price(pos, limit_offset)

    if len(pos.legs) == 1:
        order = broker.submit(
            close_proposal, pos.quantity, limit_price, pos.id, role=OrderRole.CLOSE
        )
    else:
        order = broker.submit_multi_leg(
            close_proposal, pos.quantity, limit_price, pos.id, role=OrderRole.CLOSE
        )

    updated_pos = pos.model_copy(update={"status": PositionStatus.PENDING_CLOSE})
    insert_order(conn, order)
    update_position(conn, updated_pos)

    return order


def check_time_stop(
    pos: Position,
    conn: Connection,
    broker: BrokerClient,
    now: datetime,
    max_mark_age: timedelta,
    limit_offset: float = 0.01,
) -> Order | None:
    """Evaluate DTE time-stop for pos; submit and persist a closing order if triggered.

    DTE trigger formula:
        min_dte = (pos.nearest_expiration - today_ET).days
        trigger when: min_dte <= exit_plan.time_stop_dte

    today is derived from now converted to America/New_York (market time), NOT
    UTC: UTC rolls to the next calendar day at ~7–8 pm ET, which would compute
    DTE as one day too few for several hours each evening, firing the time-stop
    a full day early and corrupting every position's DTE schedule. WP-5.5 / WP-8
    must pass a UTC-aware now; this function handles the market-timezone
    conversion internally.

    time_stop_dte is calendar days (not trading days). The WP-0 default of 21
    DTE means 21 calendar days to expiration — consistent with how ExitPlan emits
    it.

    max_mark_age is accepted for API uniformity with check_stop_loss and
    check_profit_target (WP-5.5 can call all three through the same signature);
    it is deliberately not enforced here because the DTE rule is mark-independent
    (no unrealized_pnl or current_mark is read).

    The DTE condition is monotonic and permanent once met (DTE only decreases),
    so the PENDING_CLOSE guard in _SKIPPABLE_STATUSES is non-optional. Without
    it, every subsequent monitor cycle from the trigger day until fill would
    re-submit a closing order. This is more acute than for the price-based exits
    because the condition never self-heals.

    IMPORTANT — nearest_expiration sync on rolls: pos.nearest_expiration is
    denormalised at position-open time. If rolling is ever implemented (WP-5 /
    WP-8), nearest_expiration must be recomputed on the roll; otherwise this
    evaluator reads a stale expiration date and computes incorrect DTE.

    Guards (return None without submitting):
      - pos.asset_class != OPTION_STRATEGY
      - pos.exit_plan is None
      - pos.status in _SKIPPABLE_STATUSES

    On trigger:
      - Submits a closing order via broker.submit() or broker.submit_multi_leg()
      - Inserts the Order into the DB via conn
      - Transitions pos.status to PENDING_CLOSE in the DB
      - Returns the submitted Order

    Returns None if min_dte > exit_plan.time_stop_dte.

    now must be UTC-aware.
    """
    if pos.asset_class != AssetClass.OPTION_STRATEGY:
        logger.info(
            "check_time_stop: skipping equity position %s (asset_class=%s); "
            "disposition belongs to WP-8",
            pos.id,
            pos.asset_class,
        )
        return None

    if pos.exit_plan is None:
        logger.warning(
            "check_time_stop: position %s has no exit_plan; skipping", pos.id
        )
        return None

    if pos.status in _SKIPPABLE_STATUSES:
        return None

    # Order-table idempotency guard. No MarkStaleError precedes this in
    # check_time_stop (DTE is mark-independent), but the monitor cycle is still
    # expected to run reconcile at cycle-top, keeping the Order table fresh.
    if has_pending_close(conn, pos.id):
        logger.debug(
            "check_time_stop: pending close/roll order exists for %s — skipping",
            pos.id,
        )
        return None

    today = now.astimezone(_MARKET_TZ).date()
    min_dte = (pos.nearest_expiration - today).days

    if min_dte > pos.exit_plan.time_stop_dte:
        return None

    logger.info(
        "time-stop triggered: position=%s min_dte=%d threshold=%d "
        "(nearest_expiration=%s today_ET=%s)",
        pos.id,
        min_dte,
        pos.exit_plan.time_stop_dte,
        pos.nearest_expiration,
        today,
    )

    close_proposal = _close_proposal(pos, thesis="Monitor time-stop trigger")
    limit_price = _closing_limit_price(pos, limit_offset)

    if len(pos.legs) == 1:
        order = broker.submit(
            close_proposal, pos.quantity, limit_price, pos.id, role=OrderRole.CLOSE
        )
    else:
        order = broker.submit_multi_leg(
            close_proposal, pos.quantity, limit_price, pos.id, role=OrderRole.CLOSE
        )

    updated_pos = pos.model_copy(update={"status": PositionStatus.PENDING_CLOSE})
    insert_order(conn, order)
    update_position(conn, updated_pos)

    return order
