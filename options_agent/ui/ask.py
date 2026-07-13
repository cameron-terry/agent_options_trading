"""POST /api/ask — WP-9.8 backend for the Ask-the-journal analyst.

Thin wrapper around agent/ask.py's ask(): opens a connection from the
console's read-only engine (WP-9.1) and calls the analyst loop. See
agent/ask.py's module docstring for the run_sql / submit_ask_answer loop
design.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.engine import Connection

from options_agent.agent.ask import ask


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    executed_sql: list[str]
    cited_cycle_ids: list[str]


def get_ask_answer(conn: Connection, question: str) -> AskResponse:
    """Run the analyst against *question* over *conn* and shape the response."""
    result = ask(question, conn)
    return AskResponse(
        answer=result.answer_text,
        executed_sql=result.executed_sql,
        cited_cycle_ids=result.cited_cycle_ids,
    )
