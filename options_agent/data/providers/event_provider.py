"""EventProvider protocol — swappable event-data backend.

EventProvider is the seam between get_events() and the underlying data source
(yfinance v1, Polygon later). Consumers always depend on this Protocol, never
on YFinanceProvider directly, so swapping providers is a one-line injection
change rather than a cross-file refactor.

Implementations must never raise per-symbol — failures are expressed as
available=False so the caller handles each symbol independently without losing
data for healthy symbols.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from options_agent.contracts.data import EarningsEvent, ExDividendEvent


class RawEarningsResult(BaseModel):
    """Per-symbol result of an earnings fetch.

    event=None + available=True means the provider succeeded and found no
    earnings within the lookahead window.
    event=None + available=False means the fetch failed — None does NOT mean
    "no upcoming earnings." Callers must treat available=False as unknown and
    fail-closed rather than assuming it is safe to trade through earnings.
    """

    event: EarningsEvent | None
    available: bool


class RawDividendResult(BaseModel):
    """Per-symbol result of an ex-dividend fetch.

    Same available/None semantics as RawEarningsResult.
    """

    event: ExDividendEvent | None
    available: bool


@runtime_checkable
class EventProvider(Protocol):
    """Protocol for event-data backends.

    Both methods accept a list of symbols and return one result per symbol.
    Implementations must catch all per-symbol exceptions internally and return
    available=False rather than propagating them. Systemic failures (e.g., no
    network before any symbol is attempted) may raise, but per-symbol failures
    must always be expressed in the return value.
    """

    def fetch_earnings(
        self,
        symbols: list[str],
        lookahead_days: int,
        as_of: date | None = None,
    ) -> dict[str, RawEarningsResult]:
        """Return upcoming earnings events for each symbol within the window.

        as_of defaults to today when None. The lookahead window is
        [as_of, as_of + lookahead_days].
        """
        ...

    def fetch_dividends(
        self,
        symbols: list[str],
        lookahead_days: int,
        as_of: date | None = None,
    ) -> dict[str, RawDividendResult]:
        """Return upcoming ex-dividend events for each symbol within the window.

        Same window semantics as fetch_earnings.
        """
        ...
