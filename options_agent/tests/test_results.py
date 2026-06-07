import pytest
from pydantic import ValidationError

from options_agent.contracts import (
    RejectionReason,
    Severity,
    SizingResult,
    ValidationResult,
    ValidationRuleId,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_reason(
    rule_id: ValidationRuleId = ValidationRuleId.NAKED_SHORT,
    *,
    field_affected: str | None = None,
    observed: float | None = None,
    limit: float | None = None,
) -> RejectionReason:
    return RejectionReason(
        rule_id=rule_id,
        severity=Severity.ERROR,
        human_message="Hard rejection",
        field_affected=field_affected,
        observed=observed,
        limit=limit,
    )


def _warning_reason(
    rule_id: ValidationRuleId = ValidationRuleId.LOW_CONVICTION,
) -> RejectionReason:
    return RejectionReason(
        rule_id=rule_id,
        severity=Severity.WARNING,
        human_message="Advisory warning",
    )


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


def test_severity_values() -> None:
    assert set(Severity) == {Severity.ERROR, Severity.WARNING}


# ---------------------------------------------------------------------------
# ValidationRuleId — catalog completeness
# ---------------------------------------------------------------------------


def test_validation_rule_id_hard_rules_present() -> None:
    hard_rules = {
        ValidationRuleId.INVALID_SCHEMA,
        ValidationRuleId.UNKNOWN_STRATEGY,
        ValidationRuleId.APPROVAL_LEVEL,
        ValidationRuleId.NAKED_SHORT,
        ValidationRuleId.MAX_LOSS_CAP,
        ValidationRuleId.MAX_LOSS_NOT_FINITE,
        ValidationRuleId.PORTFOLIO_DELTA_BAND,
        ValidationRuleId.PORTFOLIO_VEGA_BAND,
        ValidationRuleId.PORTFOLIO_THETA_FLOOR,
        ValidationRuleId.CONCENTRATION_UNDERLYING,
        ValidationRuleId.CONCENTRATION_SECTOR,
        ValidationRuleId.LIQUIDITY_SPREAD,
        ValidationRuleId.LIQUIDITY_OPEN_INTEREST,
        ValidationRuleId.INVALID_EXIT_PLAN,
        ValidationRuleId.EVENT_BLACKOUT,
        ValidationRuleId.BUYING_POWER,
        ValidationRuleId.DUPLICATE_POSITION,
        ValidationRuleId.KILL_SWITCH,
    }
    assert hard_rules.issubset(set(ValidationRuleId))


def test_validation_rule_id_advisory_rules_present() -> None:
    advisory = {
        ValidationRuleId.LOW_CONVICTION,
        ValidationRuleId.NEAR_DELTA_BAND,
        ValidationRuleId.NEAR_VEGA_BAND,
        ValidationRuleId.NEAR_THETA_FLOOR,
    }
    assert advisory.issubset(set(ValidationRuleId))


# ---------------------------------------------------------------------------
# RejectionReason
# ---------------------------------------------------------------------------


def test_rejection_reason_error_minimal() -> None:
    r = _error_reason()
    assert r.rule_id == ValidationRuleId.NAKED_SHORT
    assert r.severity == Severity.ERROR
    assert r.field_affected is None
    assert r.observed is None
    assert r.limit is None


def test_rejection_reason_with_observed_and_limit() -> None:
    r = _error_reason(
        ValidationRuleId.PORTFOLIO_DELTA_BAND,
        field_affected="net_delta",
        observed=0.62,
        limit=0.40,
    )
    assert r.field_affected == "net_delta"
    assert r.observed == 0.62
    assert r.limit == 0.40


def test_rejection_reason_warning() -> None:
    r = _warning_reason()
    assert r.severity == Severity.WARNING
    assert r.rule_id == ValidationRuleId.LOW_CONVICTION


def test_rejection_reason_round_trip() -> None:
    r = _error_reason(
        ValidationRuleId.MAX_LOSS_CAP,
        field_affected="est_max_loss",
        observed=5000.0,
        limit=2500.0,
    )
    assert RejectionReason.model_validate(r.model_dump()) == r


def test_rejection_reason_json_round_trip() -> None:
    r = _warning_reason(ValidationRuleId.NEAR_VEGA_BAND)
    assert RejectionReason.model_validate_json(r.model_dump_json()) == r


# ---------------------------------------------------------------------------
# ValidationResult — construction
# ---------------------------------------------------------------------------


def test_validation_result_clean_pass() -> None:
    vr = ValidationResult(passed=True)
    assert vr.passed is True
    assert vr.reasons == []


def test_validation_result_pass_with_warning() -> None:
    vr = ValidationResult(passed=True, reasons=[_warning_reason()])
    assert vr.passed is True
    assert len(vr.reasons) == 1
    assert vr.reasons[0].severity == Severity.WARNING


def test_validation_result_rejection_single_error() -> None:
    vr = ValidationResult(passed=False, reasons=[_error_reason()])
    assert vr.passed is False
    assert len(vr.reasons) == 1
    assert vr.reasons[0].severity == Severity.ERROR


def test_validation_result_rejection_multiple_errors() -> None:
    reasons = [
        _error_reason(ValidationRuleId.NAKED_SHORT),
        _error_reason(ValidationRuleId.KILL_SWITCH),
    ]
    vr = ValidationResult(passed=False, reasons=reasons)
    assert len(vr.reasons) == 2


def test_validation_result_rejection_error_plus_warning() -> None:
    reasons = [_error_reason(), _warning_reason()]
    vr = ValidationResult(passed=False, reasons=reasons)
    errors = [r for r in vr.reasons if r.severity == Severity.ERROR]
    warnings = [r for r in vr.reasons if r.severity == Severity.WARNING]
    assert len(errors) == 1
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# ValidationResult — invariant enforcement
# ---------------------------------------------------------------------------


def test_validation_result_passed_true_with_error_raises() -> None:
    with pytest.raises(ValidationError, match="ERROR-severity"):
        ValidationResult(passed=True, reasons=[_error_reason()])


def test_validation_result_passed_false_no_error_raises() -> None:
    # Only a WARNING — not enough to justify passed=False
    with pytest.raises(ValidationError, match="ERROR-severity"):
        ValidationResult(passed=False, reasons=[_warning_reason()])


def test_validation_result_passed_false_empty_reasons_raises() -> None:
    with pytest.raises(ValidationError, match="ERROR-severity"):
        ValidationResult(passed=False, reasons=[])


# ---------------------------------------------------------------------------
# ValidationResult — round-trips
# ---------------------------------------------------------------------------


def test_validation_result_pass_round_trip() -> None:
    vr = ValidationResult(passed=True, reasons=[_warning_reason()])
    assert ValidationResult.model_validate(vr.model_dump()) == vr


def test_validation_result_rejection_round_trip() -> None:
    vr = ValidationResult(
        passed=False,
        reasons=[
            _error_reason(
                ValidationRuleId.PORTFOLIO_DELTA_BAND,
                field_affected="net_delta",
                observed=0.62,
                limit=0.40,
            )
        ],
    )
    assert ValidationResult.model_validate_json(vr.model_dump_json()) == vr


# ---------------------------------------------------------------------------
# SizingResult — construction
# ---------------------------------------------------------------------------


def test_sizing_result_normal() -> None:
    sr = SizingResult(
        contracts=3,
        sized_max_loss=750.0,
        sized_max_profit=375.0,
        risk_budget_used=0.015,
        binding_constraint="RISK_BUDGET",
    )
    assert sr.contracts == 3
    assert sr.sized_max_loss == 750.0
    assert sr.sized_max_profit == 375.0
    assert sr.risk_budget_used == 0.015
    assert sr.binding_constraint == "RISK_BUDGET"
    assert sr.capped_to_zero is False


def test_sizing_result_zero_contracts_conviction_floor() -> None:
    sr = SizingResult(
        contracts=0,
        sized_max_loss=0.0,
        sized_max_profit=0.0,
        risk_budget_used=0.0,
        binding_constraint="CONVICTION_FLOOR",
        capped_to_zero=True,
    )
    assert sr.contracts == 0
    assert sr.capped_to_zero is True
    assert sr.binding_constraint == "CONVICTION_FLOOR"


def test_sizing_result_zero_contracts_risk_budget() -> None:
    sr = SizingResult(
        contracts=0,
        sized_max_loss=0.0,
        sized_max_profit=0.0,
        risk_budget_used=0.0,
        binding_constraint="RISK_BUDGET",
        capped_to_zero=True,
    )
    assert sr.capped_to_zero is True


def test_sizing_result_zero_contracts_buying_power() -> None:
    sr = SizingResult(
        contracts=0,
        sized_max_loss=0.0,
        sized_max_profit=0.0,
        risk_budget_used=0.0,
        binding_constraint="BUYING_POWER",
        capped_to_zero=True,
    )
    assert sr.capped_to_zero is True


def test_sizing_result_no_binding_constraint() -> None:
    sr = SizingResult(
        contracts=1,
        sized_max_loss=250.0,
        sized_max_profit=125.0,
        risk_budget_used=0.005,
    )
    assert sr.binding_constraint is None


# ---------------------------------------------------------------------------
# SizingResult — round-trips
# ---------------------------------------------------------------------------


def test_sizing_result_round_trip_dict() -> None:
    sr = SizingResult(
        contracts=5,
        sized_max_loss=1250.0,
        sized_max_profit=625.0,
        risk_budget_used=0.025,
        binding_constraint="RISK_BUDGET",
    )
    assert SizingResult.model_validate(sr.model_dump()) == sr


def test_sizing_result_round_trip_json() -> None:
    sr = SizingResult(
        contracts=0,
        sized_max_loss=0.0,
        sized_max_profit=0.0,
        risk_budget_used=0.0,
        binding_constraint="CONVICTION_FLOOR",
        capped_to_zero=True,
    )
    assert SizingResult.model_validate_json(sr.model_dump_json()) == sr
