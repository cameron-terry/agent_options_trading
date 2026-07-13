"""WP-9.8/9.9: POST /api/ask wiring tests (ui/ask.py, ui/app.py).

agent/ask.py's ask()/ask_stream() are mocked throughout — no Anthropic API
calls. WP-9.9 converted the endpoint to an SSE stream; tests parse the raw
`event: X\\ndata: Y\\n\\n` frames the same way the frontend does.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import sqlalchemy as sa
from fastapi.testclient import TestClient

from options_agent.agent.ask import (
    Answer,
    AskError,
    AskResult,
    QueryError,
    QueryResult,
    QueryStarted,
)
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


def _parse_sse(text: str) -> list[tuple[str, str]]:
    """Parse raw SSE body text into a list of (event, data_json) pairs —
    mirrors the frame-splitting logic frontend/src/api.ts's streamAsk()
    implements over fetch()'s ReadableStream.
    """
    frames = [f for f in text.split("\n\n") if f.strip()]
    parsed = []
    for frame in frames:
        event = "message"
        data = ""
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        parsed.append((event, data))
    return parsed


_OPENED_SQL = "SELECT cycle_id FROM journal_records WHERE action_taken='OPENED'"

_FAKE_EVENTS = [
    QueryStarted(sql=_OPENED_SQL),
    QueryResult(
        sql=_OPENED_SQL,
        columns=["cycle_id"],
        rows=[{"cycle_id": "c1"}, {"cycle_id": "c2"}],
        truncated=False,
        row_cap=500,
    ),
    Answer(
        answer_text="2 cycles opened positions in that window.",
        executed_sql=[_OPENED_SQL],
        cited_cycle_ids=["c1", "c2"],
    ),
]


_FAKE_RESULT = AskResult(
    answer_text="2 cycles opened positions in that window.",
    executed_sql=[_OPENED_SQL],
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
    assert response.tables_touched == ["journal_records"]


def test_post_api_ask_streams_query_and_answer_events() -> None:
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    with patch("options_agent.ui.ask.ask_stream", return_value=iter(_FAKE_EVENTS)):
        resp = client.post("/api/ask", json={"question": "How many cycles opened?"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(resp.text)
    events = [event for event, _ in frames]
    assert events == ["query_started", "query_result", "answer"]

    answer_data = json.loads(frames[2][1])
    assert answer_data["answer_text"] == "2 cycles opened positions in that window."
    assert answer_data["cited_cycle_ids"] == ["c1", "c2"]
    assert answer_data["tables_touched"] == ["journal_records"]

    query_result_data = json.loads(frames[1][1])
    assert query_result_data["columns"] == ["cycle_id"]
    assert query_result_data["rows"] == [{"cycle_id": "c1"}, {"cycle_id": "c2"}]


def test_post_api_ask_streams_query_error_event() -> None:
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)
    events = [
        QueryStarted(sql="DELETE FROM journal_records"),
        QueryError(
            sql="DELETE FROM journal_records",
            error="Only SELECT statements are allowed.",
        ),
        Answer(answer_text="I can't run that.", executed_sql=[], cited_cycle_ids=[]),
    ]

    with patch("options_agent.ui.ask.ask_stream", return_value=iter(events)):
        resp = client.post("/api/ask", json={"question": "Delete everything"})

    frames = _parse_sse(resp.text)
    assert [event for event, _ in frames] == ["query_started", "query_error", "answer"]

    error_data = json.loads(frames[1][1])
    assert error_data["sql"] == "DELETE FROM journal_records"
    assert "SELECT" in error_data["error"]


def test_post_api_ask_streams_terminal_error_event_on_ask_error() -> None:
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    def _raise(*args: object, **kwargs: object) -> object:
        raise AskError("Model called unknown tool 'cancel_order'")
        yield  # pragma: no cover — makes this a generator function

    with patch("options_agent.ui.ask.ask_stream", side_effect=_raise):
        resp = client.post("/api/ask", json={"question": "Cancel my orders"})

    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert [event for event, _ in frames] == ["error"]

    error_data = json.loads(frames[0][1])
    assert "cancel_order" in error_data["message"]


def test_post_api_ask_sends_history_to_ask_stream() -> None:
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    with patch(
        "options_agent.ui.ask.ask_stream", return_value=iter(_FAKE_EVENTS)
    ) as mock_ask_stream:
        resp = client.post(
            "/api/ask",
            json={
                "question": "And how many were profitable?",
                "history": [
                    {"question": "How many SPY trades?", "answer_text": "3 SPY trades."}
                ],
            },
        )

    assert resp.status_code == 200
    _ = resp.text  # drain the stream so the generator (and the mock call) runs
    mock_ask_stream.assert_called_once()
    history_arg = mock_ask_stream.call_args.kwargs["history"]
    assert len(history_arg) == 1
    assert history_arg[0].question == "How many SPY trades?"
    assert history_arg[0].answer_text == "3 SPY trades."


def test_post_api_ask_requires_question_field() -> None:
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    resp = client.post("/api/ask", json={})

    assert resp.status_code == 422
