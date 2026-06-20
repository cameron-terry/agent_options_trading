from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import SizingResult, ValidationResult

# Sentinel used for equity positions produced by assignment: equity doesn't expire.
# WP-5 must check asset_class == AssetClass.EQUITY and skip DTE/time-stop logic.
EQUITY_NEVER_EXPIRES: date = date(9999, 12, 31)


class AssetClass(StrEnum):
    """Discriminator for Position records.

    OPTION_STRATEGY — one or more option legs forming a defined-risk strategy.
    EQUITY          — shares held as the result of an options assignment event.
                      WP-5 must skip DTE/exit-plan logic for EQUITY positions.
                      WP-7 uses assigned_from_position_id to attribute P&L.
                      WP-8 owns the disposition policy (auto-liquidate vs. halt).
    """

    OPTION_STRATEGY = "option_strategy"
    EQUITY = "equity"


class KillSwitchState(StrEnum):
    """System-wide kill-switch flag written by WP-7 and read at the top of every cycle.

    NONE    — system operating normally.
    HALT    — no new entries; monitor exits continue.
    FLATTEN — close all open positions immediately; no new entries.
    """

    NONE = "NONE"
    HALT = "HALT"
    FLATTEN = "FLATTEN"


class ActionTaken(StrEnum):
    """Outcome of one entry cycle — primary grouping key for all WP-7 analytics.

    Enum values are used directly as DB/JSON strings; never pass a free str
    where ActionTaken is expected (same discipline as ValidationRuleId).

    OPENED          — proposal passed validation and sizing; order submitted.
    CLOSED          — cycle explicitly closed an existing position.
    ROLLED          — cycle rolled a position to new strikes or expiry.
    NO_ACTION_GATED — short-circuited before the LLM call (kill-switch, blackout,
                      buying power, max-positions gate).
    NO_ACTION_AGENT — LLM was called; agent returned action=NO_ACTION.
    SIZED_TO_ZERO   — proposal passed validation but sizing returned 0 contracts;
                      not a rejection and not an agent no-action.
    REJECTED        — proposal failed deterministic validation (ERROR rules fired).
    EXECUTION_FAILED — passed validation and sizing, but broker rejected the order.
    """

    OPENED = "OPENED"
    CLOSED = "CLOSED"
    ROLLED = "ROLLED"
    NO_ACTION_GATED = "NO_ACTION_GATED"
    NO_ACTION_AGENT = "NO_ACTION_AGENT"
    SIZED_TO_ZERO = "SIZED_TO_ZERO"
    REJECTED = "REJECTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class LegStatus(StrEnum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    EXERCISED = "EXERCISED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


class PositionStatus(StrEnum):
    PENDING_OPEN = "PENDING_OPEN"
    OPEN = "OPEN"
    PENDING_CLOSE = "PENDING_CLOSE"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    ASSIGNED = "ASSIGNED"


