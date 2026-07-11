"""Alpaca implementation of the DataProvider Protocol.

Usage (at process startup):
    client = AlpacaDataClient()  # reads ALPACA_API_KEY / ALPACA_SECRET_KEY from env

Usage (at the top of every cycle):
    client.begin_cycle()         # clears the within-cycle cache

Usage (within a cycle):
    contracts = client.fetch_option_chain("SPY")
    price     = client.fetch_latest_price("SPY")

Sequential-execution invariant: this client is NOT thread-safe. The entry
and monitor loops must never overlap. See DataProvider docstring for context.
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast

from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.snapshots import OptionsSnapshot
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockLatestBarRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from options_agent.data.providers import (
    DailyBar,
    DataAuthError,
    DataUnavailableError,
    RawOptionContract,
)

logger = logging.getLogger(__name__)

# Base delays (seconds) between retry attempts.
# 3 total attempts → 2 inter-attempt delays.
# Actual sleep = base ± 50% jitter.
_BACKOFF_BASE: list[float] = [1.0, 2.0]


def _parse_occ_symbol(
    symbol: str,
    underlying: str,
) -> tuple[date, Literal["call", "put"], float]:
    """Parse an OCC option symbol into (expiration, right, strike).

    OCC format: {UNDERLYING}{YYMMDD}{C|P}{STRIKE_x1000_8_DIGITS}

    Examples:
        "SPY260718P00450000" → (date(2026, 7, 18), "put", 450.0)
        "AAPL260718C00185000" → (date(2026, 7, 18), "call", 185.0)
    """
    suffix = symbol[len(underlying) :]
    year = 2000 + int(suffix[0:2])
    month = int(suffix[2:4])
    day = int(suffix[4:6])
    right_char = suffix[6]
    strike_thousandths = int(suffix[7:])

    exp = date(year, month, day)
    right: Literal["call", "put"] = "put" if right_char == "P" else "call"
    strike = strike_thousandths / 1000.0
    return exp, right, strike


def _snapshot_to_raw(snap: OptionsSnapshot, underlying: str) -> RawOptionContract:
    """Translate an Alpaca OptionsSnapshot into a provider-agnostic raw contract."""
    exp, right, strike = _parse_occ_symbol(snap.symbol, underlying)

    quote = snap.latest_quote
    bid_raw = quote.bid_price if quote is not None else None
    ask_raw = quote.ask_price if quote is not None else None
    bid = float(bid_raw) if bid_raw is not None else None
    ask = float(ask_raw) if ask_raw is not None else None

    greeks = snap.greeks

    return RawOptionContract(
        symbol=snap.symbol,
        underlying=underlying,
        strike=strike,
        expiration=exp,
        right=right,
        bid=bid,
        ask=ask,
        # Alpaca options-snapshot endpoint does not return cumulative volume or
        # open interest. WP-3.2 must handle None for both fields.
        volume=None,
        open_interest=None,
        implied_volatility=snap.implied_volatility,
        delta=greeks.delta if greeks is not None else None,
        gamma=greeks.gamma if greeks is not None else None,
        theta=greeks.theta if greeks is not None else None,
        vega=greeks.vega if greeks is not None else None,
        rho=greeks.rho if greeks is not None else None,
    )


class AlpacaDataClient:
    """Alpaca market-data adapter implementing the DataProvider Protocol.

    Credentials (ALPACA_API_KEY, ALPACA_SECRET_KEY) are read from the
    environment at construction time and never stored or logged.

    Auth is lazy: the SDK clients do not validate credentials at construction.
    A 401 from the first API call raises DataAuthError.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")

        missing = [
            name
            for name, val in (
                ("ALPACA_API_KEY", api_key),
                ("ALPACA_SECRET_KEY", secret_key),
            )
            if not val
        ]
        if missing:
            raise OSError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY before constructing "
                "AlpacaDataClient."
            )

        self._option_client = OptionHistoricalDataClient(api_key, secret_key)
        self._stock_client = StockHistoricalDataClient(api_key, secret_key)
        # Within-cycle dedup cache. Keyed on full (method, *args) tuple.
        # Cleared by begin_cycle() at the top of each run_*_cycle().
        self._cache: dict[tuple[Any, ...], Any] = {}

    def begin_cycle(self) -> None:
        """Clear the within-cycle cache.

        Call this at the top of every run_entry_cycle() and run_monitor_cycle()
        before any data fetch. All fetches in the new cycle will see fresh data.
        """
        self._cache.clear()

    def fetch_option_chain(self, symbol: str) -> list[RawOptionContract]:
        """Return all option contracts for the given underlying.

        Results are cached within the cycle. Call begin_cycle() before each
        cycle so that successive calls for the same symbol within one cycle
        share the same snapshot (consistent Greeks, IV, bid/ask).
        """
        cache_key = ("fetch_option_chain", symbol)
        if cache_key in self._cache:
            return self._cache[cache_key]  # type: ignore[return-value]

        def _call() -> dict[str, OptionsSnapshot]:
            return self._option_client.get_option_chain(  # type: ignore[return-value]
                OptionChainRequest(underlying_symbol=symbol)
            )

        raw: dict[str, OptionsSnapshot] = self._with_retry(
            _call, "fetch_option_chain", symbol
        )
        result = [_snapshot_to_raw(snap, symbol) for snap in raw.values()]
        self._cache[cache_key] = result
        return result

    def fetch_latest_price(self, symbol: str) -> float:
        """Return the latest bar close price for the underlying equity."""
        cache_key = ("fetch_latest_price", symbol)
        if cache_key in self._cache:
            return self._cache[cache_key]  # type: ignore[return-value]

        def _call() -> Any:
            return self._stock_client.get_stock_latest_bar(
                StockLatestBarRequest(symbol_or_symbols=symbol)
            )

        raw = self._with_retry(_call, "fetch_latest_price", symbol)
        price = float(raw[symbol].close)
        self._cache[cache_key] = price
        return price

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 380) -> list[DailyBar]:
        """Return split-adjusted daily bars for *symbol*, oldest first.

        380 calendar days of lookback yields ~252 trading sessions — enough
        for 52-week range and 50-day SMA computation with headroom for
        holidays. Cached within the cycle like all other fetches.
        """
        cache_key = ("fetch_daily_bars", symbol, lookback_days)
        if cache_key in self._cache:
            return self._cache[cache_key]  # type: ignore[return-value]

        start = datetime.now(UTC) - timedelta(days=lookback_days)

        def _call() -> Any:
            return self._stock_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    # Constructed explicitly — TimeFrame.Day is a classproperty
                    # that pyright cannot type through (and TimeFrameUnit
                    # members type as plain str in alpaca-py's stubs).
                    timeframe=TimeFrame(1, cast(TimeFrameUnit, TimeFrameUnit.Day)),
                    start=start,
                    adjustment=Adjustment.SPLIT,
                )
            )

        raw = self._with_retry(_call, "fetch_daily_bars", symbol)
        bars = raw[symbol] if symbol in raw.data else []
        result = [
            DailyBar(
                day=bar.timestamp.date(),
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume) if bar.volume is not None else None,
            )
            for bar in bars
        ]
        result.sort(key=lambda b: b.day)
        self._cache[cache_key] = result
        return result

    def _with_retry(
        self,
        fn: Any,
        operation: str,
        symbol: str,
    ) -> Any:
        """Call fn() with exponential backoff on 429; raise after 3 total attempts.

        - 401 → DataAuthError immediately (no retry).
        - 429 → sleep with jitter and retry; DataUnavailableError after 3 attempts.
        - Other APIError → DataUnavailableError immediately.
        """
        last_exc: APIError | None = None
        for attempt in range(3):
            try:
                return fn()
            except APIError as exc:
                code = exc.status_code
                if code == 401:
                    logger.error(
                        "Auth failure (401) on %s(%r); check credentials "
                        "(key values withheld from log).",
                        operation,
                        symbol,
                    )
                    raise DataAuthError(
                        f"Alpaca rejected credentials (401) during "
                        f"{operation}({symbol!r})"
                    ) from exc
                if code == 429:
                    last_exc = exc
                    if attempt < len(_BACKOFF_BASE):
                        base = _BACKOFF_BASE[attempt]
                        jitter = random.uniform(-base * 0.5, base * 0.5)
                        delay = max(0.0, base + jitter)
                        logger.warning(
                            "Rate-limit (429) on %s(%r) attempt %d/%d; "
                            "sleeping %.2fs before retry.",
                            operation,
                            symbol,
                            attempt + 1,
                            3,
                            delay,
                        )
                        time.sleep(delay)
                else:
                    raise DataUnavailableError(operation, symbol, exc) from exc

        assert last_exc is not None
        logger.error(
            "Rate-limit (429) persists after 3 attempts for %s(%r); giving up.",
            operation,
            symbol,
        )
        raise DataUnavailableError(operation, symbol, last_exc) from last_exc
