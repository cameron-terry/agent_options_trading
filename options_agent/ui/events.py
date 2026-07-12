"""Live activity stream — WP-9.4.

GET /api/events (wired in ui/app.py) pushes a lightweight "something changed"
tick whenever a new row lands in journal_records or kill_switch_log, or any
position's mark is refreshed. The event carries only which table changed —
not the row itself — so the client re-fetches via the REST endpoints it
already has (fetchOverview, fetchPositions, fetchCycles). That keeps exactly
one source of truth for response shapes (the REST handlers in overview.py /
cycles.py); the stream never serializes a row of its own.

Poll-only (decision recorded on the WP-9.4 Trello card, 2026-07-12): a single
code path across SQLite and Postgres, keyed on a per-table high-water mark
(max timestamp column). POLL_INTERVAL_SECONDS is decoupled from
config.monitor_interval_minutes on purpose — that setting governs how often
the scheduler *writes* updates, not how often this read-only service should
check for them; the high-water-mark queries are cheap (indexed on
journal_records.timestamp and kill_switch_log.created_at) even at a 5s
cadence. positions.marked_at has no index, but the open-position count this
system runs with (a handful at a time) makes a full scan negligible.

Reconnect semantics: no Last-Event-ID / server-side per-connection cursor.
A client that reconnects (the browser's EventSource does this automatically)
is expected to re-fetch full state via the REST endpoints, same as on first
load — the tick protocol carries no history to replay, only "check now."
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Literal, Protocol

import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy.engine import Engine

from options_agent.state.db import (
    journal_records_table,
    kill_switch_log_table,
    positions_table,
)

logger = logging.getLogger(__name__)

# Referenced as a module global (not a function default) inside event_stream
# so tests can monkeypatch it to a small value without waiting out a real 5s
# sleep per poll.
POLL_INTERVAL_SECONDS = 5.0

EventKind = Literal["journal", "killswitch", "positions"]

HighWaterMarks = dict[EventKind, datetime | None]

_HIGH_WATER_COLUMN: dict[EventKind, Column[datetime]] = {
    "journal": journal_records_table.c.timestamp,
    "killswitch": kill_switch_log_table.c.created_at,
    "positions": positions_table.c.marked_at,
}


class _DisconnectableRequest(Protocol):
    """Structural type for the one Request method event_stream needs.

    fastapi.Request satisfies this. Kept as a protocol (rather than typing
    the parameter as fastapi.Request directly) so tests can drive the
    generator with a bare stand-in instead of constructing a real ASGI
    Request.
    """

    async def is_disconnected(self) -> bool: ...


def _read_high_water_marks(engine: Engine) -> HighWaterMarks:
    """Snapshot the current max per-table timestamp.

    Establishes the baseline at connect time so a client that just opened the
    stream isn't immediately flooded with ticks for rows that already existed
    before it subscribed — only rows/updates newer than this baseline (or
    later polls) are reported.
    """
    with engine.connect() as conn:
        return {
            kind: conn.execute(sa.select(sa.func.max(column))).scalar()
            for kind, column in _HIGH_WATER_COLUMN.items()
        }


def _poll_for_changes(
    engine: Engine, marks: HighWaterMarks
) -> tuple[list[EventKind], HighWaterMarks]:
    """Compare current per-table maxes against the last-seen marks.

    Returns the kinds that advanced, plus the updated marks dict (also
    mutated in place; the return value is for callers that prefer to treat
    this as pure).
    """
    changed: list[EventKind] = []
    with engine.connect() as conn:
        for kind, column in _HIGH_WATER_COLUMN.items():
            current = conn.execute(sa.select(sa.func.max(column))).scalar()
            previous = marks.get(kind)
            if current is not None and (previous is None or current > previous):
                changed.append(kind)
                marks[kind] = current
    return changed, marks


async def event_stream(
    engine: Engine, request: _DisconnectableRequest
) -> AsyncGenerator[str, None]:
    """SSE body for GET /api/events.

    Each iteration sleeps POLL_INTERVAL_SECONDS, then polls the DB in a
    worker thread (SQLAlchemy's engine is sync). Emits one `event: update`
    per changed table kind, or a comment-only heartbeat when nothing changed
    — keeps intermediate proxies from timing out an idle connection.
    """
    marks = await asyncio.to_thread(_read_high_water_marks, engine)
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        changed, marks = await asyncio.to_thread(_poll_for_changes, engine, marks)
        if changed:
            for kind in changed:
                yield f"event: update\ndata: {json.dumps({'kind': kind})}\n\n"
        else:
            yield ": heartbeat\n\n"
