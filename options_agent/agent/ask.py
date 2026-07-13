"""WP-9.8: Ask-the-journal analyst — the second (read-only) LLM call in the
system, alongside agent/reasoner.py's trade reasoner.

Single-tool agentic loop over run_sql, followed by a forced commit call to
submit_ask_answer — the same two-phase shape as reasoner.py's reason(),
simplified because there is no deterministic-validation feedback loop here
(an SQL answer has no equivalent of a TradeProposal risk check).

executed_sql on the returned AskResult is derived from the actual run_sql
tool-call transcript, not self-reported by the model — see ask_schema.py's
module docstring for why.
"""

from __future__ import annotations

import json
import logging
import time
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
    AGENT_ASK_TOOLS,
    TOOL_RUN_SQL,
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


def ask(
    question: str,
    conn: Connection,
    *,
    model_id: str = "claude-sonnet-4-6",
    max_turns: int = 5,
    max_tokens: int = 1024,
    max_tokens_explore: int = 1024,
    result_token_budget: int = 8000,
    row_cap: int = DEFAULT_ROW_CAP,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    max_schema_retries: int = 2,
) -> AskResult:
    """Answer one natural-language question over the journal via run_sql.

    conn must be opened read-only (state.db.build_engine(url, read_only=True))
    — this function does not itself enforce that, it trusts its caller, same
    contract as ui/app.py's engine and agent/sql_guard.py's execute_guarded_select.

    Args:
        question:            The operator's natural-language question.
        conn:                Read-only SQLAlchemy connection the run_sql
                              tool executes against.
        model_id:             Anthropic model identifier.
        max_turns:            Exploration-phase turn cap (run_sql calls).
                              When hit, proceeds to commit with whatever
                              evidence has been gathered.
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

    Returns:
        AskResult with the model's prose answer, the SQL actually executed
        (ground truth, not self-reported), and the cited cycle_ids.

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
    messages: list[Any] = [{"role": "user", "content": question}]
    executed_sql: list[str] = []

    _t0 = time.monotonic()
    log.info(
        "ask() starting — model=%s max_turns=%d question=%r",
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
            tools=AGENT_ASK_TOOLS,  # type: ignore[arg-type]
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
            try:
                result = execute_guarded_select(
                    conn, sql, row_cap=row_cap, timeout_secs=timeout_secs
                )
            except SqlGuardError as exc:
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
    while True:
        log.info(
            "  commit attempt (schema %d/%d) — waiting for model response",
            schema_attempts,
            max_schema_retries,
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

        log.info(
            "ask() done — %d query call(s), %d cited cycle(s), %.1fs",
            len(executed_sql),
            len(answer.cited_cycle_ids),
            time.monotonic() - _t0,
        )
        return AskResult(
            answer_text=answer.answer_text,
            executed_sql=executed_sql,
            cited_cycle_ids=answer.cited_cycle_ids,
        )
