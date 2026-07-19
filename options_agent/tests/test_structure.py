"""Tests for risk/structure.py — deterministic proposal risk metrics."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from options_agent.contracts.data import (
    ChainFilterParams,
    FilteredChain,
    OptionContract,
    PortfolioState,
)
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.risk.limits import Limits
from options_agent.risk.sizing import size
from options_agent.risk.structure import (
    apply_fill_metrics,
    apply_structure_metrics,
    compute_payoff_bounds,
    compute_structure_metrics,
    recompute_fill_metrics,
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


# ---------------------------------------------------------------------------
# Incident-pattern regression (via risk.sizing.size)
#
# 2026-07-18 journal audit follow-up: two live QQQ cycles mis-sized because
# the agent's self-reported est_max_loss didn't match its own legs —
# eab4b54c put a 3-contract *total* in a per-contract field (SIZED_TO_ZERO
# when it should have sized 2); 05c9b8da doubled the per-contract value
# (sized 1 when it should have sized 3). Exact historical chain quotes from
# those dates weren't preserved beyond the audit's summary numbers, so these
# fixtures reproduce the *pattern* (total-instead-of-per-contract; doubled
# value) at the same per-contract dollar magnitudes the audit recorded
# ($491 and $295), not a byte-exact replay.
# ---------------------------------------------------------------------------

_INCIDENT_EXIT = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)


def _incident_proposal(
    legs: list[Leg], est_max_loss: float, est_max_profit: float
) -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying="QQQ",
        strategy="bull_put_spread",
        legs=legs,
        thesis="placeholder",
        iv_rationale="placeholder",
        catalyst_check="no earnings within 30 days",
        conviction=0.70,
        est_max_loss=est_max_loss,
        est_max_profit=est_max_profit,
        breakevens=[],
        net_delta=0.0,
        net_theta=0.0,
        net_vega=0.0,
        exit_plan=_INCIDENT_EXIT,
        informed_by=[],
    )


def _incident_portfolio() -> PortfolioState:
    return PortfolioState(
        positions=[],
        account_equity=100_000.0,  # -> $1,000 risk budget @ default 1% cap
        buying_power=30_000.0,
        options_buying_power=15_000.0,
        unrealized_pnl=0.0,
        realized_pnl_today=0.0,
        approval_level=2,
        net_dollar_delta=0.0,
        net_dollar_gamma=0.0,
        net_dollar_theta=50.0,
        net_dollar_vega=-200.0,
    )


def test_total_instead_of_per_contract_pattern_corrects_sizing() -> None:
    # width 7, credit 2.09 -> per-contract max loss (7 - 2.09) * 100 = $491
    chain = _chain(
        [
            _contract(380.0, "put", 3.09),
            _contract(373.0, "put", 1.00),
        ]
    )
    legs = [_leg(380.0, "put", "sell"), _leg(373.0, "put", "buy")]
    metrics = compute_structure_metrics(legs, chain)
    assert metrics is not None
    assert metrics.est_max_loss == 491.0

    buggy = _incident_proposal(legs, est_max_loss=3 * 491.0, est_max_profit=3 * 209.0)
    limits = Limits()
    portfolio = _incident_portfolio()

    # Uncorrected: sizer sees the 3x total and zeroes the trade out.
    assert size(buggy, portfolio, limits).contracts == 0

    updates = apply_structure_metrics(
        {},
        metrics,
        agent_est_max_loss=buggy.est_max_loss,
        agent_est_max_profit=buggy.est_max_profit,
        log_context="cycle eab4b54c",
    )
    corrected = buggy.model_copy(update=updates)
    assert size(corrected, portfolio, limits).contracts == 2


def test_doubled_value_pattern_corrects_sizing() -> None:
    # width 5, credit 2.05 -> per-contract max loss (5 - 2.05) * 100 = $295
    chain = _chain(
        [
            _contract(380.0, "put", 3.05),
            _contract(375.0, "put", 1.00),
        ]
    )
    legs = [_leg(380.0, "put", "sell"), _leg(375.0, "put", "buy")]
    metrics = compute_structure_metrics(legs, chain)
    assert metrics is not None
    assert metrics.est_max_loss == 295.0

    buggy = _incident_proposal(legs, est_max_loss=2 * 295.0, est_max_profit=2 * 205.0)
    limits = Limits()
    portfolio = _incident_portfolio()

    assert size(buggy, portfolio, limits).contracts == 1

    updates = apply_structure_metrics(
        {},
        metrics,
        agent_est_max_loss=buggy.est_max_loss,
        agent_est_max_profit=buggy.est_max_profit,
        log_context="cycle 05c9b8da",
    )
    corrected = buggy.model_copy(update=updates)
    assert size(corrected, portfolio, limits).contracts == 3


def test_correct_agent_value_passes_through_unchanged() -> None:
    # When the agent's arithmetic already matches the legs, enrichment
    # is a no-op on the resulting contract count.
    chain = _chain(
        [
            _contract(380.0, "put", 3.05),
            _contract(375.0, "put", 1.00),
        ]
    )
    legs = [_leg(380.0, "put", "sell"), _leg(375.0, "put", "buy")]
    metrics = compute_structure_metrics(legs, chain)
    assert metrics is not None

    correct = _incident_proposal(legs, est_max_loss=295.0, est_max_profit=205.0)
    limits = Limits()
    portfolio = _incident_portfolio()

    before = size(correct, portfolio, limits)
    updates = apply_structure_metrics(
        {},
        metrics,
        agent_est_max_loss=correct.est_max_loss,
        agent_est_max_profit=correct.est_max_profit,
        log_context="cycle correct",
    )
    after = size(correct.model_copy(update=updates), portfolio, limits)
    assert before.contracts == after.contracts == 3


# ---------------------------------------------------------------------------
# WP-1: fill-time recompute (est_max_loss/profit corrected against the
# actual fill price, not the chain-mid estimate baked in pre-trade).
# ---------------------------------------------------------------------------


def test_compute_payoff_bounds_matches_chain_mid_result() -> None:
    # Same bull put spread as test_bull_put_spread_credit_metrics — verifies
    # the extracted helper reproduces compute_structure_metrics's result when
    # given the same net price.
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy"),
    ]
    est_max_loss, est_max_profit = compute_payoff_bounds(legs, -1.50)
    assert est_max_loss == 350.0
    assert est_max_profit == 150.0


def test_recompute_fill_metrics_uses_fill_price_not_mid() -> None:
    # Width 5 credit spread. Proposal assumed a 1.50 credit (mid); the order
    # actually filled at a better 2.00 credit — max loss should shrink from
    # 350 to 300, matching the improved fill, not the original estimate.
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy"),
    ]
    est_max_loss, est_max_profit = recompute_fill_metrics(legs, -2.00)
    assert est_max_loss == 300.0
    assert est_max_profit == 200.0


def test_recompute_fill_metrics_mixed_expirations_returns_none() -> None:
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy", expiration=_OTHER_EXPIRY),
    ]
    assert recompute_fill_metrics(legs, -1.50) == (None, None)


def test_apply_fill_metrics_corrects_position_values() -> None:
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy"),
    ]
    # Position was created with the pre-trade chain-mid estimate (350/150);
    # the order actually filled at a better credit (2.00 vs. 1.50 mid).
    est_max_loss, est_max_profit = apply_fill_metrics(
        legs,
        -2.00,
        prior_est_max_loss=350.0,
        prior_est_max_profit=150.0,
        log_context="test fill",
    )
    assert est_max_loss == 300.0
    assert est_max_profit == 200.0


def test_apply_fill_metrics_falls_back_on_mixed_expirations() -> None:
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy", expiration=_OTHER_EXPIRY),
    ]
    est_max_loss, est_max_profit = apply_fill_metrics(
        legs,
        -1.50,
        prior_est_max_loss=350.0,
        prior_est_max_profit=150.0,
        log_context="test fill",
    )
    assert est_max_loss == 350.0
    assert est_max_profit == 150.0


def test_apply_fill_metrics_logs_large_deviation() -> None:
    legs = [
        _leg(560.0, "put", "sell"),
        _leg(555.0, "put", "buy"),
    ]
    with patch("options_agent.risk.structure.logger") as mock_logger:
        apply_fill_metrics(
            legs,
            -2.50,  # credit far better than the 1.50 mid estimate
            prior_est_max_loss=350.0,
            prior_est_max_profit=150.0,
            log_context="cycle test-deviation",
        )
    assert mock_logger.warning.called
    assert "deviates" in mock_logger.warning.call_args[0][0]
