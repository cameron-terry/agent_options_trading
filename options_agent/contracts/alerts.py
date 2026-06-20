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

    FILL                  — a position leg was filled by the broker.
    REJECTION             — a TradeProposal was rejected by the validator.
    KILL_SWITCH_CHANGE    — kill-switch state changed (HALT, FLATTEN, or NONE resume).
    ALERT_DELIVERY_FAILED — the alerting channel itself failed; written to DB so the
                            absence of a notification alert is queryable rather than
                            silently missing.
    """

    FILL = "FILL"
    REJECTION = "REJECTION"
    KILL_SWITCH_CHANGE = "KILL_SWITCH_CHANGE"
    ALERT_DELIVERY_FAILED = "ALERT_DELIVERY_FAILED"


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
    AlertEventType.FILL: AlertSeverity.INFO,
    AlertEventType.REJECTION: AlertSeverity.WARN,
    AlertEventType.KILL_SWITCH_CHANGE: AlertSeverity.CRITICAL,
    AlertEventType.ALERT_DELIVERY_FAILED: AlertSeverity.WARN,
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
