"""Tier-1 eval harness self-test — runs in CI on every push.

This is NOT the prompt eval. It verifies that the eval harness itself is
correct: assertion functions behave as expected, scenarios are well-formed, and
tool implementations return the right types. It does NOT call reason() and does
not hit the Anthropic API.

What this catches:
  - Invariant/preference functions that would silently pass invalid proposals
  - Invariant/preference functions that would incorrectly flag valid proposals
  - Scenario tool impl maps that return wrong types
  - Broken scenario structure (missing invariants, duplicate IDs, etc.)

What this does NOT catch:
  - Actual model behaviour regressions — those require the tier-2 real-API eval
    in tests/evals/test_prompt_eval.py

See tests/evals/ for the tier-2 eval and its README.
"""

from __future__ import annotations

from datetime import date

import pytest

from options_agent.agent.eval_scenarios import (
    _BASE_INVARIANTS,
    EVAL_SCENARIOS,
    _catalyst_check_substantive,
    _filtered_chain_called_for_open,
    _iv_rationale_substantive,
    _low_iv_strategy_or_no_action,
    _no_action_required,
    _no_naked_shorts,
    _nvda_not_opened,
    _schema_valid,
    _spy_credit_spread_or_no_action,
    _strategy_in_global_playbook,
    _universe_snapshot_called,
    make_spy_tool_impls,
)
from options_agent.agent.tools import (
    TOOL_GET_FILTERED_CHAIN,
    TOOL_GET_UNIVERSE_SNAPSHOT,
)
from options_agent.contracts.data import (
    FilteredChain,
    PortfolioState,
    UniverseSnapshot,
)
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures — valid and invalid TradeProposal variants
# ──────────────────────────────────────────────────────────────────────────────

_NEAR_EXPIRY = date(2026, 7, 18)

_VALID_PROPOSAL = TradeProposal(
    action="OPEN",
    underlying="SPY",
    strategy="bull_put_spread",
    legs=[
        Leg(right="put", side="sell", strike=530.0, expiration=_NEAR_EXPIRY),
        Leg(right="put", side="buy", strike=525.0, expiration=_NEAR_EXPIRY),
    ],
    thesis=(
        "SPY is trending sideways. "
        "The 530/525 put spread collects credit below support."
    ),
    iv_rationale=(
        "iv_rank is 62 — 62nd percentile of the trailing year. "
        "Options are pricing in elevated uncertainty relative to recent history, "
        "placing SPY in the high-IV band (≥50th percentile). "
        "Selling a put spread captures the vol risk premium."
    ),
    catalyst_check=(
        "No upcoming earnings for SPY. FOMC on 2026-06-18 falls within the DTE window; "
        "sized conservatively at 0.6 conviction."
    ),
    conviction=0.65,
    est_max_loss=500.0,
    est_max_profit=270.0,
    breakevens=[527.30],
    net_delta=0.13,
    net_theta=9.0,
    net_vega=-0.38,
    exit_plan=ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21),
    informed_by=[],
)

_NO_ACTION_PROPOSAL = TradeProposal(
    action="NO_ACTION",
    underlying="NVDA",
    strategy="",
    legs=[],
    thesis="iv_rank is None — insufficient IV history. Skipping this cycle.",
    iv_rationale=(
        "iv_rank is None — NVDA is in its warm-up period with insufficient IV history "
        "to determine the correct volatility band. Per policy, NO_ACTION is required."
    ),
    catalyst_check="No upcoming events confirmed for NVDA.",
    conviction=0.0,
    est_max_loss=0.0,
    est_max_profit=0.0,
    breakevens=[],
    net_delta=0.0,
    net_theta=0.0,
    net_vega=0.0,
    exit_plan=ExitPlan(profit_target_pct=0.5, stop_loss_mult=2.0, time_stop_dte=21),
    informed_by=[],
)

_NO_CALLS: list[str] = []
_WITH_UNIVERSE_CALL = [TOOL_GET_UNIVERSE_SNAPSHOT]
_WITH_BOTH_CALLS = [TOOL_GET_UNIVERSE_SNAPSHOT, TOOL_GET_FILTERED_CHAIN]


# ──────────────────────────────────────────────────────────────────────────────
# schema_valid
# ──────────────────────────────────────────────────────────────────────────────


def test_schema_valid_passes_on_valid_proposal() -> None:
    assert _schema_valid(_VALID_PROPOSAL, _NO_CALLS) is True


def test_schema_valid_passes_on_no_action_proposal() -> None:
    assert _schema_valid(_NO_ACTION_PROPOSAL, _NO_CALLS) is True


# ──────────────────────────────────────────────────────────────────────────────
# strategy_in_global_playbook
# ──────────────────────────────────────────────────────────────────────────────


