from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import SizingResult, ValidationResult


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
    exit_plan: ExitPlan
    status: PositionStatus
    opened_at: datetime
    closed_at: datetime | None
    nearest_expiration: date
    est_max_loss: float
    est_max_profit: float
    opening_order_id: str


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


class ContextSnapshot(BaseModel):
    """
    The assembled, post-filter context bundle the agent actually saw.

    Stored inline (not hash+pointer) for self-contained journal queryability.
    context_hash is stored alongside to support WP-7 reproducibility queries
    (e.g. 'did identical context produce different proposals?').
    model_id and prompt_version enable honest before/after prompt analysis.
    """

    assembled_context: dict[str, Any]
    context_hash: str
    model_id: str
    prompt_version: str
    assembled_at: datetime
