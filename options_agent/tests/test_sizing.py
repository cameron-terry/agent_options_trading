"""Tests for risk/sizing.py.

WP-4.6: sizing function + edge cases

Coverage targets:
  - SizingConstraint enum catalog presence
  - conviction <= conviction_floor → CONVICTION_FLOOR, contracts=0, capped_to_zero=True
  - conviction exactly at floor → CONVICTION_FLOOR (boundary: <=)
  - conviction just above floor → proceeds to budget step
  - est_max_loss > risk_budget → BELOW_MIN_SIZE, contracts=0, capped_to_zero=True
  - est_max_loss == risk_budget → exactly 1 contract (boundary)
  - normal case → contracts = floor(budget / est_max_loss), binding=RISK_BUDGET
  - sized_max_loss = contracts × est_max_loss
  - sized_max_profit = contracts × est_max_profit
  - risk_budget_used = sized_max_loss / risk_budget
  - conviction_floor sourced from Limits (not hardcoded)
  - zero conviction is handled by the floor gate (never raises)
  - SizingResult serializes/deserializes cleanly (JournalRecord storage requirement)
"""

from datetime import date

import pytest

from options_agent.contracts.data import PortfolioState
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import SizingConstraint, SizingResult
from options_agent.risk.limits import Limits
from options_agent.risk.sizing import size

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_EXIT = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)
_EXP = date(2026, 8, 15)

_EQUITY = 100_000.0
_MAX_LOSS_PCT = 0.01  # matches Limits default
_RISK_BUDGET = _EQUITY * _MAX_LOSS_PCT  # 1 000.0


def _legs() -> list[Leg]:
    return [
        Leg(right="put", side="sell", strike=450.0, expiration=_EXP),
        Leg(right="put", side="buy", strike=445.0, expiration=_EXP),
    ]


def _make_proposal(**overrides: object) -> TradeProposal:
    defaults: dict = {
        "action": "OPEN",
        "underlying": "SPY",
        "strategy": "bull_put_spread",
        "legs": _legs(),
        "thesis": "bullish bias at support",
        "iv_rationale": "IV rank 65th percentile, selling premium",
        "catalyst_check": "no earnings within 30 days",
        "conviction": 0.70,
        "est_max_loss": 250.0,
        "est_max_profit": 120.0,
        "breakevens": [447.50],
        "net_delta": 0.12,
        "net_theta": 8.50,
        "net_vega": -0.30,
        "exit_plan": _EXIT,
        "informed_by": [],
    }
    defaults.update(overrides)
    return TradeProposal(**defaults)


