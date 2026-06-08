"""Tests for risk/validator.py.

WP-4.3: structural validity checks (validate_from_dict, validate_structural)
WP-4.5: market-access checks (validate_market_access)

Coverage targets:
  - validate_from_dict: INVALID_SCHEMA on bad raw dict
  - validate_structural: one valid proposal passes all three checks
  - validate_structural: UNKNOWN_STRATEGY fires independently
  - validate_structural: NAKED_SHORT fires independently for calls and puts
  - validate_structural: NO_ACTION with empty legs is not a naked short
  - _check_naked_short: iron condor (4-leg, all covered) passes
  - _check_naked_short: 1x2 call ratio spread (sell 2, buy 1) is rejected
  - _check_naked_short: single naked short call is rejected
  - _check_naked_short: single naked short put is rejected
  - validate_market_access: kill-switch HALT/FLATTEN short-circuits
  - validate_market_access: liquidity — no chain fails closed; bad spread/OI rejected
  - validate_market_access: exit plan bounds check per field
  - validate_market_access: event gate (fail-closed on missing data;
    near-earnings rejects)
  - validate_market_access: buying power floor
  - validate_market_access: duplicate (same strategy + overlapping expiration)
  - validate_market_access: conflict detection (opposing delta direction)
"""

from datetime import UTC, date, datetime
from typing import Any

