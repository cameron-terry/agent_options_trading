from datetime import UTC, date, datetime

import pytest

from options_agent.contracts import (
    ChainFilterParams,
    EarningsEvent,
    EventInfo,
    ExDividendEvent,
    ExitPlan,
    FilteredChain,
    Leg,
    LegStatus,
    MacroEvent,
    OptionContract,
    PortfolioState,
    Position,
    PositionLeg,
    PositionStatus,
    SymbolSnapshot,
    UniverseSnapshot,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 7, 14, 30, tzinfo=UTC)
_TODAY = date(2026, 6, 7)
_EXPIRY = date(2026, 7, 18)

_FILTER_PARAMS = ChainFilterParams(
    dte_min=20,
    dte_max=60,
    delta_min=0.15,
    delta_max=0.45,
    min_open_interest=100,
    max_spread_pct_of_mid=0.10,
    max_spread_abs_floor=0.05,
)

_CONTRACT = OptionContract(
    symbol="SPY260718P00450000",
    strike=450.0,
    expiration=_EXPIRY,
    right="put",
    bid=1.20,
    ask=1.30,
    mid=1.25,
    volume=850,
    open_interest=4200,
    delta=-0.28,
    theta=-0.08,
    vega=0.22,
    iv=0.24,
    spread_width=0.10,
    dte=41,
)


def _make_exit_plan() -> ExitPlan:
    return ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21)


def _make_position_leg() -> PositionLeg:
    leg = Leg(right="put", side="sell", strike=450.0, expiration=_EXPIRY)
    return PositionLeg(
        leg=leg, filled_qty=5, avg_fill_price=1.25, status=LegStatus.OPEN
    )


def _make_position(**overrides: object) -> Position:
    defaults: dict = {
        "id": "pos-001",
        "underlying": "SPY",
        "strategy": "bull_put_spread",
        "legs": [_make_position_leg()],
        "quantity": 5,
        "entry_net_amount": -312.50,
        "current_mark": -200.00,
        "marked_at": _NOW,
        "unrealized_pnl": 112.50,
        "realized_pnl": None,
        "exit_plan": _make_exit_plan(),
        "status": PositionStatus.OPEN,
        "opened_at": _NOW,
        "closed_at": None,
        "nearest_expiration": _EXPIRY,
        "est_max_loss": 2187.50,
        "est_max_profit": 312.50,
        "opening_order_id": "ord-001",
    }
    defaults.update(overrides)
    return Position(**defaults)


def _make_portfolio_state(**overrides: object) -> PortfolioState:
    defaults: dict = {
        "positions": [_make_position()],
        "account_equity": 50_000.0,
        "buying_power": 35_000.0,
        "options_buying_power": 20_000.0,
        "unrealized_pnl": 112.50,
        "realized_pnl_today": 0.0,
        "approval_level": 3,
        "net_dollar_delta": -1_250.0,
        "net_dollar_gamma": 45.0,
        "net_dollar_theta": 42.50,
        "net_dollar_vega": -380.0,
    }
    defaults.update(overrides)
    return PortfolioState(**defaults)


def _make_symbol_snapshot(**overrides: object) -> SymbolSnapshot:
    defaults: dict = {
        "symbol": "SPY",
        "price": 456.32,
        "iv_rank": 65.0,
        "iv_percentile": 72.0,
        "historical_vol": 0.18,
        "regime": "neutral",
        "days_to_earnings": None,
    }
    defaults.update(overrides)
    return SymbolSnapshot(**defaults)


def _make_universe_snapshot(**overrides: object) -> UniverseSnapshot:
    snap = _make_symbol_snapshot()
    defaults: dict = {
        "symbol_snapshots": {"SPY": snap},
        "vix_level": 18.4,
        "market_regime": "neutral",
        "macro_events": [],
        "as_of": _NOW,
    }
    defaults.update(overrides)
    return UniverseSnapshot(**defaults)


def _make_event_info(**overrides: object) -> EventInfo:
    defaults: dict = {
        "symbol": "AAPL",
        "earnings": None,
        "ex_dividend": None,
    }
    defaults.update(overrides)
    return EventInfo(**defaults)


# ---------------------------------------------------------------------------
# ChainFilterParams
# ---------------------------------------------------------------------------


