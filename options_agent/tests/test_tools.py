"""Tests for WP-6.1: tool definitions + mocked returns harness.

Verifies:
  1. All tool schemas are structurally valid (SDK-compatible JSON Schema).
  2. The tool list contains only read-only tools — no execution tools.
  3. Mock harness returns correctly-typed, pyright-clean WP-0 objects.
  4. Mock universe exercises None/warm-up/earnings states, not just happy path.
  5. FilteredChain mock is compact and plausibly shaped.
  6. End-to-end: every tool in AGENT_TOOLS has a corresponding mock impl.
"""

from typing import cast

import pytest

from options_agent.agent.tools import (
    AGENT_TOOL_NAMES,
    AGENT_TOOLS,
    TOOL_GET_EVENTS,
    TOOL_GET_FILTERED_CHAIN,
    TOOL_GET_HELD_LEG_GREEKS,
    TOOL_GET_JOURNAL_BY_SYMBOL,
    TOOL_GET_OUTCOME_STATS,
    TOOL_GET_PORTFOLIO_STATE,
    TOOL_GET_POSITION_HISTORY,
    TOOL_GET_PRICE_HISTORY,
    TOOL_GET_UNIVERSE_SNAPSHOT,
    PositionHistory,
)
from options_agent.agent.tools_mock import (
    MOCK_TOOL_IMPLS,
    _get_events,
    _get_filtered_chain,
    _get_journal_by_symbol,
    _get_portfolio_state,
    _get_position_history,
    _get_universe_snapshot,
)
from options_agent.contracts.data import (
    EventInfo,
    FilteredChain,
    PortfolioState,
    UniverseSnapshot,
)
from options_agent.contracts.journal import JournalRecord
from options_agent.contracts.state import ActionTaken

# ──────────────────────────────────────────────────────────────────────────────
# Tool schema structural validation
# ──────────────────────────────────────────────────────────────────────────────


def test_all_tools_have_required_keys() -> None:
    for tool in AGENT_TOOLS:
        assert "name" in tool, f"Tool missing 'name': {tool}"
        assert "description" in tool, f"Tool {tool.get('name')!r} missing 'description'"
        assert "input_schema" in tool, (
            f"Tool {tool.get('name')!r} missing 'input_schema'"
        )


def test_all_input_schemas_are_objects() -> None:
    for tool in AGENT_TOOLS:
        schema = tool["input_schema"]
        assert schema.get("type") == "object", (
            f"Tool {tool['name']!r}: input_schema.type must be 'object', "
            f"got {schema.get('type')!r}"
        )


def test_all_required_fields_in_properties() -> None:
    """Required fields must be present in properties — SDK validates at call time."""
    for tool in AGENT_TOOLS:
        schema = tool["input_schema"]
        required = cast(list[str], schema.get("required", []))
        properties = cast(dict[str, object], schema.get("properties", {}))
        for field in required:
            assert field in properties, (
                f"Tool {tool['name']!r}: required field {field!r} not in properties"
            )


def test_tool_names_are_unique() -> None:
    names = [t["name"] for t in AGENT_TOOLS]
    assert len(names) == len(set(names)), f"Duplicate tool names: {names}"


def test_tool_names_constant_matches_list() -> None:
    assert AGENT_TOOL_NAMES == frozenset(t["name"] for t in AGENT_TOOLS)


def test_expected_tool_count() -> None:
    assert len(AGENT_TOOLS) == 7, (
        f"Expected 7 tools, got {len(AGENT_TOOLS)}. "
        "If you added or removed a tool, update this count and the card."
    )


def test_all_expected_tools_present() -> None:
    assert TOOL_GET_PORTFOLIO_STATE in AGENT_TOOL_NAMES
    assert TOOL_GET_UNIVERSE_SNAPSHOT in AGENT_TOOL_NAMES
    assert TOOL_GET_FILTERED_CHAIN in AGENT_TOOL_NAMES
    assert TOOL_GET_EVENTS in AGENT_TOOL_NAMES
    assert TOOL_GET_JOURNAL_BY_SYMBOL in AGENT_TOOL_NAMES
    assert TOOL_GET_POSITION_HISTORY in AGENT_TOOL_NAMES


# ──────────────────────────────────────────────────────────────────────────────
# Read-only invariant — no execution tool may appear in the agent tool list
# ──────────────────────────────────────────────────────────────────────────────

_EXECUTION_TOOL_NAMES = {
    "place_order",
    "submit_order",
    "cancel_order",
    "modify_order",
    "execute_trade",
    "open_position",
    "close_position",
    "roll_position",
}


def test_no_execution_tools_in_agent_tool_list() -> None:
    """Core Principle 1: the agent must have no path to placing an order."""
    violations = AGENT_TOOL_NAMES & _EXECUTION_TOOL_NAMES
    assert not violations, (
        f"Execution tool(s) found in AGENT_TOOLS: {violations}. "
        "The agent must have no path to placing an order (Core Principle 1)."
    )


