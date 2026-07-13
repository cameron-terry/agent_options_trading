"""WP-9.8: POST /api/ask wiring tests (ui/ask.py, ui/app.py).

agent/ask.py's ask() is mocked throughout — no Anthropic API calls.
"""

from __future__ import annotations

from unittest.mock import patch

import sqlalchemy as sa
from fastapi.testclient import TestClient

from options_agent.agent.ask import AskResult
from options_agent.state.db import build_engine, metadata
from options_agent.ui.app import create_app
from options_agent.ui.ask import get_ask_answer


def _migrated_engine() -> sa.Engine:
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(sa.text("INSERT INTO alembic_version VALUES ('test-head')"))
    return engine


_FAKE_RESULT = AskResult(
    answer_text="2 cycles opened positions in that window.",
    executed_sql=["SELECT cycle_id FROM journal_records WHERE action_taken='OPENED'"],
    cited_cycle_ids=["c1", "c2"],
)


def test_get_ask_answer_shapes_response() -> None:
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with (
        patch("options_agent.ui.ask.ask", return_value=_FAKE_RESULT) as mock_ask,
        engine.connect() as conn,
    ):
        response = get_ask_answer(conn, "How many cycles opened positions?")

    mock_ask.assert_called_once()
    assert mock_ask.call_args.args[0] == "How many cycles opened positions?"
    assert response.answer == _FAKE_RESULT.answer_text
    assert response.executed_sql == _FAKE_RESULT.executed_sql
    assert response.cited_cycle_ids == _FAKE_RESULT.cited_cycle_ids


def test_post_api_ask_returns_answer_json() -> None:
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    with patch("options_agent.ui.app.get_ask_answer") as mock_get_ask_answer:
        from options_agent.ui.ask import AskResponse

        mock_get_ask_answer.return_value = AskResponse(
            answer=_FAKE_RESULT.answer_text,
            executed_sql=_FAKE_RESULT.executed_sql,
            cited_cycle_ids=_FAKE_RESULT.cited_cycle_ids,
        )
        resp = client.post("/api/ask", json={"question": "How many cycles opened?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == _FAKE_RESULT.answer_text
    assert body["executed_sql"] == _FAKE_RESULT.executed_sql
    assert body["cited_cycle_ids"] == _FAKE_RESULT.cited_cycle_ids


def test_post_api_ask_requires_question_field() -> None:
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    resp = client.post("/api/ask", json={})

    assert resp.status_code == 422
