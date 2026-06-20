"""Kill-switch state management for WP-7.1.

Public API
----------
is_halted(state)    — True when state is HALT *or* FLATTEN (FLATTEN implies HALT).
is_flatten(state)   — True when state is FLATTEN.
get_current_state   — Read latest state from kill_switch_log; empty table → NONE.
set_state           — Append a new log entry (any KillSwitchState value).
resume              — Append a NONE entry with explicit acknowledgement semantics.
list_history        — Read the N most-recent log entries (newest first).

Fail-safe contract (orchestrator responsibility, not enforced here)
-------------------------------------------------------------------
Entry cycle:  if get_current_state raises, treat as HALT — fail closed.
Monitor cycle: if get_current_state raises, proceed with NONE (normal exits);
               never auto-FLATTEN on an unreadable flag.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.engine import Connection

from options_agent.contracts.state import KillSwitchState
from options_agent.state.db import kill_switch_log_table

# Deterministic ordering: latest created_at first; UUID descending as tie-breaker.
# Two entries in the same microsecond are vanishingly unlikely in production, but
# this ordering ensures a reproducible result in tests that insert multiple rows
# at identical timestamps.
_ORDER_LATEST = (
    kill_switch_log_table.c.created_at.desc(),
    kill_switch_log_table.c.id.desc(),
)


class KillSwitchEntry(BaseModel):
    """One row in the kill_switch_log append-only audit table."""

    id: str
    state: KillSwitchState
    set_by: str
    reason: str
    created_at: datetime


def is_halted(state: KillSwitchState) -> bool:
    """True when new entries must be blocked.

    CRITICAL INVARIANT: returns True under FLATTEN, not only HALT.
    FLATTEN implies HALT — the entry cycle must be blocked in both cases.
    Implementing this as ``state == KillSwitchState.HALT`` is wrong and
    dangerous: under FLATTEN, entries would proceed while positions are
    being closed, actively fighting the flatten operation.
    """
    return state in (KillSwitchState.HALT, KillSwitchState.FLATTEN)


def is_flatten(state: KillSwitchState) -> bool:
    """True when all open positions must be closed immediately."""
    return state == KillSwitchState.FLATTEN


def get_current_state(conn: Connection) -> KillSwitchState:
    """Return the current kill-switch state from the DB.

    Returns KillSwitchState.NONE when the log is empty (system has never
    been armed — normal operating state).

    Propagates DB exceptions to the caller.  The caller is responsible for
    fail-safe handling:
      - Entry cycle: catch and treat as HALT (fail closed).
      - Monitor cycle: catch and treat as NONE (normal exits; never auto-FLATTEN).
    """
    row = conn.execute(
        sa.select(kill_switch_log_table.c.state).order_by(*_ORDER_LATEST).limit(1)
    ).one_or_none()
    if row is None:
        return KillSwitchState.NONE
    return KillSwitchState(row.state)


def set_state(
    conn: Connection,
    state: KillSwitchState,
    *,
    set_by: str,
    reason: str,
) -> KillSwitchEntry:
    """Append a kill-switch log entry with the given state.

    Prefer resume() when transitioning to NONE — it carries the semantic
    distinction between arming and resuming that the audit log makes visible.
    """
    if not set_by:
        raise ValueError("set_by must not be empty")
    if not reason:
        raise ValueError("reason must not be empty — document why the switch was set")

    entry_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    conn.execute(
        kill_switch_log_table.insert().values(
            id=entry_id,
            state=state.value,
            set_by=set_by,
            reason=reason,
            created_at=now,
        )
    )
    return KillSwitchEntry(
        id=entry_id,
        state=state,
        set_by=set_by,
        reason=reason,
        created_at=now,
    )


def resume(
    conn: Connection,
    *,
    set_by: str,
    reason: str,
) -> KillSwitchEntry:
    """Resume normal trading by setting kill-switch state to NONE.

    Semantically distinct from set_state(NONE): a resume is a deliberate
    decision to re-enable trading after a halt.  The reason must document
    that the underlying issue has been resolved.

    Asymmetry is intentional: halting is instant and frictionless; resuming
    requires a conscious acknowledgement.  The CLI enforces an additional
    confirmation prompt and displays open positions before allowing resume.
    """
    return set_state(conn, KillSwitchState.NONE, set_by=set_by, reason=reason)


def list_history(conn: Connection, *, limit: int = 10) -> list[KillSwitchEntry]:
    """Return the most recent kill-switch log entries, newest first."""
    rows = conn.execute(
        sa.select(kill_switch_log_table).order_by(*_ORDER_LATEST).limit(limit)
    ).fetchall()
    return [
        KillSwitchEntry(
            id=row.id,
            state=KillSwitchState(row.state),
            set_by=row.set_by,
            reason=row.reason,
            created_at=row.created_at,
        )
        for row in rows
    ]
