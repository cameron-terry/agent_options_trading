"""WP-9.8: Forced-commit tool for the ask-the-journal analyst's answer.

Mirrors agent/schema.py's SUBMIT_TRADE_PROPOSAL pattern: input_schema is
derived from AskAnswer.model_json_schema() so the tool definition and the
Pydantic model share one source of truth — never hand-write the fields here.

executed_sql is deliberately NOT part of this schema. It is derived
server-side (agent/ask.py) from the actual run_sql tool-call transcript, not
self-reported by the model, so an answer can never cite a query that wasn't
really run — the same "show your work" discipline as the trading agent's
tool_calls_transcript.
"""

from typing import Any

from anthropic.types import ToolParam
from pydantic import BaseModel, Field

TOOL_SUBMIT_ASK_ANSWER = "submit_ask_answer"


class AskAnswer(BaseModel):
    """The analyst's final answer to one ask-the-journal question."""

    answer_text: str = Field(
        description=(
            "The prose answer to the operator's question, including any"
            " caveats (e.g. small sample sizes, warm-up period null iv_rank,"
            " still-open positions excluded from a hit rate)."
        )
    )
    cited_cycle_ids: list[str] = Field(
        default_factory=list,
        description=(
            "cycle_id values (from journal_records) that this answer's"
            " factual claims are drawn from. Empty only when the answer is a"
            " purely aggregate/statistical claim not attributable to"
            " specific cycles."
        ),
    )


def _build_input_schema() -> dict[str, Any]:
    schema: dict[str, Any] = AskAnswer.model_json_schema()
    schema.pop("title", None)
    return schema


SUBMIT_ASK_ANSWER: ToolParam = {
    "name": TOOL_SUBMIT_ASK_ANSWER,
    "description": (
        "Submit your final answer to the operator's question. Call this"
        " exactly once, after you have gathered sufficient evidence via"
        " run_sql. Every factual claim must be traceable to a query you"
        " actually ran — cite the specific cycle_ids that support the"
        " answer in cited_cycle_ids. Do NOT produce a bare text response;"
        " this tool call is the only accepted output format."
    ),
    "input_schema": _build_input_schema(),  # type: ignore[typeddict-item]
}
