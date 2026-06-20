"""VolatilityIndexProvider protocol — swappable VIX/vol-index data backend.

VolatilityIndexProvider is the seam between data/market.py and the underlying
data source for the CBOE Volatility Index (VIX). Consumers always depend on
this Protocol, never on YFinanceVolatilityProvider directly, so swapping to
Alpaca (if index data becomes available) or Polygon is a one-line injection
change.

The v1 implementation is YFinanceVolatilityProvider (yfinance ^VIX). yfinance
is already a project dependency (events.py) and requires no additional API key.
Alpaca's standard equity/options data plans do not include index quotes, so
yfinance covers the PoC at no extra credential cost. Polygon is the natural
upgrade path when a paid index-data feed is needed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class VixFetchResult(BaseModel):
    """Result of a single VIX fetch attempt.

    level=None + available=False means the fetch failed — callers must not
    treat it as "VIX is zero." Failure causes market_regime=UNKNOWN on the
    returned UniverseSnapshot; the cycle continues with degraded regime context
    rather than aborting.

    level=None + available=True should not occur in practice (a successful
    fetch always returns a numeric level), but callers must handle it the same
    way as available=False.
    """

    level: float | None
    available: bool


@runtime_checkable
class VolatilityIndexProvider(Protocol):
    """Protocol for VIX / volatility-index data backends.

    fetch_vix must never raise per-implementation — failures are expressed as
    available=False so the caller can degrade gracefully without aborting the
    cycle. Systemic failures (e.g., no network before any attempt) may raise.
    """

    def fetch_vix(self) -> VixFetchResult:
        """Return the latest VIX level.

        A successful call returns VixFetchResult(level=<float>, available=True).
        A failed call returns VixFetchResult(level=None, available=False).
        """
        ...