def _make_portfolio(**overrides: object) -> PortfolioState:
    defaults: dict = {
        "positions": [],
        "account_equity": _EQUITY,
        "buying_power": 30_000.0,
        "options_buying_power": 15_000.0,
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


def _default_limits() -> Limits:
    return Limits()  # conviction_floor=0.35, max_loss_per_trade_pct=0.01


# ---------------------------------------------------------------------------
# SizingConstraint — catalog completeness
# ---------------------------------------------------------------------------


def test_sizing_constraint_values_present() -> None:
    required = {
        SizingConstraint.RISK_BUDGET,
        SizingConstraint.CONVICTION_FLOOR,
        SizingConstraint.BELOW_MIN_SIZE,
        SizingConstraint.BUYING_POWER,
    }
    assert required.issubset(set(SizingConstraint))


def test_sizing_constraint_string_values() -> None:
    assert SizingConstraint.RISK_BUDGET == "RISK_BUDGET"
    assert SizingConstraint.CONVICTION_FLOOR == "CONVICTION_FLOOR"
    assert SizingConstraint.BELOW_MIN_SIZE == "BELOW_MIN_SIZE"
    assert SizingConstraint.BUYING_POWER == "BUYING_POWER"


# ---------------------------------------------------------------------------
# Conviction gate — CONVICTION_FLOOR
# ---------------------------------------------------------------------------


def test_zero_conviction_returns_zero_contracts() -> None:
    # conviction=0.0 is below any reasonable floor; must not raise
    result = size(_make_proposal(conviction=0.0), _make_portfolio(), _default_limits())
    assert result.contracts == 0
    assert result.capped_to_zero is True
    assert result.binding_constraint == SizingConstraint.CONVICTION_FLOOR
    assert result.sized_max_loss == 0.0
    assert result.sized_max_profit == 0.0
    assert result.risk_budget_used == 0.0


def test_conviction_exactly_at_floor_returns_zero() -> None:
    # floor is inclusive: conviction <= floor → zero contracts
    limits = Limits(conviction_floor=0.35)
    result = size(_make_proposal(conviction=0.35), _make_portfolio(), limits)
    assert result.contracts == 0
    assert result.binding_constraint == SizingConstraint.CONVICTION_FLOOR
    assert result.capped_to_zero is True


def test_conviction_just_below_floor_returns_zero() -> None:
    limits = Limits(conviction_floor=0.50)
    result = size(_make_proposal(conviction=0.49), _make_portfolio(), limits)
    assert result.contracts == 0
    assert result.binding_constraint == SizingConstraint.CONVICTION_FLOOR
    assert result.capped_to_zero is True


def test_conviction_just_above_floor_proceeds_to_budget() -> None:
    # 0.36 > 0.35 default floor → should reach the budget step
    limits = Limits(conviction_floor=0.35)
    result = size(
        _make_proposal(conviction=0.36, est_max_loss=250.0), _make_portfolio(), limits
    )
    # budget = 100_000 * 0.01 = 1_000; floor(1_000/250) = 4 contracts
    assert result.contracts == 4
    assert result.binding_constraint == SizingConstraint.RISK_BUDGET
    assert result.capped_to_zero is False


def test_conviction_floor_sourced_from_limits() -> None:
    # Verify the floor comes from Limits, not a hardcoded constant
    limits_strict = Limits(conviction_floor=0.80)
    limits_loose = Limits(conviction_floor=0.10)
    proposal = _make_proposal(conviction=0.50, est_max_loss=250.0)
    portfolio = _make_portfolio()

    strict = size(proposal, portfolio, limits_strict)
    loose = size(proposal, portfolio, limits_loose)

    assert strict.contracts == 0
    assert strict.binding_constraint == SizingConstraint.CONVICTION_FLOOR
    assert loose.contracts > 0
    assert loose.binding_constraint == SizingConstraint.RISK_BUDGET


# ---------------------------------------------------------------------------
# BELOW_MIN_SIZE — est_max_loss > risk_budget
# ---------------------------------------------------------------------------


def test_single_contract_exceeds_budget_returns_zero() -> None:
    # budget = 100_000 * 0.01 = 1_000; est_max_loss = 1_500 > 1_000
    result = size(
        _make_proposal(conviction=0.80, est_max_loss=1_500.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert result.contracts == 0
    assert result.binding_constraint == SizingConstraint.BELOW_MIN_SIZE
    assert result.capped_to_zero is True
    assert result.sized_max_loss == 0.0
    assert result.sized_max_profit == 0.0
    assert result.risk_budget_used == 0.0


def test_est_max_loss_just_above_budget_returns_zero() -> None:
    # budget = 1_000; est_max_loss = 1_001 → floor(1_000/1_001) = 0
    result = size(
        _make_proposal(conviction=0.90, est_max_loss=1_001.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert result.contracts == 0
    assert result.binding_constraint == SizingConstraint.BELOW_MIN_SIZE


def test_est_max_loss_exactly_budget_yields_one_contract() -> None:
    # budget = 1_000; est_max_loss = 1_000 → floor(1_000/1_000) = 1 (boundary)
    result = size(
        _make_proposal(conviction=0.80, est_max_loss=1_000.0, est_max_profit=400.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert result.contracts == 1
    assert result.binding_constraint == SizingConstraint.RISK_BUDGET
    assert result.capped_to_zero is False


def test_below_min_size_never_floors_to_one() -> None:
    # Confirm no silent rounding up — budget affords 0 contracts, returns 0
    result = size(
        _make_proposal(conviction=1.0, est_max_loss=5_000.0),
        _make_portfolio(account_equity=10_000.0),  # budget = 100
        _default_limits(),
    )
    assert result.contracts == 0
    assert result.binding_constraint == SizingConstraint.BELOW_MIN_SIZE


# ---------------------------------------------------------------------------
# Normal case — risk budget governs count
# ---------------------------------------------------------------------------


def test_normal_case_contract_count() -> None:
    # budget = 100_000 * 0.01 = 1_000; est_max_loss = 250 → 4 contracts
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=250.0, est_max_profit=120.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert result.contracts == 4
    assert result.binding_constraint == SizingConstraint.RISK_BUDGET
    assert result.capped_to_zero is False


def test_normal_case_sized_max_loss() -> None:
    # sized_max_loss = contracts × est_max_loss
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=300.0, est_max_profit=130.0),
        _make_portfolio(),
        _default_limits(),
    )
    # floor(1_000 / 300) = 3
    assert result.contracts == 3
    assert result.sized_max_loss == pytest.approx(3 * 300.0)


def test_normal_case_sized_max_profit() -> None:
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=300.0, est_max_profit=130.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert result.sized_max_profit == pytest.approx(3 * 130.0)


def test_normal_case_risk_budget_used() -> None:
    # budget = 1_000; floor(1_000/300) = 3; sized_max_loss = 900
    # risk_budget_used = 900 / 1_000 = 0.9
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=300.0, est_max_profit=130.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert result.risk_budget_used == pytest.approx(0.9)


def test_normal_case_full_budget_used() -> None:
    # est_max_loss divides evenly into budget → risk_budget_used = 1.0
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=250.0, est_max_profit=120.0),
        _make_portfolio(),
        _default_limits(),
    )
    # floor(1_000/250) = 4; 4 × 250 = 1_000 = budget
    assert result.risk_budget_used == pytest.approx(1.0)


def test_small_account_scales_contracts_down() -> None:
    # equity = 10_000, budget = 100; est_max_loss = 50 → 2 contracts
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=50.0, est_max_profit=25.0),
        _make_portfolio(account_equity=10_000.0),
        _default_limits(),
    )
    assert result.contracts == 2
    assert result.sized_max_loss == pytest.approx(100.0)


def test_large_account_scales_contracts_up() -> None:
    # equity = 500_000, budget = 5_000; est_max_loss = 250 → 20 contracts
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=250.0, est_max_profit=120.0),
        _make_portfolio(account_equity=500_000.0),
        _default_limits(),
    )
    assert result.contracts == 20
    assert result.sized_max_loss == pytest.approx(20 * 250.0)


