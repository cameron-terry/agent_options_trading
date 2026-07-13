"""POST /api/ask — WP-9.8 backend for the Ask-the-journal analyst, converted
to an SSE stream in WP-9.9.

Thin wrapper around agent/ask.py's ask_stream(): opens a connection from the
console's read-only engine (WP-9.1) and streams the analyst loop's events as
Server-Sent Events. See agent/ask.py's module docstring for the run_sql /
submit_ask_answer loop design.

Event grain (one SSE `event:` name per line below), matching agent/ask.py's
AskEvent variants 1:1:
  query_started  {"sql": str}
  query_result   {"sql", "columns", "rows", "truncated", "row_cap"}
  query_error    {"sql": str, "error": str}
  answer         {"answer_text", "executed_sql", "cited_cycle_ids", "tables_touched"}
  error          {"message": str} — terminal; AskError raised before an Answer
                 was produced (unknown tool call, schema validation
                 exhausted). Anthropic SDK errors are NOT caught here, same
                 policy as agent/ask.py — the connection just drops.

tables_touched is derived server-side from executed_sql via sqlglot (already
a dependency — see agent/sql_guard.py), not self-reported by the model, for
the same reason executed_sql itself is derived rather than trusted: it feeds
the Ask screen's plan chips and must not be able to drift from what was
actually queried.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from typing import Any

import sqlglot
from pydantic import BaseModel
from sqlalchemy.engine import Connection
from sqlglot import exp

from options_agent.agent.ask import (
    Answer,
    AskError,
    AskEvent,
    HistoryTurn,
    QueryError,
    QueryResult,
    QueryStarted,
    ask,
    ask_stream,
)

logger = logging.getLogger(__name__)


class AskHistoryTurn(BaseModel):
    question: str
    answer_text: str


class AskRequest(BaseModel):
    question: str
    history: list[AskHistoryTurn] = []


class AskResponse(BaseModel):
    answer: str
    executed_sql: list[str]
    cited_cycle_ids: list[str]
    tables_touched: list[str]


def _tables_touched(executed_sql: Sequence[str]) -> list[str]:
    """Best-effort table-name extraction from executed SQL, for the plan
    chip. Parse failures are impossible in practice (every entry here already
    passed sql_guard's validate_select_only), but this is presentation-layer
    plumbing, not a guardrail — swallow rather than fail the whole answer
    over a chip.
    """
    tables: set[str] = set()
    for sql in executed_sql:
        try:
            parsed = sqlglot.parse_one(sql, dialect="sqlite")
        except Exception:
            continue
        for table in parsed.find_all(exp.Table):
            tables.add(table.name)
    return sorted(tables)


def get_ask_answer(
    conn: Connection, question: str, history: Sequence[HistoryTurn] | None = None
) -> AskResponse:
    """Non-streaming convenience wrapper — drains ask() fully. Used by tests
    and any future caller that doesn't need incremental progress; the actual
    /api/ask endpoint streams via ask_event_stream() below instead.
    """
    result = ask(question, conn, history=history)
    return AskResponse(
        answer=result.answer_text,
        executed_sql=result.executed_sql,
        cited_cycle_ids=result.cited_cycle_ids,
        tables_touched=_tables_touched(result.executed_sql),
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _serialize_event(event: AskEvent) -> str:
    if isinstance(event, QueryStarted):
        return _sse("query_started", {"sql": event.sql})
    if isinstance(event, QueryResult):
        return _sse(
            "query_result",
            {
                "sql": event.sql,
                "columns": event.columns,
                "rows": event.rows,
                "truncated": event.truncated,
                "row_cap": event.row_cap,
            },
        )
    if isinstance(event, QueryError):
        return _sse("query_error", {"sql": event.sql, "error": event.error})
    if isinstance(event, Answer):
        return _sse(
            "answer",
            {
                "answer_text": event.answer_text,
                "executed_sql": event.executed_sql,
                "cited_cycle_ids": event.cited_cycle_ids,
                "tables_touched": _tables_touched(event.executed_sql),
            },
        )
    raise AssertionError(f"unhandled AskEvent variant: {event!r}")  # pragma: no cover


def ask_event_stream(
    conn: Connection, question: str, history: Sequence[HistoryTurn] | None = None
) -> Iterator[str]:
    """SSE body for POST /api/ask. See this module's docstring for the event
    grain. AskError (unknown tool / schema validation exhausted) is caught
    and surfaced as a terminal `error` event rather than a broken connection
    — everything else (Anthropic SDK errors) propagates, same policy as
    agent/ask.py.
    """
    try:
        for event in ask_stream(question, conn, history=history):
            yield _serialize_event(event)
    except AskError as exc:
        logger.warning("ask_event_stream: AskError — %s", exc)
        yield _sse("error", {"message": str(exc)})
