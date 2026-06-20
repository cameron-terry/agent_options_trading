"""Tests for data/chains.py — get_filtered_chain() and get_held_leg_greeks().

All tests use fixture RawOptionContracts and a mock DataProvider so no live
API calls are made. The mock provider is created inline via unittest.mock to
keep each test self-contained.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from options_agent.contracts.data import FilteredChain
from options_agent.contracts.proposal import ExitPlan, Leg
from options_agent.contracts.state import (
    AssetClass,
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.data.chains import LegKey, get_filtered_chain, get_held_leg_greeks
from options_agent.data.providers import (
    DataAuthError,
    DataProvider,
    DataUnavailableError,
    RawOptionContract,
)
from options_agent.risk.limits import ChainFilterLimits

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 6, 14, 14, 30, tzinfo=UTC)
_TODAY = _AS_OF.date()
_UNDERLYING_PRICE = 456.0

# Expiries that fall inside / outside the default DTE window (min=20, max=45).
_EXP_IN_RANGE = date(2026, 7, 18)  # 34 DTE from _TODAY
_EXP_TOO_NEAR = date(2026, 6, 25)  # 11 DTE — outside min
_EXP_TOO_FAR = date(2026, 9, 19)  # 97 DTE — outside max

# Default limits used in most tests.
_LIMITS = ChainFilterLimits(
    min_open_interest=100,
    max_spread_pct_of_mid=0.10,
    max_spread_abs_floor=0.05,
    min_dte=20,
    max_dte=45,
    min_abs_delta=0.15,
    max_abs_delta=0.45,
    max_contracts_per_chain=100,
)


# ---------------------------------------------------------------------------
# Helper: build a RawOptionContract with sensible defaults
# ---------------------------------------------------------------------------


def _raw(
    *,
    underlying: str = "SPY",
    right: str = "put",
    strike: float = 450.0,
    expiration: date = _EXP_IN_RANGE,
    bid: float | None = 1.20,
    ask: float | None = 1.30,
    delta: float | None = -0.28,
    theta: float | None = -0.05,
    vega: float | None = 0.20,
    implied_volatility: float | None = 0.24,
    volume: int | None = None,
    open_interest: int | None = None,
    **kwargs: Any,
) -> RawOptionContract:
    # Build OCC-style symbol from components.
    year2 = expiration.year - 2000
    right_char = "P" if right == "put" else "C"
    strike_str = f"{int(strike * 1000):08d}"
    symbol = (
        f"{underlying}{year2:02d}{expiration.month:02d}"
        f"{expiration.day:02d}{right_char}{strike_str}"
    )
    return RawOptionContract(
        symbol=symbol,
        underlying=underlying,
        strike=strike,
        expiration=expiration,
        right=right,  # type: ignore[arg-type]
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=open_interest,
        implied_volatility=implied_volatility,
        delta=delta,
        gamma=None,
        theta=theta,
        vega=vega,
        rho=None,
    )


def _make_provider(
    contracts: list[RawOptionContract],
    price: float = _UNDERLYING_PRICE,
) -> DataProvider:
    mock = MagicMock(spec=DataProvider)
    mock.fetch_option_chain.return_value = contracts
    mock.fetch_latest_price.return_value = price
    return mock  # type: ignore[return-value]


def _run(
    contracts: list[RawOptionContract],
    *,
    symbol: str = "SPY",
    limits: ChainFilterLimits = _LIMITS,
    strategy_hint: str | None = None,
    price: float = _UNDERLYING_PRICE,
) -> FilteredChain:
    provider = _make_provider(contracts, price)
    return get_filtered_chain(symbol, provider, limits, strategy_hint, as_of=_AS_OF)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_in_range_contract_included() -> None:
    chain = _run([_raw()])
    assert len(chain.contracts) == 1


def test_underlying_fields_populated() -> None:
    chain = _run([_raw()])
    assert chain.underlying == "SPY"
    assert chain.underlying_price == _UNDERLYING_PRICE
    assert chain.as_of == _AS_OF


def test_filter_params_embedded() -> None:
    chain = _run([_raw()])
    p = chain.filter_params
    assert p.dte_min == _LIMITS.min_dte
    assert p.dte_max == _LIMITS.max_dte
    assert p.delta_min == _LIMITS.min_abs_delta
    assert p.delta_max == _LIMITS.max_abs_delta
    assert p.min_open_interest == _LIMITS.min_open_interest
    assert p.max_spread_pct_of_mid == _LIMITS.max_spread_pct_of_mid
    assert p.max_spread_abs_floor == _LIMITS.max_spread_abs_floor


def test_output_contract_fields_correct() -> None:
    # bid=1.20, ask=1.28 → spread=0.08, mid=1.24 → 6.5% < 10% → passes filter.
    chain = _run([_raw(bid=1.20, ask=1.28, delta=-0.28)])
    c = chain.contracts[0]
    assert c.bid == pytest.approx(1.20)
    assert c.ask == pytest.approx(1.28)
    assert c.mid == pytest.approx(1.24)
    assert c.spread_width == pytest.approx(0.08)
    assert c.delta == pytest.approx(-0.28)
    assert c.dte == 34  # (_EXP_IN_RANGE - _TODAY).days


# ---------------------------------------------------------------------------
# DTE filter
# ---------------------------------------------------------------------------


def test_dte_too_near_excluded() -> None:
    chain = _run([_raw(expiration=_EXP_TOO_NEAR)])
    assert len(chain.contracts) == 0


def test_dte_too_far_excluded() -> None:
    chain = _run([_raw(expiration=_EXP_TOO_FAR)])
    assert len(chain.contracts) == 0


def test_dte_boundary_min_included() -> None:
    # Expiry exactly at min_dte (20 days) should be included.
    exp = date(2026, 7, 4)  # 20 DTE from 2026-06-14
    chain = _run([_raw(expiration=exp)])
    assert len(chain.contracts) == 1


def test_dte_boundary_max_included() -> None:
    exp = date(2026, 7, 29)  # 45 DTE from 2026-06-14
    chain = _run([_raw(expiration=exp)])
    assert len(chain.contracts) == 1


# ---------------------------------------------------------------------------
# Missing bid/ask
# ---------------------------------------------------------------------------


def test_missing_bid_excluded() -> None:
    chain = _run([_raw(bid=None)])
    assert len(chain.contracts) == 0


def test_missing_ask_excluded() -> None:
    chain = _run([_raw(ask=None)])
    assert len(chain.contracts) == 0


# ---------------------------------------------------------------------------
# Missing Greeks / IV
# ---------------------------------------------------------------------------


def test_missing_delta_excluded_and_counted() -> None:
    chain = _run([_raw(delta=None)])
    assert len(chain.contracts) == 0
    assert chain.excluded_for_missing_greeks == 1


def test_missing_theta_excluded_and_counted() -> None:
    chain = _run([_raw(theta=None)])
    assert len(chain.contracts) == 0
    assert chain.excluded_for_missing_greeks == 1


def test_missing_vega_excluded_and_counted() -> None:
    chain = _run([_raw(vega=None)])
    assert len(chain.contracts) == 0
    assert chain.excluded_for_missing_greeks == 1


def test_missing_iv_excluded_and_counted() -> None:
    chain = _run([_raw(implied_volatility=None)])
    assert len(chain.contracts) == 0
    assert chain.excluded_for_missing_greeks == 1


def test_missing_greeks_count_accumulates() -> None:
    raws = [_raw(delta=None), _raw(theta=None), _raw()]
    chain = _run(raws)
    assert chain.excluded_for_missing_greeks == 2
    assert len(chain.contracts) == 1  # only the complete contract survives


# ---------------------------------------------------------------------------
# Delta range filter
# ---------------------------------------------------------------------------


def test_delta_too_small_excluded() -> None:
    chain = _run([_raw(delta=-0.10)])  # |delta|=0.10 < min=0.15
    assert len(chain.contracts) == 0


def test_delta_too_large_excluded() -> None:
    chain = _run([_raw(delta=-0.50)])  # |delta|=0.50 > max=0.45
    assert len(chain.contracts) == 0


def test_delta_boundary_min_included() -> None:
    chain = _run([_raw(delta=-0.15)])
    assert len(chain.contracts) == 1


def test_delta_boundary_max_included() -> None:
    chain = _run([_raw(delta=-0.45)])
    assert len(chain.contracts) == 1


def test_call_delta_positive_handled() -> None:
    # Calls have positive delta; abs() must be applied before comparing.
    chain = _run([_raw(right="call", delta=0.30)])
    assert len(chain.contracts) == 1


# ---------------------------------------------------------------------------
# Spread filter
# ---------------------------------------------------------------------------


def test_wide_spread_excluded() -> None:
    # spread=0.50 on mid=1.30 → 38% of mid, > 10% pct AND > $0.05 floor.
    chain = _run([_raw(bid=1.05, ask=1.55)])  # spread=0.50, mid=1.30
    assert len(chain.contracts) == 0


def test_spread_passes_by_pct_rule() -> None:
    # spread=0.10 on mid=1.30 → 7.7% < 10% → passes pct rule.
    chain = _run([_raw(bid=1.25, ask=1.35)])
    assert len(chain.contracts) == 1


def test_spread_passes_by_abs_floor_cheap_option() -> None:
    # mid=0.30, spread=0.04 → pct_limit = 0.10 * 0.30 = 0.03 < 0.04 → pct fails.
    # But spread=0.04 ≤ abs_floor=0.05 → passes by floor.
    chain = _run([_raw(bid=0.28, ask=0.32, delta=-0.16)])
    assert len(chain.contracts) == 1


# ---------------------------------------------------------------------------
# OI filter
# ---------------------------------------------------------------------------


def test_oi_unavailable_flag_set() -> None:
    # All raw contracts have None OI (Alpaca behaviour).
    chain = _run([_raw(open_interest=None)])
    assert chain.oi_available is False


def test_oi_available_flag_set_when_present() -> None:
    chain = _run([_raw(open_interest=500)])
    assert chain.oi_available is True


def test_oi_below_threshold_excluded_when_available() -> None:
    chain = _run([_raw(open_interest=50)])  # 50 < min=100
    assert len(chain.contracts) == 0


def test_oi_above_threshold_included() -> None:
    chain = _run([_raw(open_interest=200)])
    assert len(chain.contracts) == 1


def test_oi_none_not_excluded_when_unavailable() -> None:
    # All contracts have None OI → oi_available=False → OI filter not applied.
    chain = _run([_raw(open_interest=None)])
    assert len(chain.contracts) == 1  # not excluded by OI filter


def test_oi_none_excluded_when_some_oi_available() -> None:
    # Mixed chain: one contract has OI, one has None OI.
    # oi_available=True (at least one non-None) → None-OI contract is treated as
    # failing the min_oi threshold (excluded), not bypassed. This upholds the
    # RawOptionContract invariant: "must not be silently passed through a min_oi
    # filter."
    raws = [
        _raw(strike=450.0, open_interest=200),  # passes OI check
        _raw(strike=451.0, open_interest=None),  # None OI → excluded when oi_available
    ]
    chain = _run(raws)
    assert chain.oi_available is True
    assert len(chain.contracts) == 1
    assert chain.contracts[0].strike == 450.0


def test_spread_abs_floor_path_near_zero_mid() -> None:
    # Contracts with mid ≤ _MID_EPSILON (0.01) skip the pct rule and fall through
    # to the abs-floor-only path. bid=0.00, ask=0.01 → spread=0.01 ≤ abs_floor=0.05.
    # delta=-0.16 is within the default range [0.15, 0.45].
    raws = [_raw(bid=0.00, ask=0.01, delta=-0.16)]
    chain = _run(raws)
    assert len(chain.contracts) == 1


# ---------------------------------------------------------------------------
# Strategy hint / right filter
# ---------------------------------------------------------------------------


def test_hint_none_includes_both_rights() -> None:
    raws = [_raw(right="call", delta=0.30), _raw(right="put", delta=-0.30)]
    chain = _run(raws, strategy_hint=None)
    rights = {c.right for c in chain.contracts}
    assert rights == {"call", "put"}


def test_hint_bull_put_spread_puts_only() -> None:
    raws = [_raw(right="call", delta=0.30), _raw(right="put", delta=-0.30)]
    chain = _run(raws, strategy_hint="bull_put_spread")
    assert all(c.right == "put" for c in chain.contracts)
    assert len(chain.contracts) == 1


def test_hint_bear_call_spread_calls_only() -> None:
    raws = [_raw(right="call", delta=0.30), _raw(right="put", delta=-0.30)]
    chain = _run(raws, strategy_hint="bear_call_spread")
    assert all(c.right == "call" for c in chain.contracts)
    assert len(chain.contracts) == 1


def test_hint_iron_condor_both_rights() -> None:
    raws = [_raw(right="call", delta=0.30), _raw(right="put", delta=-0.30)]
    chain = _run(raws, strategy_hint="iron_condor")
    rights = {c.right for c in chain.contracts}
    assert rights == {"call", "put"}


def test_hint_cash_secured_put_puts_only() -> None:
    raws = [_raw(right="call", delta=0.30), _raw(right="put", delta=-0.30)]
    chain = _run(raws, strategy_hint="cash_secured_put")
    assert all(c.right == "put" for c in chain.contracts)


def test_unknown_hint_falls_back_to_both_rights() -> None:
    raws = [_raw(right="call", delta=0.30), _raw(right="put", delta=-0.30)]
    chain = _run(raws, strategy_hint="unknown_strategy_xyz")
    rights = {c.right for c in chain.contracts}
    assert rights == {"call", "put"}


def test_strategy_hint_recorded_on_chain() -> None:
    chain = _run([_raw()], strategy_hint="iron_condor")
    assert chain.strategy_hint == "iron_condor"


def test_strategy_hint_none_recorded_on_chain() -> None:
    chain = _run([_raw()], strategy_hint=None)
    assert chain.strategy_hint is None


# ---------------------------------------------------------------------------
# Max contracts cap and truncation
# ---------------------------------------------------------------------------


def test_cap_truncates_when_exceeded() -> None:
    limits = ChainFilterLimits(
        min_open_interest=0,
        max_spread_pct_of_mid=1.0,
        max_spread_abs_floor=100.0,
        min_dte=0,
        max_dte=100,
        min_abs_delta=0.01,
        max_abs_delta=0.99,
        max_contracts_per_chain=4,
    )
    # 6 puts, puts-only hint → n_rights=1 → cap_per_right=4//1=4.
    raws = [_raw(strike=float(450 + i), delta=-(0.20 + i * 0.01)) for i in range(6)]
    chain = _run(raws, limits=limits, strategy_hint="bull_put_spread")
    assert len(chain.contracts) == 4
    assert chain.truncated is True
    assert chain.total_before_cap == 6


def test_no_truncation_when_under_cap() -> None:
    chain = _run([_raw()])
    assert chain.truncated is False
    assert chain.total_before_cap == 1


def test_two_sided_cap_per_right() -> None:
    # Cap of 4 for two-sided → 2 per right.
    limits = ChainFilterLimits(
        min_open_interest=0,
        max_spread_pct_of_mid=1.0,
        max_spread_abs_floor=100.0,
        min_dte=0,
        max_dte=100,
        min_abs_delta=0.01,
        max_abs_delta=0.99,
        max_contracts_per_chain=4,
    )
    raws = [
        _raw(right="put", strike=float(450 + i), delta=-(0.20 + i * 0.01))
        for i in range(4)
    ] + [
        _raw(right="call", strike=float(460 + i), delta=(0.20 + i * 0.01))
        for i in range(4)
    ]
    chain = _run(raws, limits=limits, strategy_hint="iron_condor")
    calls = [c for c in chain.contracts if c.right == "call"]
    puts = [c for c in chain.contracts if c.right == "put"]
    assert len(calls) == 2
    assert len(puts) == 2
    assert chain.truncated is True


def test_total_before_cap_excludes_non_filter_drops() -> None:
    # Contracts excluded by DTE or delta don't count toward total_before_cap.
    raws = [
        _raw(expiration=_EXP_TOO_FAR),  # excluded by DTE
        _raw(delta=-0.05),  # excluded by delta
        _raw(),  # passes
        _raw(strike=451.0, delta=-0.30),  # passes
    ]
    chain = _run(raws)
    assert chain.total_before_cap == 2


# ---------------------------------------------------------------------------
# Empty chain
# ---------------------------------------------------------------------------


def test_empty_raw_chain_returns_empty_filtered_chain() -> None:
    chain = _run([])
    assert chain.contracts == []
    assert chain.excluded_for_missing_greeks == 0
    assert chain.truncated is False
    assert chain.total_before_cap == 0


def test_all_contracts_filtered_out() -> None:
    raws = [_raw(expiration=_EXP_TOO_FAR), _raw(expiration=_EXP_TOO_NEAR)]
    chain = _run(raws)
    assert chain.contracts == []


# ---------------------------------------------------------------------------
# Relevance sort
# ---------------------------------------------------------------------------


def test_contracts_sorted_by_delta_proximity() -> None:
    # delta_centre = (0.15 + 0.45) / 2 = 0.30
    # Contracts: |delta|=0.45 (far), |delta|=0.30 (on centre), |delta|=0.20 (mid).
    # Expected order: 0.30, 0.20, 0.45.
    raws = [
        _raw(strike=440.0, delta=-0.45),  # distance = |0.45 - 0.30| = 0.15
        _raw(strike=450.0, delta=-0.30),  # distance = 0.00 — closest
        _raw(strike=455.0, delta=-0.20),  # distance = 0.10
    ]
    chain = _run(raws)
    deltas = [abs(c.delta) for c in chain.contracts]
    assert deltas == pytest.approx([0.30, 0.20, 0.45])


def test_spread_width_as_tiebreaker() -> None:
    # Two contracts at same delta, both passing spread filter.
    # mid≈1.225 → pct_limit=0.10*1.225=0.1225; spread=0.10 ≤ 0.1225 → passes.
    # mid≈1.20 → pct_limit=0.10*1.20=0.12; spread=0.05 ≤ 0.12 → passes.
    # Tighter spread (0.05) should sort first.
    raws = [
        _raw(strike=450.0, delta=-0.30, bid=1.15, ask=1.25),  # spread=0.10
        _raw(strike=451.0, delta=-0.30, bid=1.20, ask=1.25),  # spread=0.05 (tighter)
    ]
    chain = _run(raws)
    assert chain.contracts[0].spread_width == pytest.approx(0.05)
    assert chain.contracts[1].spread_width == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# get_held_leg_greeks
# ---------------------------------------------------------------------------

_AGED_EXPIRY = date(2026, 6, 28)  # 14 DTE from _TODAY — below dte_min=20


def _make_position(
    pos_id: str = "pos-001",
    underlying: str = "SPY",
    right: str = "put",
    strike: float = 450.0,
    expiration: date = _AGED_EXPIRY,
    filled_qty: int = 1,
) -> Position:
    return Position(
        id=pos_id,
        underlying=underlying,
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=Leg(
                    right=right,  # type: ignore[arg-type]
                    side="sell",
                    strike=strike,
                    expiration=expiration,
                ),
                filled_qty=filled_qty,
                avg_fill_price=1.50,
                status=LegStatus.OPEN,
            )
        ],
        quantity=1,
        entry_net_amount=-1.50,
        current_mark=-0.80,
        marked_at=datetime(2026, 6, 14, 14, 30, tzinfo=UTC),
        unrealized_pnl=70.0,
        realized_pnl=None,
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=7
        ),
        status=PositionStatus.OPEN,
        opened_at=datetime(2026, 6, 1, 15, 0, tzinfo=UTC),
        closed_at=None,
        nearest_expiration=expiration,
        est_max_loss=300.0,
        est_max_profit=150.0,
        opening_order_id="ord-001",
        asset_class=AssetClass.OPTION_STRATEGY,
    )


def _held_provider(
    contracts: list[RawOptionContract],
    price: float = _UNDERLYING_PRICE,
) -> DataProvider:
    mock = MagicMock(spec=DataProvider)
    mock.fetch_option_chain.return_value = contracts
    mock.fetch_latest_price.return_value = price
    return mock  # type: ignore[return-value]


def test_held_leg_greeks_returns_greeks_for_held_leg() -> None:
    position = _make_position(
        underlying="SPY", right="put", strike=450.0, expiration=_AGED_EXPIRY
    )
    raw = _raw(
        underlying="SPY",
        right="put",
        strike=450.0,
        expiration=_AGED_EXPIRY,
        delta=-0.35,
        vega=0.22,
        theta=-0.07,
    )
    provider = _held_provider([raw])
    result = get_held_leg_greeks([position], provider)

    key: LegKey = ("SPY", "put", 450.0, _AGED_EXPIRY.isoformat())
    assert key in result
    delta, vega, theta = result[key]
    assert delta == pytest.approx(-0.35)
    assert vega == pytest.approx(0.22)
    assert theta == pytest.approx(-0.07)


def test_held_leg_greeks_no_dte_filter_applied() -> None:
    # _AGED_EXPIRY is 14 DTE — below dte_min=20, excluded by get_filtered_chain.
    # get_held_leg_greeks must still return it.
    position = _make_position(expiration=_AGED_EXPIRY)
    raw = _raw(expiration=_AGED_EXPIRY, delta=-0.30, vega=0.18, theta=-0.05)
    provider = _held_provider([raw])
    result = get_held_leg_greeks([position], provider)

    key: LegKey = ("SPY", "put", 450.0, _AGED_EXPIRY.isoformat())
    assert key in result


def test_held_leg_greeks_no_delta_filter_applied() -> None:
    # |delta|=0.05 is below min_abs_delta=0.15; get_filtered_chain would drop it.
    # get_held_leg_greeks must still return it.
    position = _make_position(expiration=_AGED_EXPIRY)
    raw = _raw(expiration=_AGED_EXPIRY, delta=-0.05, vega=0.10, theta=-0.01)
    provider = _held_provider([raw])
    result = get_held_leg_greeks([position], provider)

    key: LegKey = ("SPY", "put", 450.0, _AGED_EXPIRY.isoformat())
    assert key in result
    assert result[key][0] == pytest.approx(-0.05)


def test_held_leg_greeks_omits_contract_with_none_delta() -> None:
    position = _make_position(expiration=_AGED_EXPIRY)
    raw = _raw(expiration=_AGED_EXPIRY, delta=None, vega=0.18, theta=-0.05)
    provider = _held_provider([raw])
    result = get_held_leg_greeks([position], provider)

    key: LegKey = ("SPY", "put", 450.0, _AGED_EXPIRY.isoformat())
    assert key not in result


def test_held_leg_greeks_omits_contract_with_none_vega() -> None:
    position = _make_position(expiration=_AGED_EXPIRY)
    raw = _raw(expiration=_AGED_EXPIRY, delta=-0.30, vega=None, theta=-0.05)
    provider = _held_provider([raw])
    result = get_held_leg_greeks([position], provider)

    key: LegKey = ("SPY", "put", 450.0, _AGED_EXPIRY.isoformat())
    assert key not in result


def test_held_leg_greeks_omits_contract_with_none_theta() -> None:
    position = _make_position(expiration=_AGED_EXPIRY)
    raw = _raw(expiration=_AGED_EXPIRY, delta=-0.30, vega=0.18, theta=None)
    provider = _held_provider([raw])
    result = get_held_leg_greeks([position], provider)

    key: LegKey = ("SPY", "put", 450.0, _AGED_EXPIRY.isoformat())
    assert key not in result


def test_held_leg_greeks_multiple_underlyings_fetched() -> None:
    spy_pos = _make_position(underlying="SPY", expiration=_AGED_EXPIRY)
    aapl_pos = _make_position(
        pos_id="pos-002",
        underlying="AAPL",
        strike=185.0,
        expiration=_AGED_EXPIRY,
    )

    def _multi_side_effect(sym: str) -> list[RawOptionContract]:
        strike = 450.0 if sym == "SPY" else 185.0
        delta = -0.30 if sym == "SPY" else -0.25
        return [
            _raw(underlying=sym, strike=strike, expiration=_AGED_EXPIRY, delta=delta)
        ]

    mock = MagicMock(spec=DataProvider)
    mock.fetch_option_chain.side_effect = _multi_side_effect
    get_held_leg_greeks([spy_pos, aapl_pos], mock)  # type: ignore[arg-type]

    assert mock.fetch_option_chain.call_count == 2
    called_with = {c.args[0] for c in mock.fetch_option_chain.call_args_list}
    assert called_with == {"SPY", "AAPL"}


def test_held_leg_greeks_provider_error_logs_warning_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spy_pos = _make_position(underlying="SPY", expiration=_AGED_EXPIRY)
    aapl_pos = _make_position(
        pos_id="pos-002",
        underlying="AAPL",
        strike=185.0,
        expiration=_AGED_EXPIRY,
    )
    good_raw = _raw(
        underlying="SPY",
        strike=450.0,
        expiration=_AGED_EXPIRY,
        delta=-0.30,
        vega=0.18,
        theta=-0.05,
    )

    mock = MagicMock(spec=DataProvider)

    def side_effect(sym: str) -> list[RawOptionContract]:
        if sym == "AAPL":
            raise DataUnavailableError(
                "fetch_option_chain", "AAPL", RuntimeError("timeout")
            )
        return [good_raw]

    mock.fetch_option_chain.side_effect = side_effect
    result = get_held_leg_greeks([spy_pos, aapl_pos], mock)  # type: ignore[arg-type]

    # SPY should still be present despite AAPL failing.
    spy_key: LegKey = ("SPY", "put", 450.0, _AGED_EXPIRY.isoformat())
    assert spy_key in result

    # A warning should have been logged for AAPL.
    assert any("AAPL" in r.message for r in caplog.records)


def test_held_leg_greeks_auth_error_logs_warning_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spy_pos = _make_position(underlying="SPY", expiration=_AGED_EXPIRY)
    aapl_pos = _make_position(
        pos_id="pos-002",
        underlying="AAPL",
        strike=185.0,
        expiration=_AGED_EXPIRY,
    )
    good_raw = _raw(
        underlying="SPY",
        strike=450.0,
        expiration=_AGED_EXPIRY,
        delta=-0.30,
        vega=0.18,
        theta=-0.05,
    )

    mock = MagicMock(spec=DataProvider)

    def side_effect(sym: str) -> list[RawOptionContract]:
        if sym == "AAPL":
            raise DataAuthError("Alpaca rejected credentials (401)")
        return [good_raw]

    mock.fetch_option_chain.side_effect = side_effect
    result = get_held_leg_greeks([spy_pos, aapl_pos], mock)  # type: ignore[arg-type]

    spy_key: LegKey = ("SPY", "put", 450.0, _AGED_EXPIRY.isoformat())
    assert spy_key in result
    assert any("AAPL" in r.message for r in caplog.records)


def test_held_leg_greeks_empty_positions_returns_empty() -> None:
    provider = _held_provider([])
    result = get_held_leg_greeks([], provider)
    assert result == {}
    provider.fetch_option_chain.assert_not_called()  # type: ignore[attr-defined]


def test_held_leg_greeks_equity_position_skipped() -> None:
    equity_pos = Position(
        id="eq-001",
        underlying="SPY",
        strategy="equity",
        legs=[],  # EQUITY positions have no option legs
        quantity=100,
        entry_net_amount=54520.0,
        current_mark=54520.0,
        marked_at=datetime(2026, 6, 14, 14, 30, tzinfo=UTC),
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=None,
        status=PositionStatus.OPEN,
        opened_at=datetime(2026, 6, 14, 14, 0, tzinfo=UTC),
        closed_at=None,
        nearest_expiration=date(9999, 12, 31),
        est_max_loss=0.0,
        est_max_profit=0.0,
        opening_order_id="ord-eq-001",
        asset_class=AssetClass.EQUITY,
    )
    provider = _held_provider([])
    result = get_held_leg_greeks([equity_pos], provider)
    assert result == {}
    provider.fetch_option_chain.assert_not_called()  # type: ignore[attr-defined]
