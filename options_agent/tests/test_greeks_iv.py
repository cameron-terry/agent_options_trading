"""Tests for data/greeks_iv.py — enrich_greeks_iv().

Uses mocked RawOptionContract fixtures with known Greek values.
No live API calls.
"""

from __future__ import annotations

import logging
import math
from datetime import date

import pytest

from options_agent.data.greeks_iv import _IV_MAX, enrich_greeks_iv
from options_agent.data.providers import RawOptionContract

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPIRY = date(2026, 7, 18)


def _raw(
    *,
    symbol: str = "SPY260718P00450000",
    underlying: str = "SPY",
    right: str = "put",
    strike: float = 450.0,
    delta: float | None = -0.28,
    gamma: float | None = 0.04,
    theta: float | None = -0.08,
    vega: float | None = 0.22,
    implied_volatility: float | None = 0.24,
    rho: float | None = -0.05,
) -> RawOptionContract:
    return RawOptionContract(
        symbol=symbol,
        underlying=underlying,
        strike=strike,
        expiration=_EXPIRY,
        right=right,  # type: ignore[arg-type]
        bid=1.20,
        ask=1.30,
        volume=None,
        open_interest=None,
        implied_volatility=implied_volatility,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=rho,
    )


# ---------------------------------------------------------------------------
# Happy path — valid Greeks pass through unchanged
# ---------------------------------------------------------------------------


def test_valid_put_passes_through() -> None:
    raw = _raw()
    (result,) = enrich_greeks_iv([raw])
    assert result.delta == pytest.approx(-0.28)
    assert result.gamma == pytest.approx(0.04)
    assert result.theta == pytest.approx(-0.08)
    assert result.vega == pytest.approx(0.22)
    assert result.implied_volatility == pytest.approx(0.24)
    assert result.rho == pytest.approx(-0.05)


def test_valid_call_passes_through() -> None:
    raw = _raw(right="call", delta=0.30, gamma=0.03, theta=-0.07, vega=0.20)
    (result,) = enrich_greeks_iv([raw])
    assert result.delta == pytest.approx(0.30)
    assert result.gamma == pytest.approx(0.03)
    assert result.theta == pytest.approx(-0.07)
    assert result.vega == pytest.approx(0.20)


def test_greek_source_set_to_alpaca() -> None:
    (result,) = enrich_greeks_iv([_raw()])
    assert result.greek_source == "alpaca"


def test_empty_list_returns_empty() -> None:
    assert enrich_greeks_iv([]) == []


def test_batch_processes_all_contracts() -> None:
    raws = [_raw(symbol=f"SPY{i}") for i in range(5)]
    results = enrich_greeks_iv(raws)
    assert len(results) == 5
    assert all(r.greek_source == "alpaca" for r in results)


# ---------------------------------------------------------------------------
# None values pass through unchanged (not coerced, not warned)
# ---------------------------------------------------------------------------


def test_none_delta_stays_none(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        (result,) = enrich_greeks_iv([_raw(delta=None)])
    assert result.delta is None
    assert "delta" not in caplog.text


def test_none_gamma_stays_none() -> None:
    (result,) = enrich_greeks_iv([_raw(gamma=None)])
    assert result.gamma is None


def test_none_theta_stays_none() -> None:
    (result,) = enrich_greeks_iv([_raw(theta=None)])
    assert result.theta is None


def test_none_vega_stays_none() -> None:
    (result,) = enrich_greeks_iv([_raw(vega=None)])
    assert result.vega is None


def test_none_iv_stays_none() -> None:
    (result,) = enrich_greeks_iv([_raw(implied_volatility=None)])
    assert result.implied_volatility is None


# ---------------------------------------------------------------------------
# delta plausibility: |delta| > 1.0 → None
# ---------------------------------------------------------------------------


def test_delta_over_one_coerced(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        (result,) = enrich_greeks_iv([_raw(delta=1.5)])
    assert result.delta is None
    assert "delta" in caplog.text


def test_delta_negative_over_one_coerced() -> None:
    (result,) = enrich_greeks_iv([_raw(delta=-1.1)])
    assert result.delta is None


def test_delta_exactly_one_passes() -> None:
    # |delta| == 1.0 is the boundary for deep-ITM; Alpaca can return this.
    (result,) = enrich_greeks_iv([_raw(delta=-1.0)])
    assert result.delta == pytest.approx(-1.0)


def test_delta_exactly_minus_one_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(delta=-1.0)])
    assert result.delta is not None