def test_strategy_in_playbook_known_strategy() -> None:
    assert _strategy_in_global_playbook(_VALID_PROPOSAL, _NO_CALLS) is True


def test_strategy_in_playbook_no_action_exempt() -> None:
    assert _strategy_in_global_playbook(_NO_ACTION_PROPOSAL, _NO_CALLS) is True


def test_strategy_in_playbook_unknown_strategy() -> None:
    bad = _VALID_PROPOSAL.model_copy(update={"strategy": "calendar_spread"})
    assert _strategy_in_global_playbook(bad, _NO_CALLS) is False


# ──────────────────────────────────────────────────────────────────────────────
# iv_rationale_substantive
# ──────────────────────────────────────────────────────────────────────────────


def test_iv_rationale_passes_on_good_rationale() -> None:
    assert _iv_rationale_substantive(_VALID_PROPOSAL, _NO_CALLS) is True


def test_iv_rationale_fails_on_short_rationale() -> None:
    bad = _VALID_PROPOSAL.model_copy(update={"iv_rationale": "IV elevated."})
    assert _iv_rationale_substantive(bad, _NO_CALLS) is False


def test_iv_rationale_fails_on_rationale_without_number() -> None:
    bad = _VALID_PROPOSAL.model_copy(
        update={"iv_rationale": "x" * 60}  # long but no numeric value
    )
    assert _iv_rationale_substantive(bad, _NO_CALLS) is False


def test_iv_rationale_passes_no_action_with_none_mention() -> None:
    assert _iv_rationale_substantive(_NO_ACTION_PROPOSAL, _NO_CALLS) is True


# ──────────────────────────────────────────────────────────────────────────────
# catalyst_check_substantive
# ──────────────────────────────────────────────────────────────────────────────


def test_catalyst_check_passes_on_good_check() -> None:
    assert _catalyst_check_substantive(_VALID_PROPOSAL, _NO_CALLS) is True


def test_catalyst_check_fails_on_trivial_check() -> None:
    bad = _VALID_PROPOSAL.model_copy(update={"catalyst_check": "OK."})
    assert _catalyst_check_substantive(bad, _NO_CALLS) is False


# ──────────────────────────────────────────────────────────────────────────────
# no_naked_shorts
# ──────────────────────────────────────────────────────────────────────────────


def test_no_naked_shorts_passes_on_spread() -> None:
    assert _no_naked_shorts(_VALID_PROPOSAL, _NO_CALLS) is True


def test_no_naked_shorts_passes_on_no_action() -> None:
    assert _no_naked_shorts(_NO_ACTION_PROPOSAL, _NO_CALLS) is True


def test_no_naked_shorts_fails_on_naked_put() -> None:
    naked = _VALID_PROPOSAL.model_copy(
        update={
            "legs": [
                Leg(right="put", side="sell", strike=530.0, expiration=_NEAR_EXPIRY)
            ]
        }
    )
    assert _no_naked_shorts(naked, _NO_CALLS) is False


def test_no_naked_shorts_fails_on_naked_call() -> None:
    naked = _VALID_PROPOSAL.model_copy(
        update={
            "legs": [
                Leg(right="call", side="sell", strike=560.0, expiration=_NEAR_EXPIRY)
            ]
        }
    )
    assert _no_naked_shorts(naked, _NO_CALLS) is False


def test_no_naked_shorts_correct_for_iron_condor() -> None:
    condor = _VALID_PROPOSAL.model_copy(
        update={
            "strategy": "iron_condor",
            "legs": [
                Leg(right="put", side="sell", strike=530.0, expiration=_NEAR_EXPIRY),
                Leg(right="put", side="buy", strike=525.0, expiration=_NEAR_EXPIRY),
                Leg(right="call", side="sell", strike=555.0, expiration=_NEAR_EXPIRY),
                Leg(right="call", side="buy", strike=560.0, expiration=_NEAR_EXPIRY),
            ],
        }
    )
    assert _no_naked_shorts(condor, _NO_CALLS) is True


# ──────────────────────────────────────────────────────────────────────────────
# tool call invariants
# ──────────────────────────────────────────────────────────────────────────────


def test_universe_snapshot_called_passes_when_in_calls() -> None:
    assert _universe_snapshot_called(_VALID_PROPOSAL, _WITH_UNIVERSE_CALL) is True


def test_universe_snapshot_called_fails_when_absent() -> None:
    assert _universe_snapshot_called(_VALID_PROPOSAL, _NO_CALLS) is False


def test_filtered_chain_called_passes_for_open_when_in_calls() -> None:
    assert _filtered_chain_called_for_open(_VALID_PROPOSAL, _WITH_BOTH_CALLS) is True


def test_filtered_chain_called_fails_for_open_when_absent() -> None:
    assert _filtered_chain_called_for_open(_VALID_PROPOSAL, _WITH_UNIVERSE_CALL) is False


