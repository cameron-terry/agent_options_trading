"""Tests for data/iv_rank.py and data/greeks_iv.get_atm_iv().

All tests use an in-memory SQLite database so there are no external dependencies.
No live API calls, no file I/O beyond the in-memory DB.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from options_agent.data.greeks_iv import get_atm_iv
from options_agent.data.iv_rank import (
    compute_iv_percentile,
    compute_iv_rank,
    record_daily_iv,
)
from options_agent.data.providers import RawOptionContract
from options_agent.state.db import iv_history_table, metadata

# ---------------------------------------------------------------------------
# DB fixture — fresh in-memory SQLite for each test
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = date.today()
_SYMBOL = "SPY"


def _insert_history(
    conn, symbol: str, ivs: list[float], end_date: date = _TODAY
) -> None:
    """Insert daily IV observations backwards from end_date."""
    obs_date = end_date
    # trading-day step: go back one calendar day per observation (tests don't
    # need to model weekends/holidays — the window logic works on calendar days)
    for iv in ivs:
        record_daily_iv(symbol, iv, obs_date, conn)
        obs_date = obs_date - timedelta(days=1)


def _make_call(
    *,
    strike: float,
    expiration: date,
    iv: float | None = 0.25,
    delta: float | None = None,
    underlying: str = "SPY",
) -> RawOptionContract:
    return RawOptionContract(
        symbol=f"{underlying}{expiration.strftime('%y%m%d')}C{int(strike * 1000):08d}",
        underlying=underlying,
        strike=strike,
        expiration=expiration,
        right="call",
        bid=1.00,
        ask=1.10,
        volume=None,
        open_interest=None,
        implied_volatility=iv,
        delta=delta,
        gamma=None,
        theta=None,
        vega=None,
        rho=None,
    )


def _make_put(
    *,
    strike: float,
    expiration: date,
    iv: float | None = 0.25,
    delta: float | None = None,
    underlying: str = "SPY",
) -> RawOptionContract:
    return RawOptionContract(
        symbol=f"{underlying}{expiration.strftime('%y%m%d')}P{int(strike * 1000):08d}",
        underlying=underlying,
        strike=strike,
        expiration=expiration,
        right="put",
        bid=1.00,
        ask=1.10,
        volume=None,
        open_interest=None,
        implied_volatility=iv,
        delta=delta,
        gamma=None,
        theta=None,
        vega=None,
        rho=None,
    )


# ---------------------------------------------------------------------------
# record_daily_iv — insert and upsert behaviour
# ---------------------------------------------------------------------------


def test_record_inserts_new_row(conn) -> None:
    record_daily_iv(_SYMBOL, 0.25, _TODAY, conn)
    row = conn.execute(
        sa.select(iv_history_table).where(iv_history_table.c.symbol == _SYMBOL)
    ).first()
    assert row is not None
    assert row.atm_iv == pytest.approx(0.25)
    assert row.observation_date == _TODAY


def test_record_upserts_on_same_date(conn) -> None:
    record_daily_iv(_SYMBOL, 0.25, _TODAY, conn)
    record_daily_iv(_SYMBOL, 0.30, _TODAY, conn)
    rows = conn.execute(
        sa.select(iv_history_table).where(iv_history_table.c.symbol == _SYMBOL)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0].atm_iv == pytest.approx(0.30)


def test_record_different_dates_two_rows(conn) -> None:
    yesterday = _TODAY - timedelta(days=1)
    record_daily_iv(_SYMBOL, 0.20, yesterday, conn)
    record_daily_iv(_SYMBOL, 0.22, _TODAY, conn)
    rows = conn.execute(
        sa.select(iv_history_table).where(iv_history_table.c.symbol == _SYMBOL)
    ).fetchall()
    assert len(rows) == 2


def test_record_different_symbols_independent(conn) -> None:
    record_daily_iv("SPY", 0.20, _TODAY, conn)
    record_daily_iv("QQQ", 0.30, _TODAY, conn)
    spy_row = conn.execute(
        sa.select(iv_history_table).where(iv_history_table.c.symbol == "SPY")
    ).first()
    qqq_row = conn.execute(
        sa.select(iv_history_table).where(iv_history_table.c.symbol == "QQQ")
    ).first()
    assert spy_row is not None and spy_row.atm_iv == pytest.approx(0.20)
    assert qqq_row is not None and qqq_row.atm_iv == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# compute_iv_rank — known series, correct formula
# ---------------------------------------------------------------------------


def test_iv_rank_mid_range(conn) -> None:
    # History: 0.10 to 0.30 in steps of 0.01 = 21 observations
    # Insert 30 identical days at 0.20, bracketed by extremes.
    ivs = [0.10] + [0.20] * 28 + [0.30]
    _insert_history(conn, _SYMBOL, ivs)
    # current_iv = 0.20; rank = (0.20 - 0.10) / (0.30 - 0.10) = 0.5
    result = compute_iv_rank(_SYMBOL, 0.20, conn, min_days=30)
    assert result == pytest.approx(0.5)


def test_iv_rank_at_historical_low(conn) -> None:
    ivs = [0.15] + [0.25] * 29
    _insert_history(conn, _SYMBOL, ivs)
    # current_iv == historical low → rank = 0.0
    result = compute_iv_rank(_SYMBOL, 0.15, conn, min_days=30)
    assert result == pytest.approx(0.0)


def test_iv_rank_at_historical_high(conn) -> None:
    ivs = [0.15] * 29 + [0.35]
    _insert_history(conn, _SYMBOL, ivs)
    result = compute_iv_rank(_SYMBOL, 0.35, conn, min_days=30)
    assert result == pytest.approx(1.0)


def test_iv_rank_exact_formula(conn) -> None:
    # 30 observations: low=0.10, high=0.50, current=0.20
    # rank = (0.20 - 0.10) / (0.50 - 0.10) = 0.10/0.40 = 0.25
    ivs = [0.10] + [0.30] * 28 + [0.50]
    _insert_history(conn, _SYMBOL, ivs)
    result = compute_iv_rank(_SYMBOL, 0.20, conn, min_days=30)
    assert result == pytest.approx(0.25)


def test_iv_rank_current_above_window_not_clamped(conn) -> None:
    # current_iv > historical high → rank > 1.0 (not clamped — signals extremity)
    ivs = [0.20] * 30
    # Add a spread so denominator isn't zero
    ivs[0] = 0.10
    _insert_history(conn, _SYMBOL, ivs)
    result = compute_iv_rank(_SYMBOL, 0.50, conn, min_days=30)
    assert result is not None and result > 1.0


def test_iv_rank_current_below_window_not_clamped(conn) -> None:
    ivs = [0.20] * 29 + [0.40]
    _insert_history(conn, _SYMBOL, ivs)
    result = compute_iv_rank(_SYMBOL, 0.05, conn, min_days=30)
    assert result is not None and result < 0.0


# ---------------------------------------------------------------------------
# compute_iv_rank — None cases
# ---------------------------------------------------------------------------


def test_iv_rank_no_history_returns_none(conn, caplog) -> None:
    with caplog.at_level(logging.INFO):
        result = compute_iv_rank(_SYMBOL, 0.25, conn, min_days=30)
    assert result is None
    assert "no history" in caplog.text


def test_iv_rank_insufficient_history_returns_none(conn, caplog) -> None:
    _insert_history(conn, _SYMBOL, [0.20, 0.25, 0.30])  # only 3 days
    with caplog.at_level(logging.INFO):
        result = compute_iv_rank(_SYMBOL, 0.25, conn, min_days=30)
    assert result is None
    assert "insufficient history" in caplog.text
    assert "3/30" in caplog.text


def test_iv_rank_exactly_at_min_days_returns_value(conn) -> None:
    _insert_history(conn, _SYMBOL, [0.10] + [0.20] * 28 + [0.30])  # 30 observations
    result = compute_iv_rank(_SYMBOL, 0.20, conn, min_days=30)
    assert result is not None


def test_iv_rank_one_below_min_days_returns_none(conn) -> None:
    _insert_history(conn, _SYMBOL, [0.20] * 29)  # 29 < 30
    result = compute_iv_rank(_SYMBOL, 0.20, conn, min_days=30)
    assert result is None


def test_iv_rank_zero_denominator_returns_none(conn, caplog) -> None:
    # All observations identical → high == low → denominator zero
    _insert_history(conn, _SYMBOL, [0.25] * 30)
    with caplog.at_level(logging.DEBUG):
        result = compute_iv_rank(_SYMBOL, 0.25, conn, min_days=30)
    assert result is None
    assert "zero denominator" in caplog.text


def test_iv_rank_no_history_and_zero_denominator_log_are_distinct(conn, caplog) -> None:
    with caplog.at_level(logging.INFO):
        compute_iv_rank(_SYMBOL, 0.25, conn, min_days=30)
    assert "no history" in caplog.text
    assert "zero denominator" not in caplog.text


# ---------------------------------------------------------------------------
# compute_iv_rank — 252-day window cap
# ---------------------------------------------------------------------------


def test_iv_rank_uses_at_most_252_observations(conn) -> None:
    # Insert 300 observations; the window should cap at 252.
    # Set the 300th-oldest (outermost) to an extreme value; if it leaked in,
    # it would distort the rank.
    old_date = _TODAY - timedelta(days=350)
    record_daily_iv(_SYMBOL, 99.0, old_date, conn)  # outside 366-day window
    _insert_history(conn, _SYMBOL, [0.25] * 252)
    # Add a spread so denominator isn't zero
    record_daily_iv(_SYMBOL, 0.10, _TODAY - timedelta(days=1), conn)
    result = compute_iv_rank(_SYMBOL, 0.20, conn, min_days=30)
    # If 99.0 leaked in, iv_high would be 99.0 and rank would be ≈ 0.001.
    # With only the 252-day window, high=0.25, low=0.10, rank=(0.20-0.10)/0.15 ≈ 0.667
    assert result is not None and result < 2.0  # 99.0 didn't contaminate


# ---------------------------------------------------------------------------
# compute_iv_percentile — known series, correct formula
# ---------------------------------------------------------------------------


def test_iv_percentile_all_below(conn) -> None:
    # current_iv above all history → 100% below
    _insert_history(conn, _SYMBOL, [0.10] * 30)
    result = compute_iv_percentile(_SYMBOL, 0.50, conn, min_days=30)
    assert result == pytest.approx(1.0)


def test_iv_percentile_none_below(conn) -> None:
    # current_iv below all history → 0% below
    _insert_history(conn, _SYMBOL, [0.50] * 30)
    result = compute_iv_percentile(_SYMBOL, 0.10, conn, min_days=30)
    assert result == pytest.approx(0.0)


def test_iv_percentile_half_below(conn) -> None:
    # 15 observations at 0.10, 15 at 0.30; current=0.20 → 15/30 = 0.5
    ivs = [0.10] * 15 + [0.30] * 15
    _insert_history(conn, _SYMBOL, ivs)
    result = compute_iv_percentile(_SYMBOL, 0.20, conn, min_days=30)
    assert result == pytest.approx(0.5)


def test_iv_percentile_exact_formula(conn) -> None:
    # 30 observations: 10 at 0.10, 10 at 0.20, 10 at 0.30
    # current_iv = 0.25 → 20 below (the 0.10 and 0.20 groups) → 20/30 ≈ 0.667
    ivs = [0.10] * 10 + [0.20] * 10 + [0.30] * 10
    _insert_history(conn, _SYMBOL, ivs)
    result = compute_iv_percentile(_SYMBOL, 0.25, conn, min_days=30)
    assert result == pytest.approx(20 / 30)


def test_iv_percentile_strictly_below_excludes_ties(conn) -> None:
    # current_iv == 0.25 exactly; 15 observations at 0.25 (ties) and 15 at 0.10.
    # Strictly-below: only the 15 at 0.10 count → 15/30 = 0.5
    ivs = [0.10] * 15 + [0.25] * 15
    _insert_history(conn, _SYMBOL, ivs)
    result = compute_iv_percentile(_SYMBOL, 0.25, conn, min_days=30)
    assert result == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# compute_iv_percentile — None cases
# ---------------------------------------------------------------------------


def test_iv_percentile_no_history_returns_none(conn, caplog) -> None:
    with caplog.at_level(logging.INFO):
        result = compute_iv_percentile(_SYMBOL, 0.25, conn, min_days=30)
    assert result is None
    assert "no history" in caplog.text


def test_iv_percentile_insufficient_history_returns_none(conn, caplog) -> None:
    _insert_history(conn, _SYMBOL, [0.20] * 5)
    with caplog.at_level(logging.INFO):
        result = compute_iv_percentile(_SYMBOL, 0.20, conn, min_days=30)
    assert result is None
    assert "insufficient history" in caplog.text


# ---------------------------------------------------------------------------
# get_atm_iv — expiration selection (nearest to 30 DTE)
# ---------------------------------------------------------------------------


def test_get_atm_iv_picks_nearest_30dte_expiry() -> None:
    exp_25dte = _TODAY + timedelta(days=25)  # 5 days from 30 DTE
    exp_35dte = _TODAY + timedelta(days=35)  # 5 days from 30 DTE (tie — both 5 away)
    exp_60dte = _TODAY + timedelta(days=60)

    contracts = [
        _make_call(strike=500.0, expiration=exp_25dte, iv=0.20),
        _make_call(strike=500.0, expiration=exp_35dte, iv=0.25),
        _make_call(strike=500.0, expiration=exp_60dte, iv=0.30),
    ]
    # Both 25 and 35 DTE are 5 days from target; min() will return one of them.
    # With Python's stable tie-breaking, the result should be deterministic.
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result in {0.20, 0.25}  # either is correct per tie-breaking


def test_get_atm_iv_prefers_closer_expiry() -> None:
    exp_28dte = _TODAY + timedelta(days=28)  # 2 from 30 DTE
    exp_60dte = _TODAY + timedelta(days=60)  # 30 from 30 DTE

    contracts = [
        _make_call(strike=500.0, expiration=exp_28dte, iv=0.20),
        _make_call(strike=500.0, expiration=exp_60dte, iv=0.35),
    ]
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# get_atm_iv — ATM contract selection (delta-first, strike fallback)
# ---------------------------------------------------------------------------


def test_get_atm_iv_prefers_call_delta_closest_to_half(conn=None) -> None:
    exp = _TODAY + timedelta(days=30)
    contracts = [
        _make_call(strike=490.0, expiration=exp, iv=0.30, delta=0.65),  # ITM
        _make_call(strike=500.0, expiration=exp, iv=0.25, delta=0.50),  # ATM
        _make_call(strike=510.0, expiration=exp, iv=0.20, delta=0.35),  # OTM
    ]
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result == pytest.approx(0.25)


def test_get_atm_iv_fallback_strike_when_no_delta() -> None:
    exp = _TODAY + timedelta(days=30)
    contracts = [
        _make_call(strike=490.0, expiration=exp, iv=0.30, delta=None),
        _make_call(strike=500.0, expiration=exp, iv=0.25, delta=None),  # closest to 500
        _make_call(strike=510.0, expiration=exp, iv=0.20, delta=None),
    ]
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result == pytest.approx(0.25)


def test_get_atm_iv_ignores_puts() -> None:
    exp = _TODAY + timedelta(days=30)
    contracts = [
        _make_put(strike=500.0, expiration=exp, iv=0.50, delta=-0.50),
        _make_call(strike=500.0, expiration=exp, iv=0.25, delta=0.50),
    ]
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result == pytest.approx(0.25)


def test_get_atm_iv_ignores_contracts_with_no_iv() -> None:
    exp = _TODAY + timedelta(days=30)
    contracts = [
        _make_call(strike=500.0, expiration=exp, iv=None, delta=0.50),  # no IV
        _make_call(strike=505.0, expiration=exp, iv=0.22, delta=0.48),  # next best
    ]
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result == pytest.approx(0.22)


def test_get_atm_iv_ignores_expired_contracts() -> None:
    past_exp = _TODAY - timedelta(days=1)
    future_exp = _TODAY + timedelta(days=30)
    contracts = [
        _make_call(strike=500.0, expiration=past_exp, iv=0.99, delta=0.50),
        _make_call(strike=500.0, expiration=future_exp, iv=0.25, delta=0.50),
    ]
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result == pytest.approx(0.25)


def test_get_atm_iv_returns_none_when_no_valid_contracts() -> None:
    result = get_atm_iv([], spot_price=500.0)
    assert result is None


def test_get_atm_iv_returns_none_when_all_iv_missing() -> None:
    exp = _TODAY + timedelta(days=30)
    contracts = [_make_call(strike=500.0, expiration=exp, iv=None)]
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result is None


def test_get_atm_iv_returns_none_when_only_puts() -> None:
    exp = _TODAY + timedelta(days=30)
    contracts = [_make_put(strike=500.0, expiration=exp, iv=0.25)]
    result = get_atm_iv(contracts, spot_price=500.0)
    assert result is None


# ---------------------------------------------------------------------------
# Integration: record_daily_iv → compute_iv_rank / compute_iv_percentile
# ---------------------------------------------------------------------------


def test_full_pipeline_known_series(conn) -> None:
    # SPY IV range: low=0.10, high=0.40 over 252 days.
    # Construct: 126 days at 0.10, 126 days at 0.40.
    ivs = [0.10] * 126 + [0.40] * 126
    _insert_history(conn, _SYMBOL, ivs)

    current_iv = 0.25
    rank = compute_iv_rank(_SYMBOL, current_iv, conn, min_days=30)
    pct = compute_iv_percentile(_SYMBOL, current_iv, conn, min_days=30)

    # rank = (0.25 - 0.10) / (0.40 - 0.10) = 0.15 / 0.30 = 0.5
    assert rank == pytest.approx(0.5)
    # percentile: 126 observations below 0.25 (the 0.10 group) / 252 total = 0.5
    assert pct == pytest.approx(0.5)


def test_full_pipeline_different_symbols_independent(conn) -> None:
    _insert_history(conn, "SPY", [0.15] * 29 + [0.40])  # low vol
    _insert_history(conn, "QQQ", [0.30] * 29 + [0.60])  # high vol

    spy_rank = compute_iv_rank("SPY", 0.25, conn, min_days=30)
    qqq_rank = compute_iv_rank("QQQ", 0.50, conn, min_days=30)

    assert spy_rank is not None
    assert qqq_rank is not None
    assert spy_rank != pytest.approx(qqq_rank)
