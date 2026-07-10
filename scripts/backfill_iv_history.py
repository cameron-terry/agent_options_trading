"""Backfill iv_history with realized-volatility proxy for the paper run (WP-3.4b).

yfinance does not expose historical options chain snapshots, so ATM IV for past
sessions is approximated as annualized 30-day trailing realized volatility (HV30)
computed from daily closing prices.  This proxy seeds the iv_history table so
compute_iv_rank() / compute_iv_percentile() return non-null values immediately
rather than after ~30 live-accumulation sessions.

Known trade-off: the bootstrapped rows use realized vol (HV30) while the live
daily IV job accumulates real ATM IV from options chains.  The two metrics
correlate but differ, especially around earnings events.  The approximation
degrades gracefully: over a live 252-trading-day window, real IV observations
replace bootstrapped rows one-by-one until the window is fully live-sourced.
record_daily_iv() is idempotent on (symbol, date), so re-running this script
after real data exists for a date is safe — the live observation wins.

Usage:
    uv run python scripts/backfill_iv_history.py

Reads db_url and universe_file from config.toml in the current directory.
Requires no Alpaca or Anthropic credentials — only yfinance (no API key).
"""

from __future__ import annotations

import logging
import math
import os
import sys
from datetime import date, datetime
from pathlib import Path

import yfinance as yf

from options_agent.config import Config
from options_agent.data.iv_rank import (
    compute_iv_percentile,
    compute_iv_rank,
    record_daily_iv,
)
from options_agent.state.db import build_engine, get_connection

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Rolling window for realized volatility (trading days).
_HV_WINDOW = 30
# Trailing window to populate (trading days, matching iv_rank._WINDOW_DAYS).
_TARGET_SESSIONS = 252
# Fetch this many months of price history so that after computing log-returns
# and the first 29-row warm-up, we still have _TARGET_SESSIONS complete windows.
# 15 months ≈ 315 trading days → ~285 complete HV30 observations.
_HISTORY_PERIOD = "15mo"


def _load_symbols(path: Path) -> list[str]:
    return [s.strip() for s in path.read_text().splitlines() if s.strip()]


def _sample_std(values: list[float]) -> float:
    """Bessel-corrected sample standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def _compute_hv_series(symbol: str) -> dict[date, float]:
    """Return {date: annualized_HV30} for the trailing _TARGET_SESSIONS sessions.

    Returns an empty dict and logs a warning on any yfinance failure.
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=_HISTORY_PERIOD, auto_adjust=True)
        if hist.empty:
            logger.warning(
                "backfill: yfinance returned no price history for %s", symbol
            )
            return {}

        # Convert to a plain list of (date, close) so we stay stdlib-only.
        # Pandas Timestamp.__str__ starts with "YYYY-MM-DD"; parsing that string
        # avoids pyright complaints about pandas' generic Hashable index type.
        close_series = hist["Close"]
        rows: list[tuple[date, float]] = []
        for ts, close in zip(close_series.index, close_series.to_list()):
            obs_date = datetime.strptime(str(ts)[:10], "%Y-%m-%d").date()
            rows.append((obs_date, float(close)))

        if len(rows) < _HV_WINDOW + 1:
            logger.warning(
                "backfill: only %d price rows for %s; need at least %d",
                len(rows),
                symbol,
                _HV_WINDOW + 1,
            )
            return {}

        # Compute log returns, then rolling HV30.
        log_returns: list[tuple[date, float]] = []
        for i in range(1, len(rows)):
            prev_close = rows[i - 1][1]
            curr_close = rows[i][1]
            if prev_close > 0 and curr_close > 0:
                log_returns.append((rows[i][0], math.log(curr_close / prev_close)))

        result: dict[date, float] = {}
        for i in range(_HV_WINDOW - 1, len(log_returns)):
            window = [lr[1] for lr in log_returns[i - (_HV_WINDOW - 1) : i + 1]]
            hv = _sample_std(window) * math.sqrt(252)
            obs_date = log_returns[i][0]
            if hv > 0:
                result[obs_date] = hv

        # Keep only the most recent _TARGET_SESSIONS sessions.
        sorted_dates = sorted(result)
        if len(sorted_dates) > _TARGET_SESSIONS:
            for d in sorted_dates[: len(sorted_dates) - _TARGET_SESSIONS]:
                del result[d]

        return result

    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: price history fetch failed for %s: %s", symbol, exc)
        return {}


def main() -> None:
    config_path = Path("config.toml")
    if not config_path.exists():
        print(
            "ERROR: config.toml not found. Run from the project root.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = Config.from_toml(config_path)
    symbols = _load_symbols(config.universe_file)
    if not symbols:
        print("ERROR: universe file is empty or missing.", file=sys.stderr)
        sys.exit(1)

    # Honor the DB_URL env override the same way options_agent.__main__ does,
    # so running this inside the container targets the mounted volume DB
    # (sqlite:////app/data/options_agent.db) rather than config.toml's default.
    db_url = os.environ.get("DB_URL", config.db_url)
    engine = build_engine(db_url)

    print(f"WP-3.4b iv_history backfill — {date.today()}")
    print(f"Source: {_HV_WINDOW}-day realized volatility (HV30) proxy via yfinance")
    print(f"Target: up to {_TARGET_SESSIONS} sessions per symbol → {db_url}")
    print(f"Symbols ({len(symbols)}): {', '.join(symbols)}")
    print()
    print(
        f"{'Symbol':<8} {'Sessions':>9} {'Min HV':>9} {'Max HV':>9}"
        f" {'Mean HV':>9} {'IV Rank':>9} {'IV Pct':>9}"
    )
    print("-" * 72)

    non_null_rank_count = 0

    for symbol in symbols:
        hv_series = _compute_hv_series(symbol)

        if not hv_series:
            print(f"{symbol:<8}  ERROR: no price data from yfinance")
            continue

        with get_connection(engine) as conn:
            for obs_date, hv_val in sorted(hv_series.items()):
                record_daily_iv(symbol, hv_val, obs_date, conn)

        sessions_loaded = len(hv_series)

        # Use the most-recent HV as the "current_iv" stand-in for the rank display.
        current_iv = hv_series[max(hv_series)]

        with get_connection(engine) as conn:
            iv_rank = compute_iv_rank(symbol, current_iv, conn)
            iv_pct = compute_iv_percentile(symbol, current_iv, conn)

        values = list(hv_series.values())
        min_hv = min(values)
        max_hv = max(values)
        mean_hv = sum(values) / len(values)

        rank_str = f"{iv_rank:.3f}" if iv_rank is not None else "null"
        pct_str = f"{iv_pct:.3f}" if iv_pct is not None else "null"

        print(
            f"{symbol:<8} {sessions_loaded:>9}"
            f" {min_hv:>8.1%} {max_hv:>8.1%} {mean_hv:>8.1%}"
            f" {rank_str:>9} {pct_str:>9}"
        )

        if iv_rank is not None:
            non_null_rank_count += 1

    print()
    print(f"Symbols with non-null iv_rank: {non_null_rank_count}/{len(symbols)}")
    if non_null_rank_count >= 3:
        print("Acceptance criterion met (>= 3 symbols with non-null iv_rank).")
    else:
        print(
            f"ERROR: acceptance criterion NOT met "
            f"(need >= 3 symbols; got {non_null_rank_count}).",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
