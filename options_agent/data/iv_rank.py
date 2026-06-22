"""IV-rank and IV-percentile computation with daily ATM IV accumulation (WP-3.4).

Public API:
    record_daily_iv(symbol, atm_iv, obs_date, conn) -> None
    compute_iv_rank(symbol, current_iv, conn, min_days=30) -> float | None
    compute_iv_percentile(symbol, current_iv, conn, min_days=30) -> float | None

Historical IV is accumulated by calling record_daily_iv() once per trading day,
independent of the entry cycle. Call it from a dedicated daily scheduler (WP-8),
not from the entry cycle itself — tying observations to entry-cycle cadence
produces ragged history that silently corrupts the 252-day window.

Both rank and percentile return None when fewer than min_days observations exist
in the trailing 252-day window. None is the correct SymbolSnapshot value for
"exclude from entry candidates" — not 0, not an estimated value. See:
  - contracts/data.py: SymbolSnapshot — "None iv_rank means exclude today"
  - WP-4 entry gate: treats None iv_rank as a disqualifier, not low-IV signal

Cold-start behaviour:
  < min_days observations → None (logged as "insufficient history, day N/M")
  0 observations          → None (logged as "no history recorded yet")
  These two cases log at different levels so "warming up" is distinguishable
  from "data pipeline never ran."

IV-rank formula:
    (current_iv - window_low) / (window_high - window_low)
    Zero denominator (flat IV over the window) → None, not 0.5.

IV-percentile formula:
    fraction of past 252 observations strictly below current_iv
    Ties (obs == current_iv) are excluded from the "below" count; this is the
    conventional definition and avoids ambiguity at boundary values.

"Current IV" definition — identical rule for stored observations and live value:
    ATM call at nearest-to-30-DTE expiration.
    See data/greeks_iv.get_atm_iv() for the selection logic.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from options_agent.state.db import iv_history_table

logger = logging.getLogger(__name__)

# Trailing window: 252 trading days ≈ 1 calendar year.
# We fetch rows within a 366-day calendar window and cap at 252 rows so that
# a year with extra trading days (e.g., no holidays) never silently expands
# the window past the 52-week definition.
_WINDOW_DAYS = 252
_CALENDAR_LOOKBACK = timedelta(days=366)


def record_daily_iv(
    symbol: str,
    atm_iv: float,
    obs_date: date,
    conn: Connection,
) -> None:
    """Upsert one daily ATM IV observation for the symbol.

    If a row already exists for (symbol, obs_date), the atm_iv is updated
    (idempotent: re-running the daily job on the same day is safe).
    If no row exists, one is inserted.

    obs_date should be the current trading day (date.today() for live runs).
    Pass it explicitly so tests can inject deterministic dates.
    """
    existing = conn.execute(
        sa.select(iv_history_table.c.atm_iv).where(
            sa.and_(
                iv_history_table.c.symbol == symbol,
                iv_history_table.c.observation_date == obs_date,
            )
        )
    ).first()

    if existing is not None:
        conn.execute(
            iv_history_table.update()
            .where(
                sa.and_(
                    iv_history_table.c.symbol == symbol,
                    iv_history_table.c.observation_date == obs_date,
                )
            )
            .values(atm_iv=atm_iv)
        )
        logger.debug(
            "iv_rank: updated observation for %s on %s (atm_iv=%.4f).",
            symbol,
            obs_date,
            atm_iv,
        )
    else:
        conn.execute(
            iv_history_table.insert().values(
                symbol=symbol,
                observation_date=obs_date,
                atm_iv=atm_iv,
            )
        )
        logger.debug(
            "iv_rank: recorded observation for %s on %s (atm_iv=%.4f).",
            symbol,
            obs_date,
            atm_iv,
        )


def compute_iv_rank(
    symbol: str,
    current_iv: float,
    conn: Connection,
    min_days: int = 30,
) -> float | None:
    """Compute IV-rank for the symbol against its trailing 252-day history.

    IV-rank = (current_iv - window_low) / (window_high - window_low)

    Returns None when:
      - Fewer than min_days observations exist (insufficient history).
      - window_high == window_low (flat IV; denominator is zero — undefined).

    Result is not clamped: a current_iv outside the historical window produces
    a value < 0 or > 1. This is intentional — it signals that the current IV
    is more extreme than any observation in the window, which is useful signal.
    """
    history = _fetch_history(symbol, conn)
    n = len(history)

    if n == 0:
        logger.info("iv_rank: %s — no history recorded yet; returning None.", symbol)
        return None

    if n < min_days:
        logger.info(
            "iv_rank: %s — insufficient history (%d/%d days); returning None.",
            symbol,
            n,
            min_days,
        )
        return None

    iv_low = min(history)
    iv_high = max(history)
    denom = iv_high - iv_low

    if denom == 0.0:
        logger.debug(
            "iv_rank: %s — zero denominator (flat IV over window); returning None.",
            symbol,
        )
        return None

    return (current_iv - iv_low) / denom


def compute_iv_percentile(
    symbol: str,
    current_iv: float,
    conn: Connection,
    min_days: int = 30,
) -> float | None:
    """Compute IV-percentile for the symbol against its trailing 252-day history.

    IV-percentile = (# of observations strictly below current_iv) / total_observations

    Strictly-below convention: days where obs == current_iv do not count as
    "below." With continuous IV values ties are rare in live data but can appear
    in tests; the behaviour is deliberate and consistent.

    Returns None when fewer than min_days observations exist.
    """
    history = _fetch_history(symbol, conn)
    n = len(history)

    if n == 0:
        logger.info(
            "iv_percentile: %s — no history recorded yet; returning None.", symbol
        )
        return None

    if n < min_days:
        logger.info(
            "iv_percentile: %s — insufficient history (%d/%d days); returning None.",
            symbol,
            n,
            min_days,
        )
        return None

    count_below = sum(1 for iv in history if iv < current_iv)
    return count_below / n


def _fetch_history(symbol: str, conn: Connection) -> list[float]:
    """Return up to _WINDOW_DAYS ATM IV observations within the trailing year.

    Rows are ordered newest-first internally for the LIMIT to keep the 252
    most recent, then the caller receives an unordered list (rank/percentile
    computations don't depend on ordering).
    """
    cutoff = date.today() - _CALENDAR_LOOKBACK
    rows = conn.execute(
        sa.select(iv_history_table.c.atm_iv)
        .where(
            sa.and_(
                iv_history_table.c.symbol == symbol,
                iv_history_table.c.observation_date >= cutoff,
            )
        )
        .order_by(iv_history_table.c.observation_date.desc())
        .limit(_WINDOW_DAYS)
    ).fetchall()
    return [row.atm_iv for row in rows]