def test_high_max_loss_per_trade_pct_increases_budget() -> None:
    # max_loss_per_trade_pct = 0.02 → budget = 2_000; floor(2_000/250) = 8 contracts
    limits = Limits(max_loss_per_trade_pct=0.02, conviction_floor=0.35)
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=250.0, est_max_profit=120.0),
        _make_portfolio(),
        limits,
    )
    assert result.contracts == 8


def test_contracts_non_negative() -> None:
    # Property: size() must never return negative contracts
    for conviction in [0.0, 0.35, 0.36, 0.70, 1.0]:
        for est_max_loss in [50.0, 250.0, 1_500.0, 5_000.0]:
            result = size(
                _make_proposal(conviction=conviction, est_max_loss=est_max_loss),
                _make_portfolio(),
                _default_limits(),
            )
            assert result.contracts >= 0, (
                f"contracts<0 for conviction={conviction}, est_max_loss={est_max_loss}"
            )


# ---------------------------------------------------------------------------
# Precondition assertion — est_max_loss must be positive
# ---------------------------------------------------------------------------


def test_zero_est_max_loss_raises_assertion() -> None:
    # Precondition: size() must only be called after validate() passes.
    # est_max_loss=0 is blocked by MAX_LOSS_NOT_FINITE; if it reaches size()
    # the assert fires rather than producing a silent ZeroDivisionError.
    with pytest.raises(AssertionError, match="est_max_loss"):
        size(
            _make_proposal(conviction=0.80, est_max_loss=0.0),
            _make_portfolio(),
            _default_limits(),
        )


# ---------------------------------------------------------------------------
# Serialization — JournalRecord storage requirement
# ---------------------------------------------------------------------------


def test_sizing_result_round_trip_conviction_floor() -> None:
    result = size(_make_proposal(conviction=0.0), _make_portfolio(), _default_limits())
    assert SizingResult.model_validate(result.model_dump()) == result


def test_sizing_result_round_trip_below_min_size() -> None:
    result = size(
        _make_proposal(conviction=0.80, est_max_loss=5_000.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert result.binding_constraint == SizingConstraint.BELOW_MIN_SIZE
    assert SizingResult.model_validate(result.model_dump()) == result


def test_sizing_result_round_trip_normal() -> None:
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=250.0, est_max_profit=120.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert SizingResult.model_validate(result.model_dump()) == result


def test_sizing_result_json_round_trip() -> None:
    result = size(
        _make_proposal(conviction=0.70, est_max_loss=250.0, est_max_profit=120.0),
        _make_portfolio(),
        _default_limits(),
    )
    assert SizingResult.model_validate_json(result.model_dump_json()) == result