def test_descriptions_non_empty() -> None:
    for tool in AGENT_TOOLS:
        desc = tool.get("description", "")
        assert desc and len(desc) > 50, (
            f"Tool {tool['name']!r} has a suspiciously short description"
            f" ({len(desc)} chars). Descriptions are how the model learns"
            " to use the tool correctly."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Mock return type correctness
# ──────────────────────────────────────────────────────────────────────────────


def test_mock_portfolio_state_type() -> None:
    result = _get_portfolio_state({})
    assert isinstance(result, PortfolioState)


def test_mock_portfolio_state_has_positions() -> None:
    state = _get_portfolio_state({})
    assert len(state.positions) >= 1


def test_mock_portfolio_state_net_greeks_populated() -> None:
    state = _get_portfolio_state({})
    assert state.net_dollar_delta != 0 or state.net_dollar_vega != 0, (
        "All net dollar Greeks are zero — mock is not exercising any"
        " real portfolio state"
    )


def test_mock_portfolio_state_pnl_arithmetic_consistent() -> None:
    """Mock Position P&L must be internally consistent.

    entry_net_amount and current_mark are in option-price×contracts units
    (pre-×100). unrealized_pnl = (|entry| - |current|) × 100.
    """
    state = _get_portfolio_state({})
    for pos in state.positions:
        if pos.entry_net_amount is not None and pos.current_mark is not None:
            expected = (abs(pos.entry_net_amount) - abs(pos.current_mark)) * 100
            assert abs(pos.unrealized_pnl - expected) < 0.01, (
                f"Position {pos.id!r} P&L inconsistent: "
                f"entry={pos.entry_net_amount}, mark={pos.current_mark},"
                f" unrealized_pnl={pos.unrealized_pnl} (expected ~{expected})"
            )


def test_mock_portfolio_state_pnl_equals_sum_of_positions() -> None:
    state = _get_portfolio_state({})
    expected = sum(pos.unrealized_pnl for pos in state.positions)
    assert abs(state.unrealized_pnl - expected) < 0.01, (
        f"Portfolio unrealized_pnl={state.unrealized_pnl} != "
        f"sum of positions ({expected})"
    )


def test_mock_universe_snapshot_type() -> None:
    result = _get_universe_snapshot({})
    assert isinstance(result, UniverseSnapshot)


def test_mock_universe_has_null_iv_rank_symbol() -> None:
    """Warm-up period (iv_rank=None) must be representable from day 1."""
    snapshot = _get_universe_snapshot({})
    null_iv_symbols = [
        sym for sym, ss in snapshot.symbol_snapshots.items() if ss.iv_rank is None
    ]
    assert null_iv_symbols, (
        "Mock universe has no symbol with iv_rank=None. "
        "The warm-up period where IV history is insufficient is a real"
        " operating state; the agent must be tested against it."
    )


def test_mock_universe_has_earnings_symbol() -> None:
    """Near-earnings state must be representable so the agent learns to skip it."""
    snapshot = _get_universe_snapshot({})
    blackout_days = 5
    earnings_symbols = [
        sym
        for sym, ss in snapshot.symbol_snapshots.items()
        if ss.days_to_earnings is not None and ss.days_to_earnings <= blackout_days
    ]
    assert earnings_symbols, (
        f"Mock universe has no symbol with days_to_earnings <= {blackout_days}. "
        "The agent must be tested against the event-blackout state."
    )


def test_mock_universe_has_clean_tradeable_symbol() -> None:
    """At least one symbol must be fully tradeable (non-null iv_rank, no near earnings)."""  # noqa: E501
    snapshot = _get_universe_snapshot({})
    clean_symbols = [
        sym
        for sym, ss in snapshot.symbol_snapshots.items()
        if ss.iv_rank is not None
        and (ss.days_to_earnings is None or ss.days_to_earnings > 5)
    ]
    assert clean_symbols, "Mock universe has no clean, fully-tradeable symbol."


def test_mock_filtered_chain_type() -> None:
    result = _get_filtered_chain({"symbol": "SPY"})
    assert isinstance(result, FilteredChain)


def test_mock_filtered_chain_compact() -> None:
    chain = _get_filtered_chain({"symbol": "SPY"})
    assert 2 <= len(chain.contracts) <= 20, (
        f"Mock FilteredChain has {len(chain.contracts)} contracts — "
        "expected a compact but non-empty chain (2–20 contracts)"
    )


def test_mock_filtered_chain_strategy_hint_puts_only() -> None:
    chain = _get_filtered_chain({"symbol": "SPY", "strategy_hint": "bull_put_spread"})
    assert all(c.right == "put" for c in chain.contracts), (
        "bull_put_spread strategy_hint should filter to puts only"
    )


def test_mock_filtered_chain_strategy_hint_calls_only() -> None:
    chain = _get_filtered_chain({"symbol": "SPY", "strategy_hint": "bear_call_spread"})
    assert all(c.right == "call" for c in chain.contracts), (
        "bear_call_spread strategy_hint should filter to calls only"
    )


def test_mock_filtered_chain_no_hint_has_both_rights() -> None:
    chain = _get_filtered_chain({"symbol": "SPY"})
    rights = {c.right for c in chain.contracts}
    assert "call" in rights and "put" in rights, (
        "No strategy_hint should return both calls and puts"
    )


def test_mock_filtered_chain_respects_symbol() -> None:
    chain = _get_filtered_chain({"symbol": "AAPL"})
    assert chain.underlying == "AAPL"


def test_mock_events_type() -> None:
    result = _get_events({"symbols": ["SPY", "AAPL"]})
    assert isinstance(result, dict)
    for key, val in result.items():
        assert isinstance(key, str)
        assert isinstance(val, EventInfo)


def test_mock_events_known_earnings_symbol() -> None:
    result = _get_events({"symbols": ["AAPL"]})
    assert "AAPL" in result
    assert result["AAPL"].earnings is not None
    assert result["AAPL"].earnings.confirmed is True


def test_mock_events_no_earnings_symbol() -> None:
    result = _get_events({"symbols": ["SPY"]})
    assert result["SPY"].earnings is None


def test_mock_events_unknown_symbol_returns_empty() -> None:
    result = _get_events({"symbols": ["XYZ_UNKNOWN"]})
    assert "XYZ_UNKNOWN" in result
    assert result["XYZ_UNKNOWN"].earnings is None
    assert result["XYZ_UNKNOWN"].ex_dividend is None


def test_mock_journal_by_symbol_type() -> None:
    result = _get_journal_by_symbol({"symbol": "SPY"})
    assert isinstance(result, list)
    for record in result:
        assert isinstance(record, JournalRecord)


def test_mock_journal_by_symbol_opened_cycle() -> None:
    records = _get_journal_by_symbol({"symbol": "SPY"})
    opened = [r for r in records if r.action_taken == ActionTaken.OPENED]
    assert opened, "Mock journal for SPY should contain at least one OPENED cycle"


def test_mock_journal_by_symbol_unknown_returns_empty() -> None:
    result = _get_journal_by_symbol({"symbol": "UNKNOWN_TICKER"})
    assert result == []


def test_mock_position_history_known_position() -> None:
    result = _get_position_history({"position_id": "pos-001"})
    assert isinstance(result, PositionHistory)
    assert result.opening_record is not None
    assert isinstance(result.outcome_records, list)


def test_mock_position_history_unknown_returns_none() -> None:
    result = _get_position_history({"position_id": "pos-DOES_NOT_EXIST"})
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# Mock impl map completeness
# ──────────────────────────────────────────────────────────────────────────────


def test_mock_impls_cover_all_tools() -> None:
    """Every tool in AGENT_TOOLS must have a corresponding mock implementation."""
    missing = AGENT_TOOL_NAMES - set(MOCK_TOOL_IMPLS.keys())
    assert not missing, (
        f"MOCK_TOOL_IMPLS is missing implementations for: {missing}. "
        "Add a mock for each tool so the end-to-end harness can run without live data."
    )


def test_mock_impls_no_extra_tools() -> None:
    """MOCK_TOOL_IMPLS should not contain tools outside the agent tool list.

    Internal assembler-only impl keys (never exposed to the LLM) are the
    one sanctioned exception — they ride in the same map so the DI pattern
    stays uniform between mock and real backings.
    """
    internal_keys = {TOOL_GET_HELD_LEG_GREEKS, TOOL_GET_OUTCOME_STATS}
    extra = set(MOCK_TOOL_IMPLS.keys()) - AGENT_TOOL_NAMES - internal_keys
    assert not extra, (
        f"MOCK_TOOL_IMPLS contains implementations for unknown tools: {extra}. "
        "These will never be called and indicate a naming drift."
    )


def test_mock_impls_all_callable() -> None:
    for name, impl in MOCK_TOOL_IMPLS.items():
        assert callable(impl), f"Mock impl for {name!r} is not callable"


# ──────────────────────────────────────────────────────────────────────────────
# End-to-end: call every mock impl via the dispatch map
# ──────────────────────────────────────────────────────────────────────────────

_SAMPLE_INPUTS: dict[str, dict] = {
    TOOL_GET_PORTFOLIO_STATE: {},
    TOOL_GET_UNIVERSE_SNAPSHOT: {},
    TOOL_GET_FILTERED_CHAIN: {"symbol": "SPY", "strategy_hint": "bull_put_spread"},
    TOOL_GET_EVENTS: {"symbols": ["SPY", "AAPL", "NVDA"]},
    TOOL_GET_JOURNAL_BY_SYMBOL: {"symbol": "SPY"},
    TOOL_GET_POSITION_HISTORY: {"position_id": "pos-001"},
    TOOL_GET_PRICE_HISTORY: {"symbol": "SPY"},
}


@pytest.mark.parametrize("tool_name", list(AGENT_TOOL_NAMES))
def test_end_to_end_mock_call(tool_name: str) -> None:
    """Each mock impl must return a non-None result for a valid input."""
    impl = MOCK_TOOL_IMPLS[tool_name]
    sample_input = _SAMPLE_INPUTS[tool_name]
    result = impl(sample_input)
    assert result is not None, (
        f"Mock impl for {tool_name!r} returned None for input {sample_input!r}. "
        "All mock impls must return a value for valid inputs."
    )
