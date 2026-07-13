"""WP-9.8: Ask-the-journal analyst — the second (read-only) LLM call in the
system, alongside agent/reasoner.py's trade reasoner.

Single-tool agentic loop over run_sql, followed by a forced commit call to
submit_ask_answer — the same two-phase shape as reasoner.py's reason(),
simplified because there is no deterministic-validation feedback loop here
(an SQL answer has no equivalent of a TradeProposal risk check).

Both executed_sql and cited_cycle_ids on the returned AskResult are grounded
server-side, not self-reported:
  - executed_sql is built from the actual run_sql tool-call transcript (see
    ask_schema.py's module docstring for why).
  - cited_cycle_ids is cross-checked against the cycle_id values that
    actually appeared in some run_sql result this turn (seen_cycle_ids
    below). A citation the model can't ground gets one retry with feedback,
    then is silently dropped rather than returned as an unverifiable link —
    WP-9.9's Decision-explorer citation links must never be able to 404 on a
    fabricated cycle_id (code-review finding, WP-9.8 PR #94).

WP-9.9: the exploration loop below is a generator (ask_stream()) yielding
QueryStarted/QueryResult/QueryError as each run_sql call happens, then a
final Answer — this is what ui/ask.py wraps into the /api/ask SSE response.
ask() itself is now a thin wrapper draining ask_stream() to the same
AskResult it always returned, so every existing caller/test is unaffected.
The retry budgets (max_turns, max_schema_retries, max_citation_retries)
are the only "give up" mechanism — there is no separate error-count cap;
a run_sql failure is just fed back to the model as one more turn against
the same max_turns budget it already has (WP-9.9 decision, 2026-07-13).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, cast

import anthropic
from pydantic import ValidationError
from sqlalchemy.engine import Connection

from options_agent.agent.ask_prompts import build_ask_system_prompt
from options_agent.agent.ask_schema import (
    SUBMIT_ASK_ANSWER,
    TOOL_SUBMIT_ASK_ANSWER,
    AskAnswer,
)
from options_agent.agent.ask_tool import (
    AGENT_ASK_TOOL_NAMES,
    TOOL_RUN_SQL,
    build_run_sql_tool,
)
from options_agent.agent.sql_guard import (
    DEFAULT_ROW_CAP,
    DEFAULT_TIMEOUT_SECS,
    GuardedQueryResult,
    SqlGuardError,
    execute_guarded_select,
)

log = logging.getLogger(__name__)

# Rough chars-per-token heuristic for capping a single run_sql result fed
# back into the conversation — journal_records/context_snapshot JSON blobs
# can be large; without this, one query could consume the whole per-call
# token budget. ~4 chars/token is the standard English-text approximation;
# erring conservative (undercounting tokens) is safe for a cap.
_CHARS_PER_TOKEN_ESTIMATE = 4


@dataclass
class HistoryTurn:
    """One prior (question, answer) exchange, client-resent for multi-turn
    conversations. Prepended to the new question as plain user/assistant
    text messages — not a tool-call transcript replay (WP-9.9 decision,
    2026-07-13): the model's own prose answer is a cheap, sufficient summary
    of what it found, and re-running full tool transcripts through the
    result-token budget for every follow-up would multiply cost for turns
    that don't need the underlying rows again.
    """

    question: str
    answer_text: str


@dataclass
class QueryStarted:
    """A run_sql call the model made, before its result is known."""

    sql: str


@dataclass
class QueryResult:
    """A run_sql call that executed successfully."""

    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    row_cap: int


@dataclass
class QueryError:
    """A run_sql call rejected by the guardrail or failed/timed out.

    Always emitted even though the loop then feeds the same error back to
    the model as a tool_result and lets it retry — the point is that a UI
    consumer sees every failure, never just the eventually-successful retry
    (card requirement: "never silently retried away").
    """

    sql: str
    error: str


@dataclass
class Answer:
    """The final, grounded answer — the last event ask_stream() yields."""

    answer_text: str
    executed_sql: list[str]
    cited_cycle_ids: list[str]


AskEvent = QueryStarted | QueryResult | QueryError | Answer


@dataclass
class AskResult:
    answer_text: str
    executed_sql: list[str]
    cited_cycle_ids: list[str]


class AskError(Exception):
    """The model called an unknown tool, or schema validation of the final
    answer failed after all retries.

    API errors from the Anthropic SDK are NOT caught here — same policy as
    agent/reasoner.py's ReasonerError; let them propagate to the caller.
    """


def _fmt_sql(sql: str, max_len: int = 80) -> str:
    s = " ".join(sql.split())
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _serialize_run_sql_result(result: GuardedQueryResult, *, max_tokens: int) -> str:
    payload = json.dumps(
        {
            "columns": result.columns,
            "rows": result.rows,
            "truncated": result.truncated,
            "row_cap": result.row_cap,
        },
        default=str,
    )
    max_chars = max_tokens * _CHARS_PER_TOKEN_ESTIMATE
    if len(payload) <= max_chars:
        return payload
    return (
        payload[:max_chars] + "…[cut off: result exceeded the "
        f"{max_tokens}-token budget for query results — narrow the query"
        " (fewer columns, tighter WHERE, add aggregation) rather than"
        " relying on this truncated data]"
    )


def _build_initial_messages(
    question: str, history: Sequence[HistoryTurn] | None
) -> list[Any]:
    """Prepend prior (question, answer_text) pairs as plain user/assistant
    text turns before the new question — see HistoryTurn's docstring for why
    this is plain text rather than a tool-call transcript replay.
    """
    messages: list[Any] = []
    for turn in history or ():
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer_text})
    messages.append({"role": "user", "content": question})
    return messages


def ask_stream(
    question: str,
    conn: Connection,
    *,
    history: Sequence[HistoryTurn] | None = None,
    model_id: str = "claude-sonnet-4-6",
    max_turns: int = 5,
    max_tokens: int = 1024,
    max_tokens_explore: int = 1024,
    result_token_budget: int = 8000,
    row_cap: int = DEFAULT_ROW_CAP,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    max_schema_retries: int = 2,
    max_citation_retries: int = 1,
) -> Iterator[AskEvent]:
    """Answer one natural-language question over the journal via run_sql,
    yielding progress events as they happen.

    conn must be opened read-only (state.db.build_engine(url, read_only=True))
    — this function does not itself enforce that, it trusts its caller, same
    contract as ui/app.py's engine and agent/sql_guard.py's execute_guarded_select.

    Args:
        question:            The operator's natural-language question.
        conn:                Read-only SQLAlchemy connection the run_sql
                              tool executes against.
        history:              Prior (question, answer_text) exchanges in
                              this conversation, oldest first — see
                              HistoryTurn's docstring.
        model_id:             Anthropic model identifier.
        max_turns:            Exploration-phase turn cap (run_sql calls).
                              When hit, proceeds to commit with whatever
                              evidence has been gathered. Also the de facto
                              cap on how many times a failed run_sql call
                              can be retried — there is no separate
                              consecutive-error budget (WP-9.9 decision).
        max_tokens:           Output token cap for the commit API call.
        max_tokens_explore:   Output token cap for exploration turns.
        result_token_budget:  Max tokens' worth of a single run_sql result
                              fed back into the conversation (see
                              _serialize_run_sql_result).
        row_cap:              Forwarded to execute_guarded_select.
        timeout_secs:         Forwarded to execute_guarded_select.
        max_schema_retries:   Additional commit attempts after a schema-
                              invalid submit_ask_answer call. Total attempts
                              = max_schema_retries + 1.
        max_citation_retries: Attempts to get the model to correct
                              cited_cycle_ids that reference a cycle_id
                              never returned by any run_sql result this
                              turn, before giving up and dropping just the
                              ungrounded ids from the returned result.

    Yields:
        QueryStarted/QueryResult/QueryError for each run_sql call, in order,
        followed by exactly one final Answer event.

    Raises:
        AskError: the model called an unknown tool, or schema validation
                  failed after all retries.
    """
    client = anthropic.Anthropic()
    system_blocks: list[Any] = [
        {
            "type": "text",
            "text": build_ask_system_prompt(row_cap=row_cap, timeout_secs=timeout_secs),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages: list[Any] = _build_initial_messages(question, history)
    executed_sql: list[str] = []
    seen_cycle_ids: set[str] = set()
    # Built once with the row_cap/timeout_secs this call actually enforces —
    # never the module-level defaults — so the tool description the model
    # reads can't drift from what execute_guarded_select() really does.
    ask_tools = [build_run_sql_tool(row_cap=row_cap, timeout_secs=timeout_secs)]

    _t0 = time.monotonic()
    log.info(
        "ask_stream() starting — model=%s max_turns=%d question=%r",
        model_id,
        max_turns,
        question,
    )

    for _turn in range(max_turns):
        log.info("  exploration turn %d — waiting for model response", _turn + 1)
        response = client.messages.create(
            model=model_id,
            max_tokens=max_tokens_explore,
            system=system_blocks,
            tools=ask_tools,  # type: ignore[arg-type]
            tool_choice={"type": "auto"},
            messages=messages,  # type: ignore[arg-type]
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            log.info(
                "  exploration complete after %d turn(s) — %d query call(s) recorded",
                _turn + 1,
                len(executed_sql),
            )
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name not in AGENT_ASK_TOOL_NAMES:
                raise AskError(
                    f"Model called unknown tool {block.name!r} — only"
                    f" {TOOL_RUN_SQL} is available during exploration."
                )

            tool_input = cast(dict[str, Any], block.input)
            sql = tool_input.get("sql", "")
            log.info("    → run_sql(%s)", _fmt_sql(sql))
            yield QueryStarted(sql=sql)
            try:
                result = execute_guarded_select(
                    conn, sql, row_cap=row_cap, timeout_secs=timeout_secs
                )
            except SqlGuardError as exc:
                yield QueryError(sql=sql, error=str(exc))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "is_error": True,
                        "content": str(exc),
                    }
                )
                continue

            executed_sql.append(sql)
            for row in result.rows:
                cycle_id = row.get("cycle_id")
                if isinstance(cycle_id, str):
                    seen_cycle_ids.add(cycle_id)
            yield QueryResult(
                sql=sql,
                columns=result.columns,
                rows=result.rows,
                truncated=result.truncated,
                row_cap=result.row_cap,
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _serialize_run_sql_result(
                        result, max_tokens=result_token_budget
                    ),
                }
            )

        messages.append({"role": "user", "content": tool_results})
    else:
        log.warning(
            "Exploration phase reached max_turns=%d cap; proceeding to"
            " commit with %d query call(s) recorded.",
            max_turns,
            len(executed_sql),
        )

    commit_messages: list[Any] = list(messages)
    if commit_messages[-1]["role"] == "assistant":
        commit_messages.append(
            {
                "role": "user",
                "content": "Now call submit_ask_answer with your final answer.",
            }
        )

    schema_attempts = 0
    citation_attempts = 0
    while True:
        log.info(
            "  commit attempt (schema %d/%d, citation %d/%d) — waiting for"
            " model response",
            schema_attempts,
            max_schema_retries,
            citation_attempts,
            max_citation_retries,
        )
        commit_response = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system_blocks,
            tools=[SUBMIT_ASK_ANSWER],  # type: ignore[arg-type]
            tool_choice={"type": "tool", "name": TOOL_SUBMIT_ASK_ANSWER},
            messages=commit_messages,  # type: ignore[arg-type]
        )
        answer_block = next(
            (b for b in commit_response.content if b.type == "tool_use"), None
        )
        if answer_block is None:
            raise AskError(
                "Commit call produced no tool_use block — Anthropic SDK"
                " contract violation."
            )

        try:
            answer = AskAnswer.model_validate(cast(dict[str, Any], answer_block.input))
        except ValidationError as exc:
            schema_attempts += 1
            if schema_attempts > max_schema_retries:
                raise AskError(
                    f"Schema validation failed after"
                    f" {max_schema_retries + 1} attempt(s). Last error: {exc}"
                ) from exc

            log.warning(
                "Schema retry %d/%d: validation failed — %s",
                schema_attempts,
                max_schema_retries,
                exc,
            )
            commit_messages = commit_messages + [
                {"role": "assistant", "content": commit_response.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": answer_block.id,
                            "is_error": True,
                            "content": (
                                "Validation failed. Fix these errors and call"
                                f" submit_ask_answer again:\n\n{exc}"
                            ),
                        }
                    ],
                },
            ]
            continue

        # Ground cited_cycle_ids against cycle_id values a run_sql result
        # actually returned this turn — the model self-reports these (unlike
        # executed_sql), so nothing prevents it from citing an id it recalls
        # but never queried. Give it one corrective round-trip; if it still
        # can't ground every id, drop just the ungrounded ones rather than
        # failing the whole answer or returning an unverifiable citation.
        ungrounded = [
            cid for cid in answer.cited_cycle_ids if cid not in seen_cycle_ids
        ]
        if ungrounded and citation_attempts < max_citation_retries:
            citation_attempts += 1
            log.warning(
                "Citation retry %d/%d: ungrounded cited_cycle_ids %s",
                citation_attempts,
                max_citation_retries,
                ungrounded,
            )
            commit_messages = commit_messages + [
                {"role": "assistant", "content": commit_response.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": answer_block.id,
                            "is_error": True,
                            "content": (
                                "cited_cycle_ids included id(s) that never"
                                f" appeared in any run_sql result you already"
                                f" ran this turn: {ungrounded}. Call"
                                " submit_ask_answer again with cited_cycle_ids"
                                " restricted to ids that appeared in a query"
                                " result, or leave it empty if the claim is"
                                " purely aggregate."
                            ),
                        }
                    ],
                },
            ]
            continue

        if ungrounded:
            log.warning(
                "Dropping %d ungrounded cited_cycle_id(s) after retries exhausted: %s",
                len(ungrounded),
                ungrounded,
            )
        cited_cycle_ids = [
            cid for cid in answer.cited_cycle_ids if cid in seen_cycle_ids
        ]

        log.info(
            "ask_stream() done — %d query call(s), %d cited cycle(s), %.1fs",
            len(executed_sql),
            len(cited_cycle_ids),
            time.monotonic() - _t0,
        )
        yield Answer(
            answer_text=answer.answer_text,
            executed_sql=executed_sql,
            cited_cycle_ids=cited_cycle_ids,
        )
        return


def ask(
    question: str,
    conn: Connection,
    *,
    history: Sequence[HistoryTurn] | None = None,
    **kwargs: Any,
) -> AskResult:
    """Drain ask_stream() to the final AskResult — for callers that don't
    need incremental progress (existing tests, any future non-streaming
    caller). See ask_stream()'s docstring for all other parameters.

    Raises:
        AskError: propagated from ask_stream() if raised before an Answer
                  event is produced.
    """
    for event in ask_stream(question, conn, history=history, **kwargs):
        if isinstance(event, Answer):
            return AskResult(
                answer_text=event.answer_text,
                executed_sql=event.executed_sql,
                cited_cycle_ids=event.cited_cycle_ids,
            )
    raise AskError("ask_stream() ended without producing an Answer event.")