# ---------------------------------------------------------------------------
# gamma plausibility: gamma < 0 → None
# ---------------------------------------------------------------------------


def test_negative_gamma_coerced(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        (result,) = enrich_greeks_iv([_raw(gamma=-0.01)])
    assert result.gamma is None
    assert "gamma" in caplog.text


def test_zero_gamma_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(gamma=0.0)])
    assert result.gamma == pytest.approx(0.0)


def test_positive_gamma_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(gamma=0.05)])
    assert result.gamma == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# theta plausibility: theta > 0 → None
# ---------------------------------------------------------------------------


def test_positive_theta_coerced(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        (result,) = enrich_greeks_iv([_raw(theta=0.01)])
    assert result.theta is None
    assert "theta" in caplog.text


def test_zero_theta_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(theta=0.0)])
    assert result.theta == pytest.approx(0.0)


def test_negative_theta_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(theta=-0.10)])
    assert result.theta == pytest.approx(-0.10)


# ---------------------------------------------------------------------------
# vega plausibility: vega < 0 → None
# ---------------------------------------------------------------------------


def test_negative_vega_coerced(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        (result,) = enrich_greeks_iv([_raw(vega=-0.01)])
    assert result.vega is None
    assert "vega" in caplog.text


def test_zero_vega_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(vega=0.0)])
    assert result.vega == pytest.approx(0.0)


def test_positive_vega_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(vega=0.30)])
    assert result.vega == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# IV plausibility: IV ≤ 0 or IV > _IV_MAX → None
# ---------------------------------------------------------------------------


def test_zero_iv_coerced(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        (result,) = enrich_greeks_iv([_raw(implied_volatility=0.0)])
    assert result.implied_volatility is None
    assert "implied_volatility" in caplog.text


def test_negative_iv_coerced() -> None:
    (result,) = enrich_greeks_iv([_raw(implied_volatility=-0.10)])
    assert result.implied_volatility is None


def test_iv_above_cap_coerced() -> None:
    (result,) = enrich_greeks_iv([_raw(implied_volatility=_IV_MAX + 0.01)])
    assert result.implied_volatility is None


def test_iv_exactly_at_cap_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(implied_volatility=_IV_MAX)])
    assert result.implied_volatility == pytest.approx(_IV_MAX)


def test_iv_just_below_cap_passes() -> None:
    (result,) = enrich_greeks_iv([_raw(implied_volatility=_IV_MAX - 0.01)])
    assert result.implied_volatility == pytest.approx(_IV_MAX - 0.01)


def test_high_but_valid_iv_passes() -> None:
    # Meme/earnings stocks can have IV of 200–300% (2.0–3.0); should pass.
    (result,) = enrich_greeks_iv([_raw(implied_volatility=2.50)])
    assert result.implied_volatility == pytest.approx(2.50)


# ---------------------------------------------------------------------------
# Non-finite values: NaN / Inf → None
# ---------------------------------------------------------------------------


