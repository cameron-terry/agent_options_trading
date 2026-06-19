"""Hardcoded FOMC, CPI, and NFP release calendar.

This calendar is stamped with the year(s) it covers. When a lookahead window
extends beyond CALENDAR_COVERS_THROUGH, get_macro_events() logs a WARNING so
the system announces its own staleness rather than silently returning no events.

UPDATE THIS FILE EACH JANUARY:
- FOMC: Federal Reserve publishes the meeting schedule in November of the prior year.
  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- CPI: BLS publishes the release schedule in December of the prior year.
  https://www.bls.gov/schedule/news_release/cpi.htm
- NFP: BLS publishes the Employment Situation release schedule in December of the
  prior year. https://www.bls.gov/schedule/news_release/empsit.htm

Dates listed are the decision/release date (the day the number is published),
not the start of the meeting or reference period.
"""

from __future__ import annotations

from datetime import date

from options_agent.contracts.data import MacroEvent

# The last date covered by this calendar. get_macro_events() warns when a
# lookahead window extends beyond this date.
CALENDAR_COVERS_THROUGH = date(2026, 12, 31)

# ---------------------------------------------------------------------------
# 2026 release schedule
# ---------------------------------------------------------------------------
# Note: these dates reflect the published schedules as of late 2025.
# Verify against official sources when updating for 2027.

_FOMC_2026: list[MacroEvent] = [
    MacroEvent(name="FOMC Jan 2026", event_date=date(2026, 1, 28), event_type="FOMC"),
    MacroEvent(name="FOMC Mar 2026", event_date=date(2026, 3, 18), event_type="FOMC"),
    MacroEvent(name="FOMC Apr 2026", event_date=date(2026, 4, 29), event_type="FOMC"),
    MacroEvent(name="FOMC Jun 2026", event_date=date(2026, 6, 17), event_type="FOMC"),
    MacroEvent(name="FOMC Jul 2026", event_date=date(2026, 7, 29), event_type="FOMC"),
    MacroEvent(name="FOMC Sep 2026", event_date=date(2026, 9, 16), event_type="FOMC"),
    MacroEvent(name="FOMC Oct 2026", event_date=date(2026, 10, 28), event_type="FOMC"),
    MacroEvent(name="FOMC Dec 2026", event_date=date(2026, 12, 9), event_type="FOMC"),
]

_CPI_2026: list[MacroEvent] = [
    MacroEvent(
        name="CPI Jan 2026 (Dec data)", event_date=date(2026, 1, 15), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Feb 2026 (Jan data)", event_date=date(2026, 2, 12), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Mar 2026 (Feb data)", event_date=date(2026, 3, 11), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Apr 2026 (Mar data)", event_date=date(2026, 4, 10), event_type="CPI"
    ),
    MacroEvent(
        name="CPI May 2026 (Apr data)", event_date=date(2026, 5, 13), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Jun 2026 (May data)", event_date=date(2026, 6, 11), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Jul 2026 (Jun data)", event_date=date(2026, 7, 14), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Aug 2026 (Jul data)", event_date=date(2026, 8, 13), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Sep 2026 (Aug data)", event_date=date(2026, 9, 10), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Oct 2026 (Sep data)", event_date=date(2026, 10, 14), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Nov 2026 (Oct data)", event_date=date(2026, 11, 12), event_type="CPI"
    ),
    MacroEvent(
        name="CPI Dec 2026 (Nov data)", event_date=date(2026, 12, 11), event_type="CPI"
    ),
]

_NFP_2026: list[MacroEvent] = [
    MacroEvent(
        name="NFP Jan 2026 (Dec data)", event_date=date(2026, 1, 9), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Feb 2026 (Jan data)", event_date=date(2026, 2, 6), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Mar 2026 (Feb data)", event_date=date(2026, 3, 6), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Apr 2026 (Mar data)", event_date=date(2026, 4, 3), event_type="NFP"
    ),
    MacroEvent(
        name="NFP May 2026 (Apr data)", event_date=date(2026, 5, 8), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Jun 2026 (May data)", event_date=date(2026, 6, 5), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Jul 2026 (Jun data)", event_date=date(2026, 7, 2), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Aug 2026 (Jul data)", event_date=date(2026, 8, 7), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Sep 2026 (Aug data)", event_date=date(2026, 9, 4), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Oct 2026 (Sep data)", event_date=date(2026, 10, 2), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Nov 2026 (Oct data)", event_date=date(2026, 11, 6), event_type="NFP"
    ),
    MacroEvent(
        name="NFP Dec 2026 (Nov data)", event_date=date(2026, 12, 4), event_type="NFP"
    ),
]

_ALL_EVENTS: list[MacroEvent] = sorted(
    _FOMC_2026 + _CPI_2026 + _NFP_2026,
    key=lambda e: e.event_date,
)


def get_events_in_window(start: date, end: date) -> list[MacroEvent]:
    """Return all macro events with event_date in [start, end] inclusive."""
    return [e for e in _ALL_EVENTS if start <= e.event_date <= end]