def test_filtered_chain_called_passes_for_no_action_without_chain() -> None:
    # NO_ACTION: chain fetch not required, so the invariant must not fire.
    assert _filtered_chain_called_for_open(_NO_ACTION_PROPOSAL, _NO_CALLS) is True


# ──────────────────────────────────────────────────────────────────────────────
# scenario-specific checks
# ──────────────────────────────────────────────────────────────────────────────


def test_nvda_not_opened_passes_on_spy() -> None:
    assert _nvda_not_opened(_VALID_PROPOSAL, _NO_CALLS) is True


def test_nvda_not_opened_passes_on_no_action() -> None:
    assert _nvda_not_opened(_NO_ACTION_PROPOSAL, _NO_CALLS) is True


def test_nvda_not_opened_fails_when_nvda_opened() -> None:
    nvda_open = _VALID_PROPOSAL.model_copy(update={"underlying": "NVDA"})
    assert _nvda_not_opened(nvda_open, _NO_CALLS) is False


def test_spy_credit_spread_passes_for_credit_strategy() -> None:
    assert _spy_credit_spread_or_no_action(_VALID_PROPOSAL, _NO_CALLS) is True


def test_spy_credit_spread_fails_for_debit_on_spy() -> None:
    debit_on_spy = _VALID_PROPOSAL.model_copy(update={"strategy": "bull_call_spread"})
    # bull_call_spread is in medium/low but NOT in high_iv_strategies
    from options_agent.config import PlaybookConfig

    pb = PlaybookConfig()
    assert "bull_call_spread" not in pb.high_iv_strategies
    assert _spy_credit_spread_or_no_action(debit_on_spy, _NO_CALLS) is False


def test_low_iv_strategy_passes_for_debit() -> None:
    debit = _VALID_PROPOSAL.model_copy(update={"strategy": "bull_call_spread"})
    assert _low_iv_strategy_or_no_action(debit, _NO_CALLS) is True


def test_low_iv_strategy_fails_for_credit() -> None:
    credit = _VALID_PROPOSAL.model_copy(update={"strategy": "iron_condor"})
    assert _low_iv_strategy_or_no_action(credit, _NO_CALLS) is False


def test_no_action_required_passes() -> None:
    assert _no_action_required(_NO_ACTION_PROPOSAL, _NO_CALLS) is True


def test_no_action_required_fails_on_open() -> None:
    assert _no_action_required(_VALID_PROPOSAL, _NO_CALLS) is False


# ──────────────────────────────────────────────────────────────────────────────
# spy wrapper
# ──────────────────────────────────────────────────────────────────────────────


def test_spy_records_tool_calls() -> None:
    from options_agent.agent.tools import TOOL_GET_UNIVERSE_SNAPSHOT
    from options_agent.agent.tools_mock import MOCK_TOOL_IMPLS

    spy_impls, calls = make_spy_tool_impls(MOCK_TOOL_IMPLS)
    assert calls == []
    spy_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})
    assert calls == [TOOL_GET_UNIVERSE_SNAPSHOT]


def test_spy_delegates_to_impl() -> None:
    from options_agent.agent.tools import TOOL_GET_UNIVERSE_SNAPSHOT
    from options_agent.agent.tools_mock import MOCK_TOOL_IMPLS
    from options_agent.contracts.data import UniverseSnapshot

    spy_impls, _ = make_spy_tool_impls(MOCK_TOOL_IMPLS)
    result = spy_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})
    assert isinstance(result, UniverseSnapshot)


