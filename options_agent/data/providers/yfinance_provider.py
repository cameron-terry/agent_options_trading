"""YFinance implementation of EventProvider.

yfinance scrapes Yahoo Finance; it is free and requires no API key but is
scraping-adjacent and may break when Yahoo changes its data format. Per-symbol
failures are caught and expressed as available=False so callers fail-closed
rather than treating a scrape failure as "no events."

To swap to a different provider (Polygon, Intrinio, etc.), implement the
EventProvider protocol in a new class and inject it in place of YFinanceProvider.
No other file needs to change.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import yfinance as yf

from options_agent.contracts.data import EarningsEvent, ExDividendEvent
from options_agent.data.providers.event_provider import (
    RawDividendResult,
    RawEarningsResult,
)

logger = logging.getLogger(__name__)


class YFinanceProvider:
    """EventProvider backed by yfinance.

    Construct once; the class holds no state between calls (no cycle cache needed
    — event data is fetched per get_events() call, not per cycle).
    """

    # ---------------------------------------------------------------------------
    # Earnings
    # ---------------------------------------------------------------------------

    def fetch_earnings(
        self,
        symbols: list[str],
        lookahead_days: int,
        as_of: date | None = None,
    ) -> dict[str, RawEarningsResult]:
        today = as_of or date.today()
        cutoff = today + timedelta(days=lookahead_days)
        return {s: self._fetch_earnings_one(s, today, cutoff) for s in symbols}

    def _fetch_earnings_one(
        self, symbol: str, today: date, cutoff: date
    ) -> RawEarningsResult:
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.earnings_dates
            if df is None or df.empty:
                return RawEarningsResult(event=None, available=True)

            for dt_idx in df.index:
                event_date = self._to_date(dt_idx)
                if event_date is None:
                    continue
                if event_date < today or event_date > cutoff:
                    continue
                # yfinance does not reliably distinguish confirmed vs. estimated
                # for future earnings dates. Mark all as unconfirmed to avoid
                # false confidence — a spread blacked out for an estimated date
                # that later moves is a different risk posture than a confirmed one.
                return RawEarningsResult(
                    event=EarningsEvent(event_date=event_date, confirmed=False),
                    available=True,
                )

            return RawEarningsResult(event=None, available=True)

        except Exception as exc:
            logger.warning(
                "yfinance earnings fetch failed for %s: %s"
                " — marking data_available=False",
                symbol,
                exc,
            )
            return RawEarningsResult(event=None, available=False)

    # ---------------------------------------------------------------------------
    # Ex-dividends
    # ---------------------------------------------------------------------------

    def fetch_dividends(
        self,
        symbols: list[str],
        lookahead_days: int,
        as_of: date | None = None,
    ) -> dict[str, RawDividendResult]:
        today = as_of or date.today()
        cutoff = today + timedelta(days=lookahead_days)
        return {s: self._fetch_dividends_one(s, today, cutoff) for s in symbols}

    def _fetch_dividends_one(
        self, symbol: str, today: date, cutoff: date
    ) -> RawDividendResult:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info

            ex_div_ts = info.get("exDividendDate")
            if not ex_div_ts:
                return RawDividendResult(event=None, available=True)

            ex_div_date = datetime.fromtimestamp(float(ex_div_ts), tz=UTC).date()
            if ex_div_date < today or ex_div_date > cutoff:
                return RawDividendResult(event=None, available=True)

            amount = float(
                info.get("dividendRate") or info.get("lastDividendValue") or 0.0
            )
            return RawDividendResult(
                event=ExDividendEvent(event_date=ex_div_date, amount=amount),
                available=True,
            )

        except Exception as exc:
            logger.warning(
                "yfinance dividend fetch failed for %s: %s"
                " — marking data_available=False",
                symbol,
                exc,
            )
            return RawDividendResult(event=None, available=False)

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _to_date(dt_idx: object) -> date | None:
        """Convert a pandas Timestamp index entry to a plain date."""
        if hasattr(dt_idx, "date"):
            return dt_idx.date()  # type: ignore[union-attr]
        if hasattr(dt_idx, "to_pydatetime"):
            return dt_idx.to_pydatetime().date()  # type: ignore[union-attr]
        return None