class OrderRole(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    ROLL = "ROLL"


class ExitReason(StrEnum):
    """Why the monitor submitted a closing order for this position.

    Stored on Order and carried through to OutcomeRecord so WP-7 can group
    closed positions by exit trigger without unpacking log files.

    STOP_LOSS     — unrealized P&L crossed the stop-loss threshold.
    PROFIT_TARGET — unrealized P&L reached the profit-target percentage.
    DTE           — days-to-expiration reached the time-stop threshold.
    FLATTEN       — kill-switch FLATTEN overrode normal exit evaluation.
    """

    STOP_LOSS = "STOP_LOSS"
    PROFIT_TARGET = "PROFIT_TARGET"
    DTE = "DTE"
    FLATTEN = "FLATTEN"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PositionLeg(BaseModel):
    """One leg of a held position, wrapping the contract spec with fill details."""

    leg: Leg
    filled_qty: int
    avg_fill_price: float
    status: LegStatus


class LegFill(BaseModel):
    """Per-leg fill detail from the broker — source of truth for slippage analysis."""

    leg: Leg
    filled_qty: int
    fill_price: float


class EquityLeg(BaseModel):
    """Shares held as the direct result of an options assignment.

    qty is signed: positive = long (received shares, e.g., short put assigned);
    negative = short (delivered shares, e.g., short call assigned).
    avg_price is the strike at which the assignment occurred.
    symbol is the underlying equity ticker (e.g., 'SPY'), not the OCC string.
    """

    symbol: str
    qty: int
    avg_price: float


class Position(BaseModel):
    """
    One strategy-level position record (one iron condor = one Position).

    Lifecycle of orders: Order.position_id links many Orders to this Position.
    opening_order_id is an immutable convenience pointer set once at open.
    broker_order_id lives only on Order — never duplicated here.

    entry_net_amount sign convention:
        positive = net debit paid, negative = net credit received.
    current_mark and unrealized_pnl are cached snapshots from the last
    reconcile; not authoritative.
    nearest_expiration is denormalised for cheap DTE computation by WP-5.
    est_max_loss / est_max_profit are carried from the proposal for exit rules.
    """

    id: str
    underlying: str
    strategy: str
    legs: list[PositionLeg]
    quantity: int
    entry_net_amount: float
    current_mark: float
    marked_at: datetime
    unrealized_pnl: float
    realized_pnl: float | None
    # None for EQUITY positions (no predefined exit plan); always set for
    # OPTION_STRATEGY. WP-5 must guard: if pos.exit_plan is None: skip.
    exit_plan: ExitPlan | None
    status: PositionStatus
    opened_at: datetime
    closed_at: datetime | None
    nearest_expiration: date
    est_max_loss: float
    est_max_profit: float
    opening_order_id: str
    # WP-1.5 contract additions — WP-0 change approved 2026-06-13
    asset_class: AssetClass = AssetClass.OPTION_STRATEGY
    # Populated for EQUITY positions only; empty for OPTION_STRATEGY.
    equity_legs: list[EquityLeg] = []
    # Set on equity positions to the option Position.id that caused the assignment.
    # WP-7 uses this to attribute end-to-end P&L across the full cycle.
    assigned_from_position_id: str | None = None


class Order(BaseModel):
    """
    Broker-facing order entity.

    position_id is the FK linking this Order to its Position.
    role (OPEN/CLOSE/ROLL) distinguishes order purpose — use this instead of
    a closing_order_id on Position, since a position may have multiple
    closing/roll orders (partial closes, re-prices).

    broker_status_raw preserves Alpaca's exact status string alongside the
    canonical enum so mapping bugs remain recoverable.
    legs_filled is the per-leg source of truth; net_fill_price and filled_qty
    are derived.

    limit_price is the price submitted to the broker (set by submit(); None
    for orders sourced from reconcile that pre-date this field).
    """

    id: str
    broker_order_id: str
    position_id: str
    role: OrderRole
    status: OrderStatus
    broker_status_raw: str
    submitted_at: datetime
    filled_at: datetime | None
    limit_price: float | None = None
    legs_filled: list[LegFill]
    net_fill_price: float | None
    filled_qty: int
    # Set by the monitor cycle when submitting a closing order so the reason
    # is queryable at OutcomeRecord write time (next cycle after fill confirms).
    # None for opening orders and for orders sourced from pre-WP-5.5 reconcile.
    exit_reason: ExitReason | None = None


class Decision(BaseModel):
    """
    Embedded value type for the reasoning outcome of one entry cycle.

    Not persisted as a standalone entity — lives inside JournalRecord (WP-0.4).
    Covers NO_ACTION and REJECTED cycles (proposal is None for NO_ACTION).
    """

    proposal: TradeProposal | None
    validation_result: ValidationResult | None
    sizing_result: SizingResult | None
    action_taken: ActionTaken


class ToolCallRecord(BaseModel):
    """One tool-call exchange from the agent's exploration phase.

    Stored in ContextSnapshot.tool_calls_transcript (WP-6.4 additive amendment).
    tool_input mirrors the model's input dict so the exchange is replayable.
    result_json is pre-serialized because tool results are heterogeneous
    (Pydantic models, dicts, lists) and uniform JSON storage avoids
    per-type deserialization logic in WP-7 analytics.
    """

    tool_name: str
    tool_input: dict[str, Any]
    result_json: str


class ContextSnapshot(BaseModel):
    """
    The assembled, post-filter context bundle the agent actually saw.

    Stored inline (not hash+pointer) for self-contained journal queryability.
    context_hash is stored alongside to support WP-7 reproducibility queries
    (e.g. 'did identical context produce different proposals?').
    model_id and prompt_version enable honest before/after prompt analysis.
    tool_calls_transcript records the read-only tool exchanges the agent made
    during the exploration phase (WP-6.4 additive amendment). Empty for cycles
    that short-circuit before the LLM call and for pre-WP-6.4 records.
    """

    assembled_context: dict[str, Any]
    context_hash: str
    model_id: str
    prompt_version: str
    assembled_at: datetime
    tool_calls_transcript: list[ToolCallRecord] = []


# ---------------------------------------------------------------------------
# Reconcile types (WP-1.4)
# ---------------------------------------------------------------------------


class FillEvent(BaseModel):
    """Immutable, append-only record of one broker fill execution.

    Idempotency is enforced via broker_exec_id — insert only if that key is
    not already present.  For Alpaca REST reconcile, broker_exec_id is
    "{broker_order_id}@{cumulative_filled_qty}" since the REST API does not
    surface per-execution IDs; each unique (order, qty-level) pair is one
    execution observation.

    filled_qty is the INCREMENTAL quantity filled in this execution (derived
    from current broker cumulative minus previously-recorded cumulative).
    leg_symbol is the OCC option symbol for the filled leg.
    occurred_at is the broker-reported fill time; observed_at is when this
    reconcile pass ran.
    """

    id: str
    order_id: str
    broker_exec_id: str
    leg_symbol: str
    filled_qty: int
    fill_price: float
    occurred_at: datetime
    observed_at: datetime


class OrderRef(BaseModel):
    """Minimal broker-side order reference used for orphan reporting.

    Carries only the fields needed to surface an orphan in the StateDiff
    without importing raw broker objects into the contract layer.
    raw is an opaque dict for debugging (not queried by application code).
    """

    broker_order_id: str
    broker_status_raw: str
    submitted_at: datetime | None
    raw: dict[str, Any] = {}


class ReconcileAnomaly(BaseModel):
    """A fill-detection inconsistency that could not be cleanly reconciled.

    Examples: filled_qty went backwards, leg count mismatch between broker
    and local order, broker_order_id not found after multiple retries.
    Both order_id and broker_order_id are optional because anomalies can
    arise from either side of the diff.
    """

    order_id: str | None
    broker_order_id: str | None
    description: str
    raw: dict[str, Any] = {}


class AssignmentEvent(BaseModel):
    """One options assignment detected by the WP-1.5 reconcile pass.

    Keeps expiration-driven closes (expired_option_positions) and assignment
    events (assigned_positions) as separate first-class entities in StateDiff
    so consumers never have to re-infer causality from closed+new pairs.

    created_equity_position is the EQUITY Position row inserted by reconcile.
    WP-8 must act on this: an options bot should not silently hold assigned
    equity — implement an auto-liquidate or halt policy.
    """

    closed_option_position_id: str
    created_equity_position: Position | None
    assigned_qty: int
    assignment_price: float
    occurred_at: datetime


class StateDiff(BaseModel):
    """Result of one reconcile pass — observed broker state vs. local DB.

    The top block (newly_*) contains orders whose status or fill count changed
    this pass.  new_positions / closed_positions are the position-level
    consequences of fills.

    Re-entry semantics for newly_partial: an order can appear in newly_partial
    on multiple consecutive passes as it accumulates fills
    (e.g. 3/5 on pass 1, 4/5 on pass 2).  WP-8 callers must treat
    newly_partial as "new incremental fills this pass," not "first time this
    order went partial."  Each pass's incremental fill qty is recorded in the
    corresponding FillEvent row.

    The bottom block surfaces unhappy-path cases that could not be
    auto-reconciled.  Callers (WP-5, WP-8) must act on these — reconcile
    only detects and reports, never silently drops them.

      orphans                  — open at broker, no matching local record.
      unmatched_local          — local PENDING_SUBMIT with empty broker_order_id
                                 (crash between DB write and broker submit).
      anomalies                — data-integrity problems requiring human review.
      expired_option_positions — option positions closed by expiry this pass
                                 (WP-1.5: externally-initiated, no fill event).
      assigned_positions       — assignment events this pass; each carries the
                                 closed option position id and the new equity
                                 position record (WP-1.5).

    reconciled_at is the UTC time the pass completed.
    """

    newly_filled: list[Order] = []
    newly_partial: list[Order] = []
    newly_cancelled: list[Order] = []
    newly_rejected: list[Order] = []
    newly_expired: list[Order] = []
    new_positions: list[Position] = []
    closed_positions: list[Position] = []

    orphans: list[OrderRef] = []
    unmatched_local: list[Order] = []
    anomalies: list[ReconcileAnomaly] = []

    # WP-1.5 additions
    expired_option_positions: list[Position] = []
    assigned_positions: list[AssignmentEvent] = []

    reconciled_at: datetime