from options_agent.contracts.data import (
    ChainFilterParams,
    FilteredChain,
    OptionContract,
    PortfolioState,
    SymbolSnapshot,
)
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import Severity, ValidationRuleId
from options_agent.contracts.state import (
    KillSwitchState,
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.risk.limits import Limits
from options_agent.risk.validator import (
    validate_from_dict,
    validate_market_access,
    validate_structural,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_EXIT = ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21)
_EXP = date(2026, 8, 15)


def _put_spread_legs() -> list[Leg]:
    return [
        Leg(right="put", side="sell", strike=450.0, expiration=_EXP),
        Leg(right="put", side="buy", strike=445.0, expiration=_EXP),
    ]


def _make_proposal(**overrides: object) -> TradeProposal:
    defaults: dict = {
        "action": "OPEN",
        "underlying": "SPY",
        "strategy": "bull_put_spread",
        "legs": _put_spread_legs(),
        "thesis": "bullish bias at support",
        "iv_rationale": "IV rank 65th percentile, selling premium",
        "catalyst_check": "no earnings within 30 days",
        "conviction": 0.7,
        "est_max_loss": 350.0,
        "est_max_profit": 150.0,
        "breakevens": [447.50],
        "net_delta": 0.12,
        "net_theta": 8.50,
        "net_vega": -0.30,
        "exit_plan": _EXIT,
        "informed_by": [],
    }
    defaults.update(overrides)
    return TradeProposal(**defaults)


def _default_limits() -> Limits:
    return Limits()


# ---------------------------------------------------------------------------
# validate_from_dict — INVALID_SCHEMA
# ---------------------------------------------------------------------------


def test_invalid_schema_missing_required_field() -> None:
    raw = _make_proposal().model_dump()
    del raw["underlying"]
    result = validate_from_dict(raw, _default_limits())
    assert not result.passed
    assert len(result.reasons) == 1
    r = result.reasons[0]
    assert r.rule_id == ValidationRuleId.INVALID_SCHEMA
    assert r.severity == Severity.ERROR
    assert r.field_affected == "underlying"


def test_invalid_schema_conviction_out_of_range() -> None:
    raw = _make_proposal().model_dump()
    raw["conviction"] = 2.5
    result = validate_from_dict(raw, _default_limits())
    assert not result.passed
    assert result.reasons[0].rule_id == ValidationRuleId.INVALID_SCHEMA
    assert "conviction" in (result.reasons[0].field_affected or "")


def test_invalid_schema_bad_action_literal() -> None:
    raw = _make_proposal().model_dump()
    raw["action"] = "BUY_LOTS"
    result = validate_from_dict(raw, _default_limits())
    assert not result.passed
    assert result.reasons[0].rule_id == ValidationRuleId.INVALID_SCHEMA


def test_valid_dict_passes_all_structural_checks() -> None:
    raw = _make_proposal().model_dump()
    result = validate_from_dict(raw, _default_limits())
    assert result.passed
    assert result.reasons == []


# ---------------------------------------------------------------------------
# validate_structural — one valid proposal
# ---------------------------------------------------------------------------


def test_valid_proposal_passes() -> None:
    result = validate_structural(_make_proposal(), _default_limits())
    assert result.passed
    assert result.reasons == []


# ---------------------------------------------------------------------------
# validate_structural — UNKNOWN_STRATEGY
# ---------------------------------------------------------------------------


def test_unknown_strategy_rejected() -> None:
    proposal = _make_proposal(strategy="long_straddle")
    result = validate_structural(proposal, _default_limits())
    assert not result.passed
    assert len(result.reasons) == 1
    r = result.reasons[0]
    assert r.rule_id == ValidationRuleId.UNKNOWN_STRATEGY
    assert r.severity == Severity.ERROR
    assert r.field_affected == "strategy"
    assert "long_straddle" in r.human_message


def test_unknown_strategy_does_not_also_check_naked_short() -> None:
    # A naked-short proposal with an unknown strategy should only report the
    # strategy error — not both. Checks stop at the first ERROR.
    naked_legs = [Leg(right="call", side="sell", strike=500.0, expiration=_EXP)]
    proposal = _make_proposal(strategy="unknown", legs=naked_legs)
    result = validate_structural(proposal, _default_limits())
    assert not result.passed
    assert all(r.rule_id == ValidationRuleId.UNKNOWN_STRATEGY for r in result.reasons)


def test_all_default_strategies_pass_playbook_check() -> None:
    limits = _default_limits()
    for strategy in limits.allowed_strategies:
        proposal = _make_proposal(strategy=strategy)
        result = validate_structural(proposal, limits)
        # Strategy check should pass; other checks may or may not pass
        assert not any(
            r.rule_id == ValidationRuleId.UNKNOWN_STRATEGY for r in result.reasons
        )


# ---------------------------------------------------------------------------
# validate_structural — NAKED_SHORT
# ---------------------------------------------------------------------------


def test_naked_short_call_rejected() -> None:
    legs = [Leg(right="call", side="sell", strike=500.0, expiration=_EXP)]
    proposal = _make_proposal(legs=legs)
    result = validate_structural(proposal, _default_limits())
    assert not result.passed
    r = result.reasons[0]
    assert r.rule_id == ValidationRuleId.NAKED_SHORT
    assert r.severity == Severity.ERROR
    assert "call" in r.human_message
    assert r.field_affected is not None
    assert "call" in r.field_affected


def test_naked_short_put_rejected() -> None:
    legs = [Leg(right="put", side="sell", strike=440.0, expiration=_EXP)]
    proposal = _make_proposal(legs=legs)
    result = validate_structural(proposal, _default_limits())
    assert not result.passed
    r = result.reasons[0]
    assert r.rule_id == ValidationRuleId.NAKED_SHORT
    assert "put" in r.human_message


def test_ratio_spread_1x2_call_rejected() -> None:
    # Buy 1 call, sell 2 calls — the extra sell is uncovered.
    legs = [
        Leg(right="call", side="buy", strike=500.0, expiration=_EXP, ratio=1),
        Leg(right="call", side="sell", strike=510.0, expiration=_EXP, ratio=2),
    ]
    proposal = _make_proposal(legs=legs)
    result = validate_structural(proposal, _default_limits())
    assert not result.passed
    assert result.reasons[0].rule_id == ValidationRuleId.NAKED_SHORT


def test_covered_call_spread_passes() -> None:
    # sell 1 call + buy 1 call (vertical call spread)
    legs = [
        Leg(right="call", side="sell", strike=510.0, expiration=_EXP),
        Leg(right="call", side="buy", strike=500.0, expiration=_EXP),
    ]
    proposal = _make_proposal(strategy="bear_call_spread", legs=legs)
    result = validate_structural(proposal, _default_limits())
    assert result.passed


def test_iron_condor_passes_naked_short_check() -> None:
    # Iron condor: sell put + buy put + sell call + buy call — all covered.
    legs = [
        Leg(right="put", side="sell", strike=440.0, expiration=_EXP),
        Leg(right="put", side="buy", strike=435.0, expiration=_EXP),
        Leg(right="call", side="sell", strike=460.0, expiration=_EXP),
        Leg(right="call", side="buy", strike=465.0, expiration=_EXP),
    ]
    proposal = _make_proposal(strategy="iron_condor", legs=legs)
    result = validate_structural(proposal, _default_limits())
    assert result.passed


def test_no_action_empty_legs_passes_naked_short_check() -> None:
    proposal = _make_proposal(action="NO_ACTION", legs=[])
    result = validate_structural(proposal, _default_limits())
    assert result.passed


def test_naked_short_unconditional_no_config_override() -> None:
    # Even if we construct a custom Limits that might theoretically allow
    # uncovered shorts, the check must still fire — it reads no config flag.
    limits = _default_limits()
    legs = [Leg(right="call", side="sell", strike=500.0, expiration=_EXP)]
    proposal = _make_proposal(legs=legs)
    result = validate_structural(proposal, limits)
    assert not result.passed
    assert result.reasons[0].rule_id == ValidationRuleId.NAKED_SHORT


# ---------------------------------------------------------------------------
# Limits — allowed_strategies loads from config.toml
# ---------------------------------------------------------------------------


def test_allowed_strategies_loads_from_toml() -> None:
    from pathlib import Path

    from options_agent.config import Config

    config = Config.from_toml(Path("config.toml"))
    assert "bull_put_spread" in config.limits.allowed_strategies
    assert "iron_condor" in config.limits.allowed_strategies


def test_custom_limits_with_restricted_strategies() -> None:
    limits = Limits(allowed_strategies=frozenset({"iron_condor"}))
    proposal = _make_proposal(strategy="bull_put_spread")
    result = validate_structural(proposal, limits)
    assert not result.passed
    assert result.reasons[0].rule_id == ValidationRuleId.UNKNOWN_STRATEGY


# ===========================================================================
# validate_market_access — WP-4.5
# ===========================================================================

_NOW = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Shared market-access fixtures
# ---------------------------------------------------------------------------


def _make_portfolio(**overrides: Any) -> PortfolioState:
    defaults: dict = {
        "positions": [],
        "account_equity": 100_000.0,
        "buying_power": 30_000.0,
        "options_buying_power": 15_000.0,  # 15% of equity — above 10% floor
        "unrealized_pnl": 0.0,
        "realized_pnl_today": 0.0,
        "approval_level": 2,
        "net_dollar_delta": 0.0,
        "net_dollar_gamma": 0.0,
        "net_dollar_theta": 50.0,
        "net_dollar_vega": -200.0,
    }
    defaults.update(overrides)
    return PortfolioState(**defaults)


def _make_snapshot(**overrides: Any) -> SymbolSnapshot:
    defaults: dict = {
        "symbol": "SPY",
        "price": 455.0,
        "iv_rank": 65.0,
        "iv_percentile": 70.0,
        "historical_vol": 0.18,
        "regime": "normal",
        "days_to_earnings": 30,  # well beyond 5-day blackout
    }
    defaults.update(overrides)
    return SymbolSnapshot(**defaults)


def _make_option_contract(
    strike: float,
    right: str,
    expiration: date,
    **overrides: Any,
) -> OptionContract:
    mid = 2.50
    defaults: dict = {
        "symbol": f"SPY{right[0].upper()}{int(strike)}",
        "strike": strike,
        "expiration": expiration,
        "right": right,
        "bid": 2.40,
        "ask": 2.60,
        "mid": mid,
        "volume": 500,
        "open_interest": 1000,  # above 500 min
        "delta": -0.30 if right == "put" else 0.30,
        "theta": -0.05,
        "vega": 0.10,
        "iv": 0.25,
        "spread_width": 0.20,  # 8% of mid — within 10% limit
        "dte": 45,
    }
    defaults.update(overrides)
    return OptionContract(**defaults)


def _make_chain(legs: list[Leg] | None = None) -> FilteredChain:
    if legs is None:
        legs = _put_spread_legs()
    return FilteredChain(
        underlying="SPY",
        underlying_price=455.0,
        as_of=_NOW,
        filter_params=ChainFilterParams(
            dte_min=20,
            dte_max=45,
            delta_min=0.15,
            delta_max=0.45,
            min_open_interest=500,
            max_spread_width=0.10,
        ),
        contracts=[
            _make_option_contract(leg.strike, leg.right, leg.expiration) for leg in legs
        ],
    )


def _make_open_position(**overrides: Any) -> Position:
    defaults: dict = {
        "id": "pos-001",
        "underlying": "SPY",
        "strategy": "bull_put_spread",
        "legs": [
            PositionLeg(
                leg=Leg(right="put", side="sell", strike=450.0, expiration=_EXP),
                filled_qty=5,
                avg_fill_price=1.25,
                status=LegStatus.OPEN,
            )
        ],
        "quantity": 5,
        "entry_net_amount": -312.50,
        "current_mark": -200.00,
        "marked_at": _NOW,
        "unrealized_pnl": 112.50,
        "realized_pnl": None,
        "exit_plan": _EXIT,
        "status": PositionStatus.OPEN,
        "opened_at": _NOW,
        "closed_at": None,
        "nearest_expiration": _EXP,  # same as proposal default
        "est_max_loss": 2187.50,
        "est_max_profit": 312.50,
        "opening_order_id": "ord-001",
    }
    defaults.update(overrides)
    return Position(**defaults)


_UNSET: object = object()  # sentinel — distinguishes "not passed" from explicit None


def _run(
    proposal: TradeProposal | None = None,
    limits: Limits | None = None,
    snapshot: SymbolSnapshot | None | object = _UNSET,
    portfolio: PortfolioState | None = None,
    kill_switch: KillSwitchState = KillSwitchState.NONE,
    chain: FilteredChain | None | object = _UNSET,
) -> list:
    """Helper that fills happy-path defaults so each test only touches one axis.

    Pass ``snapshot=None`` or ``chain=None`` to explicitly test the None path.
    Omit the argument to get a valid default.
    """
    if proposal is None:
        proposal = _make_proposal()
    if limits is None:
        limits = _default_limits()
    if portfolio is None:
        portfolio = _make_portfolio()
    resolved_snapshot = _make_snapshot() if snapshot is _UNSET else snapshot
    resolved_chain = _make_chain() if chain is _UNSET else chain
    return validate_market_access(
        proposal=proposal,
        limits=limits,
        symbol_snapshot=resolved_snapshot,  # type: ignore[arg-type]
        portfolio=portfolio,
        kill_switch_state=kill_switch,
        filtered_chain=resolved_chain,  # type: ignore[arg-type]
    )


def _rule_ids(reasons: list) -> list[ValidationRuleId]:
    return [r.rule_id for r in reasons]


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


def test_kill_switch_halt_rejects() -> None:
    reasons = _run(kill_switch=KillSwitchState.HALT)
    assert len(reasons) == 1
    assert reasons[0].rule_id == ValidationRuleId.KILL_SWITCH
    assert reasons[0].severity == Severity.ERROR
    assert "HALT" in reasons[0].human_message


def test_kill_switch_flatten_rejects() -> None:
    reasons = _run(kill_switch=KillSwitchState.FLATTEN)
    assert len(reasons) == 1
    assert reasons[0].rule_id == ValidationRuleId.KILL_SWITCH
    assert "FLATTEN" in reasons[0].human_message


def test_kill_switch_none_passes() -> None:
    reasons = _run(kill_switch=KillSwitchState.NONE)
    assert not any(r.rule_id == ValidationRuleId.KILL_SWITCH for r in reasons)


def test_kill_switch_short_circuits_other_checks() -> None:
    # HALT with a drained portfolio — only KILL_SWITCH should fire, not BUYING_POWER.
    poor_portfolio = _make_portfolio(options_buying_power=0.0)
    reasons = _run(kill_switch=KillSwitchState.HALT, portfolio=poor_portfolio)
    assert _rule_ids(reasons) == [ValidationRuleId.KILL_SWITCH]


# ---------------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------------


def test_liquidity_no_chain_fails_closed_for_each_leg() -> None:
    reasons = _run(chain=None)
    assert len(reasons) == len(_put_spread_legs())
    assert all(r.rule_id == ValidationRuleId.LIQUIDITY_SPREAD for r in reasons)


def test_liquidity_leg_not_in_chain_fails_closed() -> None:
    # Chain only contains the first leg; second leg is absent.
    legs = _put_spread_legs()
    chain = _make_chain([legs[0]])  # only one contract in chain
    reasons = _run(chain=chain)
    spread_reasons = [
        r for r in reasons if r.rule_id == ValidationRuleId.LIQUIDITY_SPREAD
    ]
    assert len(spread_reasons) >= 1
    assert any("not found" in r.human_message for r in spread_reasons)


def test_liquidity_spread_too_wide_rejected() -> None:
    legs = _put_spread_legs()
    # mid=2.50, max_spread_pct=0.10 → pct_limit=0.25; abs_floor=0.05
    # spread=0.30 exceeds both → rejected
    contracts = [
        _make_option_contract(leg.strike, leg.right, leg.expiration, spread_width=0.30)
        for leg in legs
    ]
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=455.0,
        as_of=_NOW,
        filter_params=_make_chain().filter_params,
        contracts=contracts,
    )
    reasons = _run(chain=chain)
    assert any(r.rule_id == ValidationRuleId.LIQUIDITY_SPREAD for r in reasons)


