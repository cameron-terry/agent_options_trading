from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from options_agent.contracts.results import ValidationRuleId
from options_agent.contracts.state import (
    ActionTaken,
    ContextSnapshot,
    Decision,
    ExitReason,
)


class OutcomeEventType(StrEnum):
    """Type of terminal-ish event on a position.

    Multiple events per position are normal (partial close, roll, final close).
    """

    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    FULL_CLOSE = "FULL_CLOSE"
    ROLL = "ROLL"
    EXPIRED = "EXPIRED"
    ASSIGNED = "ASSIGNED"


class StrategyOutcomeStats(BaseModel):
    """Per-strategy slice of a symbol's realized track record."""

    closed_positions: int
    wins: int
    total_realized_pnl: float


class SymbolOutcomeStats(BaseModel):
    """Aggregated realized outcomes for one underlying.

    Pre-loaded into the agent's context bundle so past results inform new
    proposals without requiring a get_position_history drill-in per position.
    A "win" is an outcome event with realized_pnl > 0. win_rate and
    avg_realized_pnl are None when no outcomes exist yet.

    Counts are per outcome event, not per position — a position closed in two
    partial fills contributes two events. With the current full-close-only
    exit paths the two coincide; revisit if partial closes are implemented.
    """

    symbol: str
    closed_positions: int
    wins: int
    losses: int
    win_rate: float | None
    total_realized_pnl: float
    avg_realized_pnl: float | None
    by_strategy: dict[str, StrategyOutcomeStats]
    recent_exit_reasons: list[str]


class OutcomeRecord(BaseModel):
    """One terminal-ish event on a position — append-only, never updated.

    Multiple OutcomeRecords may exist per position (e.g. partial close then
    full close, or roll then expiry). Link to the opening JournalRecord via:
        OutcomeRecord.position_id → Position → JournalRecord.position_ids

    Never linked directly to a cycle_id: monitor-driven closes have no
    entry-cycle JournalRecord. Position is the stable join spine.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    position_id: str
    event_type: OutcomeEventType
    recorded_at: datetime
    contracts_closed: int
    realized_pnl: float
    fill_price: float | None = None
    closing_order_id: str | None = None
    # Which monitor rule closed this position — queryable by WP-7 to attribute
    # performance by exit trigger. None for pre-WP-5.5 records and non-monitor
    # closes (expiry, assignment).
    exit_reason: ExitReason | None = None


class JournalRecord(BaseModel):
    """Immutable record written once at the end of each entry cycle.

    Never updated after write. Outcome data lives in OutcomeRecord, joined via:
        JournalRecord.position_ids → Position → OutcomeRecord.position_id

    Invariants (enforced by model_validator):
    - action_taken must equal decision.action_taken.
    - rejection_rule_ids must be non-empty when action_taken == REJECTED.

    Denormalized fields (strategy, underlying, etc.) are a queryable index over
    the nested decision and context_snapshot. They must be derived from those
    objects at write time and must never be set independently — the nested
    objects are the source of truth. If a denormalized field disagrees with its
    source, the nested object wins.
    """

    model_config = ConfigDict(frozen=True)

    # Identity
    cycle_id: str
    timestamp: datetime

    # Primary grouping key for all WP-7 analytics — top-level for indexing
    action_taken: ActionTaken

    # Full source-of-truth records
    decision: Decision
    context_snapshot: ContextSnapshot

    # Position and order linkage (populated when the cycle touched a position)
    position_ids: list[str] = []
    order_ids: list[str] = []

    # Denormalized analytics index — derived from decision/context_snapshot at write
    strategy: str | None = None
    underlying: str | None = None
    net_delta_at_open: float | None = None
    earnings_within_dte: bool | None = None
    conviction: float | None = None
    iv_rank_at_open: float | None = None

    # Versioning fields — top-level so before/after analysis can filter without
    # unpacking nested snapshots
    limits_version: str
    prompt_version: str
    model_id: str

    # Rejection index — ValidationRuleId values from ValidationResult.reasons
    # when action_taken == REJECTED; empty for all other action_taken values
    rejection_rule_ids: list[ValidationRuleId] = []

    @model_validator(mode="after")
    def _check_invariants(self) -> "JournalRecord":
        if self.action_taken != self.decision.action_taken:
            raise ValueError(
                f"action_taken={self.action_taken!r} disagrees with "
                f"decision.action_taken={self.decision.action_taken!r}"
            )
        if self.action_taken == ActionTaken.REJECTED and not self.rejection_rule_ids:
            raise ValueError(
                "rejection_rule_ids must be non-empty when action_taken == REJECTED"
            )
        return self
