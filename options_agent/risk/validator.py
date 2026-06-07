from pydantic import ValidationError

from options_agent.contracts.proposal import Leg, TradeProposal
from options_agent.contracts.results import (
    RejectionReason,
    Severity,
    ValidationResult,
    ValidationRuleId,
)
from options_agent.risk.limits import Limits


def validate_from_dict(raw: dict, limits: Limits) -> ValidationResult:
    """Parse *raw* into a TradeProposal and run structural validation.

    Use this at the orchestrator boundary where the agent's JSON output may
    fail schema validation before a TradeProposal can be constructed.
    """
    try:
        proposal = TradeProposal.model_validate(raw)
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        field = ".".join(str(loc) for loc in first_error["loc"]) or "<root>"
        return ValidationResult(
            passed=False,
            reasons=[
                RejectionReason(
                    rule_id=ValidationRuleId.INVALID_SCHEMA,
                    severity=Severity.ERROR,
                    human_message=f"schema error on '{field}': {first_error['msg']}",
                    field_affected=field,
                )
            ],
        )
    return validate_structural(proposal, limits)


def validate_structural(proposal: TradeProposal, limits: Limits) -> ValidationResult:
    """Run the three structural checks on an already-parsed TradeProposal.

    Checks are ordered fastest-to-slowest and stop at the first ERROR finding.
    This function needs no portfolio state or live market data.
    """
    # 1. Playbook check
    if proposal.strategy not in limits.allowed_strategies:
        return ValidationResult(
            passed=False,
            reasons=[
                RejectionReason(
                    rule_id=ValidationRuleId.UNKNOWN_STRATEGY,
                    severity=Severity.ERROR,
                    human_message=(
                        f"strategy '{proposal.strategy}' is not in the allowed"
                        " playbook; update limits.allowed_strategies or the agent"
                        " playbook"
                    ),
                    field_affected="strategy",
                )
            ],
        )

    # 2. Naked-short check (unconditional — no config override)
    naked_reason = _check_naked_short(proposal.legs)
    if naked_reason is not None:
        return ValidationResult(passed=False, reasons=[naked_reason])

    return ValidationResult(passed=True, reasons=[])


def _check_naked_short(legs: list[Leg]) -> RejectionReason | None:
    """Per-right net ratio check.

    For each option right (call/put), the total buy ratio across all legs must
    be >= the total sell ratio. A single uncovered sell leg of any ratio is
    rejected — there is no config override (Core Principle 3).

    Assumption agreed in WP-4.3 briefing: Option A (per-right net ratio).
    """
    for right in ("call", "put"):
        buy_ratio = sum(
            leg.ratio for leg in legs if leg.right == right and leg.side == "buy"
        )
        sell_ratio = sum(
            leg.ratio for leg in legs if leg.right == right and leg.side == "sell"
        )
        if sell_ratio > buy_ratio:
            offending = next(
                leg for leg in legs if leg.right == right and leg.side == "sell"
            )
            return RejectionReason(
                rule_id=ValidationRuleId.NAKED_SHORT,
                severity=Severity.ERROR,
                human_message=(
                    f"naked short {right}: sell ratio {sell_ratio} exceeds"
                    f" buy ratio {buy_ratio} — add a covering long {right} leg"
                ),
                field_affected=(
                    f"legs[right={right!r}, side='sell', strike={offending.strike}]"
                ),
            )
    return None