def test_liquidity_spread_passes_by_abs_floor_for_cheap_option() -> None:
    # This test exercises the abs_floor as the *deciding* factor.
    # Use a cheap option: mid=0.30 → pct_limit=0.03 (10% of 0.30).
    # spread=0.04 > pct_limit (0.03), so the percentage rule would reject it.
    # But spread=0.04 ≤ abs_floor=0.05, so the contract passes by the floor.
    # Without the floor, cheap-but-tight contracts would be falsely excluded.
    legs = _put_spread_legs()
    contracts = [
        _make_option_contract(
            leg.strike,
            leg.right,
            leg.expiration,
            mid=0.30,
            bid=0.28,
            ask=0.32,
            spread_width=0.04,  # > pct_limit(0.03) but ≤ abs_floor(0.05) → passes
        )
        for leg in legs
    ]
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=455.0,
        as_of=_NOW,
        filter_params=_make_chain().filter_params,
        contracts=contracts,
    )
    reasons = _run(chain=chain)
    assert not any(r.rule_id == ValidationRuleId.LIQUIDITY_SPREAD for r in reasons)


def test_liquidity_low_open_interest_rejected() -> None:
    legs = _put_spread_legs()
    contracts = [
        _make_option_contract(leg.strike, leg.right, leg.expiration, open_interest=100)
        for leg in legs
    ]
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=455.0,
        as_of=_NOW,
        filter_params=_make_chain().filter_params,
        contracts=contracts,
    )
    reasons = _run(chain=chain)
    assert any(r.rule_id == ValidationRuleId.LIQUIDITY_OPEN_INTEREST for r in reasons)


