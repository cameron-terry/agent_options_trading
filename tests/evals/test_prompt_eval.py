"""Tier-2 prompt eval — calls the real Anthropic API.

Each test runs one EvalScenario K times against reason() and asserts:
  - Invariants hold on 100% of runs (zero tolerance).
  - Preferences hold at >= their stated min_pass_rate.

NOT for every-push CI. Run explicitly when prompt or playbook changes:
    uv run pytest tests/evals/ -m eval -v

See tests/evals/conftest.py for setup and cost notes.

Sequencing:
    This eval is meaningless until WP-6.4 (PR #50) merges — reason() is a
    NotImplementedError stub until then. The first successful run establishes
    the prompt baseline. Capture per-property pass rates from the -v output
    and calibrate min_pass_rate thresholds in eval_scenarios.py accordingly.

Invariant violation format:
    When an invariant fails, the assertion message includes the run index and
    the full proposal so the regression is diagnosable without re-running.

Preference report format:
    When a preference fails, the message shows pass_count/K and the threshold,
    so you know how far off the baseline the regression drove the rate.
"""

from __future__ import annotations

import pytest

from options_agent.agent.eval_scenarios import (
    EVAL_SCENARIOS,
    EvalScenario,
    make_spy_tool_impls,
)
from options_agent.agent.prompts import build_system_prompt
from options_agent.config import Config
from options_agent.contracts.proposal import TradeProposal

from .conftest import EVAL_RUNS_PER_SCENARIO

# ──────────────────────────────────────────────────────────────────────────────
# reason() import — skip gracefully until WP-6.4 merges
# ──────────────────────────────────────────────────────────────────────────────

try:
    from options_agent.agent.reasoner import reason as _reason

    def _call_reason(*args, **kwargs) -> TradeProposal:  # type: ignore[no-untyped-def]
        result = _reason(*args, **kwargs)
        if isinstance(result, TradeProposal):
            return result
        raise TypeError(f"reason() returned {type(result)!r}, expected TradeProposal")

except NotImplementedError:
    _call_reason = None  # type: ignore[assignment]


def _get_reason():  # type: ignore[no-untyped-def]
    if _call_reason is None:
        pytest.skip(
            "reason() raises NotImplementedError — WP-6.4 (PR #50) has not merged yet. "
            "Tier-2 eval will run once that PR lands."
        )
    try:
        _call_reason.__self__  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return _call_reason


# ──────────────────────────────────────────────────────────────────────────────
# System prompt — built once per session from the default config
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def system_prompt() -> str:
    config = Config()
    return build_system_prompt(config.playbook, config.limits)


# ──────────────────────────────────────────────────────────────────────────────
# Core eval runner
# ──────────────────────────────────────────────────────────────────────────────


def _run_scenario(
    scenario: EvalScenario,
    system_prompt: str,
    k: int,
    _api_key: str,  # forces dependency on anthropic_api_key fixture (skip guard)
) -> None:
    reason = _get_reason()

    proposals: list[TradeProposal] = []
    all_tool_calls: list[list[str]] = []

    for run_idx in range(k):
        spy_impls, calls = make_spy_tool_impls(scenario.tool_impls)
        proposal = reason(
            tool_impls=spy_impls,
            system_prompt=system_prompt,
        )
        proposals.append(proposal)
        all_tool_calls.append(list(calls))

    # ── Invariant checks (0 tolerance) ──────────────────────────────────────
    for inv in scenario.invariants:
        violations = [
            (idx, proposals[idx], all_tool_calls[idx])
            for idx in range(k)
            if not inv.check(proposals[idx], all_tool_calls[idx])
        ]
        assert not violations, (
            f"[{scenario.id}] INVARIANT '{inv.name}' VIOLATED on "
            f"{len(violations)}/{k} run(s).\n"
            f"Description: {inv.description}\n"
            + "\n".join(
                f"  Run {idx}: action={p.action!r}, strategy={p.strategy!r}, "
                f"underlying={p.underlying!r}, tools_called={tc}"
                for idx, p, tc in violations
            )
        )

    # ── Preference checks (rate-based) ───────────────────────────────────────
    for pref in scenario.preferences:
        results = [pref.check(proposals[i], all_tool_calls[i]) for i in range(k)]
        pass_count = sum(results)
        pass_rate = pass_count / k

        # Report all runs regardless of pass/fail for baseline calibration
        run_summary = ", ".join(
            f"run{i}={'PASS' if results[i] else 'fail'}" for i in range(k)
        )
        print(
            f"\n[{scenario.id}] PREFERENCE '{pref.name}': "
            f"{pass_count}/{k} ({pass_rate:.0%}) "
            f"threshold={pref.min_pass_rate:.0%} — {run_summary}"
        )

        assert pass_rate >= pref.min_pass_rate, (
            f"[{scenario.id}] PREFERENCE '{pref.name}' below threshold: "
            f"{pass_count}/{k} ({pass_rate:.0%}) < required {pref.min_pass_rate:.0%}.\n"
            f"Description: {pref.description}\n"
            + "\n".join(
                f"  Run {i}: action={proposals[i].action!r},"
                f" strategy={proposals[i].strategy!r},"
                f" tools_called={all_tool_calls[i]}"
                for i in range(k)
            )
        )


# ──────────────────────────────────────────────────────────────────────────────
# Parametrised tests — one test per scenario
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.eval
@pytest.mark.parametrize(
    "scenario",
    EVAL_SCENARIOS,
    ids=[s.id for s in EVAL_SCENARIOS],
)
def test_scenario(
    scenario: EvalScenario,
    system_prompt: str,
    anthropic_api_key: str,
) -> None:
    """Run one eval scenario K times and assert invariants + preferences."""
    _run_scenario(
        scenario=scenario,
        system_prompt=system_prompt,
        k=EVAL_RUNS_PER_SCENARIO,
        _api_key=anthropic_api_key,
    )
