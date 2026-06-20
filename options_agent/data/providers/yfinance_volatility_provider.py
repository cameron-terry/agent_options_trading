"""YFinance implementation of VolatilityIndexProvider.

Fetches the CBOE Volatility Index (VIX) via yfinance's ^VIX ticker.
yfinance is already a project dependency (used by events.py) and requires
no additional API key. Index entitlement spike (2026-06-19) confirmed that
fast_info.last_price on ^VIX returns the current VIX level reliably.

To swap to a different provider (Alpaca index data, Polygon, CBOE direct),
implement the VolatilityIndexProvider protocol in a new class and inject it
in place of YFinanceVolatilityProvider. No other file needs to change.
"""

from __future__ import annotations

import logging

import yfinance as yf

from options_agent.data.providers.volatility_provider import VixFetchResult

logger = logging.getLogger(__name__)

_VIX_TICKER = "^VIX"


class YFinanceVolatilityProvider:
    """VolatilityIndexProvider backed by yfinance ^VIX.

    Construct once per process; stateless between calls (no cycle cache
    needed — VIX is fetched fresh each time fetch_vix() is called).

    fetch_vix() first tries fast_info.last_price (single lightweight call),
    then falls back to ticker.history(period="1d") if fast_info returns None
    (e.g., outside market hours when last_price may not be populated).
    """

    def fetch_vix(self) -> VixFetchResult:
        try:
            ticker = yf.Ticker(_VIX_TICKER)

            # Primary: fast_info.last_price — low-latency, no full history fetch.
            level = ticker.fast_info.last_price
            if level is not None:
                return VixFetchResult(level=float(level), available=True)

            # Fallback: trailing 1-day bar close — available outside market hours.
            hist = ticker.history(period="1d")
            if not hist.empty:
                return VixFetchResult(
                    level=float(hist["Close"].iloc[-1]), available=True
                )

            logger.warning(
                "yfinance returned no VIX data for %s — marking unavailable",
                _VIX_TICKER,
            )
            return VixFetchResult(level=None, available=False)

        except Exception as exc:
            logger.warning(
                "yfinance VIX fetch failed for %s: %s — marking unavailable",
                _VIX_TICKER,
                exc,
            )
            return VixFetchResult(level=None, available=False)