def test_liquidity_valid_chain_passes() -> None:
    reasons = _run()  # default chain has valid spread and OI
    liq_ids = {
        ValidationRuleId.LIQUIDITY_SPREAD,
        ValidationRuleId.LIQUIDITY_OPEN_INTEREST,
    }
    assert not any(r.rule_id in liq_ids for r in reasons)


# ---------------------------------------------------------------------------
# Exit plan bounds
# ---------------------------------------------------------------------------


def test_exit_plan_profit_target_too_low_rejected() -> None:
    # 0.10 is structurally valid (>0) but below policy min of 0.25
    proposal = _make_proposal(
        exit_plan=ExitPlan(profit_target_pct=0.10, stop_loss_mult=2.0, time_stop_dte=21)
    )
    reasons = _run(proposal=proposal)
    ep_reasons = [r for r in reasons if r.rule_id == ValidationRuleId.INVALID_EXIT_PLAN]
    assert len(ep_reasons) == 1
    assert ep_reasons[0].field_affected == "exit_plan.profit_target_pct"


def test_exit_plan_stop_loss_mult_too_low_rejected() -> None:
    proposal = _make_proposal(
        exit_plan=ExitPlan(profit_target_pct=0.50, stop_loss_mult=1.0, time_stop_dte=21)
    )
    reasons = _run(proposal=proposal)
    ep_reasons = [r for r in reasons if r.rule_id == ValidationRuleId.INVALID_EXIT_PLAN]
    assert len(ep_reasons) == 1
    assert ep_reasons[0].field_affected == "exit_plan.stop_loss_mult"