def test_chain_filter_params_construction() -> None:
    p = _FILTER_PARAMS
    assert p.dte_min == 20
    assert p.dte_max == 60
    assert p.delta_min == 0.15
    assert p.delta_max == 0.45
    assert p.min_open_interest == 100
    assert p.max_spread_pct_of_mid == 0.10
    assert p.max_spread_abs_floor == 0.05


def test_chain_filter_params_round_trip() -> None:
    restored = ChainFilterParams.model_validate(_FILTER_PARAMS.model_dump())
    assert restored == _FILTER_PARAMS


# ---------------------------------------------------------------------------
# OptionContract
# ---------------------------------------------------------------------------


def test_option_contract_construction() -> None:
    c = _CONTRACT
    assert c.symbol == "SPY260718P00450000"
    assert c.strike == 450.0
    assert c.right == "put"
    assert c.dte == 41
    assert c.spread_width == 0.10


def test_option_contract_call_right() -> None:
    c = OptionContract(
        symbol="SPY260718C00460000",
        strike=460.0,
        expiration=_EXPIRY,
        right="call",
        bid=0.80,
        ask=0.90,
        mid=0.85,
        volume=300,
        open_interest=1500,
        delta=0.22,
        theta=-0.06,
        vega=0.18,
        iv=0.22,
        spread_width=0.10,
        dte=41,
    )
    assert c.right == "call"
    assert c.delta > 0


def test_option_contract_no_gamma_field() -> None:
    # gamma is intentionally absent from OptionContract
    assert not hasattr(_CONTRACT, "gamma")


def test_option_contract_round_trip() -> None:
    assert OptionContract.model_validate(_CONTRACT.model_dump()) == _CONTRACT


def test_option_contract_invalid_right_rejected() -> None:
    from pydantic import ValidationError

    bad: dict = _CONTRACT.model_dump()
    bad["right"] = "forward"  # only "call" or "put" are valid
    with pytest.raises(ValidationError):
        OptionContract.model_validate(bad)


# ---------------------------------------------------------------------------
# FilteredChain
# ---------------------------------------------------------------------------


def test_filtered_chain_construction() -> None:
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=456.32,
        as_of=_NOW,
        filter_params=_FILTER_PARAMS,
        contracts=[_CONTRACT],
    )
    assert chain.underlying == "SPY"
    assert chain.underlying_price == 456.32
    assert len(chain.contracts) == 1
    assert chain.contracts[0].symbol == "SPY260718P00450000"


def test_filtered_chain_empty_contracts() -> None:
    chain = FilteredChain(
        underlying="XYZ",
        underlying_price=100.0,
        as_of=_NOW,
        filter_params=_FILTER_PARAMS,
        contracts=[],
    )
    assert chain.contracts == []


def test_filtered_chain_filter_params_embedded() -> None:
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=456.32,
        as_of=_NOW,
        filter_params=_FILTER_PARAMS,
        contracts=[_CONTRACT],
    )
    assert chain.filter_params.dte_min == 20
    assert chain.filter_params.delta_max == 0.45


def test_filtered_chain_round_trip() -> None:
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=456.32,
        as_of=_NOW,
        filter_params=_FILTER_PARAMS,
        contracts=[_CONTRACT],
    )
    assert FilteredChain.model_validate(chain.model_dump()) == chain


def test_filtered_chain_metadata_defaults() -> None:
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=456.32,
        as_of=_NOW,
        filter_params=_FILTER_PARAMS,
        contracts=[_CONTRACT],
    )
    assert chain.strategy_hint is None
    assert chain.oi_available is True
    assert chain.excluded_for_missing_greeks == 0
    assert chain.truncated is False
    assert chain.total_before_cap == 0


def test_filtered_chain_metadata_explicit() -> None:
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=456.32,
        as_of=_NOW,
        filter_params=_FILTER_PARAMS,
        contracts=[_CONTRACT],
        strategy_hint="iron_condor",
        oi_available=False,
        excluded_for_missing_greeks=3,
        truncated=True,
        total_before_cap=150,
    )
    assert chain.strategy_hint == "iron_condor"
    assert chain.oi_available is False
    assert chain.excluded_for_missing_greeks == 3
    assert chain.truncated is True
    assert chain.total_before_cap == 150


