"""WP-9.8: Ask-the-journal analyst tests (agent/ask/loop.py, agent/ask/schema.py,
agent/ask/prompts.py).

All Anthropic SDK calls are mocked. No live API calls are made.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from options_agent.agent.ask import (
    Answer,
    AskError,
    HistoryTurn,
    QueryError,
    QueryResult,
    QueryStarted,
    ask,
    ask_stream,
)
from options_agent.agent.ask.prompts import build_ask_system_prompt
from options_agent.agent.ask.schema import (
    SUBMIT_ASK_ANSWER,
    TOOL_SUBMIT_ASK_ANSWER,
    _build_input_schema,
)
from options_agent.agent.ask.tool import (
    AGENT_ASK_TOOL_NAMES,
    TOOL_RUN_SQL,
    build_run_sql_tool,
)
from options_agent.state.db import build_engine, metadata

# ──────────────────────────────────────────────────────────────────────────────
# Shared fixtures / mock helpers — mirrors tests/test_reasoner.py's pattern
# ──────────────────────────────────────────────────────────────────────────────


def _seeded_engine() -> sa.Engine:
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO journal_records (cycle_id, timestamp, action_taken,"
            " decision, context_snapshot, position_ids, order_ids,"
            " limits_version, prompt_version, model_id, rejection_rule_ids)"
            " VALUES ('c1', '2026-01-01', 'OPENED', '{}', '{}', '[]', '[]',"
            " '1', '1', 'm', '[]')"
        )
    return engine


def _mock_block(block_type: str, **kwargs: Any) -> MagicMock:
    block = MagicMock()
    block.type = block_type
    for k, v in kwargs.items():
        setattr(block, k, v)
    return block


def _tool_use_block(
    name: str, input_: dict[str, Any], block_id: str = "tu_001"
) -> MagicMock:
    return _mock_block("tool_use", name=name, input=input_, id=block_id)


def _text_block(text: str = "Looking into this...") -> MagicMock:
    return _mock_block("text", text=text)


def _mock_response(stop_reason: str, content: list[Any]) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content
    return resp


_VALID_ANSWER_INPUT: dict[str, Any] = {
    "answer_text": "3 cycles opened positions in the window.",
    "cited_cycle_ids": ["c1"],
}

# For scenarios where no run_sql call (or none returning a cycle_id column)
# precedes the commit — citing "c1" here would be an ungrounded citation and
# trigger the retry-then-drop path tested separately below. Tests that only
# care about the schema/loop mechanics, not citation grounding, use this one.
_NO_CITATION_ANSWER_INPUT: dict[str, Any] = {
    "answer_text": "3 cycles opened positions in the window.",
    "cited_cycle_ids": [],
}


def _patched_ask(
    mock_responses: list[MagicMock], conn: sa.Connection | None = None, **kwargs: Any
) -> tuple[Any, MagicMock]:
    """Call ask() with a mocked Anthropic client; returns (result, mock_client)."""
    with patch("options_agent.agent.ask.loop.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = mock_responses
        MockCls.return_value = mock_client
        if conn is None:
            engine = _seeded_engine()
            with engine.connect() as c:
                result = ask("How many cycles opened positions?", c, **kwargs)
        else:
            result = ask("How many cycles opened positions?", conn, **kwargs)
    return result, mock_client


def _patched_ask_stream(
    mock_responses: list[MagicMock], **kwargs: Any
) -> tuple[list[Any], MagicMock]:
    """Call ask_stream() with a mocked Anthropic client, fully drained;
    returns (events, mock_client)."""
    with patch("options_agent.agent.ask.loop.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = mock_responses
        MockCls.return_value = mock_client
        engine = _seeded_engine()
        with engine.connect() as c:
            events = list(ask_stream("How many cycles opened positions?", c, **kwargs))
    return events, mock_client


# ──────────────────────────────────────────────────────────────────────────────
# agent/ask/schema.py — SUBMIT_ASK_ANSWER definition
# ──────────────────────────────────────────────────────────────────────────────


def test_submit_ask_answer_has_required_keys() -> None:
    assert "name" in SUBMIT_ASK_ANSWER
    assert "description" in SUBMIT_ASK_ANSWER
    assert "input_schema" in SUBMIT_ASK_ANSWER


def test_submit_ask_answer_name_constant_matches() -> None:
    assert SUBMIT_ASK_ANSWER["name"] == TOOL_SUBMIT_ASK_ANSWER
    assert TOOL_SUBMIT_ASK_ANSWER == "submit_ask_answer"


def test_submit_ask_answer_schema_has_no_title() -> None:
    schema = _build_input_schema()
    assert "title" not in schema


def test_submit_ask_answer_schema_required_fields() -> None:
    schema = _build_input_schema()
    assert "answer_text" in set(schema.get("required", []))


def test_submit_ask_answer_schema_has_no_executed_sql_field() -> None:
    # executed_sql is derived server-side from the tool-call transcript, not
    # self-reported by the model — see schema.py's module docstring.
    schema = _build_input_schema()
    assert "executed_sql" not in schema.get("properties", {})


# ──────────────────────────────────────────────────────────────────────────────
# agent/ask/prompts.py — conventions honored (WP-9.8 acceptance criterion)
# ──────────────────────────────────────────────────────────────────────────────


def test_system_prompt_states_null_iv_rank_convention() -> None:
    prompt = build_ask_system_prompt()
    assert "warm-up" in prompt
    assert "iv_rank_at_open" in prompt


def test_system_prompt_states_hit_definition() -> None:
    prompt = build_ask_system_prompt()
    assert "realized_pnl > 0" in prompt


def test_system_prompt_states_open_closed_separation() -> None:
    prompt = build_ask_system_prompt()
    assert "still open" in prompt or "still-open" in prompt


def test_system_prompt_interpolates_guardrail_limits() -> None:
    prompt = build_ask_system_prompt(row_cap=42, timeout_secs=7.0)
    assert "42" in prompt
    assert "7s" in prompt


# ──────────────────────────────────────────────────────────────────────────────
# agent/ask/loop.py — ask() loop
# ──────────────────────────────────────────────────────────────────────────────


def test_ask_no_tool_calls_commits_directly() -> None:
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    result, _ = _patched_ask(responses)
    assert result.answer_text == _NO_CITATION_ANSWER_INPUT["answer_text"]
    assert result.cited_cycle_ids == []
    assert result.executed_sql == []


def test_ask_runs_run_sql_and_records_executed_sql() -> None:
    sql = "SELECT cycle_id FROM journal_records"
    responses = [
        _mock_response(
            "tool_use", [_tool_use_block(TOOL_RUN_SQL, {"sql": sql}, block_id="tu_1")]
        ),
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _VALID_ANSWER_INPUT)],
        ),
    ]
    result, mock_client = _patched_ask(responses)
    assert result.executed_sql == [sql]
    assert result.cited_cycle_ids == ["c1"]

    # The tool_result fed back to the model must carry the real query output.
    # index 2: mock.call_args stores a reference to the *live* messages list,
    # which keeps growing after this call — [0] user question, [1] assistant
    # tool_use, [2] user tool_results (what we want), [3+] later turns.
    second_call_messages = mock_client.messages.create.call_args_list[1].kwargs[
        "messages"
    ]
    tool_result_content = second_call_messages[2]["content"][0]["content"]
    assert "c1" in tool_result_content


def test_ask_feeds_guardrail_rejection_back_as_tool_error() -> None:
    responses = [
        _mock_response(
            "tool_use",
            [
                _tool_use_block(
                    TOOL_RUN_SQL,
                    {"sql": "DELETE FROM journal_records"},
                    block_id="tu_1",
                )
            ],
        ),
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    result, mock_client = _patched_ask(responses)
    # A rejected query is never counted as "executed".
    assert result.executed_sql == []

    # index 2: see the analogous comment in
    # test_ask_runs_run_sql_and_records_executed_sql above.
    second_call_messages = mock_client.messages.create.call_args_list[1].kwargs[
        "messages"
    ]
    tool_result_block = second_call_messages[2]["content"][0]
    assert tool_result_block["is_error"] is True
    assert "SELECT" in tool_result_block["content"]


def test_ask_raises_on_unknown_tool() -> None:
    responses = [
        _mock_response(
            "tool_use",
            [_tool_use_block("cancel_order", {"order_id": "x"}, block_id="tu_1")],
        ),
    ]
    with pytest.raises(AskError, match="unknown tool"):
        _patched_ask(responses)


def test_ask_raises_when_commit_produces_no_tool_use() -> None:
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response("end_turn", [_text_block("I refuse to use the tool.")]),
    ]
    with pytest.raises(AskError, match="no tool_use block"):
        _patched_ask(responses)


def test_ask_retries_on_schema_validation_error_then_succeeds() -> None:
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            # Missing required answer_text.
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, {"cited_cycle_ids": []})],
        ),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    result, mock_client = _patched_ask(responses, max_schema_retries=2)
    assert result.answer_text == _NO_CITATION_ANSWER_INPUT["answer_text"]
    assert mock_client.messages.create.call_count == 3


def test_ask_raises_after_schema_retries_exhausted() -> None:
    bad_response = _mock_response(
        "tool_use",
        [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, {"cited_cycle_ids": []})],
    )
    responses = [
        _mock_response("end_turn", [_text_block()]),
        bad_response,
        bad_response,
    ]
    with pytest.raises(AskError, match="Schema validation failed"):
        _patched_ask(responses, max_schema_retries=1)


def test_ask_stops_exploration_at_max_turns() -> None:
    tool_call = _mock_response(
        "tool_use",
        [_tool_use_block(TOOL_RUN_SQL, {"sql": "SELECT 1"}, block_id="tu_loop")],
    )
    responses = [
        tool_call,
        tool_call,
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    result, mock_client = _patched_ask(responses, max_turns=2)
    assert result.answer_text == _NO_CITATION_ANSWER_INPUT["answer_text"]
    # 2 exploration turns + 1 commit turn.
    assert mock_client.messages.create.call_count == 3


def test_agent_ask_tool_names_is_run_sql_only() -> None:
    assert AGENT_ASK_TOOL_NAMES == frozenset({TOOL_RUN_SQL})


# ──────────────────────────────────────────────────────────────────────────────
# agent/ask/tool.py — build_run_sql_tool interpolates actual limits, not
# module-level defaults (regression coverage for the PR #94 review finding)
# ──────────────────────────────────────────────────────────────────────────────


def test_build_run_sql_tool_interpolates_custom_limits() -> None:
    tool = build_run_sql_tool(row_cap=42, timeout_secs=3.0)
    description = tool.get("description", "")
    assert "42" in description
    assert "3-second" in description


def test_build_run_sql_tool_defaults_differ_from_custom() -> None:
    default_tool = build_run_sql_tool()
    custom_tool = build_run_sql_tool(row_cap=42, timeout_secs=3.0)
    assert default_tool.get("description") != custom_tool.get("description")


def test_ask_sends_tool_description_matching_actual_limits() -> None:
    # End-to-end: ask() must build the run_sql tool from the row_cap/
    # timeout_secs it was actually called with, not sql_guard's defaults —
    # otherwise the model reads limits that don't match what
    # execute_guarded_select() enforces the moment a caller overrides them.
    responses = [
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    with patch("options_agent.agent.ask.loop.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _mock_response("end_turn", [_text_block()])
        ] + responses
        MockCls.return_value = mock_client
        engine = _seeded_engine()
        with engine.connect() as c:
            ask("Anything?", c, row_cap=17, timeout_secs=2.0)

    first_call_tools = mock_client.messages.create.call_args_list[0].kwargs["tools"]
    description = first_call_tools[0].get("description", "")
    assert "17" in description
    assert "2-second" in description


# ──────────────────────────────────────────────────────────────────────────────
# agent/ask/loop.py — citation grounding: cited_cycle_ids is cross-checked against
# cycle_id values a run_sql result actually returned this turn, not trusted
# from the model verbatim (PR #94 review finding)
# ──────────────────────────────────────────────────────────────────────────────


def test_ask_accepts_grounded_citation_without_retry() -> None:
    sql = "SELECT cycle_id FROM journal_records"
    responses = [
        _mock_response(
            "tool_use", [_tool_use_block(TOOL_RUN_SQL, {"sql": sql}, block_id="tu_1")]
        ),
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _VALID_ANSWER_INPUT)],
        ),
    ]
    result, mock_client = _patched_ask(responses)
    assert result.cited_cycle_ids == ["c1"]
    # No citation retry needed — exactly 3 calls (2 explore + 1 commit).
    assert mock_client.messages.create.call_count == 3


def test_ask_retries_ungrounded_citation_then_succeeds() -> None:
    sql = "SELECT cycle_id FROM journal_records"
    responses = [
        _mock_response(
            "tool_use", [_tool_use_block(TOOL_RUN_SQL, {"sql": sql}, block_id="tu_1")]
        ),
        _mock_response("end_turn", [_text_block()]),
        # First commit attempt cites a real id plus a fabricated one.
        _mock_response(
            "tool_use",
            [
                _tool_use_block(
                    TOOL_SUBMIT_ASK_ANSWER,
                    {
                        "answer_text": "1 cycle opened a position.",
                        "cited_cycle_ids": ["c1", "c-fabricated"],
                    },
                    block_id="tu_answer_1",
                )
            ],
        ),
        # Second attempt drops the fabricated id after feedback.
        _mock_response(
            "tool_use",
            [
                _tool_use_block(
                    TOOL_SUBMIT_ASK_ANSWER,
                    {
                        "answer_text": "1 cycle opened a position.",
                        "cited_cycle_ids": ["c1"],
                    },
                    block_id="tu_answer_2",
                )
            ],
        ),
    ]
    result, mock_client = _patched_ask(responses, max_citation_retries=1)
    assert result.cited_cycle_ids == ["c1"]
    assert mock_client.messages.create.call_count == 4

    # The feedback fed back to the model must name the fabricated id.
    retry_messages = mock_client.messages.create.call_args_list[3].kwargs["messages"]
    feedback_content = retry_messages[-1]["content"][0]["content"]
    assert "c-fabricated" in feedback_content
    assert retry_messages[-1]["content"][0]["is_error"] is True


def test_ask_drops_ungrounded_citation_after_retries_exhausted() -> None:
    # No run_sql call at all this turn — nothing is grounded — and the model
    # never corrects its citation even after feedback.
    bad_answer = _mock_response(
        "tool_use",
        [
            _tool_use_block(
                TOOL_SUBMIT_ASK_ANSWER,
                {
                    "answer_text": "1 cycle opened a position.",
                    "cited_cycle_ids": ["c-fabricated"],
                },
            )
        ],
    )
    responses = [
        _mock_response("end_turn", [_text_block()]),
        bad_answer,
        bad_answer,
    ]
    result, mock_client = _patched_ask(responses, max_citation_retries=1)
    # Ungrounded citation dropped, not raised — the prose answer still ships.
    assert result.answer_text == "1 cycle opened a position."
    assert result.cited_cycle_ids == []
    assert mock_client.messages.create.call_count == 3


# ──────────────────────────────────────────────────────────────────────────────
# WP-9.9: ask_stream() — event sequence for the /api/ask SSE conversion
# ──────────────────────────────────────────────────────────────────────────────


def test_ask_stream_yields_query_started_then_result_then_answer() -> None:
    sql = "SELECT cycle_id FROM journal_records"
    responses = [
        _mock_response(
            "tool_use", [_tool_use_block(TOOL_RUN_SQL, {"sql": sql}, block_id="tu_1")]
        ),
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _VALID_ANSWER_INPUT)],
        ),
    ]
    events, _ = _patched_ask_stream(responses)

    assert [type(e) for e in events] == [QueryStarted, QueryResult, Answer]
    started, result, answer = events
    assert isinstance(started, QueryStarted)
    assert started.sql == sql
    assert isinstance(result, QueryResult)
    assert result.columns == ["cycle_id"]
    assert isinstance(answer, Answer)
    assert answer.answer_text == _VALID_ANSWER_INPUT["answer_text"]
    assert answer.executed_sql == [sql]
    assert answer.cited_cycle_ids == ["c1"]


def test_ask_stream_yields_query_error_on_guardrail_rejection() -> None:
    responses = [
        _mock_response(
            "tool_use",
            [
                _tool_use_block(
                    TOOL_RUN_SQL,
                    {"sql": "DELETE FROM journal_records"},
                    block_id="tu_1",
                )
            ],
        ),
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    events, _ = _patched_ask_stream(responses)

    assert [type(e) for e in events] == [QueryStarted, QueryError, Answer]
    error_event = events[1]
    assert isinstance(error_event, QueryError)
    assert error_event.sql == "DELETE FROM journal_records"
    assert "SELECT" in error_event.error
    # A rejected query never advances executed_sql on the final Answer.
    answer = events[2]
    assert isinstance(answer, Answer)
    assert answer.executed_sql == []


def test_ask_stream_ends_with_exactly_one_answer_event() -> None:
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    events, _ = _patched_ask_stream(responses)
    assert len(events) == 1
    assert isinstance(events[0], Answer)


def test_ask_drains_ask_stream_to_same_result_as_direct_call() -> None:
    # ask() must remain behaviorally identical after the ask_stream() split —
    # this is the "existing callers/tests are unaffected" guarantee.
    sql = "SELECT cycle_id FROM journal_records"
    responses = [
        _mock_response(
            "tool_use", [_tool_use_block(TOOL_RUN_SQL, {"sql": sql}, block_id="tu_1")]
        ),
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _VALID_ANSWER_INPUT)],
        ),
    ]
    result, _ = _patched_ask(responses)
    assert result.answer_text == _VALID_ANSWER_INPUT["answer_text"]
    assert result.executed_sql == [sql]
    assert result.cited_cycle_ids == ["c1"]


# ──────────────────────────────────────────────────────────────────────────────
# WP-9.9: multi-turn history — prepended as plain user/assistant text turns
# ──────────────────────────────────────────────────────────────────────────────


def test_ask_history_prepended_as_plain_text_messages() -> None:
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    history = [
        HistoryTurn(question="How many SPY trades?", answer_text="3 SPY trades."),
    ]
    with patch("options_agent.agent.ask.loop.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        MockCls.return_value = mock_client
        engine = _seeded_engine()
        with engine.connect() as c:
            ask("And how many were profitable?", c, history=history)

    first_call_messages = mock_client.messages.create.call_args_list[0].kwargs[
        "messages"
    ]
    assert first_call_messages[0] == {
        "role": "user",
        "content": "How many SPY trades?",
    }
    assert first_call_messages[1] == {
        "role": "assistant",
        "content": "3 SPY trades.",
    }
    assert first_call_messages[2] == {
        "role": "user",
        "content": "And how many were profitable?",
    }


def test_ask_with_no_history_matches_prior_single_message_behavior() -> None:
    # Regression guard: omitting history must produce exactly the same
    # messages[0] shape ask() always sent pre-WP-9.9.
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_ASK_ANSWER, _NO_CITATION_ANSWER_INPUT)],
        ),
    ]
    result, mock_client = _patched_ask(responses)
    # index 0: mock.call_args stores a reference to the *live* messages list
    # (see the analogous comment on second_call_messages above), so only the
    # first entry — set before any mutation — is meaningful to assert on.
    first_call_messages = mock_client.messages.create.call_args_list[0].kwargs[
        "messages"
    ]
    assert first_call_messages[0] == {
        "role": "user",
        "content": "How many cycles opened positions?",
    }
    assert result.answer_text == _NO_CITATION_ANSWER_INPUT["answer_text"]