def test_exit_plan_stop_loss_mult_too_high_rejected() -> None:
    proposal = _make_proposal(
        exit_plan=ExitPlan(profit_target_pct=0.50, stop_loss_mult=6.0, time_stop_dte=21)
    )
    reasons = _run(proposal=proposal)
    ep_reasons = [r for r in reasons if r.rule_id == ValidationRuleId.INVALID_EXIT_PLAN]
    assert len(ep_reasons) == 1
    assert ep_reasons[0].field_affected == "exit_plan.stop_loss_mult"


def test_exit_plan_time_stop_dte_too_low_rejected() -> None:
    proposal = _make_proposal(
        exit_plan=ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=3)
    )
    reasons = _run(proposal=proposal)
    ep_reasons = [r for r in reasons if r.rule_id == ValidationRuleId.INVALID_EXIT_PLAN]
    assert len(ep_reasons) == 1
    assert ep_reasons[0].field_affected == "exit_plan.time_stop_dte"


def test_exit_plan_time_stop_dte_too_high_rejected() -> None:
    proposal = _make_proposal(
        exit_plan=ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=60)
    )
    reasons = _run(proposal=proposal)
    ep_reasons = [r for r in reasons if r.rule_id == ValidationRuleId.INVALID_EXIT_PLAN]
    assert len(ep_reasons) == 1
    assert ep_reasons[0].field_affected == "exit_plan.time_stop_dte"


