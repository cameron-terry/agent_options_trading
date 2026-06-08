"""Pre-flight gate functions for the entry cycle.

Gates are coarse-grained checks that abort the entry cycle before any
expensive operation (context assembly, LLM call). A failed gate is logged
as NO_ACTION (gated) with a ShortCircuitReason; the cycle stops at the
first failure.

Intended evaluation order (cheapest / no portfolio state first):
  1. market_is_open          — pure datetime + calendar, no portfolio needed
  2. within_blackout_window  — pure datetime + calendar, no portfolio needed
  3. has_buying_power        — requires PortfolioState
  4. under_position_cap      — requires PortfolioState

Stopping at first failure means "market closed" correctly dominates "no
buying power" — the latter is irrelevant when the exchange is shut.

All gate functions accept *now* as an explicit parameter so callers (and
tests) can inject any moment without touching system time.  *now* must be
timezone-aware; passing a naive datetime raises ValueError immediately.

Orchestrator injection contract (WP-8):
  The orchestrator is responsible for constructing the ExchangeCalendar once
  at startup and passing it into the temporal gates each cycle:

    import exchange_calendars as xcals
    calendar = xcals.get_calendar(config.exchange_calendar)  # once at startup
    market_is_open(now, calendar)
    within_blackout_window(now, calendar,
                           config.session_open_blackout_minutes,
                           config.session_close_blackout_minutes)
    has_buying_power(portfolio_state, config.limits)
    under_position_cap(portfolio_state, config.limits)
"""

from datetime import UTC, datetime
from typing import cast

import exchange_calendars as xcals
import pandas as pd

from options_agent.contracts.data import PortfolioState
from options_agent.risk.limits import Limits


def _to_utc(now: datetime) -> pd.Timestamp:
    """Convert *now* to a UTC pd.Timestamp, rejecting naive datetimes.

    A naive datetime fed to exchange_calendars is silently treated as UTC,
    which produces wrong results for callers operating in ET or any other
    local timezone.  Fail loudly instead.
    """
    if now.tzinfo is None:
        raise ValueError(
            "now must be timezone-aware; a naive datetime is silently "
            "misinterpreted by exchange_calendars and produces wrong results."
        )
    # cast: pd.Timestamp stubs widen the return to Timestamp | NaTType, but
    # constructing from a valid datetime never returns NaT.
    return cast(pd.Timestamp, pd.Timestamp(now.astimezone(UTC)))


def market_is_open(
    now: datetime,
    calendar: xcals.ExchangeCalendar,
) -> tuple[bool, str]:
    """Gate: is the exchange open at *now*?

    Handles regular close times, exchange holidays, and early-close days
    via the exchange_calendars calendar.  Returns (True, "") when open,
    (False, reason) otherwise.
    """
    ts = _to_utc(now)
    if not calendar.is_open_on_minute(ts.floor("min")):
        return False, "exchange is closed"
    return True, ""


def within_blackout_window(
    now: datetime,
    calendar: xcals.ExchangeCalendar,
    open_blackout_minutes: int,
    close_blackout_minutes: int,
) -> tuple[bool, str]:
    """Gate: is *now* outside the session open/close blackout windows?

    Rejects runs within *open_blackout_minutes* of session open or
    *close_blackout_minutes* of session close (wide spreads, auction risk).

    Boundary semantics: both boundaries are inclusive on the allowed side.
    A run exactly N minutes after open is permitted; a run exactly N minutes
    before close is also permitted. Only runs *strictly inside* the first or
    last N minutes are blocked.

      Allowed window: [session_open + N, session_close - N]  (both endpoints inclusive)

    Designed to be called after market_is_open() has already returned True.
    If called when the market is closed, returns (True, "") — the
    market_is_open gate owns that case.
    """
    ts = _to_utc(now)
    minute = ts.floor("min")

    if not calendar.is_open_on_minute(minute):
        return True, ""

    session_label = calendar.minute_to_session(minute)
    session_open = calendar.session_open(session_label)
    session_close = calendar.session_close(session_label)

    minutes_since_open = (ts - session_open).total_seconds() / 60
    minutes_to_close = (session_close - ts).total_seconds() / 60

    if minutes_since_open < open_blackout_minutes:
        return (
            False,
            f"within open blackout window ({minutes_since_open:.0f}m since open,"
            f" blackout is {open_blackout_minutes}m)",
        )
    if minutes_to_close < close_blackout_minutes:
        return (
            False,
            f"within close blackout window ({minutes_to_close:.0f}m to close,"
            f" blackout is {close_blackout_minutes}m)",
        )

    return True, ""


def has_buying_power(
    portfolio_state: PortfolioState,
    limits: Limits,
) -> tuple[bool, str]:
    """Gate: is options buying power above the configured floor?

    Compares options_buying_power (Alpaca's options-specific figure, the
    honest constraint for spreads) against min_buying_power_pct * equity.
    Returns ShortCircuitReason.NO_BUYING_POWER context via the reason string.
    """
    floor = limits.min_buying_power_pct * portfolio_state.account_equity
    if portfolio_state.options_buying_power < floor:
        return (
            False,
            f"options buying power ${portfolio_state.options_buying_power:.2f}"
            f" below floor ${floor:.2f}"
            f" ({limits.min_buying_power_pct:.0%} of"
            f" ${portfolio_state.account_equity:.2f} equity)",
        )
    return True, ""


def under_position_cap(
    portfolio_state: PortfolioState,
    limits: Limits,
) -> tuple[bool, str]:
    """Gate: is the open position count below the maximum?"""
    count = len(portfolio_state.positions)
    if count >= limits.max_open_positions:
        return (
            False,
            f"open position count {count} >= max {limits.max_open_positions}",
        )
    return True, ""
