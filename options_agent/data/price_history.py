"""Daily-bar trend summary for the agent's directional thesis (get_price_history).

The system prompt's reasoning method asks the agent to form a directional bias
from "price, technicals, and regime context" — but until this module existed no
tool supplied anything beyond a spot price and IV rank. summarize_daily_bars()
turns a year of split-adjusted daily bars into a compact indicator set (SMA
posture, 52-week range position, ATR, multi-horizon returns) so the bias is
grounded in trend data instead of guessed.

Pure computation lives in summarize_daily_bars() (unit-testable without a
provider); get_price_history() is the fetch-and-summarize wrapper injected as
the agent tool implementation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from options_agent.contracts.data import PriceHistorySummary
from options_agent.data.providers import DailyBar, DataProvider

logger = logging.getLogger(__name__)

# Trading sessions in a 52-week window.
_SESSIONS_52W = 252


def _pct(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return round((numerator / denominator) * 100, 2)


def summarize_daily_bars(
    symbol: str,
    bars: list[DailyBar],
    as_of: datetime | None = None,
) -> PriceHistorySummary | None:
    """Reduce *bars* (oldest first) to a PriceHistorySummary.

    Returns None when no bars are available at all. Individual indicators are
    None when the window they need exceeds the available history; the caller
    can see how much history backed the summary via bars_available.
    """
    if not bars:
        return None

    closes = [b.close for b in bars]
    n = len(closes)
    price = closes[-1]

    def _sma(window: int) -> float | None:
        if n < window:
            return None
        return round(sum(closes[-window:]) / window, 4)

    sma_20 = _sma(20)
    sma_50 = _sma(50)

    window_52w = bars[-_SESSIONS_52W:]
    high_52w = round(max(b.high for b in window_52w), 4)
    low_52w = round(min(b.low for b in window_52w), 4)

    # ATR-14: mean true range over the last 14 sessions.
    atr_14: float | None = None
    if n >= 15:
        true_ranges = []
        for prev, cur in zip(bars[-15:-1], bars[-14:]):
            true_ranges.append(
                max(
                    cur.high - cur.low,
                    abs(cur.high - prev.close),
                    abs(cur.low - prev.close),
                )
            )
        atr_14 = round(sum(true_ranges) / len(true_ranges), 4)

    def _trailing_return(sessions: int) -> float | None:
        if n < sessions + 1:
            return None
        return _pct(price - closes[-(sessions + 1)], closes[-(sessions + 1)])

    return PriceHistorySummary(
        symbol=symbol,
        as_of=as_of if as_of is not None else datetime.now(UTC),
        price=round(price, 4),
        sma_20=sma_20,
        sma_50=sma_50,
        price_vs_sma_20_pct=_pct(price - sma_20, sma_20) if sma_20 else None,
        price_vs_sma_50_pct=_pct(price - sma_50, sma_50) if sma_50 else None,
        high_52w=high_52w,
        low_52w=low_52w,
        pct_from_52w_high=_pct(price - high_52w, high_52w),
        pct_from_52w_low=_pct(price - low_52w, low_52w),
        atr_14=atr_14,
        atr_14_pct=_pct(atr_14, price) if atr_14 is not None else None,
        return_5d_pct=_trailing_return(5),
        return_21d_pct=_trailing_return(21),
        return_63d_pct=_trailing_return(63),
        recent_closes=[round(c, 2) for c in closes[-10:]],
        bars_available=n,
    )


def get_price_history(
    symbol: str,
    provider: DataProvider,
    lookback_days: int = 380,
) -> PriceHistorySummary | None:
    """Fetch daily bars for *symbol* and summarize them.

    Returns None when the provider has no bars (or the fetch fails after
    retries) — the agent tool surfaces that as "no history available" and the
    agent must not fabricate a trend view.
    """
    bars = provider.fetch_daily_bars(symbol, lookback_days)
    return summarize_daily_bars(symbol, bars)
