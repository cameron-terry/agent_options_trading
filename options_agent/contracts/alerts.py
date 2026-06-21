"""Alert contracts for WP-7.2.

AlertEvent is emitted by WP-8 (orchestrator) when a fill, rejection, or
kill-switch change occurs, and consumed by obs/alerts.py for delivery.

Both event_type (what happened) and severity (how urgent) are present — they
answer different questions and are independently useful: event_type is the
queryable semantic key; severity drives routing and filtering on the channel.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class AlertEventType(StrEnum):
    """What triggered the alert.

    ENTRY_SUBMITTED    — order sent to broker (working, not yet confirmed filled).
    EXIT_SUBMITTED     — a closing order was sent to the broker (may be WORKING).
    FILL               — a position was confirmed closed by the broker
                         (fill confirmed, realized_pnl known).
    REJECTION          — a TradeProposal was rejected by the validator.
    KILL_SWITCH_CHANGE — kill-switch state changed (HALT, FLATTEN, or NONE resume).
    STATE_INTEGRITY    — reconcile detected an anomaly (orphan, unmatched-local, etc).

    EXIT_SUBMITTED fires at close-order-submit time so the operator has an early
    signal that an exit was triggered. FILL fires in _finalize_closed_positions()
    once reconcile confirms CLOSED and realized_pnl is available — two distinct
    events, two distinct moments. Never fire two FILLs for the same close.

    Delivery failures are recorded as rows in alert_delivery_failures (DB),
    not as AlertEvents dispatched through the channel — dispatching would be
    circular. If a future WP needs to react programmatically to persistent
    delivery failures, scope the meta-alert flow in that WP's ticket.

    WP-0 amendment: #68 (WP-7.2) introduced FILL, REJECTION, KILL_SWITCH_CHANGE.
    Extended in #72/WP-8.2 (ENTRY_SUBMITTED, STATE_INTEGRITY), #73/WP-8.3
    (EXIT_SUBMITTED), and #74/WP-8.4 (SCHEDULER_SKIP).
    """

    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    EXIT_SUBMITTED = "EXIT_SUBMITTED"
    FILL = "FILL"
    REJECTION = "REJECTION"
    KILL_SWITCH_CHANGE = "KILL_SWITCH_CHANGE"
    STATE_INTEGRITY = "STATE_INTEGRITY"
    SCHEDULER_SKIP = "SCHEDULER_SKIP"


class AlertSeverity(StrEnum):
    """How urgently the alert should be treated.

    INFO     — routine event; expected during normal operation (fills).
    WARN     — degraded but recoverable; needs attention soon (rejections).
    CRITICAL — system-level event requiring immediate action (kill-switch).
    """

    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


DEFAULT_SEVERITY: dict[AlertEventType, AlertSeverity] = {
    AlertEventType.ENTRY_SUBMITTED: AlertSeverity.INFO,
    AlertEventType.EXIT_SUBMITTED: AlertSeverity.INFO,
    AlertEventType.FILL: AlertSeverity.INFO,
    AlertEventType.REJECTION: AlertSeverity.WARN,
    AlertEventType.KILL_SWITCH_CHANGE: AlertSeverity.CRITICAL,
    AlertEventType.STATE_INTEGRITY: AlertSeverity.WARN,
    AlertEventType.SCHEDULER_SKIP: AlertSeverity.WARN,
}


class AlertEvent(BaseModel):
    """A single alertable system event.

    symbol and order_id are optional: kill-switch changes are not
    position-specific, so those fields will be None.
    """

    event_type: AlertEventType
    severity: AlertSeverity
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    symbol: str | None = None
    order_id: str | None = None
    detail: str
