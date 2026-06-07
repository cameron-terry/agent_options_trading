"""Tests for risk/validator.py — WP-4.3: structural validity checks.

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
"""

from datetime import date

from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import Severity, ValidationRuleId
from options_agent.risk.limits import Limits
from options_agent.risk.validator import validate_from_dict, validate_structural

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
