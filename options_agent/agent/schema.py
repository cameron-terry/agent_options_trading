"""WP-6.4: Forced-commit tool for structured output enforcement.

submit_trade_proposal is NOT a read-only data tool. It is the mechanism by
which the agent commits a TradeProposal at the end of the reasoning loop.
It must NOT appear in AGENT_TOOLS; it is passed only on the final forced-commit
call, never during the exploration phase.

The input_schema is derived from TradeProposal.model_json_schema() so the JSON
Schema and Pydantic model share a single source of truth. Schema drift between
the two is the class of bug this approach prevents — never hand-write the fields
here or maintain a parallel definition elsewhere.
"""

from typing import Any

from anthropic.types import ToolParam

from options_agent.contracts.proposal import TradeProposal

TOOL_SUBMIT_TRADE_PROPOSAL = "submit_trade_proposal"


def _build_input_schema() -> dict[str, Any]:
    schema: dict[str, Any] = TradeProposal.model_json_schema()
    schema.pop("title", None)
    return schema


SUBMIT_TRADE_PROPOSAL: ToolParam = {
    "name": TOOL_SUBMIT_TRADE_PROPOSAL,
    "description": (
        "Submit your final TradeProposal for this reasoning cycle. Call this "
        "exactly once, after you have gathered sufficient context via the "
        "read-only tools. action=NO_ACTION is a valid and expected response when "
        "you determine no trade is warranted this cycle — the system journals all "
        "outcomes including NO_ACTION. Do NOT produce a bare text response; this "
        "tool call is the only accepted output format."
    ),
    "input_schema": _build_input_schema(),  # type: ignore[typeddict-item]
}
