"""WP-6.4: Core reasoning function — the only LLM call in the system.

Design (see Trello WP-6.4 for full rationale):

  Two-phase agentic loop
  ──────────────────────
  Phase 1 — Exploration: tool_choice="auto" with AGENT_TOOLS (read-only).
    The agent calls data tools (get_filtered_chain, get_events, etc.) as
    needed. Loops until stop_reason != "tool_use" or max_turns is hit.
    submit_trade_proposal is NOT available here — the agent cannot pre-empt
    the commit phase.

  Phase 2 — Commit: tool_choice={"type":"tool","name":"submit_trade_proposal"}.
    Forces a TradeProposal-shaped structured response. Forcing on turn 1
    would prevent the agent from drilling into chains; the two-phase approach
    is required to preserve that capability.

  Retry on schema-invalid output
  ───────────────────────────────
  Up to max_schema_retries additional commit attempts (default 2, total 3).
  Each retry feeds the exact Pydantic validation error back as a tool_result
  error so the model knows what to fix. API errors (rate limits, timeouts)
  do not consume schema-retry budget and are not caught here — let them
  propagate; run_entry_cycle owns the API-error handling policy.

  Failure path
  ────────────
  Raises ReasonerError after retries exhausted. run_entry_cycle catches this
  and converts to CycleError(stage=CycleStage.REASON). reason()'s return type
  is strictly -> TradeProposal to keep the happy-path signature clean.

  Transcript
  ──────────
  Exploration-phase tool exchanges are recorded in
  ContextSnapshot.tool_calls_transcript (WP-6.4 additive amendment to WP-0.4).
  The transcript is stamped onto the caller-provided context snapshot on
  success; callers should save the snapshot to the journal after reason()
  returns.
"""

import json
import logging
import time
from collections.abc import Callable
from typing import Any, cast

import anthropic
from pydantic import ValidationError

from options_agent.agent.prompts import build_system_prompt
from options_agent.agent.schema import SUBMIT_TRADE_PROPOSAL, TOOL_SUBMIT_TRADE_PROPOSAL
from options_agent.agent.tools import AGENT_TOOL_NAMES, AGENT_TOOLS
from options_agent.config import PlaybookConfig
from options_agent.contracts.proposal import TradeProposal
from options_agent.contracts.state import ContextSnapshot, ToolCallRecord
from options_agent.risk.limits import Limits

log = logging.getLogger(__name__)

# Sonnet 4.6 list pricing — used for the per-call cost estimate in log output.
# Update if model or pricing tier changes.
_INPUT_COST_PER_TOKEN: float = 3e-6  # $3.00 / 1M input tokens
_OUTPUT_COST_PER_TOKEN: float = 15e-6  # $15.00 / 1M output tokens

# Type alias for a tool implementation callable.
# Mirrors tools_mock.ToolImpl — kept local so reasoner.py has no import
# dependency on the mock module (which must never reach production).
ToolImpl = Callable[[dict[str, Any]], Any]


class ReasonerError(Exception):
    """Schema-validation retries exhausted without a valid TradeProposal.

    Raised by reason() when the model cannot produce a valid TradeProposal
    after max_schema_retries + 1 attempts. Callers (run_entry_cycle) must
    catch this and convert to CycleError(stage=CycleStage.REASON).

    last_validation_error carries the final Pydantic ValidationError for
    structured logging — use it to distinguish genuine schema failures from
    intermittent model confusion.
    """

    def __init__(
        self,
        message: str,
        *,
        last_validation_error: ValidationError | None = None,
    ) -> None:
        super().__init__(message)
        self.last_validation_error = last_validation_error


def _fmt_input(tool_input: dict[str, Any], max_len: int = 60) -> str:
    """Format tool input dict as a short string for log lines."""
    s = json.dumps(tool_input, default=str)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _serialize_tool_result(result: Any) -> str:
    """Serialize a tool return value to a JSON string for the messages list."""
    if result is None:
        return "null"
    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(mode="json"))
    if isinstance(result, list):
        items = [
            r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in result
        ]
        return json.dumps(items)
    if isinstance(result, dict):
        out: dict[str, Any] = {}
        for k, v in result.items():
            out[k] = v.model_dump(mode="json") if hasattr(v, "model_dump") else v
        return json.dumps(out)
    return json.dumps(result)


