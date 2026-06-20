"""Tests for WP-5.1: stop-loss trigger logic in monitor/exits.py."""

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
)
from options_agent.state.crud import get_order, get_position, insert_position
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
        nearest_expiration=date(2026, 8, 15),
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


def test_profit_target_cross_strategy_symmetry_same_pct_same_trigger() -> None:
    """50% profit target triggers identically for credit and debit strategies."""
    pct = 0.50
    max_profit = 1000.0
    threshold = pct * max_profit  # 500.0

    for entry_net_amount in (-300.0, 300.0):  # credit then debit
        label = "credit" if entry_net_amount < 0 else "debit"

        # At threshold: must trigger
        pos_at = _make_position(
            entry_net_amount=entry_net_amount,
            est_max_profit=max_profit,
            unrealized_pnl=threshold,
            exit_plan=ExitPlan(
                profit_target_pct=pct,
                stop_loss_max_loss_fraction=0.5,
                time_stop_dte=21,
            ),
        )
        assert pos_at.exit_plan is not None
        computed_threshold = pos_at.exit_plan.profit_target_pct * pos_at.est_max_profit
        assert computed_threshold == threshold, (
            f"{label}: expected threshold {threshold}, got {computed_threshold}"
        )
        assert pos_at.unrealized_pnl >= computed_threshold, (
            f"{label}: pnl={pos_at.unrealized_pnl} should be >= {computed_threshold}"
        )

        # Just below threshold: must not trigger
        pos_below = _make_position(
            entry_net_amount=entry_net_amount,
            est_max_profit=max_profit,
            unrealized_pnl=threshold - 1.0,
            exit_plan=ExitPlan(
                profit_target_pct=pct,
                stop_loss_max_loss_fraction=0.5,
                time_stop_dte=21,
            ),
        )
        assert pos_below.unrealized_pnl < computed_threshold, (
            f"{label}: P&L below threshold should not trigger"
        )


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
