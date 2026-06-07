from enum import StrEnum

from pydantic import BaseModel, model_validator


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationRuleId(StrEnum):
    """Frozen catalog of validator rule IDs.

    WP-4 emits these; WP-7 groups by them. Add new IDs here before using them
    — never pass a free string as rule_id.
    """

    # Hard rejection rules — always ERROR severity
    INVALID_SCHEMA = "INVALID_SCHEMA"
    UNKNOWN_STRATEGY = "UNKNOWN_STRATEGY"
    APPROVAL_LEVEL = "APPROVAL_LEVEL"
    NAKED_SHORT = "NAKED_SHORT"
    MAX_LOSS_CAP = "MAX_LOSS_CAP"
    MAX_LOSS_NOT_FINITE = "MAX_LOSS_NOT_FINITE"
    PORTFOLIO_DELTA_BAND = "PORTFOLIO_DELTA_BAND"
    PORTFOLIO_VEGA_BAND = "PORTFOLIO_VEGA_BAND"
    PORTFOLIO_THETA_FLOOR = "PORTFOLIO_THETA_FLOOR"
    CONCENTRATION_UNDERLYING = "CONCENTRATION_UNDERLYING"
    CONCENTRATION_SECTOR = "CONCENTRATION_SECTOR"
    LIQUIDITY_SPREAD = "LIQUIDITY_SPREAD"
    LIQUIDITY_OPEN_INTEREST = "LIQUIDITY_OPEN_INTEREST"
    INVALID_EXIT_PLAN = "INVALID_EXIT_PLAN"
    EVENT_BLACKOUT = "EVENT_BLACKOUT"
    BUYING_POWER = "BUYING_POWER"
    DUPLICATE_POSITION = "DUPLICATE_POSITION"
    KILL_SWITCH = "KILL_SWITCH"

    # Advisory rules — WARNING severity (non-blocking)
    LOW_CONVICTION = "LOW_CONVICTION"
    NEAR_DELTA_BAND = "NEAR_DELTA_BAND"
    NEAR_VEGA_BAND = "NEAR_VEGA_BAND"
    NEAR_THETA_FLOOR = "NEAR_THETA_FLOOR"


class RejectionReason(BaseModel):
    """One structured validator finding.

    observed/limit carry the actual numbers so WP-7 can report not just
    that a band was breached, but by how much — essential for distinguishing
    mis-tuned limits from misbehaving proposals.
    """

    rule_id: ValidationRuleId
    severity: Severity
    human_message: str
    field_affected: str | None = None
    observed: float | None = None
    limit: float | None = None


class ValidationResult(BaseModel):
    """Verdict from risk/validator.py.

    Invariant: passed ⟺ no ERROR-severity reasons.
    WARNING-severity reasons may be present on a passing result — these are
    the non-blocking signals WP-7 correlates against outcomes to find soft
    failure modes.
    """

    passed: bool
    reasons: list[RejectionReason] = []

    @model_validator(mode="after")
    def _check_passed_invariant(self) -> "ValidationResult":
        has_error = any(r.severity == Severity.ERROR for r in self.reasons)
        if self.passed and has_error:
            raise ValueError("passed=True but ERROR-severity reasons are present")
        if not self.passed and not has_error:
            raise ValueError("passed=False but no ERROR-severity reasons are present")
        return self


class SizingResult(BaseModel):
    """Output of risk/sizing.py.

    sized_max_loss and sized_max_profit are the authoritative figures at the
    chosen contract count — WP-5 exit triggers and WP-7 risk attribution read
    these, never re-deriving from the proposal's per-contract estimates.

    binding_constraint names which limit governed the final contract count.
    Expected values: "RISK_BUDGET", "CONVICTION_FLOOR", "BUYING_POWER".
    On a zero-contract result, capped_to_zero=True and binding_constraint
    explains why — the orchestrator records this as a clean NO_ACTION cycle.
    """

    contracts: int
    sized_max_loss: float
    sized_max_profit: float
    risk_budget_used: float
    binding_constraint: str | None = None
    capped_to_zero: bool = False