def reason(
    context: ContextSnapshot,
    tool_impls: dict[str, ToolImpl],
    *,
    playbook: PlaybookConfig,
    limits: Limits,
    model_id: str = "claude-sonnet-4-6",
    max_schema_retries: int = 2,
    max_turns: int = 10,
    max_tokens: int = 2048,
    max_tokens_explore: int = 512,
) -> TradeProposal:
    """Run the agent reasoning loop and return a validated TradeProposal.

    Two-phase agentic loop — see module docstring for full design rationale.

    Args:
        context:             Assembled context bundle from context/assembler.py.
                             tool_calls_transcript is stamped onto this object
                             before returning so the caller can save it to the
                             journal with the full exploration transcript.
        tool_impls:          Map of tool_name -> callable. Production callers
                             inject real WP-3 implementations; tests inject mocks
                             from agent/tools_mock.py. Never defaults to mocks.
        playbook:            Strategy playbook — passed to build_system_prompt().
        limits:              Risk limits — passed to build_system_prompt().
        model_id:            Anthropic model identifier. Defaults match Config.
        max_schema_retries:  Additional commit attempts after first failure.
                             Total attempts = max_schema_retries + 1.
        max_turns:           Exploration phase turn cap. When hit, proceeds to
                             commit with whatever context the agent has gathered.
        max_tokens:          Output token cap for commit API calls. Observed
                             commit responses run ~1300-1500 tokens when the
                             model produces verbose thesis/iv_rationale/
                             catalyst_check fields; 2048 provides safe headroom.
                             Raise only if commit responses are being truncated.
        max_tokens_explore:  Output token cap for exploration turns. Each turn
                             only needs tool-call JSON (~30-50 tokens per call)
                             plus brief bridging text — 512 tokens is ample.
                             Capping here prevents the model from generating
                             multi-hundred-token reasoning text on the final
                             exploration turn (discarded once commit runs).

    Returns:
        A pyright-clean TradeProposal on success (including action=NO_ACTION).

    Raises:
        ReasonerError: Schema validation failed after all retries, or the model
                       called an unknown tool, or a required impl was missing.
                       API errors from the Anthropic SDK are NOT caught here.
                       Exceptions raised by tool_impl callables also propagate
                       uncaught — run_entry_cycle owns the error handling policy.
    """
    client = anthropic.Anthropic()
    system_prompt = build_system_prompt(playbook=playbook, limits=limits)

    # Seed the conversation with the assembled context bundle.
    messages: list[Any] = [
        {
            "role": "user",
            "content": (
                "Here is the assembled market context for this reasoning cycle:\n\n"
                + json.dumps(context.assembled_context, indent=2, default=str)
                + "\n\nThe context above already contains portfolio state, universe "
                "snapshot, events calendar, and recent journal history — do not "
                "re-fetch any of those. Use the read-only tools only for targeted "
                "drill-ins not available above (e.g. get_filtered_chain to inspect "
                "specific strikes and expiries before committing to a structure). "
                "Then call submit_trade_proposal with your final decision."
            ),
        }
    ]

    tool_calls_transcript: list[ToolCallRecord] = []
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    _reason_t0 = time.monotonic()
    log.info(
        "reason() starting — model=%s max_turns=%d "
        "max_tokens_explore=%d max_tokens_commit=%d",
        model_id,
        max_turns,
        max_tokens_explore,
        max_tokens,
    )

    # ── Phase 1: Exploration ──────────────────────────────────────────────────
    for _turn in range(max_turns):
        log.info("  exploration turn %d — waiting for model response", _turn + 1)
        _t0 = time.monotonic()
        response = client.messages.create(
            model=model_id,
            max_tokens=max_tokens_explore,
            system=system_prompt,
            tools=AGENT_TOOLS,  # type: ignore[arg-type]
            tool_choice={"type": "auto"},
            messages=messages,  # type: ignore[arg-type]
        )
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens
        log.info(
            "  exploration turn %d response — %.1fs (%d in, %d out tokens)",
            _turn + 1,
            time.monotonic() - _t0,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # Model stopped calling tools — exploration complete.
            log.info(
                "  exploration complete after %d turn(s) — %d tool call(s) recorded",
                _turn + 1,
                len(tool_calls_transcript),
            )
            break

        # Dispatch all tool calls in this response and collect results.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name not in AGENT_TOOL_NAMES:
                raise ReasonerError(
                    f"Model called unknown tool {block.name!r} during exploration. "
                    "This indicates a tool definition or system prompt bug — "
                    "the agent must only call tools from AGENT_TOOLS."
                )

            impl = tool_impls.get(block.name)
            if impl is None:
                raise ReasonerError(
                    f"No implementation provided for tool {block.name!r}. "
                    "Pass a complete tool_impls map to reason(); "
                    "never rely on a default fallback to mocks."
                )

            tool_input = cast(dict[str, Any], block.input)
            log.info("    → tool_use: %s(%s)", block.name, _fmt_input(tool_input))
            result = impl(tool_input)
            result_json = _serialize_tool_result(result)

            tool_calls_transcript.append(
                ToolCallRecord(
                    tool_name=block.name,
                    tool_input=tool_input,
                    result_json=result_json,
                )
            )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_json,
                }
            )

        messages.append({"role": "user", "content": tool_results})
    else:
        # for/else: loop completed without a break — turn cap was hit while the
        # model was still calling tools. The last message is a user (tool_results)
        # message; the model hasn't responded yet. The commit phase will force
        # the next turn directly, which is valid in the Anthropic messages API.
        log.warning(
            "Exploration phase reached max_turns=%d cap; proceeding to commit "
            "with %d tool calls recorded.",
            max_turns,
            len(tool_calls_transcript),
        )

    # ── Phase 2: Commit — force the structured proposal ───────────────────────
    # If exploration ended naturally (last message = assistant), the model needs
    # a user turn to prompt the commit. If the turn cap fired (last message =
    # user tool_results), the model is already waiting to respond — no extra
    # user message is needed; the forced tool_choice handles the next turn.
    commit_messages: list[Any] = list(messages)
    if commit_messages[-1]["role"] == "assistant":
        commit_messages.append(
            {
                "role": "user",
                "content": (
                    "Now call submit_trade_proposal with your final"
                    " decision for this cycle."
                ),
            }
        )

    for attempt in range(max_schema_retries + 1):
        log.info(
            "  commit attempt %d/%d — waiting for model response",
            attempt + 1,
            max_schema_retries + 1,
        )
        _t0 = time.monotonic()
        commit_response = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=[SUBMIT_TRADE_PROPOSAL],  # type: ignore[arg-type]
            tool_choice={"type": "tool", "name": TOOL_SUBMIT_TRADE_PROPOSAL},
            messages=commit_messages,  # type: ignore[arg-type]
        )

        total_input_tokens += commit_response.usage.input_tokens
        total_output_tokens += commit_response.usage.output_tokens
        log.info(
            "  commit attempt %d/%d response — %.1fs (%d in, %d out tokens)",
            attempt + 1,
            max_schema_retries + 1,
            time.monotonic() - _t0,
            commit_response.usage.input_tokens,
            commit_response.usage.output_tokens,
        )
        proposal_block = next(
            (b for b in commit_response.content if b.type == "tool_use"),
            None,
        )
        if proposal_block is None:
            raise ReasonerError(
                f"Commit attempt {attempt + 1}: forced tool_choice produced no "
                "tool_use block. This is an Anthropic SDK contract violation."
            )

        try:
            proposal = TradeProposal.model_validate(
                cast(dict[str, Any], proposal_block.input)
            )
        except ValidationError as exc:
            if attempt == max_schema_retries:
                raise ReasonerError(
                    f"Schema validation failed after"
                    f" {max_schema_retries + 1} attempt(s). Last error: {exc}",
                    last_validation_error=exc,
                ) from exc

            error_detail = str(exc)
            log.warning(
                "Schema retry %d/%d: validation failed — %s",
                attempt + 1,
                max_schema_retries,
                error_detail,
            )

            # Feed the exact validation error back via a tool_result error so
            # the model sees what to fix, not just "your output was invalid".
            commit_messages = commit_messages + [
                {"role": "assistant", "content": commit_response.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": proposal_block.id,
                            "is_error": True,
                            "content": (
                                "Validation failed. Fix these errors and call "
                                f"submit_trade_proposal again:\n\n{error_detail}"
                            ),
                        }
                    ],
                },
            ]
            continue

        # Success — stamp the exploration transcript onto the context snapshot
        # so the caller can persist it with the journal record.
        est_cost = (
            total_input_tokens * _INPUT_COST_PER_TOKEN
            + total_output_tokens * _OUTPUT_COST_PER_TOKEN
        )
        log.info(
            "reason() done — %d in + %d out tokens, est. $%.4f (Sonnet 4.6)",
            total_input_tokens,
            total_output_tokens,
            est_cost,
        )
        log.info(
            "  commit success — action=%s strategy=%r underlying=%s (total %.1fs)",
            proposal.action,
            proposal.strategy,
            proposal.underlying,
            time.monotonic() - _reason_t0,
        )
        context.tool_calls_transcript = tool_calls_transcript
        return proposal

    # Unreachable: the retry loop raises ReasonerError on exhaustion above.
    raise ReasonerError(  # pragma: no cover
        "reason() exited the retry loop without returning or raising"
    )