def test_exit_plan_valid_passes() -> None:
    # default _EXIT: profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21
    reasons = _run()
    assert not any(r.rule_id == ValidationRuleId.INVALID_EXIT_PLAN for r in reasons)


def test_exit_plan_bounds_at_exact_minimums_pass() -> None:
    # Boundaries are inclusive; values exactly at the min must pass.
    # Defaults: profit_target_pct_min=0.25, stop_loss_mult_min=1.5, time_stop_dte_min=7
    proposal = _make_proposal(
        exit_plan=ExitPlan(profit_target_pct=0.25, stop_loss_mult=1.5, time_stop_dte=7)
    )
    reasons = _run(proposal=proposal)
    assert not any(r.rule_id == ValidationRuleId.INVALID_EXIT_PLAN for r in reasons)


def test_exit_plan_bounds_at_exact_maximums_pass() -> None:
    # Boundaries are inclusive; values exactly at the max must pass.
    # Defaults: profit_target_pct_max=1.0, stop_loss_mult_max=5.0, time_stop_dte_max=45
    proposal = _make_proposal(
        exit_plan=ExitPlan(profit_target_pct=1.0, stop_loss_mult=5.0, time_stop_dte=45)
    )
    reasons = _run(proposal=proposal)
    assert not any(r.rule_id == ValidationRuleId.INVALID_EXIT_PLAN for r in reasons)


# ---------------------------------------------------------------------------
# Event gate
# ---------------------------------------------------------------------------


def test_event_gate_no_snapshot_fails_closed() -> None:
    reasons = _run(snapshot=None)
    assert any(r.rule_id == ValidationRuleId.EVENT_DATA_MISSING for r in reasons)


def test_event_gate_no_earnings_data_fails_closed() -> None:
    snapshot = _make_snapshot(days_to_earnings=None)
    reasons = _run(snapshot=snapshot)
    assert any(r.rule_id == ValidationRuleId.EVENT_DATA_MISSING for r in reasons)


def test_event_gate_within_blackout_rejects() -> None:
    snapshot = _make_snapshot(days_to_earnings=3)  # within default 5-day window
    reasons = _run(snapshot=snapshot)
    assert any(r.rule_id == ValidationRuleId.EVENT_BLACKOUT for r in reasons)


def test_event_gate_exactly_at_blackout_boundary_rejects() -> None:
    # days_to_earnings == event_blackout_days → still within window (≤)
    snapshot = _make_snapshot(days_to_earnings=5)
    reasons = _run(snapshot=snapshot)
    assert any(r.rule_id == ValidationRuleId.EVENT_BLACKOUT for r in reasons)


def test_event_gate_beyond_blackout_passes() -> None:
    snapshot = _make_snapshot(days_to_earnings=6)
    reasons = _run(snapshot=snapshot)
    gate_ids = {ValidationRuleId.EVENT_BLACKOUT, ValidationRuleId.EVENT_DATA_MISSING}
    assert not any(r.rule_id in gate_ids for r in reasons)


# ---------------------------------------------------------------------------
# Buying power
# ---------------------------------------------------------------------------


def test_buying_power_below_floor_rejects() -> None:
    # min_buying_power_pct=0.10, equity=100_000 → floor=10_000
    portfolio = _make_portfolio(options_buying_power=8_000.0)
    reasons = _run(portfolio=portfolio)
    bp = [r for r in reasons if r.rule_id == ValidationRuleId.BUYING_POWER]
    assert len(bp) == 1
    assert bp[0].severity == Severity.ERROR
    assert bp[0].observed == 8_000.0


def test_buying_power_exactly_at_floor_passes() -> None:
    portfolio = _make_portfolio(options_buying_power=10_000.0)  # == 10% of 100_000
    reasons = _run(portfolio=portfolio)
    assert not any(r.rule_id == ValidationRuleId.BUYING_POWER for r in reasons)