def test_spy_accumulates_multiple_calls() -> None:
    from options_agent.agent.tools import (
        TOOL_GET_PORTFOLIO_STATE,
        TOOL_GET_UNIVERSE_SNAPSHOT,
    )
    from options_agent.agent.tools_mock import MOCK_TOOL_IMPLS

    spy_impls, calls = make_spy_tool_impls(MOCK_TOOL_IMPLS)
    spy_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})
    spy_impls[TOOL_GET_PORTFOLIO_STATE]({})
    spy_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})
    assert calls == [
        TOOL_GET_UNIVERSE_SNAPSHOT,
        TOOL_GET_PORTFOLIO_STATE,
        TOOL_GET_UNIVERSE_SNAPSHOT,
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Scenario structure integrity
# ──────────────────────────────────────────────────────────────────────────────


def test_scenario_ids_unique() -> None:
    ids = [s.id for s in EVAL_SCENARIOS]
    assert len(ids) == len(set(ids)), f"Duplicate scenario IDs: {ids}"


def test_all_scenarios_have_at_least_one_invariant() -> None:
    for scenario in EVAL_SCENARIOS:
        assert scenario.invariants, f"Scenario {scenario.id!r} has no invariants"


def test_all_scenarios_have_description() -> None:
    for scenario in EVAL_SCENARIOS:
        assert len(scenario.description) > 20, (
            f"Scenario {scenario.id!r} description is too short"
        )


def test_all_scenarios_have_tool_impls_for_all_tools() -> None:
    from options_agent.agent.tools import AGENT_TOOL_NAMES

    for scenario in EVAL_SCENARIOS:
        missing = AGENT_TOOL_NAMES - set(scenario.tool_impls.keys())
        assert not missing, f"Scenario {scenario.id!r} is missing tool impls: {missing}"


def test_exactly_five_scenarios() -> None:
    assert len(EVAL_SCENARIOS) == 5, (
        f"Expected 5 eval scenarios, got {len(EVAL_SCENARIOS)}. "
        "Update this count if you add or remove a scenario."
    )


@pytest.mark.parametrize("scenario", EVAL_SCENARIOS, ids=[s.id for s in EVAL_SCENARIOS])
def test_scenario_invariants_are_callable(scenario) -> None:  # type: ignore[no-untyped-def]
    for inv in scenario.invariants:
        assert callable(inv.check), (
            f"Scenario {scenario.id!r} invariant {inv.name!r} check is not callable"
        )


@pytest.mark.parametrize("scenario", EVAL_SCENARIOS, ids=[s.id for s in EVAL_SCENARIOS])
def test_scenario_preferences_have_valid_threshold(scenario) -> None:  # type: ignore[no-untyped-def]
    for pref in scenario.preferences:
        assert 0.0 <= pref.min_pass_rate <= 1.0, (
            f"Scenario {scenario.id!r} preference {pref.name!r} has invalid "
            f"min_pass_rate={pref.min_pass_rate}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Scenario tool impl return type checks
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario", EVAL_SCENARIOS, ids=[s.id for s in EVAL_SCENARIOS])
def test_get_portfolio_state_returns_portfolio(scenario) -> None:  # type: ignore[no-untyped-def]
    from options_agent.agent.tools import TOOL_GET_PORTFOLIO_STATE

    result = scenario.tool_impls[TOOL_GET_PORTFOLIO_STATE]({})
    assert isinstance(result, PortfolioState), (
        f"Scenario {scenario.id!r}: get_portfolio_state returned {type(result)!r}"
    )


@pytest.mark.parametrize("scenario", EVAL_SCENARIOS, ids=[s.id for s in EVAL_SCENARIOS])
def test_get_universe_snapshot_returns_universe(scenario) -> None:  # type: ignore[no-untyped-def]
    from options_agent.agent.tools import TOOL_GET_UNIVERSE_SNAPSHOT

    result = scenario.tool_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})
    assert isinstance(result, UniverseSnapshot), (
        f"Scenario {scenario.id!r}: get_universe_snapshot returned {type(result)!r}"
    )


@pytest.mark.parametrize("scenario", EVAL_SCENARIOS, ids=[s.id for s in EVAL_SCENARIOS])
def test_get_universe_snapshot_has_at_least_one_symbol(scenario) -> None:  # type: ignore[no-untyped-def]
    from options_agent.agent.tools import TOOL_GET_UNIVERSE_SNAPSHOT

    snapshot = scenario.tool_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})
    assert snapshot.symbol_snapshots, (
        f"Scenario {scenario.id!r}: universe has no symbols"
    )


@pytest.mark.parametrize("scenario", EVAL_SCENARIOS, ids=[s.id for s in EVAL_SCENARIOS])
def test_get_filtered_chain_returns_chain(scenario) -> None:  # type: ignore[no-untyped-def]
    from options_agent.agent.tools import TOOL_GET_FILTERED_CHAIN

    snapshot = scenario.tool_impls[
        __import__(
            "options_agent.agent.tools",
            fromlist=["TOOL_GET_UNIVERSE_SNAPSHOT"],
        ).TOOL_GET_UNIVERSE_SNAPSHOT
    ]({})
    first_symbol = next(iter(snapshot.symbol_snapshots))
    result = scenario.tool_impls[TOOL_GET_FILTERED_CHAIN]({"symbol": first_symbol})
    assert isinstance(result, FilteredChain), (
        f"Scenario {scenario.id!r}: get_filtered_chain returned {type(result)!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Base invariants cover all scenarios
# ──────────────────────────────────────────────────────────────────────────────


def test_all_base_invariants_in_all_scenarios() -> None:
    base_names = {inv.name for inv in _BASE_INVARIANTS}
    for scenario in EVAL_SCENARIOS:
        scenario_inv_names = {inv.name for inv in scenario.invariants}
        missing = base_names - scenario_inv_names
        assert not missing, (
            f"Scenario {scenario.id!r} is missing base invariants: {missing}"
        )
