"""Tests for data/price_history.py — daily-bar trend summarization."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from options_agent.data.price_history import summarize_daily_bars
from options_agent.data.providers import DailyBar

_AS_OF = datetime(2026, 7, 10, 15, 0, tzinfo=UTC)


def _flat_bars(n: int, close: float = 100.0) -> list[DailyBar]:
    start = date(2026, 1, 2)
    return [
        DailyBar(
            day=start + timedelta(days=i),
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1000.0,
        )
        for i in range(n)
    ]


def test_empty_bars_returns_none() -> None:
    assert summarize_daily_bars("SPY", []) is None


def test_flat_series_indicators() -> None:
    s = summarize_daily_bars("SPY", _flat_bars(300), as_of=_AS_OF)
    assert s is not None
    assert s.price == 100.0
    assert s.sma_20 == 100.0
    assert s.sma_50 == 100.0
    assert s.price_vs_sma_20_pct == 0.0
    assert s.price_vs_sma_50_pct == 0.0
    assert s.high_52w == 101.0
    assert s.low_52w == 99.0
    # ATR: every session's true range is high−low = 2.0.
    assert s.atr_14 == 2.0
    assert s.atr_14_pct == 2.0
    assert s.return_5d_pct == 0.0
    assert s.return_21d_pct == 0.0
    assert s.return_63d_pct == 0.0
    assert s.recent_closes == [100.0] * 10
    assert s.bars_available == 300


def test_short_history_leaves_long_windows_none() -> None:
    s = summarize_daily_bars("NVDA", _flat_bars(10), as_of=_AS_OF)
    assert s is not None
    assert s.sma_20 is None
    assert s.sma_50 is None
    assert s.price_vs_sma_20_pct is None
    assert s.return_5d_pct == 0.0  # needs 6 bars — available
    assert s.return_21d_pct is None
    assert s.return_63d_pct is None
    assert s.atr_14 is None  # needs 15 bars
    assert s.bars_available == 10


def test_uptrend_returns_positive() -> None:
    start = date(2026, 1, 2)
    bars = [
        DailyBar(
            day=start + timedelta(days=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
        )
        for i in range(100)
    ]
    s = summarize_daily_bars("SPY", bars, as_of=_AS_OF)
    assert s is not None
    assert s.price == 199.0
    assert s.return_5d_pct is not None and s.return_5d_pct > 0
    assert s.return_63d_pct is not None and s.return_63d_pct > 0
    assert s.sma_20 is not None and s.price > s.sma_20  # price above rising SMA
    assert s.pct_from_52w_high is not None and s.pct_from_52w_high <= 0
    assert s.recent_closes == [190.0 + i for i in range(10)]