def test_buying_power_above_floor_passes() -> None:
    portfolio = _make_portfolio(options_buying_power=20_000.0)
    reasons = _run(portfolio=portfolio)
    assert not any(r.rule_id == ValidationRuleId.BUYING_POWER for r in reasons)


# ---------------------------------------------------------------------------
# Duplicate / conflict detection
# ---------------------------------------------------------------------------


def test_duplicate_same_strategy_overlapping_expiration_rejected() -> None:
    # Existing position: same underlying, same strategy, same expiration.
    existing = _make_open_position(nearest_expiration=_EXP)
    portfolio = _make_portfolio(positions=[existing])
    reasons = _run(portfolio=portfolio)
    dup = [r for r in reasons if r.rule_id == ValidationRuleId.DUPLICATE_POSITION]
    assert len(dup) == 1
    assert dup[0].severity == Severity.ERROR


def test_duplicate_same_strategy_non_overlapping_expiration_passes() -> None:
    # Existing position expires 90 days away from proposal → no duplicate.
    far_exp = date(2026, 11, 20)  # well beyond 5-day overlap window from _EXP
    existing = _make_open_position(nearest_expiration=far_exp)
    portfolio = _make_portfolio(positions=[existing])
    reasons = _run(portfolio=portfolio)
    assert not any(r.rule_id == ValidationRuleId.DUPLICATE_POSITION for r in reasons)


def test_duplicate_different_strategy_same_expiration_passes() -> None:
    existing = _make_open_position(strategy="iron_condor", nearest_expiration=_EXP)
    portfolio = _make_portfolio(positions=[existing])
    reasons = _run(portfolio=portfolio)
    assert not any(r.rule_id == ValidationRuleId.DUPLICATE_POSITION for r in reasons)


def test_conflict_opposing_delta_rejected() -> None:
    # Proposal is bull_put_spread (positive delta, net_delta=0.12 > tolerance=0.05).
    # Existing position is bear_call_spread (negative-delta strategy).
    existing = _make_open_position(strategy="bear_call_spread")
    portfolio = _make_portfolio(positions=[existing])
    reasons = _run(portfolio=portfolio)
    conflict = [
        r for r in reasons if r.rule_id == ValidationRuleId.CONFLICTING_POSITION
    ]
    assert len(conflict) == 1
    assert conflict[0].severity == Severity.ERROR


def test_conflict_same_direction_passes() -> None:
    # Proposal is bull_put_spread (positive); existing is bull_call_spread (positive).
    existing = _make_open_position(strategy="bull_call_spread")
    portfolio = _make_portfolio(positions=[existing])
    reasons = _run(portfolio=portfolio)
    assert not any(r.rule_id == ValidationRuleId.CONFLICTING_POSITION for r in reasons)


def test_conflict_below_delta_tolerance_passes() -> None:
    # net_delta=0.03 < tolerance=0.05 → conflict check skipped.
    proposal = _make_proposal(net_delta=0.03)
    existing = _make_open_position(strategy="bear_call_spread")
    portfolio = _make_portfolio(positions=[existing])
    reasons = _run(proposal=proposal, portfolio=portfolio)
    assert not any(r.rule_id == ValidationRuleId.CONFLICTING_POSITION for r in reasons)


def test_neutral_strategy_existing_does_not_trigger_conflict() -> None:
    # iron_condor has delta sign 0 — should not be flagged as conflicting anything.
    existing = _make_open_position(strategy="iron_condor")
    portfolio = _make_portfolio(positions=[existing])
    reasons = _run(portfolio=portfolio)
    assert not any(r.rule_id == ValidationRuleId.CONFLICTING_POSITION for r in reasons)


def test_no_positions_on_same_underlying_passes() -> None:
    # Positions exist but on a different underlying.
    other = _make_open_position(underlying="QQQ")
    portfolio = _make_portfolio(positions=[other])
    reasons = _run(portfolio=portfolio)
    dup_ids = {
        ValidationRuleId.DUPLICATE_POSITION,
        ValidationRuleId.CONFLICTING_POSITION,
    }
    assert not any(r.rule_id in dup_ids for r in reasons)


# ---------------------------------------------------------------------------
# All-pass integration
# ---------------------------------------------------------------------------


def test_all_market_access_checks_pass_for_valid_proposal() -> None:
    reasons = _run()
    assert reasons == []
