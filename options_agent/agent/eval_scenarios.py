"""Prompt eval scenario definitions for WP-6.5.

Each EvalScenario packages:
  - tool_impls: the mocked data context the agent reasons against
  - invariants: properties that MUST hold on 100% of runs (zero tolerance)
  - preferences: properties expected to hold at a minimum pass-rate over K runs

Two property classes, intentionally distinct:

  Invariants exist because a correct system produces them deterministically even
  when the model is choosing creatively among valid options. Schema validity,
  strategy-in-playbook, and iv_rationale/catalyst_check non-emptiness are not
  subject to model variance — a prompt regression that breaks them breaks them
  every time. "Failed on 2/5 runs" means the invariant check itself is wrong,
  not that the model is having a bad day.

  Preferences are rate-based because the model may legitimately vary among
  correct answers (iron_condor vs. bull_put_spread in high IV are both valid).
  Assert preferences as "≥ X of K runs" rather than "every run", tuned
  per-property after the first baseline run.

Invariant check signature: (proposal: TradeProposal, tool_calls: list[str]) -> bool
Preference check signature: same
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from options_agent.agent.tools import (
    TOOL_GET_FILTERED_CHAIN,
    TOOL_GET_PORTFOLIO_STATE,
    TOOL_GET_UNIVERSE_SNAPSHOT,
)
from options_agent.agent.tools_mock import MOCK_TOOL_IMPLS as _BASE_TOOL_IMPLS
from options_agent.agent.tools_mock import (
    ToolImpl,
    make_earnings_blackout_tool_impls,
    make_low_iv_bullish_tool_impls,
    make_no_iv_history_tool_impls,
    make_portfolio_aware_tool_impls,
)
from options_agent.config import PlaybookConfig
from options_agent.contracts.proposal import TradeProposal

# Default playbook — invariant checks import strategy sets from here, not from
# hardcoded lists, so the eval and the validator always agree on what's allowed.
_PLAYBOOK = PlaybookConfig()

# ──────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────────────

CheckFn = Any  # Callable[[TradeProposal, list[str]], bool] — spelled out below


@dataclass(frozen=True)
class InvariantCheck:
    name: str
    description: str
    check: CheckFn  # (TradeProposal, list[str]) -> bool


@dataclass(frozen=True)
class PreferenceCheck:
    name: str
    description: str
    check: CheckFn  # (TradeProposal, list[str]) -> bool
    min_pass_rate: float  # fraction in [0, 1]; e.g. 0.8 = 4 of 5 runs


@dataclass(frozen=True)
class EvalScenario:
    id: str
    description: str
    tool_impls: dict[str, ToolImpl]
    invariants: list[InvariantCheck]
    preferences: list[PreferenceCheck]


# ──────────────────────────────────────────────────────────────────────────────
# Spy helper
# ──────────────────────────────────────────────────────────────────────────────


def make_spy_tool_impls(
    tool_impls: dict[str, ToolImpl],
) -> tuple[dict[str, ToolImpl], list[str]]:
    """Wrap tool impls so every call records the tool name.

    Returns (spy_impls, calls_list). calls_list is mutated in place as tools
    are invoked — inspect it after reason() returns to assert which tools ran.
    """
    calls: list[str] = []

    def _spy(name: str, impl: ToolImpl) -> ToolImpl:
        def _wrapped(tool_input: dict[str, Any]) -> Any:
            calls.append(name)
            return impl(tool_input)

        return _wrapped

    spy_impls = {name: _spy(name, impl) for name, impl in tool_impls.items()}
    return spy_impls, calls


# ──────────────────────────────────────────────────────────────────────────────
# Shared invariant check functions
# ──────────────────────────────────────────────────────────────────────────────


def _schema_valid(proposal: TradeProposal, _calls: list[str]) -> bool:
    return isinstance(proposal, TradeProposal)


def _strategy_in_global_playbook(proposal: TradeProposal, _calls: list[str]) -> bool:
    """Strategy must be in the union of all playbook bands, or action is non-OPEN."""
    if proposal.action in ("NO_ACTION", "CLOSE", "ROLL"):
        return True
    return proposal.strategy in _PLAYBOOK.all_allowed_strategies


def _iv_rationale_substantive(proposal: TradeProposal, _calls: list[str]) -> bool:
    """iv_rationale must be > 50 chars; OPEN/ROLL must also mention a numeric value.

    NO_ACTION rationales (e.g. iv_rank=None) legitimately cite absence of data
    without a numeric value, so they pass on length alone.
    """
    if len(proposal.iv_rationale) < 50:
        return False
    if proposal.action == "NO_ACTION":
        return True
    return bool(re.search(r"\d", proposal.iv_rationale))


def _catalyst_check_substantive(proposal: TradeProposal, _calls: list[str]) -> bool:
    return len(proposal.catalyst_check) > 20


def _no_naked_shorts(proposal: TradeProposal, _calls: list[str]) -> bool:
    """Every short leg must be covered by at least one long leg of the same right."""
    if proposal.action in ("NO_ACTION",):
        return True
    short_puts = sum(
        1 for leg in proposal.legs if leg.right == "put" and leg.side == "sell"
    )
    long_puts = sum(
        1 for leg in proposal.legs if leg.right == "put" and leg.side == "buy"
    )
    short_calls = sum(
        1 for leg in proposal.legs if leg.right == "call" and leg.side == "sell"
    )
    long_calls = sum(
        1 for leg in proposal.legs if leg.right == "call" and leg.side == "buy"
    )
    return short_puts <= long_puts and short_calls <= long_calls


def _universe_snapshot_called(_proposal: TradeProposal, calls: list[str]) -> bool:
    """Agent must have called get_universe_snapshot before proposing."""
    return TOOL_GET_UNIVERSE_SNAPSHOT in calls


def _filtered_chain_called(_proposal: TradeProposal, calls: list[str]) -> bool:
    """Confirms drill-in: agent fetched a chain before committing to a structure."""
    return TOOL_GET_FILTERED_CHAIN in calls


def _portfolio_state_called(_proposal: TradeProposal, calls: list[str]) -> bool:
    return TOOL_GET_PORTFOLIO_STATE in calls


# ──────────────────────────────────────────────────────────────────────────────
# Shared base invariants — applied to all five scenarios
# ──────────────────────────────────────────────────────────────────────────────

_BASE_INVARIANTS: list[InvariantCheck] = [
    InvariantCheck(
        name="schema_valid",
        description="Output must be a TradeProposal instance (schema enforcement).",
        check=_schema_valid,
    ),
    InvariantCheck(
        name="strategy_in_global_playbook",
        description=(
            "If action=OPEN/ROLL, strategy must be in the union of all playbook bands."
        ),
        check=_strategy_in_global_playbook,
    ),
    InvariantCheck(
        name="iv_rationale_substantive",
        description="iv_rationale must be > 50 chars and contain a numeric value.",
        check=_iv_rationale_substantive,
    ),
    InvariantCheck(
        name="catalyst_check_substantive",
        description="catalyst_check must be > 20 chars.",
        check=_catalyst_check_substantive,
    ),
    InvariantCheck(
        name="no_naked_shorts",
        description="Every sell leg must be covered by a buy leg of the same right.",
        check=_no_naked_shorts,
    ),
    InvariantCheck(
        name="universe_snapshot_called",
        description="Agent must call get_universe_snapshot before proposing.",
        check=_universe_snapshot_called,
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# Scenario A — HIGH_IV_NEUTRAL (base SPY/AAPL/NVDA mock universe)
# ──────────────────────────────────────────────────────────────────────────────


def _nvda_not_opened(proposal: TradeProposal, _calls: list[str]) -> bool:
    """Agent must never open a position on NVDA (iv_rank=None → ineligible)."""
    if proposal.underlying == "NVDA" and proposal.action == "OPEN":
        return False
    return True


def _spy_credit_spread_or_no_action(proposal: TradeProposal, _calls: list[str]) -> bool:
    """Preference: SPY high-IV should drive a credit structure or NO_ACTION."""
    if proposal.action == "NO_ACTION":
        return True
    if proposal.underlying == "SPY":
        return proposal.strategy in _PLAYBOOK.high_iv_strategies
    return True  # AAPL/NVDA are expected to be skipped


def _chain_drilled_in(proposal: TradeProposal, calls: list[str]) -> bool:
    """Preference: agent must have drilled into at least one chain before proposing."""
    return TOOL_GET_FILTERED_CHAIN in calls


_CHAIN_DESC = "Agent must fetch the chain before committing to a specific structure."

SCENARIO_A = EvalScenario(
    id="A_high_iv_neutral",
    description=(
        "SPY/AAPL/NVDA universe: SPY is clean and high-IV; AAPL has earnings in "
        "5 days; NVDA has iv_rank=None. Agent should trade SPY with a credit "
        "structure, skip AAPL and NVDA."
    ),
    tool_impls=_BASE_TOOL_IMPLS,
    invariants=[
        *_BASE_INVARIANTS,
        InvariantCheck(
            name="nvda_not_opened",
            description="NVDA (iv_rank=None) must never be traded.",
            check=_nvda_not_opened,
        ),
    ],
    preferences=[
        PreferenceCheck(
            name="spy_credit_spread",
            description="SPY high IV → agent should choose a credit spread strategy.",
            check=_spy_credit_spread_or_no_action,
            min_pass_rate=0.8,  # 4 of 5 runs
        ),
        PreferenceCheck(
            name="chain_drilled_in",
            description=_CHAIN_DESC,
            check=_chain_drilled_in,
            min_pass_rate=1.0,  # should always drill in
        ),
    ],
)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario B — LOW_IV_BULLISH (QQQ only, iv_rank=12, vix=12.5)
# ──────────────────────────────────────────────────────────────────────────────


def _low_iv_strategy_or_no_action(proposal: TradeProposal, _calls: list[str]) -> bool:
    """Strategy must be in low_iv_strategies or NO_ACTION — never a credit spread."""
    if proposal.action == "NO_ACTION":
        return True
    return proposal.strategy in _PLAYBOOK.low_iv_strategies


def _debit_spread_preferred(proposal: TradeProposal, _calls: list[str]) -> bool:
    """bull_call_spread is the natural fit for bullish regime + low IV."""
    if proposal.action == "NO_ACTION":
        return False  # model should trade — low IV ≠ skip
    return proposal.strategy == "bull_call_spread"


SCENARIO_B = EvalScenario(
    id="B_low_iv_bullish",
    description=(
        "QQQ-only universe: iv_rank=12 (low band, < 25th percentile), bullish "
        "regime, VIX=12.5 (low-vol), no events, flat portfolio. Agent should "
        "choose a debit spread (buy premium), not sell premium. Without this "
        "scenario, a prompt regression that makes the agent always sell premium "
        "would pass Scenario A."
    ),
    tool_impls=make_low_iv_bullish_tool_impls(),
    invariants=[
        *_BASE_INVARIANTS,
        InvariantCheck(
            name="low_iv_strategy_only",
            description=(
                "In the low-IV band, strategy must be in low_iv_strategies "
                "(bear_put_spread or bull_call_spread) or NO_ACTION. "
                "A credit spread here is a playbook violation."
            ),
            check=_low_iv_strategy_or_no_action,
        ),
    ],
    preferences=[
        PreferenceCheck(
            name="bull_call_spread_preferred",
            description=(
                "Bullish regime + low IV → bull_call_spread is the canonical fit."
            ),
            check=_debit_spread_preferred,
            min_pass_rate=0.6,  # 3 of 5; bear_put_spread also valid
        ),
        PreferenceCheck(
            name="chain_drilled_in",
            description=_CHAIN_DESC,
            check=_chain_drilled_in,
            min_pass_rate=1.0,
        ),
    ],
)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario C — EARNINGS_BLACKOUT (AAPL only, earnings in 5 days)
# ──────────────────────────────────────────────────────────────────────────────

# AAPL earnings date from the mock: 2026-06-19; _AS_OF=2026-06-14 → 5 days out
_AAPL_EARNINGS_DATE = "2026-06-19"


def _no_open_within_blackout(proposal: TradeProposal, _calls: list[str]) -> bool:
    """If action=OPEN, no leg may expire on or before the earnings date.

    A strict read of the system prompt means the validator will reject any OPEN
    within the blackout window anyway — this catches the case where the model
    proposes it despite knowing the constraint.
    """
    if proposal.action != "OPEN":
        return True
    from datetime import date

    earnings = date.fromisoformat(_AAPL_EARNINGS_DATE)
    for leg in proposal.legs:
        if leg.expiration <= earnings:
            return False
    return True


def _no_action_for_aapl(proposal: TradeProposal, _calls: list[str]) -> bool:
    """Model should proactively propose NO_ACTION — not rely on validator rejection."""
    return proposal.action == "NO_ACTION"


SCENARIO_C = EvalScenario(
    id="C_earnings_blackout",
    description=(
        "AAPL-only universe: iv_rank=71 (high band, attractive), but confirmed "
        "earnings in 5 days within the event_blackout_days window. Agent must "
        "recognise earnings proximity and decline rather than proposing a trade "
        "the validator will reject. Tests whether catalyst_check reasoning is "
        "functional, not just present."
    ),
    tool_impls=make_earnings_blackout_tool_impls(),
    invariants=[
        *_BASE_INVARIANTS,
        InvariantCheck(
            name="no_open_within_blackout",
            description=(
                "If action=OPEN, no leg may expire on or before the AAPL earnings "
                "date (2026-06-19). A failing leg means catalyst_check reasoning "
                "did not work."
            ),
            check=_no_open_within_blackout,
        ),
    ],
    preferences=[
        PreferenceCheck(
            name="proactive_no_action",
            description=(
                "Agent should proactively propose NO_ACTION. A correct prompt "
                "produces NO_ACTION here on every run — this is a strong "
                "preference approaching an invariant."
            ),
            check=_no_action_for_aapl,
            min_pass_rate=0.8,  # 4 of 5; rare valid post-earnings expiry allowed
        ),
    ],
)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario D — NO_IV_HISTORY (NVDA only, iv_rank=None)
# ──────────────────────────────────────────────────────────────────────────────


def _no_action_required(proposal: TradeProposal, _calls: list[str]) -> bool:
    """iv_rank=None → action MUST be NO_ACTION. System prompt is explicit."""
    return proposal.action == "NO_ACTION"


def _iv_rationale_mentions_unavailable(
    proposal: TradeProposal, _calls: list[str]
) -> bool:
    """iv_rationale should explain the absence of IV data, not fabricate a value."""
    text = proposal.iv_rationale.lower()
    return any(
        w in text
        for w in (
            "none",
            "unavailable",
            "insufficient",
            "unknown",
            "warm-up",
            "warmup",
            "no iv",
            "null",
        )
    )


SCENARIO_D = EvalScenario(
    id="D_no_iv_history",
    description=(
        "NVDA-only universe: iv_rank=None (warm-up period — insufficient IV "
        "history). The system prompt is unambiguous: NO_ACTION is mandatory "
        "when iv_rank is None. This is the hardest invariant to violate with "
        "a correct prompt."
    ),
    tool_impls=make_no_iv_history_tool_impls(),
    invariants=[
        *_BASE_INVARIANTS,
        InvariantCheck(
            name="no_action_mandatory",
            description=(
                "iv_rank=None → action must be NO_ACTION. The system prompt says "
                "this explicitly. A violation means the model ignored the constraint "
                "or the prompt regression removed it."
            ),
            check=_no_action_required,
        ),
    ],
    preferences=[
        PreferenceCheck(
            name="iv_rationale_explains_absence",
            description=(
                "iv_rationale should explain the lack of IV data rather than "
                "fabricate a value. Mentioning 'None', 'unavailable', etc. "
                "confirms the model understood why it declined."
            ),
            check=_iv_rationale_mentions_unavailable,
            min_pass_rate=0.6,  # 3 of 5 — wording varies legitimately
        ),
    ],
)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario E — PORTFOLIO_AWARE (SPY only, existing position at 50% profit)
# ──────────────────────────────────────────────────────────────────────────────


def _portfolio_state_was_consulted(_proposal: TradeProposal, calls: list[str]) -> bool:
    return TOOL_GET_PORTFOLIO_STATE in calls


def _informed_by_nonempty(proposal: TradeProposal, _calls: list[str]) -> bool:
    """informed_by populated → agent referenced a past journal/position record."""
    return bool(proposal.informed_by)


def _thesis_mentions_existing(proposal: TradeProposal, _calls: list[str]) -> bool:
    """Softer check: thesis or iv_rationale acknowledges the existing position."""
    combined = (proposal.thesis + " " + proposal.iv_rationale).lower()
    return any(
        w in combined
        for w in ("existing", "open position", "pos-001", "bull_put", "held")
    )


SCENARIO_E = EvalScenario(
    id="E_portfolio_aware",
    description=(
        "SPY-only universe: iv_rank=62 (high band, neutral), with an existing "
        "open SPY bull_put_spread at exactly the 50% profit target. Journal has "
        "the opening record. Tests whether the agent uses portfolio context: "
        "consults portfolio state, references journal history in informed_by, "
        "and reasons about the existing position when deciding the next action."
    ),
    tool_impls=make_portfolio_aware_tool_impls(),
    invariants=[
        *_BASE_INVARIANTS,
        InvariantCheck(
            name="portfolio_state_consulted",
            description=(
                "Agent must call get_portfolio_state to see the existing position."
            ),
            check=_portfolio_state_was_consulted,
        ),
    ],
    preferences=[
        PreferenceCheck(
            name="informed_by_populated",
            description=(
                "informed_by should reference the opening journal record or position "
                "ID, demonstrating the agent looked at history before proposing."
            ),
            check=_informed_by_nonempty,
            min_pass_rate=0.4,  # 2 of 5 — aspirational; calibrate after baseline
        ),
        PreferenceCheck(
            name="thesis_mentions_existing_position",
            description=(
                "Thesis or iv_rationale should acknowledge the live SPY position."
            ),
            check=_thesis_mentions_existing,
            min_pass_rate=0.4,
        ),
    ],
)


# ──────────────────────────────────────────────────────────────────────────────
# Exported scenario list — ordered by increasing complexity
# ──────────────────────────────────────────────────────────────────────────────

EVAL_SCENARIOS: list[EvalScenario] = [
    SCENARIO_A,
    SCENARIO_B,
    SCENARIO_C,
    SCENARIO_D,
    SCENARIO_E,
]
