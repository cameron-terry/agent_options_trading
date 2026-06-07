from enum import StrEnum

from pydantic import BaseModel

from options_agent.contracts.proposal import TradeProposal
from options_agent.contracts.results import SizingResult, ValidationResult
from options_agent.contracts.state import ActionTaken

__all__ = ["ActionTaken"]  # re-exported; canonical definition lives in state.py


class ShortCircuitReason(StrEnum):
    """Fine-grained reason an entry cycle exited before calling the agent.

    Kept separate from ActionTaken so the two questions are answered
    independently: action_taken = what the cycle did (coarse),
    short_circuit_reason = why it stopped early (granular, null when the
    full flow ran).

    Invariant: short_circuit_reason is not None implies
    action_taken == ActionTaken.NO_ACTION_GATED.

    Values are distinct so WP-7 can distinguish a tuning problem
    (MAX_POSITIONS fires constantly) from a benign one (MARKET_CLOSED).
    """

    KILL_SWITCH_HALT = "KILL_SWITCH_HALT"
    KILL_SWITCH_FLATTEN = "KILL_SWITCH_FLATTEN"
    MARKET_CLOSED = "MARKET_CLOSED"
    BLACKOUT_WINDOW = "BLACKOUT_WINDOW"
    NO_BUYING_POWER = "NO_BUYING_POWER"
    MAX_POSITIONS = "MAX_POSITIONS"
    EMPTY_ACTION_SPACE = "EMPTY_ACTION_SPACE"


class CycleStage(StrEnum):
    """Stage of the entry cycle where a CycleError occurred.

    Used by WP-7 to cluster failures: EXECUTE failures point to broker
    problems; REASON failures point to model problems; JOURNAL failures
    are the most dangerous (cycle may have acted without a record).
    """

    RECONCILE = "RECONCILE"
    GATES = "GATES"
    ASSEMBLE = "ASSEMBLE"
    REASON = "REASON"
    VALIDATE = "VALIDATE"
    SIZE = "SIZE"
    EXECUTE = "EXECUTE"
    JOURNAL = "JOURNAL"


class CycleError(BaseModel):
    """An operational failure captured inside a cycle result.

    recoverable=True means the scheduler may retry or continue the loop.
    recoverable=False means the cycle should not repeat without intervention
    (e.g., permanent broker auth error).

    Infrastructure failures that prevent journalling (DB unreachable,
    config corrupt) are not encoded here — they raise and halt the loop.
    """

    stage: CycleStage
    message: str
    recoverable: bool


class CycleResult(BaseModel):
    """Summary returned to the immediate caller after one entry cycle.

    This is NOT a replacement for the journal. A JournalRecord is always
    written as a side-effect (step 9) regardless of outcome, even under
    error paths. journal_record_id is the FK into that durable record.
    The return value exists so the scheduler can react without a DB read.

    Invariant: short_circuit_reason is not None implies
    action_taken == ActionTaken.NO_ACTION_GATED.
    """

    cycle_id: str
    action_taken: ActionTaken
    short_circuit_reason: ShortCircuitReason | None = None
    proposal: TradeProposal | None = None
    validation: ValidationResult | None = None
    sizing: SizingResult | None = None
    error: CycleError | None = None
    journal_record_id: str | None = None


class MonitorResult(BaseModel):
    """Summary returned after one monitor cycle.

    exits_triggered: position IDs for which a closing order was submitted.
    orders_submitted: broker order IDs for those closing orders.
    errors: per-position failures. One bad position must not stop the others
    from having their stops checked — the loop continues and collects errors.
    """

    positions_evaluated: int
    exits_triggered: list[str]
    orders_submitted: list[str]
    errors: list[CycleError]
