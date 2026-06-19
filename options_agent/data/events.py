"""Events calendar — get_events() and get_macro_events().

get_events() fetches per-symbol earnings and ex-dividend events via an
injected EventProvider (YFinanceProvider by default). Consumers never
depend on yfinance directly; swapping providers is a one-line change.

get_macro_events() returns market-wide scheduled releases (FOMC, CPI, NFP)
from a hardcoded annual calendar. Macro events are advisory — they surface
as context for the agent and WP-4, but are not a hard validator block (unlike
the per-symbol earnings blackout, which WP-4 enforces as a hard rule).

Staleness handling: a fetch failure is expressed as data_available=False on
the returned EventInfo. Callers (WP-4) must fail-closed on data_available=False
rather than treating missing data as "no events." See EventInfo docstring for
the full fail-closed contract.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from options_agent.contracts.data import EventInfo, MacroEvent
from options_agent.data.macro_calendar import (
    CALENDAR_COVERS_THROUGH,
    get_events_in_window,
)
from options_agent.data.providers.event_provider import EventProvider

logger = logging.getLogger(__name__)


def get_events(
    symbols: list[str],
    lookahead_days: int,
    provider: EventProvider,
    as_of: date | None = None,
) -> dict[str, EventInfo]:
    """Return upcoming earnings and ex-dividend events for each symbol.

    Calls provider.fetch_earnings and provider.fetch_dividends then assembles
    one EventInfo per symbol. data_available=False on a result means the
    provider failed for that symbol — callers must not treat it as "no events."

    as_of defaults to today. lookahead_days defines the forward window.
    """
    today = as_of or date.today()

    earnings_results = provider.fetch_earnings(symbols, lookahead_days, as_of=today)
    dividend_results = provider.fetch_dividends(symbols, lookahead_days, as_of=today)

    result: dict[str, EventInfo] = {}
    for symbol in symbols:
        er = earnings_results.get(symbol)
        dr = dividend_results.get(symbol)

        earnings_ok = er is not None and er.available
        dividends_ok = dr is not None and dr.available

        result[symbol] = EventInfo(
            symbol=symbol,
            earnings=er.event if er is not None else None,
            ex_dividend=dr.event if dr is not None else None,
            data_available=earnings_ok and dividends_ok,
        )

    return result


def get_macro_events(
    lookahead_days: int,
    as_of: date | None = None,
) -> list[MacroEvent]:
    """Return FOMC, CPI, and NFP events within the lookahead window.

    Events come from a hardcoded annual calendar (data/macro_calendar.py).
    If the forward window extends beyond CALENDAR_COVERS_THROUGH, a WARNING
    is logged — the calendar announces its own staleness so a stale file cannot
    silently return an empty event list.

    Macro events are advisory: surface them in context so the agent and WP-4
    can reason about them, but do not use them as a hard validator block.
    """
    today = as_of or date.today()
    cutoff = today + timedelta(days=lookahead_days)

    if cutoff > CALENDAR_COVERS_THROUGH:
        logger.warning(
            "Macro calendar only covers through %s but lookahead extends to %s. "
            "Update options_agent/data/macro_calendar.py with the next year's "
            "FOMC/CPI/NFP release schedule.",
            CALENDAR_COVERS_THROUGH.isoformat(),
            cutoff.isoformat(),
        )

    return get_events_in_window(today, cutoff)
