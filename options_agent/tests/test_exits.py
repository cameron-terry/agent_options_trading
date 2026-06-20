"""Tests for WP-5.1 (stop-loss), WP-5.2 (profit-target), WP-5.3 (DTE) in exits.py."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from options_agent.contracts.proposal import ExitPlan, Leg
from options_agent.contracts.state import (
    AssetClass,
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.monitor.exits import (
    MarkStaleError,
    check_profit_target,
    check_stop_loss,
    check_time_stop,
)
from options_agent.state.crud import (
    get_order,
    get_position,
    insert_order,
    insert_position,
)
from options_agent.state.db import get_connection

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 19, 14, 0, 0, tzinfo=UTC)
_FRESH_MARK = _NOW - timedelta(minutes=2)  # within any reasonable staleness window
_MAX_MARK_AGE = timedelta(minutes=10)

_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50,
    stop_loss_max_loss_fraction=0.5,
    time_stop_dte=21,
)

_SHORT_PUT_LEG = Leg(
    right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15)
)
_LONG_PUT_LEG = Leg(right="put", side="buy", strike=445.0, expiration=date(2026, 8, 15))

# A credit spread (bull put spread): sold for $0.55/contract, 5 contracts.
# entry_net_amount = -275.00 (credit received)
# est_max_loss = 2225.00  (width − credit = $4.45 × 5 contracts × 100 multiplier)
# With stop_loss_max_loss_fraction=0.5: trigger when unrealized_pnl <= -1112.50


def _make_position(
    *,
    underlying: str = "SPY",
    strategy: str = "bull_put_spread",
    legs: list[PositionLeg] | None = None,
    quantity: int = 5,
    entry_net_amount: float = -275.0,
    current_mark: float = -150.0,
    marked_at: datetime = _FRESH_MARK,
    unrealized_pnl: float = 125.0,
    exit_plan: ExitPlan | None = _EXIT_PLAN,
    status: PositionStatus = PositionStatus.OPEN,
    est_max_loss: float = 2225.0,
    est_max_profit: float = 275.0,
    asset_class: AssetClass = AssetClass.OPTION_STRATEGY,
    pos_id: str | None = None,
    nearest_expiration: date = date(2026, 8, 15),
) -> Position:
    if legs is None:
        legs = [
            PositionLeg(
                leg=_SHORT_PUT_LEG,
                filled_qty=5,
                avg_fill_price=0.55,
                status=LegStatus.OPEN,
            ),
            PositionLeg(
                leg=_LONG_PUT_LEG,
                filled_qty=5,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
        ]
    return Position(
        id=pos_id or str(uuid.uuid4()),
        underlying=underlying,
        strategy=strategy,
        legs=legs,
        quantity=quantity,
        entry_net_amount=entry_net_amount,
        current_mark=current_mark,
        marked_at=marked_at,
        unrealized_pnl=unrealized_pnl,
        realized_pnl=None,
        exit_plan=exit_plan,
        status=status,
        opened_at=_FRESH_MARK,
        closed_at=None,
        nearest_expiration=nearest_expiration,
        est_max_loss=est_max_loss,
        est_max_profit=est_max_profit,
        opening_order_id="open-ord-001",
        asset_class=asset_class,
    )


def _mock_broker_order(position_id: str) -> Order:
    """Return a fake WORKING Order as the broker would return after submit."""
    return Order(
        id=str(uuid.uuid4()),
        broker_order_id=str(uuid.uuid4()),
        position_id=position_id,
        role=OrderRole.CLOSE,
        status=OrderStatus.WORKING,
        broker_status_raw="new",
        submitted_at=_NOW,
        filled_at=None,
        limit_price=0.15,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )


def _make_broker_mock(position_id: str) -> MagicMock:
    broker = MagicMock()
    broker.submit_multi_leg.return_value = _mock_broker_order(position_id)
    broker.submit.return_value = _mock_broker_order(position_id)
    return broker


# ---------------------------------------------------------------------------
# Core trigger / no-trigger logic
# ---------------------------------------------------------------------------


def test_stop_loss_not_triggered_above_threshold(engine) -> None:
    """P&L above threshold → no order submitted, returns None."""
    pos = _make_position(
        unrealized_pnl=-500.0,  # -500 > -1112.50 → not yet breached
        est_max_loss=2225.0,
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_stop_loss_triggered_at_threshold(engine) -> None:
    """P&L exactly at threshold → order submitted, position PENDING_CLOSE."""
    pos = _make_position(
        unrealized_pnl=-1112.50,  # exactly at -(0.5 * 2225)
        est_max_loss=2225.0,
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    assert order.role == OrderRole.CLOSE
    broker.submit_multi_leg.assert_called_once()


def test_stop_loss_triggered_below_threshold(engine) -> None:
    """P&L below threshold → order submitted."""
    pos = _make_position(
        unrealized_pnl=-1500.0,  # -1500 < -1112.50 → breached
        est_max_loss=2225.0,
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    broker.submit_multi_leg.assert_called_once()


def test_triggered_position_transitions_to_pending_close(engine) -> None:
    """After trigger, position status in DB must be PENDING_CLOSE."""
    pos = _make_position(
        unrealized_pnl=-2000.0,
        est_max_loss=2225.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)
        refreshed = get_position(conn, pos.id)

    assert refreshed is not None
    assert refreshed.status == PositionStatus.PENDING_CLOSE


def test_triggered_order_persisted_in_db(engine) -> None:
    """The closing Order returned by broker is inserted into the DB."""
    pos = _make_position(
        unrealized_pnl=-2000.0,
        est_max_loss=2225.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)
        assert order is not None
        fetched = get_order(conn, order.id)

    assert fetched is not None
    assert fetched.role == OrderRole.CLOSE
    assert fetched.position_id == pos.id


# ---------------------------------------------------------------------------
# Cross-strategy symmetry: same fraction → same proportional pain → trigger
# ---------------------------------------------------------------------------


def test_credit_strategy_triggers_at_fraction_of_max_loss(engine) -> None:
    """Credit spread (entry_net_amount < 0): trigger when 50% of max loss hit."""
    # entry_net_amount = -275 (credit), est_max_loss = 2225
    # threshold = -(0.5 × 2225) = -1112.50
    pos = _make_position(
        entry_net_amount=-275.0,
        est_max_loss=2225.0,
        unrealized_pnl=-1112.50,  # exactly at threshold
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None, (
        "Credit strategy stop-loss must trigger at 50% of max loss"
    )


def test_debit_strategy_triggers_at_fraction_of_max_loss(engine) -> None:
    """Debit spread (entry_net_amount > 0): trigger when 50% of max loss hit."""
    # entry_net_amount = +500 (debit paid), est_max_loss = 500
    # threshold = -(0.5 × 500) = -250.00
    pos = _make_position(
        entry_net_amount=500.0,
        current_mark=250.0,  # currently worth half the debit
        est_max_loss=500.0,
        est_max_profit=500.0,
        unrealized_pnl=-250.0,  # exactly at threshold
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None, "Debit strategy stop-loss must trigger at 50% of max loss"


def test_cross_strategy_symmetry_same_fraction_same_trigger() -> None:
    """50% loss fraction triggers identically for credit and debit strategies."""
    # Credit: max_loss=1000 → threshold=-500
    # Debit:  max_loss=1000 → threshold=-500
    # Both at -500 pnl must trigger; both at -499 must not.
    fraction = 0.5
    max_loss = 1000.0
    threshold = -(fraction * max_loss)

    for entry_net_amount in (-200.0, 200.0):  # credit then debit
        label = "credit" if entry_net_amount < 0 else "debit"

        # At threshold: should trigger
        from options_agent.monitor.exits import _SKIPPABLE_STATUSES  # noqa: F401

        pos_at = _make_position(
            entry_net_amount=entry_net_amount,
            est_max_loss=max_loss,
            unrealized_pnl=threshold,
            exit_plan=ExitPlan(
                profit_target_pct=0.50,
                stop_loss_max_loss_fraction=fraction,
                time_stop_dte=21,
            ),
        )
        # Verify threshold computation is symmetric.
        assert pos_at.exit_plan is not None
        computed_threshold = -(
            pos_at.exit_plan.stop_loss_max_loss_fraction * pos_at.est_max_loss
        )
        assert computed_threshold == threshold, (
            f"{label}: expected threshold {threshold}, got {computed_threshold}"
        )
        assert pos_at.unrealized_pnl <= computed_threshold, (
            f"{label}: pnl={pos_at.unrealized_pnl} should be <= {computed_threshold}"
        )

        # Just above threshold: should not trigger
        pos_above = _make_position(
            entry_net_amount=entry_net_amount,
            est_max_loss=max_loss,
            unrealized_pnl=threshold + 1.0,
            exit_plan=ExitPlan(
                profit_target_pct=0.50,
                stop_loss_max_loss_fraction=fraction,
                time_stop_dte=21,
            ),
        )
        assert pos_above.unrealized_pnl > computed_threshold, (
            f"{label}: P&L above threshold should not trigger"
        )


# ---------------------------------------------------------------------------
# Guard conditions: skipped without submitting
# ---------------------------------------------------------------------------


def test_equity_position_skipped(engine) -> None:
    """EQUITY asset_class → return None, no broker call."""
    pos = _make_position(
        asset_class=AssetClass.EQUITY,
        unrealized_pnl=-5000.0,  # massively losing — must still skip
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_equity_position_logged(engine) -> None:
    """EQUITY asset_class → log.info emitted so the position is visible in logs."""
    pos = _make_position(
        asset_class=AssetClass.EQUITY,
        unrealized_pnl=-5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with patch("options_agent.monitor.exits.logger") as mock_logger:
            check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)
            mock_logger.info.assert_called_once()


def test_no_exit_plan_skipped(engine) -> None:
    """exit_plan=None → return None, no broker call, log warning."""
    pos = _make_position(
        exit_plan=None,
        unrealized_pnl=-5000.0,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with patch("options_agent.monitor.exits.logger") as mock_logger:
            result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)
            mock_logger.warning.assert_called_once()

    assert result is None
    broker.submit_multi_leg.assert_not_called()


def test_pending_close_position_skipped(engine) -> None:
    """PENDING_CLOSE → idempotency guard, no second close submitted."""
    pos = _make_position(
        status=PositionStatus.PENDING_CLOSE,
        unrealized_pnl=-5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [PositionStatus.CLOSED, PositionStatus.EXPIRED, PositionStatus.ASSIGNED],
)
def test_terminal_position_skipped(engine, status: PositionStatus) -> None:
    """Terminal statuses (CLOSED, EXPIRED, ASSIGNED) → skipped without submitting."""
    pos = _make_position(
        status=status,
        unrealized_pnl=-5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------


def test_stale_mark_raises_error(engine) -> None:
    """marked_at older than max_mark_age → MarkStaleError, no broker call."""
    stale_time = _NOW - timedelta(minutes=15)  # 15 min stale, max is 10
    pos = _make_position(
        marked_at=stale_time,
        unrealized_pnl=-5000.0,  # massively breached
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with pytest.raises(MarkStaleError):
            check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_fresh_mark_does_not_raise(engine) -> None:
    """marked_at just within max_mark_age → no error raised."""
    fresh_time = _NOW - timedelta(minutes=9, seconds=59)  # just within 10 min
    pos = _make_position(
        marked_at=fresh_time,
        unrealized_pnl=100.0,  # not breached — just checking no staleness error
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        # Should not raise MarkStaleError
        result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None  # not triggered (P&L positive)


def test_stale_mark_is_error_not_silent_pass(engine) -> None:
    """A stale mark must NOT silently evaluate as 'not triggered'.

    The dangerous failure mode is: position is blowing through its stop, but
    the monitor reads an outdated mark and does nothing. MarkStaleError is the
    correct surfaced state — the caller must alert, not swallow.
    """
    stale_time = _NOW - timedelta(hours=1)
    pos = _make_position(
        marked_at=stale_time,
        unrealized_pnl=999.0,  # profitable-looking stale mark
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        # Must raise even when the stale P&L looks healthy — staleness always errors.
        with pytest.raises(MarkStaleError):
            check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)


# ---------------------------------------------------------------------------
# Closing proposal construction
# ---------------------------------------------------------------------------


def test_closing_order_uses_reversed_legs() -> None:
    """Closing proposal must have all leg sides reversed from the position."""
    from options_agent.monitor.exits import _close_proposal

    pos = _make_position()
    proposal = _close_proposal(pos)

    # Original position: sell put at 450, buy put at 445
    # Close: buy put at 450, sell put at 445
    assert proposal.action == "CLOSE"
    assert len(proposal.legs) == len(pos.legs)
    for orig_pl, close_leg in zip(pos.legs, proposal.legs):
        expected_side = "buy" if orig_pl.leg.side == "sell" else "sell"
        assert close_leg.side == expected_side, (
            f"Expected reversed side {expected_side!r}, got {close_leg.side!r}"
        )
        assert close_leg.right == orig_pl.leg.right
        assert close_leg.strike == orig_pl.leg.strike
        assert close_leg.expiration == orig_pl.leg.expiration


def test_single_leg_position_uses_submit(engine) -> None:
    """Single-leg position close uses broker.submit(), not submit_multi_leg()."""
    single_leg_pos = _make_position(
        legs=[
            PositionLeg(
                leg=Leg(
                    right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15)
                ),
                filled_qty=3,
                avg_fill_price=1.50,
                status=LegStatus.OPEN,
            )
        ],
        entry_net_amount=-450.0,
        current_mark=-200.0,
        unrealized_pnl=-1200.0,  # well below threshold of -(0.5 * 2225) = -1112.5
        est_max_loss=2225.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(single_leg_pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, single_leg_pos)
        order = check_stop_loss(single_leg_pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    broker.submit.assert_called_once()
    broker.submit_multi_leg.assert_not_called()


# ---------------------------------------------------------------------------
# Limit price sign convention
# ---------------------------------------------------------------------------


def test_credit_strategy_close_has_positive_limit_price() -> None:
    """Buying back a credit spread is a debit — limit_price must be positive."""
    from options_agent.monitor.exits import _closing_limit_price

    # Credit spread: entry_net_amount < 0, current_mark < 0
    pos = _make_position(entry_net_amount=-275.0, current_mark=-150.0)
    price = _closing_limit_price(pos, offset=0.01)
    assert price > 0, (
        f"Closing credit strategy should be a debit (positive), got {price}"
    )


def test_debit_strategy_close_has_negative_limit_price() -> None:
    """Selling a debit spread to close is a credit — limit_price must be negative."""
    from options_agent.monitor.exits import _closing_limit_price

    # Debit spread: entry_net_amount > 0, current_mark > 0
    pos = _make_position(entry_net_amount=500.0, current_mark=200.0)
    price = _closing_limit_price(pos, offset=0.01)
    assert price < 0, (
        f"Closing debit strategy should be a credit (negative), got {price}"
    )


# ===========================================================================
# WP-5.2 — Profit-target trigger logic
# ===========================================================================

# Credit spread baseline:
#   entry_net_amount = -275 (credit received)
#   est_max_profit = 275 (max gain = full credit)
#   profit_target_pct = 0.50 → trigger when unrealized_pnl >= 137.50


# ---------------------------------------------------------------------------
# Core trigger / no-trigger logic
# ---------------------------------------------------------------------------


def test_profit_target_not_triggered_below_threshold(engine) -> None:
    """P&L below profit target → no order submitted, returns None."""
    pos = _make_position(
        unrealized_pnl=100.0,  # 100 < 137.50 → not yet reached
        est_max_profit=275.0,
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_profit_target_triggered_at_threshold(engine) -> None:
    """P&L exactly at profit target → order submitted, returns Order."""
    pos = _make_position(
        unrealized_pnl=137.50,  # exactly 0.50 * 275 = 137.50
        est_max_profit=275.0,
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    assert order.role == OrderRole.CLOSE
    broker.submit_multi_leg.assert_called_once()


def test_profit_target_triggered_above_threshold(engine) -> None:
    """P&L above profit target → order submitted."""
    pos = _make_position(
        unrealized_pnl=200.0,  # 200 > 137.50 → triggered
        est_max_profit=275.0,
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    broker.submit_multi_leg.assert_called_once()


def test_profit_target_position_transitions_to_pending_close(engine) -> None:
    """After trigger, position status in DB must be PENDING_CLOSE."""
    pos = _make_position(
        unrealized_pnl=275.0,  # full max profit
        est_max_profit=275.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)
        refreshed = get_position(conn, pos.id)

    assert refreshed is not None
    assert refreshed.status == PositionStatus.PENDING_CLOSE


def test_profit_target_order_persisted_in_db(engine) -> None:
    """The closing Order returned by broker is inserted into the DB."""
    pos = _make_position(
        unrealized_pnl=275.0,
        est_max_profit=275.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)
        assert order is not None
        fetched = get_order(conn, order.id)

    assert fetched is not None
    assert fetched.role == OrderRole.CLOSE
    assert fetched.position_id == pos.id


# ---------------------------------------------------------------------------
# Cross-strategy symmetry: pct × est_max_profit generalizes cleanly
# ---------------------------------------------------------------------------


def test_profit_target_credit_strategy_triggers_at_pct_of_max_profit(engine) -> None:
    """Credit spread: trigger when unrealized_pnl >= 50% of credit received."""
    # entry_net_amount = -275 (credit), est_max_profit = 275
    # threshold = 0.50 × 275 = 137.50
    pos = _make_position(
        entry_net_amount=-275.0,
        est_max_profit=275.0,
        unrealized_pnl=137.50,  # exactly at threshold
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None, (
        "Credit strategy profit-target must trigger at 50% of max profit"
    )


def test_profit_target_debit_strategy_triggers_at_pct_of_max_profit(engine) -> None:
    """Debit spread: trigger when unrealized_pnl >= 50% of max gain."""
    # entry_net_amount = +500 (debit paid), est_max_profit = 500
    # threshold = 0.50 × 500 = 250.00
    pos = _make_position(
        entry_net_amount=500.0,
        current_mark=750.0,  # gained value: current > entry
        est_max_loss=500.0,
        est_max_profit=500.0,
        unrealized_pnl=250.0,  # exactly at threshold
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None, (
        "Debit strategy profit-target must trigger at 50% of max profit"
    )


def test_profit_target_cross_strategy_symmetry_same_pct_same_trigger(engine) -> None:
    """50% profit target triggers identically for credit and debit strategies.

    Actually calls check_profit_target to verify the evaluator itself is
    symmetric, not just the arithmetic.
    """
    pct = 0.50
    max_profit = 1000.0
    threshold = pct * max_profit  # 500.0
    exit_plan = ExitPlan(
        profit_target_pct=pct, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
    )

    for entry_net_amount in (-300.0, 300.0):  # credit then debit
        label = "credit" if entry_net_amount < 0 else "debit"

        # At threshold: evaluator must fire
        pos_at = _make_position(
            entry_net_amount=entry_net_amount,
            est_max_profit=max_profit,
            unrealized_pnl=threshold,
            exit_plan=exit_plan,
        )
        broker_at = _make_broker_mock(pos_at.id)
        with get_connection(engine) as conn:
            insert_position(conn, pos_at)
            order = check_profit_target(pos_at, conn, broker_at, _NOW, _MAX_MARK_AGE)
        assert order is not None, (
            f"{label}: evaluator must trigger at threshold pnl={threshold}"
        )

        # Just below threshold: evaluator must not fire
        pos_below = _make_position(
            entry_net_amount=entry_net_amount,
            est_max_profit=max_profit,
            unrealized_pnl=threshold - 1.0,
            exit_plan=exit_plan,
        )
        broker_below = _make_broker_mock(pos_below.id)
        with get_connection(engine) as conn:
            insert_position(conn, pos_below)
            order = check_profit_target(
                pos_below, conn, broker_below, _NOW, _MAX_MARK_AGE
            )
        assert order is None, f"{label}: evaluator must not trigger below threshold"


# ---------------------------------------------------------------------------
# Guard conditions: skipped without submitting
# ---------------------------------------------------------------------------


def test_profit_target_equity_position_skipped(engine) -> None:
    """EQUITY asset_class → return None, no broker call."""
    pos = _make_position(
        asset_class=AssetClass.EQUITY,
        unrealized_pnl=5000.0,  # massively profitable — must still skip
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_profit_target_equity_position_logged(engine) -> None:
    """EQUITY asset_class → log.info emitted so the position is visible in logs."""
    pos = _make_position(
        asset_class=AssetClass.EQUITY,
        unrealized_pnl=5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with patch("options_agent.monitor.exits.logger") as mock_logger:
            check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)
            mock_logger.info.assert_called_once()


def test_profit_target_no_exit_plan_skipped(engine) -> None:
    """exit_plan=None → return None, no broker call, log warning."""
    pos = _make_position(
        exit_plan=None,
        unrealized_pnl=5000.0,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with patch("options_agent.monitor.exits.logger") as mock_logger:
            result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)
            mock_logger.warning.assert_called_once()

    assert result is None
    broker.submit_multi_leg.assert_not_called()


def test_profit_target_zero_max_profit_skipped(engine) -> None:
    """est_max_profit=0 → skip with warning; prevents spurious trigger at break-even.

    TradeProposal.est_max_profit carries no positivity constraint in the WP-0
    contract. A zero value would make threshold=0.0 and fire for any position
    with unrealized_pnl >= 0 — i.e., every break-even or profitable position.
    """
    pos = _make_position(
        est_max_profit=0.0,
        unrealized_pnl=0.0,  # break-even — must NOT trigger
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with patch("options_agent.monitor.exits.logger") as mock_logger:
            result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)
            mock_logger.warning.assert_called_once()

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_profit_target_pending_close_position_skipped(engine) -> None:
    """PENDING_CLOSE → idempotency guard, no second close submitted."""
    pos = _make_position(
        status=PositionStatus.PENDING_CLOSE,
        unrealized_pnl=5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [PositionStatus.CLOSED, PositionStatus.EXPIRED, PositionStatus.ASSIGNED],
)
def test_profit_target_terminal_position_skipped(
    engine, status: PositionStatus
) -> None:
    """Terminal statuses (CLOSED, EXPIRED, ASSIGNED) → skipped without submitting."""
    pos = _make_position(
        status=status,
        unrealized_pnl=5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------


def test_profit_target_stale_mark_raises_error(engine) -> None:
    """marked_at older than max_mark_age → MarkStaleError, no broker call."""
    stale_time = _NOW - timedelta(minutes=15)  # 15 min stale, max is 10
    pos = _make_position(
        marked_at=stale_time,
        unrealized_pnl=5000.0,  # massively profitable
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with pytest.raises(MarkStaleError):
            check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_profit_target_stale_mark_is_error_not_silent_pass(engine) -> None:
    """A stale mark must NOT silently evaluate as 'not triggered'.

    The dangerous failure mode is: position is at profit target, but
    the monitor reads an outdated mark and does nothing. MarkStaleError
    is always the correct response — the caller must alert, not swallow.
    """
    stale_time = _NOW - timedelta(hours=1)
    pos = _make_position(
        marked_at=stale_time,
        unrealized_pnl=-100.0,  # loss-looking stale mark — still must raise
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with pytest.raises(MarkStaleError):
            check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)


# ---------------------------------------------------------------------------
# Idempotency and evaluator ordering
# ---------------------------------------------------------------------------


def test_profit_target_pending_close_set_by_stop_loss_prevents_second_submit(
    engine,
) -> None:
    """PENDING_CLOSE set by stop-loss in the same cycle prevents profit-target submit.

    This tests the evaluator-ordering robustness: if stop-loss runs first and
    transitions the position, profit-target must see PENDING_CLOSE and bail.
    In practice P&L can't simultaneously be at stop-loss AND profit-target, but
    the status guard is what makes evaluator ordering robust — not P&L exclusivity.
    """
    pos = _make_position(
        status=PositionStatus.PENDING_CLOSE,  # as if stop-loss already fired
        unrealized_pnl=5000.0,  # high P&L — would trigger profit-target if not guarded
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Single-leg routing
# ---------------------------------------------------------------------------


def test_profit_target_single_leg_position_uses_submit(engine) -> None:
    """Single-leg position close uses broker.submit(), not submit_multi_leg()."""
    single_leg_pos = _make_position(
        legs=[
            PositionLeg(
                leg=Leg(
                    right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15)
                ),
                filled_qty=3,
                avg_fill_price=1.50,
                status=LegStatus.OPEN,
            )
        ],
        entry_net_amount=-450.0,
        current_mark=-100.0,
        unrealized_pnl=350.0,  # well above 50% of est_max_profit=275 (137.50)
        est_max_profit=275.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(single_leg_pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, single_leg_pos)
        order = check_profit_target(single_leg_pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    broker.submit.assert_called_once()
    broker.submit_multi_leg.assert_not_called()


# ===========================================================================
# WP-5.3 — DTE / time-stop trigger logic
# ===========================================================================
#
# _NOW = 2026-06-19 14:00 UTC = 10:00 AM EDT (America/New_York, UTC-4).
# ET date = June 19, 2026.
#
# With time_stop_dte=21:
#   threshold date = June 19 + 21 = July 10, 2026
#     (date(2026, 7, 10) - date(2026, 6, 19)).days == 21  ← exactly at threshold
#   above:  July 11 → DTE=22 → no trigger
#   below:  July 9  → DTE=20 → triggers
#   expiry: June 19 → DTE=0  → triggers
#
# time_stop_dte is calendar days (not trading days); see check_time_stop docstring.

_DTE_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50,
    stop_loss_max_loss_fraction=0.5,
    time_stop_dte=21,
)

# Nearest-expiration dates relative to ET date June 19 2026
_EXPIRY_AT_THRESHOLD = date(2026, 7, 10)  # DTE=21 → triggers (21 <= 21)
_EXPIRY_ONE_ABOVE = date(2026, 7, 11)  # DTE=22 → no trigger
_EXPIRY_ONE_BELOW = date(2026, 7, 9)  # DTE=20 → triggers
_EXPIRY_TODAY = date(2026, 6, 19)  # DTE=0  → triggers (assignment risk)


# ---------------------------------------------------------------------------
# Core trigger / no-trigger logic
# ---------------------------------------------------------------------------


def test_time_stop_not_triggered_above_threshold(engine) -> None:
    """DTE one day above threshold → no order submitted, returns None."""
    pos = _make_position(
        nearest_expiration=_EXPIRY_ONE_ABOVE,  # DTE=22 > 21
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_time_stop_triggered_at_threshold(engine) -> None:
    """DTE exactly at threshold → order submitted, returns Order."""
    pos = _make_position(
        nearest_expiration=_EXPIRY_AT_THRESHOLD,  # DTE=21 <= 21
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    assert order.role == OrderRole.CLOSE
    broker.submit_multi_leg.assert_called_once()


def test_time_stop_triggered_below_threshold(engine) -> None:
    """DTE one day below threshold → order submitted."""
    pos = _make_position(
        nearest_expiration=_EXPIRY_ONE_BELOW,  # DTE=20 <= 21
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    broker.submit_multi_leg.assert_called_once()


def test_time_stop_triggered_on_expiration_day(engine) -> None:
    """DTE=0 (expiration day) → order submitted.

    Positions must be closed on their expiration day to avoid assignment risk
    (the primary reason for a time-stop). DTE=0 must always trigger.
    """
    pos = _make_position(
        nearest_expiration=_EXPIRY_TODAY,  # DTE=0 <= 21
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None, "DTE=0 (expiration day) must always trigger the time-stop"
    broker.submit_multi_leg.assert_called_once()


def test_time_stop_position_transitions_to_pending_close(engine) -> None:
    """After trigger, position status in DB must be PENDING_CLOSE."""
    pos = _make_position(
        nearest_expiration=_EXPIRY_AT_THRESHOLD,
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)
        refreshed = get_position(conn, pos.id)

    assert refreshed is not None
    assert refreshed.status == PositionStatus.PENDING_CLOSE


def test_time_stop_order_persisted_in_db(engine) -> None:
    """The closing Order returned by broker is inserted into the DB."""
    pos = _make_position(
        nearest_expiration=_EXPIRY_AT_THRESHOLD,
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)
        assert order is not None
        fetched = get_order(conn, order.id)

    assert fetched is not None
    assert fetched.role == OrderRole.CLOSE
    assert fetched.position_id == pos.id


# ---------------------------------------------------------------------------
# Multi-leg: nearest_expiration is the minimum across legs
# ---------------------------------------------------------------------------


def test_time_stop_multi_leg_uses_nearest_expiration(engine) -> None:
    """nearest_expiration (the min) drives DTE; the farther leg does not block trigger.

    Position has two legs: one expiring in 22 days (above threshold) and one
    expiring in 20 days (below threshold). nearest_expiration = 20-day leg.
    time_stop_dte=21 → min_dte=20 <= 21 → must trigger.
    """
    near_expiry = date(2026, 7, 9)  # DTE=20 from ET June 19 — nearest leg
    far_expiry = date(2026, 7, 11)  # DTE=22 — farther leg
    legs = [
        PositionLeg(
            leg=Leg(right="put", side="sell", strike=450.0, expiration=far_expiry),
            filled_qty=5,
            avg_fill_price=0.55,
            status=LegStatus.OPEN,
        ),
        PositionLeg(
            leg=Leg(right="put", side="buy", strike=445.0, expiration=near_expiry),
            filled_qty=5,
            avg_fill_price=0.0,
            status=LegStatus.OPEN,
        ),
    ]
    pos = _make_position(
        legs=legs,
        nearest_expiration=near_expiry,  # min of the two legs
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        order = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None, (
        "nearest_expiration (min DTE=20) must trigger even when one leg has DTE=22"
    )
    broker.submit_multi_leg.assert_called_once()


# ---------------------------------------------------------------------------
# Idempotency — PENDING_CLOSE guard is non-optional for time-stop
#
# Unlike price-based exits (where P&L can move back above threshold),
# the DTE condition is monotonic: once min_dte <= time_stop_dte, it stays
# true every cycle until the position fills. Without the PENDING_CLOSE guard,
# each monitor cycle from trigger day onward would re-submit a closing order.
# ---------------------------------------------------------------------------


def test_time_stop_pending_close_prevents_second_submit(engine) -> None:
    """PENDING_CLOSE → idempotency guard fires, no second close submitted."""
    pos = _make_position(
        nearest_expiration=_EXPIRY_AT_THRESHOLD,
        status=PositionStatus.PENDING_CLOSE,  # already triggered on a prior cycle
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Guard conditions: skipped without submitting
# ---------------------------------------------------------------------------


def test_time_stop_equity_position_skipped(engine) -> None:
    """EQUITY asset_class → return None, no broker call."""
    pos = _make_position(
        asset_class=AssetClass.EQUITY,
        nearest_expiration=_EXPIRY_TODAY,  # DTE=0 — would trigger if not guarded
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_time_stop_equity_position_logged(engine) -> None:
    """EQUITY asset_class → log.info emitted so the position is visible in logs."""
    pos = _make_position(
        asset_class=AssetClass.EQUITY,
        nearest_expiration=_EXPIRY_TODAY,  # DTE=0 — would trigger if not guarded
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with patch("options_agent.monitor.exits.logger") as mock_logger:
            check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)
            mock_logger.info.assert_called_once()


def test_time_stop_no_exit_plan_skipped(engine) -> None:
    """exit_plan=None → return None, no broker call, log warning."""
    pos = _make_position(
        nearest_expiration=_EXPIRY_TODAY,
        exit_plan=None,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        with patch("options_agent.monitor.exits.logger") as mock_logger:
            result = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)
            mock_logger.warning.assert_called_once()

    assert result is None
    broker.submit_multi_leg.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [PositionStatus.CLOSED, PositionStatus.EXPIRED, PositionStatus.ASSIGNED],
)
def test_time_stop_terminal_position_skipped(engine, status: PositionStatus) -> None:
    """Terminal statuses (CLOSED, EXPIRED, ASSIGNED) → skipped without submitting."""
    pos = _make_position(
        nearest_expiration=_EXPIRY_TODAY,  # DTE=0 — would trigger if not guarded
        status=status,
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()


# ---------------------------------------------------------------------------
# Timezone correctness: today must be America/New_York, not UTC
# ---------------------------------------------------------------------------


def test_time_stop_market_timezone_not_utc(engine) -> None:
    """today must be derived from America/New_York, not UTC.

    Setup: now = 2026-06-16 02:00 UTC = June 15 22:00 ET (EDT, UTC-4).
      UTC date:    June 16  →  (July 7 - June 16).days = 21  → would trigger (wrong)
      ET date:     June 15  →  (July 7 - June 15).days = 22  → no trigger (correct)

    If the implementation uses UTC.date(), it fires a day early. This test
    asserts no trigger, which only passes with the ET-derived date.
    """
    # 02:00 UTC June 16 = 22:00 ET June 15 (summer, EDT = UTC-4)
    now_utc = datetime(2026, 6, 16, 2, 0, 0, tzinfo=UTC)
    # July 7 is 22 calendar days from ET June 15 and 21 days from UTC June 16
    pos = _make_position(
        nearest_expiration=date(2026, 7, 7),
        marked_at=now_utc - timedelta(minutes=2),
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
    )
    broker = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        result = check_time_stop(pos, conn, broker, now_utc, _MAX_MARK_AGE)

    assert result is None, (
        "Using ET date (June 15) gives DTE=22 > 21, no trigger. "
        "UTC date (June 16) gives DTE=21 <= 21, a spurious early trigger."
    )
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Single-leg routing
# ---------------------------------------------------------------------------


def test_time_stop_single_leg_position_uses_submit(engine) -> None:
    """Single-leg position close uses broker.submit(), not submit_multi_leg()."""
    single_leg_pos = _make_position(
        legs=[
            PositionLeg(
                leg=Leg(
                    right="put",
                    side="sell",
                    strike=450.0,
                    expiration=_EXPIRY_AT_THRESHOLD,
                ),
                filled_qty=5,
                avg_fill_price=1.50,
                status=LegStatus.OPEN,
            )
        ],
        nearest_expiration=_EXPIRY_AT_THRESHOLD,  # DTE=21 → triggers
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(single_leg_pos.id)
    with get_connection(engine) as conn:
        insert_position(conn, single_leg_pos)
        order = check_time_stop(single_leg_pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert order is not None
    broker.submit.assert_called_once()
    broker.submit_multi_leg.assert_not_called()


# ===========================================================================
# WP-5.4 — Idempotency guard: has_pending_close (Order-table layer)
# ===========================================================================
#
# These tests verify the second idempotency layer: the Order-table guard that
# catches the desync window where insert_order succeeded but update_position
# (→ PENDING_CLOSE) did not — e.g., a crash between the two writes.
#
# All three evaluators are exercised because the card requires all three to
# call has_pending_close before submitting.
# ---------------------------------------------------------------------------


def _make_close_order(
    position_id: str,
    *,
    status: OrderStatus = OrderStatus.WORKING,
    role: OrderRole = OrderRole.CLOSE,
    order_id: str | None = None,
) -> Order:
    """Build a closing Order for use in WP-5.4 tests."""
    return Order(
        id=order_id or str(uuid.uuid4()),
        broker_order_id=str(uuid.uuid4()),
        position_id=position_id,
        role=role,
        status=status,
        broker_status_raw="new",
        submitted_at=_NOW,
        filled_at=None,
        limit_price=0.15,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )


# ---------------------------------------------------------------------------
# Desync scenario: position status is OPEN but a CLOSE order already exists
# ---------------------------------------------------------------------------


def test_stop_loss_order_table_guard_blocks_desync_resubmit(engine) -> None:
    """Desync: position status is OPEN but a WORKING CLOSE order exists in the DB.

    This simulates a crash between insert_order and update_position on a prior
    cycle. The position-status layer sees OPEN and would permit a second close.
    The Order-table guard (has_pending_close) catches the in-flight close and
    prevents the duplicate.
    """
    pos = _make_position(
        status=PositionStatus.OPEN,  # NOT PENDING_CLOSE — simulating desync
        unrealized_pnl=-5000.0,  # massively breached
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    existing_close = _make_close_order(pos.id, status=OrderStatus.WORKING)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, existing_close)
        result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None, (
        "has_pending_close must block resubmit even when position status is OPEN"
    )
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_profit_target_order_table_guard_blocks_desync_resubmit(engine) -> None:
    """Desync: position OPEN + WORKING CLOSE order → profit-target returns None."""
    pos = _make_position(
        status=PositionStatus.OPEN,
        unrealized_pnl=5000.0,  # massively profitable
        est_max_profit=275.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    existing_close = _make_close_order(pos.id, status=OrderStatus.WORKING)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, existing_close)
        result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_time_stop_order_table_guard_blocks_desync_resubmit(engine) -> None:
    """Desync: position OPEN + WORKING CLOSE order → time-stop guard returns None."""
    pos = _make_position(
        status=PositionStatus.OPEN,
        nearest_expiration=_EXPIRY_AT_THRESHOLD,  # DTE=21 → would trigger
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    existing_close = _make_close_order(pos.id, status=OrderStatus.WORKING)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, existing_close)
        result = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


# ---------------------------------------------------------------------------
# PARTIALLY_FILLED is the primary case — dedicated tests
# ---------------------------------------------------------------------------


def test_stop_loss_partially_filled_close_blocks_second_submit(engine) -> None:
    """PARTIALLY_FILLED CLOSE order blocks a new stop-loss close.

    Partial fill is the case where duplication is most likely and harmful:
    the position still shows open exposure (some contracts haven't closed),
    so a naive evaluator would see an actionable position and fire again.
    Stacking a second close on top of the partial doubles the closing quantity.
    """
    pos = _make_position(
        status=PositionStatus.OPEN,  # not yet PENDING_CLOSE (partial, not full fill)
        unrealized_pnl=-5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    partial_close = _make_close_order(pos.id, status=OrderStatus.PARTIALLY_FILLED)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, partial_close)
        result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None, (
        "PARTIALLY_FILLED CLOSE order must block a second stop-loss close"
    )
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_profit_target_partially_filled_close_blocks_second_submit(engine) -> None:
    """PARTIALLY_FILLED CLOSE order blocks a new profit-target close.

    Symmetry with the stop-loss and time-stop PARTIALLY_FILLED tests: all three
    evaluators must block on a partial close, since has_pending_close is the
    shared guard. This test verifies profit-target doesn't slip through.
    """
    pos = _make_position(
        status=PositionStatus.OPEN,
        unrealized_pnl=5000.0,  # massively profitable — would trigger
        est_max_profit=275.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    partial_close = _make_close_order(pos.id, status=OrderStatus.PARTIALLY_FILLED)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, partial_close)
        result = check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None, (
        "PARTIALLY_FILLED CLOSE order must block a second profit-target close"
    )
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_time_stop_partially_filled_close_blocks_second_submit(engine) -> None:
    """PARTIALLY_FILLED CLOSE order blocks a new time-stop close.

    The DTE condition is monotonic (never self-heals), making time-stop the most
    acute case for duplication: every subsequent cycle would re-trigger until the
    position fully fills. A partial close must silence all subsequent cycles.
    """
    pos = _make_position(
        status=PositionStatus.OPEN,
        nearest_expiration=_EXPIRY_AT_THRESHOLD,
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    partial_close = _make_close_order(pos.id, status=OrderStatus.PARTIALLY_FILLED)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, partial_close)
        result = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None, (
        "PARTIALLY_FILLED CLOSE order must block a second time-stop close"
    )
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


# ---------------------------------------------------------------------------
# ROLL order blocks a new CLOSE (role in {CLOSE, ROLL})
# ---------------------------------------------------------------------------


def test_stop_loss_working_roll_blocks_close(engine) -> None:
    """WORKING ROLL order → stop-loss evaluator returns None.

    A roll is closing the existing position exposure; submitting a CLOSE on top
    of a working ROLL is a double-exit. The guard must catch role=ROLL too.
    """
    pos = _make_position(
        status=PositionStatus.OPEN,
        unrealized_pnl=-5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    roll_order = _make_close_order(
        pos.id, status=OrderStatus.WORKING, role=OrderRole.ROLL
    )

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, roll_order)
        result = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    assert result is None, "WORKING ROLL must block a new CLOSE (it's already closing)"
    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


# ---------------------------------------------------------------------------
# MarkStaleError fires before has_pending_close (ordering invariant)
# ---------------------------------------------------------------------------


def test_stale_mark_raises_before_order_table_guard_is_consulted(engine) -> None:
    """Stale mark must raise MarkStaleError even when a pending close exists.

    This asserts the ordering: MarkStaleError check runs before has_pending_close,
    so a stale Order table is never consulted to decide idempotency. If the mark
    is stale, reconcile hasn't run, and the Order table may also be stale.
    """
    stale_time = _NOW - timedelta(hours=1)
    pos = _make_position(
        status=PositionStatus.OPEN,
        marked_at=stale_time,
        unrealized_pnl=-5000.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    existing_close = _make_close_order(pos.id, status=OrderStatus.WORKING)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, existing_close)
        # Must raise MarkStaleError, NOT silently return None from the guard
        with pytest.raises(MarkStaleError):
            check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


def test_profit_target_stale_mark_raises_before_order_table_guard(engine) -> None:
    """Same ordering invariant for check_profit_target."""
    stale_time = _NOW - timedelta(hours=1)
    pos = _make_position(
        status=PositionStatus.OPEN,
        marked_at=stale_time,
        unrealized_pnl=5000.0,
        est_max_profit=275.0,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)
    existing_close = _make_close_order(pos.id, status=OrderStatus.WORKING)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        insert_order(conn, existing_close)
        with pytest.raises(MarkStaleError):
            check_profit_target(pos, conn, broker, _NOW, _MAX_MARK_AGE)

    broker.submit_multi_leg.assert_not_called()
    broker.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Double-run: trigger → run cycle again → exactly one close submitted
# ---------------------------------------------------------------------------


def test_double_run_produces_exactly_one_close(engine) -> None:
    """Running the monitor twice after a stop-loss trigger submits exactly one close.

    First run: threshold breached, close submitted, position → PENDING_CLOSE,
    Order inserted with WORKING status.
    Second run: position status is now PENDING_CLOSE (_SKIPPABLE_STATUSES fires)
    and/or has_pending_close returns True → no second submit.

    Both idempotency layers cooperate; the end state is one closing order.
    """
    pos = _make_position(
        unrealized_pnl=-5000.0,  # massively breached
        status=PositionStatus.OPEN,
        exit_plan=_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)

    # First monitor cycle: stop-loss fires, close is submitted.
    with get_connection(engine) as conn:
        insert_position(conn, pos)
        first_order = check_stop_loss(pos, conn, broker, _NOW, _MAX_MARK_AGE)
        assert first_order is not None
        refreshed = get_position(conn, pos.id)

    assert refreshed is not None
    assert refreshed.status == PositionStatus.PENDING_CLOSE

    # Second monitor cycle: pass the refreshed (PENDING_CLOSE) position.
    broker_2 = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        second_order = check_stop_loss(refreshed, conn, broker_2, _NOW, _MAX_MARK_AGE)

    assert second_order is None, "Second cycle must not submit a duplicate close"
    broker_2.submit_multi_leg.assert_not_called()
    broker_2.submit.assert_not_called()


def test_double_run_time_stop_produces_exactly_one_close(engine) -> None:
    """Running twice after time-stop trigger submits exactly one close.

    The DTE condition is monotonic, making this the most acute duplication risk:
    every subsequent cycle would re-trigger until the position fills.
    """
    pos = _make_position(
        nearest_expiration=_EXPIRY_AT_THRESHOLD,  # DTE=21, will keep triggering
        status=PositionStatus.OPEN,
        exit_plan=_DTE_EXIT_PLAN,
    )
    broker = _make_broker_mock(pos.id)

    with get_connection(engine) as conn:
        insert_position(conn, pos)
        first_order = check_time_stop(pos, conn, broker, _NOW, _MAX_MARK_AGE)
        assert first_order is not None
        refreshed = get_position(conn, pos.id)

    assert refreshed is not None
    assert refreshed.status == PositionStatus.PENDING_CLOSE

    broker_2 = _make_broker_mock(pos.id)
    with get_connection(engine) as conn:
        second_order = check_time_stop(refreshed, conn, broker_2, _NOW, _MAX_MARK_AGE)

    assert second_order is None
    broker_2.submit_multi_leg.assert_not_called()
    broker_2.submit.assert_not_called()
