"""Kill-switch console API — WP-9.7.

The console's only write path: POST /api/killswitch reuses
obs/killswitch.py's set_state()/resume() verbatim, so the UI and the CLI
share one code path — including dispatching the existing CRITICAL alert
(obs.alerts.AlertDispatcher). Also serves the alert-delivery health panel
over alert_delivery_failures.

Write isolation (WP-9 epic invariant: exactly one write path). This module
is the only place in options_agent/ui/ that executes an INSERT — every other
router in ui/app.py reads through app.state.engine (read_only=True; see
state.db.build_engine). The functions here take a Connection from a
*separate* write-capable engine (app.state.write_engine, read_only=False,
same DB_URL) that ui/app.py builds once and passes only to this router.
test_ui_killswitch.py asserts no other ui/*.py module references
write_engine, so a future endpoint can't accidentally acquire write access
by copy-pasting a wiring line.

Confirmation UX (card decision, 2026-07-13): arming HALT is zero-friction
(reason only, matching the CLI's `set HALT` — see obs/__main__.py). RESUME
and FLATTEN both require the operator to type the action word verbatim in
`confirmation` — stricter than the CLI (whose `set FLATTEN` is also zero-
friction and whose `resume` uses a y/N prompt), a deliberate point-and-click
safeguard since a UI button has no equivalent of a CLI's explicit command
line. `reason` is mandatory for every action, matching obs/killswitch.py's
own validation.

set_by is fixed to "console" for every UI-initiated change. This
deployment has no per-operator identity (single-operator, no auth — see
the WP-9 epic's stated scope), so a free-text field would record prose no
one would ever meaningfully filter or compare.

Known gap, not fixed here (out of WP-9.7's scope): obs/__main__.py's
cmd_set/cmd_resume call set_state()/resume() without a dispatcher, so
today's CLI kill-switch changes never actually fire the CRITICAL alert
despite obs/killswitch.py supporting it. This module dispatches for real
so the alert-delivery health panel has something to show; the CLI's
missing wiring is a separate, pre-existing issue (see PR description).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, model_validator
from sqlalchemy.engine import Connection

from options_agent.contracts.state import KillSwitchState
from options_agent.obs.alerts import AlertDispatcher
from options_agent.obs.killswitch import (
    KillSwitchEntry,
    get_current_state,
    list_history,
    resume,
    set_state,
)
from options_agent.state.db import alert_delivery_failures_table

SET_BY = "console"

KillSwitchAction = Literal["HALT", "FLATTEN", "RESUME"]

# Actions requiring the operator to type the action word verbatim before the
# request is accepted. HALT is deliberately absent — arming is zero-friction
# per the card, matching the CLI's `set HALT`.
_CONFIRMATION_REQUIRED: dict[str, str] = {"FLATTEN": "FLATTEN", "RESUME": "RESUME"}


class KillSwitchActionRequest(BaseModel):
    action: KillSwitchAction
    reason: str
    confirmation: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> KillSwitchActionRequest:
        if not self.reason.strip():
            raise ValueError("reason must not be empty")
        required = _CONFIRMATION_REQUIRED.get(self.action)
        if required is not None and self.confirmation != required:
            raise ValueError(
                f"confirmation must be exactly {required!r} for action={self.action}"
            )
        return self


class KillSwitchHistoryEntry(BaseModel):
    id: str
    state: KillSwitchState
    set_by: str
    reason: str
    created_at: datetime


class AlertFailureItem(BaseModel):
    id: str
    event_type: str
    severity: str
    detail: str
    attempted_at: datetime
    attempts: int
    last_error: str


class KillSwitchStatusResponse(BaseModel):
    state: KillSwitchState
    history: list[KillSwitchHistoryEntry]
    alert_failures: list[AlertFailureItem]


def _to_history_entry(entry: KillSwitchEntry) -> KillSwitchHistoryEntry:
    return KillSwitchHistoryEntry(
        id=entry.id,
        state=entry.state,
        set_by=entry.set_by,
        reason=entry.reason,
        created_at=entry.created_at,
    )


def get_alert_delivery_failures(
    conn: Connection, *, limit: int = 20
) -> list[AlertFailureItem]:
    """Most recent alert_delivery_failures rows, newest first."""
    rows = conn.execute(
        sa.select(alert_delivery_failures_table)
        .order_by(alert_delivery_failures_table.c.attempted_at.desc())
        .limit(limit)
    ).fetchall()
    return [
        AlertFailureItem(
            id=row.id,
            event_type=row.event_type,
            severity=row.severity,
            detail=row.detail,
            attempted_at=row.attempted_at,
            attempts=row.attempts,
            last_error=row.last_error,
        )
        for row in rows
    ]


def get_killswitch_status(
    conn: Connection, *, history_limit: int = 20
) -> KillSwitchStatusResponse:
    """GET /api/killswitch — current state, history, and alert-delivery health."""
    return KillSwitchStatusResponse(
        state=get_current_state(conn),
        history=[_to_history_entry(e) for e in list_history(conn, limit=history_limit)],
        alert_failures=get_alert_delivery_failures(conn),
    )


def apply_killswitch_action(
    conn: Connection,
    request: KillSwitchActionRequest,
    *,
    dispatcher: AlertDispatcher | None,
) -> KillSwitchHistoryEntry:
    """POST /api/killswitch — arm HALT/FLATTEN or RESUME.

    request has already passed KillSwitchActionRequest's validator (reason
    non-empty, confirmation checked for RESUME/FLATTEN) by the time FastAPI
    hands it to this function.
    """
    if request.action == "RESUME":
        entry = resume(
            conn, set_by=SET_BY, reason=request.reason, dispatcher=dispatcher
        )
    else:
        entry = set_state(
            conn,
            KillSwitchState[request.action],
            set_by=SET_BY,
            reason=request.reason,
            dispatcher=dispatcher,
        )
    return _to_history_entry(entry)
