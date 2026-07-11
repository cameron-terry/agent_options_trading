"""Tests for risk/structure.py — deterministic proposal risk metrics."""

from __future__ import annotations

from datetime import UTC, date, datetime

from options_agent.contracts.data import (
    ChainFilterParams,
    FilteredChain,
    OptionContract,
)
from options_agent.contracts.proposal import Leg
from options_agent.risk.structure import (
    apply_structure_metrics,
    compute_structure_metrics,
)

_EXPIRY = date(2026, 9, 18)
_OTHER_EXPIRY = date(2026, 10, 16)


def _contract(
    strike: float,
    right: str,
    mid: float,
    *,
    expiration: date = _EXPIRY,
    delta: float = 0.30,
    theta: float = -0.10,
    vega: float = 0.40,
) -> OptionContract:
    half_spread = 0.05
    return OptionContract(
        symbol=f"SPY{strike:.0f}{right[0].upper()}",
        strike=strike,
        expiration=expiration,
        right=right,  # type: ignore[arg-type]
        bid=round(mid - half_spread, 2),
        ask=round(mid + half_spread, 2),
        mid=mid,
        volume=1000,
        open_interest=5000,
        delta=delta,
        theta=theta,
        vega=vega,
        iv=0.19,
        spread_width=2 * half_spread,
        dte=60,
        greek_source="alpaca",
    )


def _chain(contracts: list[OptionContract]) -> FilteredChain:
    return FilteredChain(
        underlying="SPY",
        underlying_price=545.0,
        as_of=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
        filter_params=ChainFilterParams(
            dte_min=30,
            dte_max=45,
            delta_min=0.15,
            delta_max=0.45,
            min_open_interest=500,
            max_spread_pct_of_mid=0.10,
            max_spread_abs_floor=0.05,
        ),
        contracts=contracts,
    )


def _leg(strike: float, right: str, side: str, expiration: date = _EXPIRY) -> Leg:
    return Leg(
        right=right,  # type: ignore[arg-type]
        side=side,  # type: ignore[arg-type]
        strike=strike,
        expiration=expiration,
    )


# ---------------------------------------------------------------------------
# Credit spread (bull put)
# ---------------------------------------------------------------------------


def test_bull_put_spread_credit_metrics() -> None:
    chain = _chain(
        [
            _contract(560.0, "put", 12.00, delta=-0.60, theta=-0.09, vega=0.52),
            _contract(555.0, "put", 10.50, delta=-0.55, theta=-0.085, vega=0.50),
        ]
    )
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy"),
    ]
    m = compute_structure_metrics(legs, chain)
    assert m is not None
    # Net credit of 1.50 → max loss (5 − 1.50) × 100 = 350; max profit 150.
    assert m.net_entry_mid == -1.50
    assert m.est_max_loss == 350.0
    assert m.est_max_profit == 150.0
    # Greeks: sell 560P (−(−0.60)) + buy 555P (−0.55) = +0.05
    assert m.net_delta == 0.05
    # Quotes ordered to match legs: sell leg first.
    assert m.leg_quotes == [(11.95, 12.05), (10.45, 10.55)]


def test_bull_call_spread_debit_metrics() -> None:
    chain = _chain(
        [
            _contract(555.0, "call", 2.90, delta=0.25),
            _contract(560.0, "call", 1.625, delta=0.18),
        ]
    )
    legs = [
        _leg(555.0, "call", "buy"),
        _leg(560.0, "call", "sell"),
    ]
    m = compute_structure_metrics(legs, chain)
    assert m is not None
    assert m.net_entry_mid == 1.275  # net debit
    assert m.est_max_loss == 127.5  # debit paid
    assert m.est_max_profit == 372.5  # width − debit
    assert m.net_delta == 0.07


def test_iron_condor_bounded_both_sides() -> None:
    chain = _chain(
        [
            _contract(520.0, "put", 1.00, delta=-0.15),
            _contract(525.0, "put", 1.50, delta=-0.20),
            _contract(565.0, "call", 1.40, delta=0.20),
            _contract(570.0, "call", 0.95, delta=0.15),
        ]
    )
    legs = [
        _leg(525.0, "put", "sell"),
        _leg(520.0, "put", "buy"),
        _leg(565.0, "call", "sell"),
        _leg(570.0, "call", "buy"),
    ]
    m = compute_structure_metrics(legs, chain)
    assert m is not None
    # Credit = 0.50 + 0.45 = 0.95; width 5 → max loss (5 − 0.95) × 100 = 405.
    assert m.net_entry_mid == -0.95
    assert m.est_max_loss == 405.0
    assert m.est_max_profit == 95.0


# ---------------------------------------------------------------------------
# Fallback / edge cases
# ---------------------------------------------------------------------------


def test_missing_leg_returns_none() -> None:
    chain = _chain([_contract(560.0, "put", 12.00)])
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy"),  # not in chain
    ]
    assert compute_structure_metrics(legs, chain) is None


def test_mixed_expirations_skips_payoff_but_computes_greeks() -> None:
    chain = _chain(
        [
            _contract(560.0, "put", 12.00, delta=-0.60),
            _contract(555.0, "put", 8.00, delta=-0.45, expiration=_OTHER_EXPIRY),
        ]
    )
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy", expiration=_OTHER_EXPIRY),
    ]
    m = compute_structure_metrics(legs, chain)
    assert m is not None
    assert m.est_max_loss is None
    assert m.est_max_profit is None
    assert m.net_delta == 0.15  # +0.60 − 0.45
    assert m.net_entry_mid == -4.00


def test_unbounded_upside_leaves_profit_none() -> None:
    # Long 2 calls / short 1 call: call slope +1 → unbounded profit,
    # bounded loss (net debit).
    chain = _chain(
        [
            _contract(555.0, "call", 2.90, delta=0.25),
            _contract(560.0, "call", 1.625, delta=0.18),
        ]
    )
    legs = [
        Leg(right="call", side="buy", strike=555.0, expiration=_EXPIRY, ratio=2),
        Leg(right="call", side="sell", strike=560.0, expiration=_EXPIRY, ratio=1),
    ]
    m = compute_structure_metrics(legs, chain)
    assert m is not None
    assert m.est_max_profit is None
    # Debit = 2 × 2.90 − 1.625 = 4.175 → max loss 417.5 (both calls expire OTM).
    assert m.est_max_loss == 417.5


def test_apply_structure_metrics_overrides_and_falls_back() -> None:
    chain = _chain(
        [
            _contract(560.0, "put", 12.00, delta=-0.60),
            _contract(555.0, "put", 8.00, delta=-0.45, expiration=_OTHER_EXPIRY),
        ]
    )
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy", expiration=_OTHER_EXPIRY),
    ]
    m = compute_structure_metrics(legs, chain)
    assert m is not None
    updates = apply_structure_metrics(
        {},
        m,
        agent_est_max_loss=999.0,
        agent_est_max_profit=111.0,
        log_context="test",
    )
    # Greeks always overridden; est_* absent → agent values retained upstream.
    assert updates["net_delta"] == 0.15
    assert "est_max_loss" not in updates
    assert "est_max_profit" not in updates
