from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from options_agent.contracts.state import ActionTaken, ContextSnapshot, Decision


class OutcomeEventType(StrEnum):
    """Type of terminal-ish event on a position.

    Multiple events per position are normal (partial close, roll, final close).
    """

    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    FULL_CLOSE = "FULL_CLOSE"
    ROLL = "ROLL"
    EXPIRED = "EXPIRED"
    ASSIGNED = "ASSIGNED"


class OutcomeRecord(BaseModel):
    """One terminal-ish event on a position — append-only, never updated.

    Multiple OutcomeRecords may exist per position (e.g. partial close then
    full close, or roll then expiry). Link to the opening JournalRecord via:
        OutcomeRecord.position_id → Position → JournalRecord.position_ids

    Never linked directly to a cycle_id: monitor-driven closes have no
    entry-cycle JournalRecord. Position is the stable join spine.
    """

    id: str
    position_id: str
    event_type: OutcomeEventType
    recorded_at: datetime
    contracts_closed: int
    realized_pnl: float
    fill_price: float | None = None
    closing_order_id: str | None = None


class JournalRecord(BaseModel):
    """Immutable record written once at the end of each entry cycle.

    Never updated after write. Outcome data lives in OutcomeRecord, joined via:
        JournalRecord.position_ids → Position → OutcomeRecord.position_id

    Denormalized fields (strategy, underlying, etc.) are a queryable index over
    the nested decision and context_snapshot. They must be derived from those
    objects at write time and must never be set independently — the nested
    objects are the source of truth. If a denormalized field disagrees with its
    source, the nested object wins.
    """

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

    # Rejection index — rule_ids from ValidationResult.reasons when REJECTED;
    # empty for all other action_taken values
    rejection_rule_ids: list[str] = []