def test_option_contract_none_volume_oi() -> None:
    c = OptionContract(
        symbol="SPY260718P00450000",
        strike=450.0,
        expiration=_EXPIRY,
        right="put",
        bid=1.20,
        ask=1.30,
        mid=1.25,
        volume=None,
        open_interest=None,
        delta=-0.28,
        theta=-0.08,
        vega=0.22,
        iv=0.24,
        spread_width=0.10,
        dte=41,
    )
    assert c.volume is None
    assert c.open_interest is None


# ---------------------------------------------------------------------------
# PortfolioState
# ---------------------------------------------------------------------------


def test_portfolio_state_construction() -> None:
    ps = _make_portfolio_state()
    assert ps.account_equity == 50_000.0
    assert ps.approval_level == 3
    assert len(ps.positions) == 1
    assert ps.net_dollar_delta == -1_250.0


def test_portfolio_state_net_greeks_present() -> None:
    ps = _make_portfolio_state()
    assert hasattr(ps, "net_dollar_delta")
    assert hasattr(ps, "net_dollar_gamma")
    assert hasattr(ps, "net_dollar_theta")
    assert hasattr(ps, "net_dollar_vega")


def test_portfolio_state_options_buying_power() -> None:
    ps = _make_portfolio_state(options_buying_power=18_000.0)
    assert ps.options_buying_power == 18_000.0


def test_portfolio_state_realized_pnl_today_zero() -> None:
    ps = _make_portfolio_state(realized_pnl_today=0.0)
    assert ps.realized_pnl_today == 0.0


def test_portfolio_state_round_trip() -> None:
    ps = _make_portfolio_state()
    assert PortfolioState.model_validate(ps.model_dump()) == ps


def test_portfolio_state_round_trip_json() -> None:
    ps = _make_portfolio_state()
    assert PortfolioState.model_validate_json(ps.model_dump_json()) == ps


def test_portfolio_state_empty_positions() -> None:
    ps = _make_portfolio_state(positions=[])
    assert ps.positions == []
    assert ps.account_equity == 50_000.0


# ---------------------------------------------------------------------------
# SymbolSnapshot
# ---------------------------------------------------------------------------


def test_symbol_snapshot_construction() -> None:
    s = _make_symbol_snapshot()
    assert s.symbol == "SPY"
    assert s.price == 456.32
    assert s.iv_rank == 65.0
    assert s.days_to_earnings is None


def test_symbol_snapshot_optional_fields_none() -> None:
    s = SymbolSnapshot(
        symbol="NFLX",
        price=620.0,
        iv_rank=None,
        iv_percentile=None,
        historical_vol=None,
        regime=None,
        days_to_earnings=None,
    )
    assert s.iv_rank is None
    assert s.historical_vol is None


def test_symbol_snapshot_days_to_earnings_integer() -> None:
    s = _make_symbol_snapshot(days_to_earnings=14)
    assert s.days_to_earnings == 14


def test_symbol_snapshot_round_trip() -> None:
    s = _make_symbol_snapshot()
    assert SymbolSnapshot.model_validate(s.model_dump()) == s


# ---------------------------------------------------------------------------
# MacroEvent
# ---------------------------------------------------------------------------


def test_macro_event_construction() -> None:
    e = MacroEvent(name="FOMC Meeting", event_date=date(2026, 7, 29), event_type="FOMC")
    assert e.event_type == "FOMC"
    assert e.event_date == date(2026, 7, 29)


def test_macro_event_other_type() -> None:
    e = MacroEvent(
        name="Quarterly GDP", event_date=date(2026, 7, 30), event_type="OTHER"
    )
    assert e.event_type == "OTHER"


def test_macro_event_round_trip() -> None:
    e = MacroEvent(name="CPI Release", event_date=date(2026, 7, 15), event_type="CPI")
    assert MacroEvent.model_validate(e.model_dump()) == e


# ---------------------------------------------------------------------------
# UniverseSnapshot
# ---------------------------------------------------------------------------


def test_universe_snapshot_construction() -> None:
    u = _make_universe_snapshot()
    assert "SPY" in u.symbol_snapshots
    assert u.vix_level == 18.4
    assert u.market_regime == "neutral"
    assert u.macro_events == []


def test_universe_snapshot_dict_keyed_by_symbol() -> None:
    spy = _make_symbol_snapshot(symbol="SPY")
    qqq = _make_symbol_snapshot(symbol="QQQ", price=480.0, iv_rank=55.0)
    u = _make_universe_snapshot(symbol_snapshots={"SPY": spy, "QQQ": qqq})
    assert u.symbol_snapshots["QQQ"].price == 480.0