def test_nan_delta_coerced(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        (result,) = enrich_greeks_iv([_raw(delta=float("nan"))])
    assert result.delta is None
    assert "non-finite" in caplog.text


def test_inf_gamma_coerced() -> None:
    (result,) = enrich_greeks_iv([_raw(gamma=math.inf)])
    assert result.gamma is None


def test_neg_inf_theta_coerced() -> None:
    (result,) = enrich_greeks_iv([_raw(theta=-math.inf)])
    assert result.theta is None


def test_nan_iv_coerced() -> None:
    (result,) = enrich_greeks_iv([_raw(implied_volatility=float("nan"))])
    assert result.implied_volatility is None


def test_inf_vega_coerced() -> None:
    (result,) = enrich_greeks_iv([_raw(vega=math.inf)])
    assert result.vega is None


# ---------------------------------------------------------------------------
# Implausible → None, not just logged (coercion is hard-rule)
# ---------------------------------------------------------------------------


def test_implausible_delta_is_none_not_retained() -> None:
    # Verify the value is truly None, not 1.7 retained with a log.
    (result,) = enrich_greeks_iv([_raw(delta=1.7)])
    assert result.delta is None
    assert isinstance(result.delta, type(None))


# ---------------------------------------------------------------------------
# IV / Greeks presence inconsistency warning
# ---------------------------------------------------------------------------


def test_iv_present_but_all_greeks_absent_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = _raw(delta=None, gamma=None, theta=None, vega=None, implied_volatility=0.30)
    with caplog.at_level(logging.WARNING):
        enrich_greeks_iv([raw])
    assert "inconsistency" in caplog.text


def test_all_greeks_present_but_iv_absent_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = _raw(implied_volatility=None)
    with caplog.at_level(logging.WARNING):
        enrich_greeks_iv([raw])
    assert "inconsistency" in caplog.text


def test_all_present_no_inconsistency_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        enrich_greeks_iv([_raw()])
    assert "inconsistency" not in caplog.text


def test_all_absent_no_inconsistency_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # All None is consistent (no data for this contract).
    raw = _raw(delta=None, gamma=None, theta=None, vega=None, implied_volatility=None)
    with caplog.at_level(logging.WARNING):
        enrich_greeks_iv([raw])
    assert "inconsistency" not in caplog.text


def test_iv_coerced_by_plausibility_with_valid_greeks_no_inconsistency_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Provider sent both IV and Greeks (self-consistent). Our plausibility rule
    # coerces IV=0.0 to None. The inconsistency warning must not fire because
    # the provider was consistent; local rules created the asymmetry.
    raw = _raw(implied_volatility=0.0)  # IV fails plausibility; Greeks valid
    with caplog.at_level(logging.WARNING):
        enrich_greeks_iv([raw])
    assert "inconsistency" not in caplog.text


def test_all_four_greeks_implausible_coerced_no_inconsistency_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Provider sent all four Greeks and a valid IV (self-consistent). Every Greek
    # fails its plausibility rule and is coerced to None. The inconsistency warning
    # must not fire — it was our coercion that created the asymmetry, not the
    # provider's response.
    raw = _raw(delta=1.5, gamma=-0.01, theta=0.01, vega=-0.01, implied_volatility=0.24)
    with caplog.at_level(logging.WARNING):
        enrich_greeks_iv([raw])
    assert "inconsistency" not in caplog.text


# ---------------------------------------------------------------------------
# Batch: one bad contract in a list leaves the rest intact
# ---------------------------------------------------------------------------


def test_one_bad_contract_does_not_affect_others() -> None:
    raws = [
        _raw(symbol="A", delta=-0.28),  # valid
        _raw(symbol="B", delta=1.5),  # implausible → delta coerced to None
        _raw(symbol="C", delta=-0.35),  # valid
    ]
    results = enrich_greeks_iv(raws)
    a, b, c = results
    assert a.delta == pytest.approx(-0.28)
    assert b.delta is None
    assert c.delta == pytest.approx(-0.35)


def test_rho_not_validated_passes_through_unchanged() -> None:
    # rho is not validated by enrich_greeks_iv (not used in FilteredChain).
    (result,) = enrich_greeks_iv([_raw(rho=-0.05)])
    assert result.rho == pytest.approx(-0.05)
