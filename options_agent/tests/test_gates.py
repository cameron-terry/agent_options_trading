"""Tests for risk/gates.py — WP-4.2: pre-flight gate functions.

All four gates are exercised with both passing and failing cases.
Tests use fixed historical NYSE dates so results are calendar-deterministic
and never require a network connection.

Calendar facts used:
  - 2024-03-15 (Friday): regular session, open 13:30–20:00 UTC (9:30 AM–4:00 PM ET)
  - 2024-11-29 (Friday): NYSE early close at 18:00 UTC (1:00 PM ET)
  - 2024-12-25 (Wednesday): NYSE holiday (Christmas)
  - 2024-03-16 (Saturday): weekend, no session
"""

from datetime import UTC, date, datetime

import exchange_calendars as xcals
import pytest

from options_agent.contracts.data import PortfolioState
from options_agent.contracts.proposal import ExitPlan, Leg
from options_agent.contracts.state import (
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.risk.gates import (
    has_buying_power,
    market_is_open,
    under_position_cap,
    within_blackout_window,
)
from options_agent.risk.limits import Limits

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

XNYS = xcals.get_calendar("XNYS")

# 2024-03-15 regular session open at 13:30 UTC, close at 20:00 UTC
REGULAR_DAY_MIDDAY = datetime(2024, 3, 15, 19, 0, tzinfo=UTC)  # 3:00 PM ET
REGULAR_DAY_JUST_OPENED = datetime(2024, 3, 15, 13, 35, tzinfo=UTC)  # 9:35 AM ET
REGULAR_DAY_NEAR_CLOSE = datetime(2024, 3, 15, 19, 45, tzinfo=UTC)  # 3:45 PM ET
REGULAR_DAY_BEFORE_OPEN = datetime(2024, 3, 15, 12, 0, tzinfo=UTC)  # 8:00 AM ET
REGULAR_DAY_AFTER_CLOSE = datetime(2024, 3, 15, 21, 0, tzinfo=UTC)  # 5:00 PM ET

# 2024-11-29 early close at 18:00 UTC (1:00 PM ET)
HALF_DAY_BEFORE_CLOSE = datetime(2024, 11, 29, 17, 30, tzinfo=UTC)  # 12:30 PM ET
HALF_DAY_AFTER_CLOSE = datetime(2024, 11, 29, 18, 1, tzinfo=UTC)  # 1:01 PM ET

# Holiday and weekend
CHRISTMAS_2024 = datetime(2024, 12, 25, 19, 0, tzinfo=UTC)
SATURDAY = datetime(2024, 3, 16, 19, 0, tzinfo=UTC)


def _make_portfolio(
    *,
    account_equity: float = 100_000.0,
    options_buying_power: float = 50_000.0,
    position_count: int = 0,
) -> PortfolioState:
    """Minimal PortfolioState for gate tests."""
    positions = [_stub_position(str(i)) for i in range(position_count)]
    return PortfolioState(
        positions=positions,
        account_equity=account_equity,
        buying_power=options_buying_power,
        options_buying_power=options_buying_power,
        unrealized_pnl=0.0,
        realized_pnl_today=0.0,
        approval_level=2,
        net_dollar_delta=0.0,
        net_dollar_gamma=0.0,
        net_dollar_theta=0.0,
        net_dollar_vega=0.0,
    )


def _stub_position(position_id: str) -> Position:
    """Minimal Position used only to inflate position_count."""
    leg = Leg(right="call", side="buy", strike=100.0, expiration=date(2025, 1, 17))
    return Position(
        id=position_id,
        underlying="SPY",
        strategy="bull_call_spread",
        legs=[
            PositionLeg(
                leg=leg,
                filled_qty=1,
                avg_fill_price=1.00,
                status=LegStatus.OPEN,
            )
        ],
        quantity=1,
        entry_net_amount=1.00,
        current_mark=1.10,
        marked_at=datetime(2024, 3, 15, 19, 0, tzinfo=UTC),
        unrealized_pnl=10.0,
        realized_pnl=None,
        exit_plan=ExitPlan(profit_target_pct=0.5, stop_loss_mult=2.0, time_stop_dte=21),
        status=PositionStatus.OPEN,
        opened_at=datetime(2024, 3, 15, 15, 0, tzinfo=UTC),
        closed_at=None,
        nearest_expiration=date(2025, 1, 17),
        est_max_loss=100.0,
        est_max_profit=150.0,
        opening_order_id="ord-001",
    )


# ---------------------------------------------------------------------------
# market_is_open
# ---------------------------------------------------------------------------


def test_market_is_open_during_regular_session() -> None:
    passed, reason = market_is_open(REGULAR_DAY_MIDDAY, XNYS)
    assert passed is True
    assert reason == ""


def test_market_is_open_before_open() -> None:
    passed, reason = market_is_open(REGULAR_DAY_BEFORE_OPEN, XNYS)
    assert passed is False
    assert reason != ""


def test_market_is_open_after_close() -> None:
    passed, reason = market_is_open(REGULAR_DAY_AFTER_CLOSE, XNYS)
    assert passed is False
    assert reason != ""


def test_market_is_open_holiday() -> None:
    passed, reason = market_is_open(CHRISTMAS_2024, XNYS)
    assert passed is False
    assert reason != ""


def test_market_is_open_weekend() -> None:
    passed, reason = market_is_open(SATURDAY, XNYS)
    assert passed is False
    assert reason != ""


def test_market_is_open_half_day_before_early_close() -> None:
    """2:00 PM ET assertion: still open on a half-day."""
    passed, _ = market_is_open(HALF_DAY_BEFORE_CLOSE, XNYS)
    assert passed is True


def test_market_is_open_half_day_after_early_close() -> None:
    """1:01 PM ET on Black Friday 2024 — early close at 1:00 PM ET."""
    passed, reason = market_is_open(HALF_DAY_AFTER_CLOSE, XNYS)
    assert passed is False
    assert reason != ""


def test_market_is_open_rejects_naive_datetime() -> None:
    naive = datetime(2024, 3, 15, 19, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        market_is_open(naive, XNYS)


# ---------------------------------------------------------------------------
# within_blackout_window
# ---------------------------------------------------------------------------


def test_within_blackout_window_safe_midday() -> None:
    """3:00 PM ET is well outside both 30-min open/close windows."""
    passed, reason = within_blackout_window(REGULAR_DAY_MIDDAY, XNYS, 30, 30)
    assert passed is True
    assert reason == ""


def test_within_blackout_window_just_after_open() -> None:
    """9:35 AM ET is within the 30-min open blackout (5 min since open)."""
    passed, reason = within_blackout_window(REGULAR_DAY_JUST_OPENED, XNYS, 30, 30)
    assert passed is False
    assert "open blackout" in reason


def test_within_blackout_window_near_close() -> None:
    """3:45 PM ET is within the 30-min close blackout (15 min to close)."""
    passed, reason = within_blackout_window(REGULAR_DAY_NEAR_CLOSE, XNYS, 30, 30)
    assert passed is False
    assert "close blackout" in reason


def test_within_blackout_window_exactly_at_boundary_passes() -> None:
    """Exactly 30 min after open should clear the open blackout."""
    exactly_at_boundary = datetime(2024, 3, 15, 14, 0, tzinfo=UTC)  # 10:00 AM ET
    passed, _ = within_blackout_window(exactly_at_boundary, XNYS, 30, 30)
    assert passed is True


def test_within_blackout_window_exactly_at_close_boundary_passes() -> None:
    """Exactly 30 min before close clears the close blackout (symmetric with open)."""
    # 3:30 PM ET = 19:30 UTC, exactly 30 min before 4:00 PM ET close (20:00 UTC)
    exactly_at_close_boundary = datetime(2024, 3, 15, 19, 30, tzinfo=UTC)
    passed, _ = within_blackout_window(exactly_at_close_boundary, XNYS, 30, 30)
    assert passed is True


def test_within_blackout_window_one_minute_inside_close_blackout() -> None:
    """29 min before close is inside the 30-min close blackout."""
    # 3:31 PM ET = 19:31 UTC, 29 min before close
    one_inside = datetime(2024, 3, 15, 19, 31, tzinfo=UTC)
    passed, reason = within_blackout_window(one_inside, XNYS, 30, 30)
    assert passed is False
    assert "close blackout" in reason


def test_within_blackout_window_zero_blackout_passes_during_session() -> None:
    """With blackout=0, any in-session minute should pass."""
    passed, _ = within_blackout_window(REGULAR_DAY_JUST_OPENED, XNYS, 0, 0)
    assert passed is True


def test_within_blackout_window_market_closed_returns_true() -> None:
    """Returns (True, "") when market is closed — market_is_open gate owns that case."""
    passed, reason = within_blackout_window(REGULAR_DAY_AFTER_CLOSE, XNYS, 30, 30)
    assert passed is True
    assert reason == ""


def test_within_blackout_window_rejects_naive_datetime() -> None:
    naive = datetime(2024, 3, 15, 19, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        within_blackout_window(naive, XNYS, 30, 30)


# ---------------------------------------------------------------------------
# has_buying_power
# ---------------------------------------------------------------------------


def test_has_buying_power_above_floor() -> None:
    portfolio = _make_portfolio(account_equity=100_000.0, options_buying_power=50_000.0)
    limits = Limits(min_buying_power_pct=0.10)  # floor = $10,000
    passed, reason = has_buying_power(portfolio, limits)
    assert passed is True
    assert reason == ""


def test_has_buying_power_exactly_at_floor_passes() -> None:
    portfolio = _make_portfolio(account_equity=100_000.0, options_buying_power=10_000.0)
    limits = Limits(min_buying_power_pct=0.10)  # floor = $10,000 exactly
    passed, _ = has_buying_power(portfolio, limits)
    assert passed is True


def test_has_buying_power_below_floor() -> None:
    portfolio = _make_portfolio(account_equity=100_000.0, options_buying_power=5_000.0)
    limits = Limits(min_buying_power_pct=0.10)  # floor = $10,000
    passed, reason = has_buying_power(portfolio, limits)
    assert passed is False
    assert "5000" in reason or "5,000" in reason


def test_has_buying_power_zero_buying_power() -> None:
    portfolio = _make_portfolio(account_equity=100_000.0, options_buying_power=0.0)
    limits = Limits(min_buying_power_pct=0.10)
    passed, reason = has_buying_power(portfolio, limits)
    assert passed is False
    assert reason != ""


def test_has_buying_power_small_account_scales_floor() -> None:
    """Floor is equity-relative, not absolute."""
    portfolio = _make_portfolio(account_equity=10_000.0, options_buying_power=900.0)
    limits = Limits(min_buying_power_pct=0.10)  # floor = $1,000
    passed, _ = has_buying_power(portfolio, limits)
    assert passed is False


# ---------------------------------------------------------------------------
# under_position_cap
# ---------------------------------------------------------------------------


def test_under_position_cap_no_positions() -> None:
    portfolio = _make_portfolio(position_count=0)
    limits = Limits(max_open_positions=5)
    passed, reason = under_position_cap(portfolio, limits)
    assert passed is True
    assert reason == ""


def test_under_position_cap_one_below_max() -> None:
    portfolio = _make_portfolio(position_count=4)
    limits = Limits(max_open_positions=5)
    passed, _ = under_position_cap(portfolio, limits)
    assert passed is True


def test_under_position_cap_exactly_at_max_fails() -> None:
    portfolio = _make_portfolio(position_count=5)
    limits = Limits(max_open_positions=5)
    passed, reason = under_position_cap(portfolio, limits)
    assert passed is False
    assert "5" in reason


def test_under_position_cap_over_max() -> None:
    portfolio = _make_portfolio(position_count=7)
    limits = Limits(max_open_positions=5)
    passed, reason = under_position_cap(portfolio, limits)
    assert passed is False
    assert reason != ""


def test_under_position_cap_max_of_one() -> None:
    """Edge case: max_open_positions=1 with one open position."""
    portfolio = _make_portfolio(position_count=1)
    limits = Limits(max_open_positions=1)
    passed, _ = under_position_cap(portfolio, limits)
    assert passed is False