def test_universe_snapshot_vix_at_top_level() -> None:
    # vix_level lives on UniverseSnapshot, not SymbolSnapshot
    u = _make_universe_snapshot()
    assert hasattr(u, "vix_level")
    assert not hasattr(u.symbol_snapshots["SPY"], "vix_level")


def test_universe_snapshot_macro_events() -> None:
    fomc = MacroEvent(name="FOMC", event_date=date(2026, 7, 29), event_type="FOMC")
    u = _make_universe_snapshot(macro_events=[fomc])
    assert len(u.macro_events) == 1
    assert u.macro_events[0].event_type == "FOMC"


def test_universe_snapshot_round_trip() -> None:
    u = _make_universe_snapshot()
    assert UniverseSnapshot.model_validate(u.model_dump()) == u


def test_universe_snapshot_round_trip_json() -> None:
    u = _make_universe_snapshot()
    assert UniverseSnapshot.model_validate_json(u.model_dump_json()) == u


# ---------------------------------------------------------------------------
# EarningsEvent / ExDividendEvent
# ---------------------------------------------------------------------------


def test_earnings_event_confirmed() -> None:
    e = EarningsEvent(event_date=date(2026, 7, 22), confirmed=True)
    assert e.confirmed is True
    assert e.event_date == date(2026, 7, 22)


def test_earnings_event_estimated() -> None:
    e = EarningsEvent(event_date=date(2026, 7, 28), confirmed=False)
    assert e.confirmed is False


def test_earnings_event_round_trip() -> None:
    e = EarningsEvent(event_date=date(2026, 7, 22), confirmed=True)
    assert EarningsEvent.model_validate(e.model_dump()) == e


def test_ex_dividend_event_construction() -> None:
    e = ExDividendEvent(event_date=date(2026, 7, 10), amount=0.68)
    assert e.amount == 0.68
    assert e.event_date == date(2026, 7, 10)


def test_ex_dividend_event_round_trip() -> None:
    e = ExDividendEvent(event_date=date(2026, 7, 10), amount=0.68)
    assert ExDividendEvent.model_validate(e.model_dump()) == e


# ---------------------------------------------------------------------------
# EventInfo
# ---------------------------------------------------------------------------


def test_event_info_no_events() -> None:
    ei = _make_event_info()
    assert ei.earnings is None
    assert ei.ex_dividend is None


def test_event_info_with_earnings() -> None:
    earnings = EarningsEvent(event_date=date(2026, 7, 22), confirmed=True)
    ei = _make_event_info(earnings=earnings)
    assert ei.earnings is not None
    assert ei.earnings.confirmed is True


def test_event_info_with_ex_dividend() -> None:
    exdiv = ExDividendEvent(event_date=date(2026, 7, 10), amount=0.68)
    ei = _make_event_info(symbol="SPY", ex_dividend=exdiv)
    assert ei.ex_dividend is not None
    assert ei.ex_dividend.amount == 0.68


def test_event_info_both_events() -> None:
    earnings = EarningsEvent(event_date=date(2026, 7, 22), confirmed=False)
    exdiv = ExDividendEvent(event_date=date(2026, 7, 10), amount=1.20)
    ei = EventInfo(symbol="MSFT", earnings=earnings, ex_dividend=exdiv)
    assert ei.earnings is not None
    assert ei.ex_dividend is not None


def test_event_info_round_trip() -> None:
    earnings = EarningsEvent(event_date=date(2026, 7, 22), confirmed=True)
    ei = _make_event_info(earnings=earnings)
    assert EventInfo.model_validate(ei.model_dump()) == ei


def test_event_info_round_trip_json() -> None:
    earnings = EarningsEvent(event_date=date(2026, 7, 22), confirmed=True)
    ei = _make_event_info(earnings=earnings)
    assert EventInfo.model_validate_json(ei.model_dump_json()) == ei


def test_event_info_dict_by_symbol() -> None:
    # Verifies the intended return shape of get_events(): dict[str, EventInfo]
    result: dict[str, EventInfo] = {
        "AAPL": _make_event_info(
            symbol="AAPL",
            earnings=EarningsEvent(event_date=date(2026, 7, 28), confirmed=False),
        ),
        "SPY": _make_event_info(symbol="SPY"),
    }
    assert result["AAPL"].earnings is not None
    assert result["SPY"].earnings is None
