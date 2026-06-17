"""Tests for WP-6.4: structured-output enforcement + retry.

Covers:
  1. agent/schema.py — SUBMIT_TRADE_PROPOSAL tool definition correctness.
  2. agent/reasoner.py — reason() two-phase loop, retry logic, error paths.

All Anthropic SDK calls are mocked. No live API calls are made.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from options_agent.agent.reasoner import ReasonerError, ToolImpl, reason
from options_agent.agent.schema import (
    SUBMIT_TRADE_PROPOSAL,
    TOOL_SUBMIT_TRADE_PROPOSAL,
    _build_input_schema,
)
from options_agent.agent.tools import AGENT_TOOL_NAMES
from options_agent.config import Config, PlaybookConfig
from options_agent.contracts.proposal import TradeProposal
from options_agent.contracts.state import ContextSnapshot, ToolCallRecord
from options_agent.risk.limits import Limits

# ──────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────────────

_VALID_PROPOSAL_INPUT: dict[str, Any] = {
    "action": "OPEN",
    "underlying": "SPY",
    "strategy": "bull_put_spread",
    "legs": [
        {
            "right": "put",
            "side": "sell",
            "strike": 530.0,
            "expiration": "2026-09-19",
            "ratio": 1,
        },
        {
            "right": "put",
            "side": "buy",
            "strike": 525.0,
            "expiration": "2026-09-19",
            "ratio": 1,
        },
    ],
    "thesis": "SPY near key support with bullish macro backdrop.",
    "iv_rationale": "IV rank 62 — elevated but not extreme; credit suits this regime.",
    "catalyst_check": "No earnings for SPY. FOMC in 7 days, outside blackout window.",
    "conviction": 0.65,
    "est_max_loss": 500.0,
    "est_max_profit": 270.0,
    "breakevens": [527.30],
    "net_delta": 0.13,
    "net_theta": 9.0,
    "net_vega": -0.38,
    "exit_plan": {
        "profit_target_pct": 0.50,
        "stop_loss_mult": 2.0,
        "time_stop_dte": 21,
    },
    "informed_by": [],
}


def _make_context() -> ContextSnapshot:
    return ContextSnapshot(
        assembled_context={"cycle": "test", "universe": ["SPY"]},
        context_hash="abc123def456",
        model_id="claude-sonnet-4-6",
        prompt_version="1.0.0",
        assembled_at=datetime(2026, 6, 15, 14, 30, 0, tzinfo=UTC),
    )


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


def _text_block(text: str = "Analyzing...") -> MagicMock:
    return _mock_block("text", text=text)


_TSUB = TOOL_SUBMIT_TRADE_PROPOSAL  # short alias used in response lists
_VP = _VALID_PROPOSAL_INPUT  # short alias for valid proposal dict


def _mock_response(stop_reason: str, content: list[Any]) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content
    return resp


def _patched_reason(
    mock_responses: list[MagicMock],
    context: ContextSnapshot | None = None,
    tool_impls: dict[str, ToolImpl] | None = None,
    playbook: PlaybookConfig | None = None,
    limits: Limits | None = None,
    **kwargs: Any,
) -> TradeProposal:
    """Call reason() with mocked Anthropic client and given response sequence."""
    with patch("options_agent.agent.reasoner.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = mock_responses
        MockCls.return_value = mock_client
        return reason(
            context=context or _make_context(),
            tool_impls=tool_impls or {},
            playbook=playbook or PlaybookConfig(),
            limits=limits or Limits(),
            **kwargs,
        )


def _patched_reason_ctx(
    mock_responses: list[MagicMock],
    context: ContextSnapshot,
    tool_impls: dict[str, ToolImpl] | None = None,
    **kwargs: Any,
) -> tuple[TradeProposal, MagicMock]:
    """Like _patched_reason but returns (proposal, mock_client) for call inspection."""
    with patch("options_agent.agent.reasoner.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = mock_responses
        MockCls.return_value = mock_client
        proposal = reason(
            context=context,
            tool_impls=tool_impls or {},
            playbook=PlaybookConfig(),
            limits=Limits(),
            **kwargs,
        )
    return proposal, mock_client


# ──────────────────────────────────────────────────────────────────────────────
# agent/schema.py — SUBMIT_TRADE_PROPOSAL definition
# ──────────────────────────────────────────────────────────────────────────────


def test_submit_tool_has_required_keys() -> None:
    assert "name" in SUBMIT_TRADE_PROPOSAL
    assert "description" in SUBMIT_TRADE_PROPOSAL
    assert "input_schema" in SUBMIT_TRADE_PROPOSAL


def test_submit_tool_name_constant_matches() -> None:
    assert SUBMIT_TRADE_PROPOSAL["name"] == TOOL_SUBMIT_TRADE_PROPOSAL
    assert TOOL_SUBMIT_TRADE_PROPOSAL == "submit_trade_proposal"


def test_submit_tool_input_schema_is_object() -> None:
    schema = SUBMIT_TRADE_PROPOSAL["input_schema"]
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"


def test_submit_tool_input_schema_has_no_title() -> None:
    schema = SUBMIT_TRADE_PROPOSAL["input_schema"]
    assert "title" not in schema, "Top-level title should be stripped from the schema"


def test_submit_tool_input_schema_has_required_fields() -> None:
    schema = _build_input_schema()
    required = set(schema.get("required", []))
    expected_required = {
        "action",
        "underlying",
        "strategy",
        "legs",
        "thesis",
        "iv_rationale",
        "catalyst_check",
        "conviction",
        "est_max_loss",
        "est_max_profit",
        "breakevens",
        "net_delta",
        "net_theta",
        "net_vega",
        "exit_plan",
        "informed_by",
    }
    assert expected_required <= required, (
        f"Missing required fields: {expected_required - required}"
    )


def test_submit_tool_action_enum_includes_no_action() -> None:
    schema = _build_input_schema()
    props = schema.get("properties", {})
    action_enum = props.get("action", {}).get("enum", [])
    assert "NO_ACTION" in action_enum, (
        "NO_ACTION must be in the action enum so the agent can abstain via the "
        "forced commit tool — without it the agent has no structured way to decline."
    )


def test_submit_tool_not_in_agent_tools() -> None:
    assert TOOL_SUBMIT_TRADE_PROPOSAL not in AGENT_TOOL_NAMES, (
        "submit_trade_proposal must not appear in AGENT_TOOLS. "
        "It is the commit mechanism, not a read-only data tool."
    )


def test_submit_tool_description_non_empty() -> None:
    desc = SUBMIT_TRADE_PROPOSAL.get("description", "")
    assert isinstance(desc, str) and len(desc) > 50


# ──────────────────────────────────────────────────────────────────────────────
# Config additions
# ──────────────────────────────────────────────────────────────────────────────


def test_config_model_id_default() -> None:
    cfg = Config()
    assert cfg.model_id == "claude-sonnet-4-6"


def test_config_max_schema_retries_default() -> None:
    cfg = Config()
    assert cfg.max_schema_retries == 2


def test_config_max_reasoning_turns_default() -> None:
    cfg = Config()
    assert cfg.max_reasoning_turns == 10


def test_config_max_schema_retries_validation() -> None:
    with pytest.raises(Exception):
        Config(max_schema_retries=-1)


def test_config_max_reasoning_turns_validation() -> None:
    with pytest.raises(Exception):
        Config(max_reasoning_turns=0)


# ──────────────────────────────────────────────────────────────────────────────
# ContextSnapshot.tool_calls_transcript (WP-0 amendment)
# ──────────────────────────────────────────────────────────────────────────────


def test_context_snapshot_transcript_defaults_empty() -> None:
    ctx = _make_context()
    assert ctx.tool_calls_transcript == []


def test_tool_call_record_fields() -> None:
    record = ToolCallRecord(
        tool_name="get_universe_snapshot",
        tool_input={},
        result_json='{"vix": 16.8}',
    )
    assert record.tool_name == "get_universe_snapshot"
    assert record.tool_input == {}
    assert record.result_json == '{"vix": 16.8}'


# ──────────────────────────────────────────────────────────────────────────────
# reason() — happy paths
# ──────────────────────────────────────────────────────────────────────────────


def test_reason_no_tool_calls_returns_valid_proposal() -> None:
    """Model stops exploration immediately and commits a valid proposal."""
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP)],
        ),
    ]
    proposal = _patched_reason(responses)
    assert isinstance(proposal, TradeProposal)
    assert proposal.action == "OPEN"
    assert proposal.underlying == "SPY"
    assert proposal.strategy == "bull_put_spread"


def test_reason_with_tool_call_dispatches_and_records() -> None:
    """Model calls get_universe_snapshot, receives result, then commits."""
    universe_block = _tool_use_block("get_universe_snapshot", {}, "tu_explore")
    responses = [
        _mock_response("tool_use", [universe_block]),
        _mock_response("end_turn", [_text_block("Ready to commit.")]),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP, "tu_commit")],
        ),
    ]

    mock_impl = MagicMock(return_value={"vix_level": 16.8, "market_regime": "neutral"})
    context = _make_context()
    proposal, _ = _patched_reason_ctx(
        responses,
        context,
        tool_impls={"get_universe_snapshot": mock_impl},
    )

    assert isinstance(proposal, TradeProposal)
    mock_impl.assert_called_once_with({})
    assert len(context.tool_calls_transcript) == 1
    assert context.tool_calls_transcript[0].tool_name == "get_universe_snapshot"
    assert context.tool_calls_transcript[0].tool_input == {}
    assert '"vix_level"' in context.tool_calls_transcript[0].result_json


def test_reason_multiple_tool_calls_all_recorded() -> None:
    """Multiple sequential tool calls are all captured in the transcript."""
    responses = [
        _mock_response(
            "tool_use",
            [
                _tool_use_block("get_universe_snapshot", {}, "tu_1"),
                _tool_use_block("get_portfolio_state", {}, "tu_2"),
            ],
        ),
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP, "tu_c")],
        ),
    ]

    mock_univ = MagicMock(return_value={"snapshots": []})
    mock_port = MagicMock(return_value={"positions": []})
    context = _make_context()
    _patched_reason_ctx(
        responses,
        context,
        tool_impls={
            "get_universe_snapshot": mock_univ,
            "get_portfolio_state": mock_port,
        },
    )

    assert len(context.tool_calls_transcript) == 2
    names = [r.tool_name for r in context.tool_calls_transcript]
    assert "get_universe_snapshot" in names
    assert "get_portfolio_state" in names


def test_reason_no_action_is_valid_response() -> None:
    """action=NO_ACTION is a valid structured output through the forced commit tool."""
    no_action_input = {**_VALID_PROPOSAL_INPUT, "action": "NO_ACTION"}
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(TOOL_SUBMIT_TRADE_PROPOSAL, no_action_input)],
        ),
    ]
    proposal = _patched_reason(responses)
    assert proposal.action == "NO_ACTION"


def test_reason_transcript_empty_when_no_tools_called() -> None:
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP)],
        ),
    ]
    context = _make_context()
    _patched_reason_ctx(responses, context)
    assert context.tool_calls_transcript == []


# ──────────────────────────────────────────────────────────────────────────────
# reason() — two-phase loop invariants
# ──────────────────────────────────────────────────────────────────────────────


def test_reason_exploration_uses_agent_tools_not_commit_tool() -> None:
    """Phase 1 calls must use AGENT_TOOLS; phase 2 must use SUBMIT_TRADE_PROPOSAL."""
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP)],
        ),
    ]
    with patch("options_agent.agent.reasoner.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        MockCls.return_value = mock_client

        reason(
            context=_make_context(),
            tool_impls={},
            playbook=PlaybookConfig(),
            limits=Limits(),
        )

    calls = mock_client.messages.create.call_args_list
    assert len(calls) == 2

    # Exploration call: tool_choice=auto, tools=AGENT_TOOLS (no submit)
    explore_kwargs = calls[0].kwargs
    assert explore_kwargs["tool_choice"] == {"type": "auto"}
    explore_tool_names = {t["name"] for t in explore_kwargs["tools"]}
    assert TOOL_SUBMIT_TRADE_PROPOSAL not in explore_tool_names

    # Commit call: forced to submit_trade_proposal only
    commit_kwargs = calls[1].kwargs
    assert commit_kwargs["tool_choice"] == {
        "type": "tool",
        "name": TOOL_SUBMIT_TRADE_PROPOSAL,
    }
    commit_tool_names = {t["name"] for t in commit_kwargs["tools"]}
    assert commit_tool_names == {TOOL_SUBMIT_TRADE_PROPOSAL}


def test_reason_commit_adds_user_message_after_end_turn() -> None:
    """When exploration ends naturally, a user message prompts the commit."""
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP)],
        ),
    ]
    with patch("options_agent.agent.reasoner.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        MockCls.return_value = mock_client

        reason(
            context=_make_context(),
            tool_impls={},
            playbook=PlaybookConfig(),
            limits=Limits(),
        )

    # The commit call should have at least 3 messages:
    # [user initial] [assistant end_turn] [user "call submit_trade_proposal"]
    commit_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
    roles = [m["role"] for m in commit_messages]
    assert roles[-1] == "user", "Last message before commit must be a user turn"
    assert roles[-2] == "assistant"


# ──────────────────────────────────────────────────────────────────────────────
# reason() — schema retry logic
# ──────────────────────────────────────────────────────────────────────────────


def test_reason_schema_retry_recovers_on_second_attempt() -> None:
    """First commit produces invalid schema; second succeeds."""
    invalid_input = {**_VALID_PROPOSAL_INPUT, "conviction": 5.0}  # >1.0 — invalid

    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, invalid_input, "tu_bad")],
        ),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP, "tu_ok")],
        ),
    ]
    proposal = _patched_reason(responses, max_schema_retries=2)
    assert isinstance(proposal, TradeProposal)
    assert proposal.conviction == 0.65


def test_reason_schema_retry_count() -> None:
    """Verify exactly 1 explore + N commit calls are made (1 initial + N-1 retries)."""
    invalid_input = {**_VALID_PROPOSAL_INPUT, "conviction": 5.0}

    def _commit_then_succeed(responses: list[MagicMock]) -> int:
        with patch("options_agent.agent.reasoner.anthropic.Anthropic") as MockCls:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = responses
            MockCls.return_value = mock_client
            reason(
                context=_make_context(),
                tool_impls={},
                playbook=PlaybookConfig(),
                limits=Limits(),
                max_schema_retries=2,
            )
            return mock_client.messages.create.call_count

    # 1 explore + 2 commit calls: first commit fails, second succeeds
    calls = _commit_then_succeed(
        [
            _mock_response("end_turn", [_text_block()]),
            _mock_response("tool_use", [_tool_use_block(_TSUB, invalid_input, "tu_1")]),
            _mock_response(
                "tool_use",
                [_tool_use_block(_TSUB, _VP, "tu_2")],
            ),
        ]
    )
    assert calls == 3  # 1 explore + 2 commit (1 fail + 1 success)


def test_reason_retry_feeds_validation_error_back() -> None:
    """The retry user message must contain the Pydantic validation error text."""
    invalid_input = {**_VALID_PROPOSAL_INPUT, "conviction": 5.0}

    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, invalid_input, "tu_bad")],
        ),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP, "tu_ok")],
        ),
    ]
    with patch("options_agent.agent.reasoner.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        MockCls.return_value = mock_client

        reason(
            context=_make_context(),
            tool_impls={},
            playbook=PlaybookConfig(),
            limits=Limits(),
            max_schema_retries=2,
        )

    # The second commit call should have a tool_result error message in its history
    second_commit_msgs = mock_client.messages.create.call_args_list[2].kwargs[
        "messages"
    ]
    # Find the tool_result message
    tool_result_msgs = [
        m
        for m in second_commit_msgs
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]
        )
    ]
    assert tool_result_msgs, "Retry call must include a tool_result with the error"
    tool_result_block = next(
        b
        for b in tool_result_msgs[-1]["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    assert tool_result_block.get("is_error") is True
    assert (
        "conviction" in tool_result_block.get("content", "").lower()
        or "valid" in tool_result_block.get("content", "").lower()
    )


def test_reason_retry_exhausted_raises_reasoner_error() -> None:
    """ReasonerError raised when all max_schema_retries+1 attempts are invalid."""
    invalid_input = {**_VALID_PROPOSAL_INPUT, "conviction": 5.0}

    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response("tool_use", [_tool_use_block(_TSUB, invalid_input, "tu_1")]),
        _mock_response("tool_use", [_tool_use_block(_TSUB, invalid_input, "tu_2")]),
        _mock_response("tool_use", [_tool_use_block(_TSUB, invalid_input, "tu_3")]),
    ]
    with pytest.raises(ReasonerError) as exc_info:
        _patched_reason(responses, max_schema_retries=2)

    assert exc_info.value.last_validation_error is not None
    assert isinstance(exc_info.value.last_validation_error, ValidationError)
    assert "3 attempt" in str(exc_info.value)


def test_reason_zero_retries_raises_on_first_invalid() -> None:
    invalid_input = {**_VALID_PROPOSAL_INPUT, "conviction": 5.0}
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response("tool_use", [_tool_use_block(_TSUB, invalid_input, "tu_1")]),
    ]
    with pytest.raises(ReasonerError):
        _patched_reason(responses, max_schema_retries=0)


# ──────────────────────────────────────────────────────────────────────────────
# reason() — error paths
# ──────────────────────────────────────────────────────────────────────────────


def test_reason_unknown_tool_raises_reasoner_error() -> None:
    """Model calling an unknown tool must raise ReasonerError immediately."""
    responses = [
        _mock_response(
            "tool_use",
            [_tool_use_block("place_order", {"symbol": "SPY"})],
        ),
    ]
    with pytest.raises(ReasonerError, match="unknown tool"):
        _patched_reason(responses)


def test_reason_missing_impl_raises_reasoner_error() -> None:
    """A tool in AGENT_TOOLS with no impl provided must raise ReasonerError."""
    responses = [
        _mock_response(
            "tool_use",
            [_tool_use_block("get_universe_snapshot", {})],
        ),
    ]
    with pytest.raises(ReasonerError, match="No implementation"):
        _patched_reason(responses, tool_impls={})


def test_reason_no_tool_use_block_in_commit_raises() -> None:
    """Forced commit returning no tool_use block is an SDK contract violation."""
    responses = [
        _mock_response("end_turn", [_text_block()]),
        _mock_response("end_turn", [_text_block("Here's my analysis...")]),
    ]
    with pytest.raises(ReasonerError, match="no tool_use block"):
        _patched_reason(responses)


def test_reason_error_has_no_last_validation_error_for_non_schema_failures() -> None:
    """ReasonerError for non-schema failures (unknown tool) has no ValidationError."""
    responses = [
        _mock_response(
            "tool_use",
            [_tool_use_block("submit_order", {"symbol": "SPY"})],
        ),
    ]
    with pytest.raises(ReasonerError) as exc_info:
        _patched_reason(responses)
    assert exc_info.value.last_validation_error is None


# ──────────────────────────────────────────────────────────────────────────────
# reason() — turn cap behaviour
# ──────────────────────────────────────────────────────────────────────────────


def test_reason_turn_cap_proceeds_to_commit() -> None:
    """When max_turns is hit, reason() still proceeds to commit (no exception)."""
    # With max_turns=1 and model calls a tool on turn 0: loop hits the cap,
    # else branch fires, commit proceeds directly (last msg is user tool_results).
    responses = [
        _mock_response(
            "tool_use",
            [_tool_use_block("get_universe_snapshot", {}, "tu_explore")],
        ),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP, "tu_commit")],
        ),
    ]
    mock_impl = MagicMock(return_value={"vix_level": 16.8})
    proposal = _patched_reason(
        responses,
        tool_impls={"get_universe_snapshot": mock_impl},
        max_turns=1,
    )
    assert isinstance(proposal, TradeProposal)


def test_reason_turn_cap_commit_does_not_add_extra_user_message() -> None:
    """When last exploration msg is user (tool_results), no extra user turn added."""
    responses = [
        _mock_response(
            "tool_use",
            [_tool_use_block("get_universe_snapshot", {}, "tu_explore")],
        ),
        _mock_response(
            "tool_use",
            [_tool_use_block(_TSUB, _VP, "tu_commit")],
        ),
    ]
    mock_impl = MagicMock(return_value={"vix_level": 16.8})
    with patch("options_agent.agent.reasoner.anthropic.Anthropic") as MockCls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        MockCls.return_value = mock_client

        reason(
            context=_make_context(),
            tool_impls={"get_universe_snapshot": mock_impl},
            playbook=PlaybookConfig(),
            limits=Limits(),
            max_turns=1,
        )

    commit_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
    # Should NOT end with two consecutive user messages
    roles = [m["role"] for m in commit_messages]
    for i in range(len(roles) - 1):
        assert not (roles[i] == "user" and roles[i + 1] == "user"), (
            f"Consecutive user messages at positions {i} and {i + 1}: {roles}"
        )
