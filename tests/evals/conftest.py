"""Tier-2 prompt eval configuration.

This eval suite hits the real Anthropic API and is excluded from the default
CI run (testpaths in pyproject.toml points to options_agent/tests only).

How to run:
    uv run pytest tests/evals/ -m eval -v          # all scenarios
    uv run pytest tests/evals/ -m eval -k A_high   # one scenario
    uv run pytest tests/evals/ -m eval -s           # show pass-rate output

Requires ANTHROPIC_API_KEY in the environment.

Cost note:
    Each scenario runs K=5 times through a multi-turn tool loop (Sonnet 4.6).
    Budget ~$0.10–0.50 per eval run across all 5 scenarios depending on
    response length. Run deliberately, not on every push.

Trigger guidance (from WP-6.5 design decisions):
    - Any PR touching agent/prompts.py, the PlaybookConfig, or reasoner.py
      should include a tier-2 eval run as part of review.
    - The first successful run establishes the baseline pass rates. Capture
      those rates and use them to calibrate min_pass_rate thresholds in
      eval_scenarios.py.
"""

import os

import pytest

# Number of repetitions per scenario. Start at 5 — raise to 10 once baselines
# are established and if high-variance properties need a wider sample.
EVAL_RUNS_PER_SCENARIO: int = 5


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "eval: prompt eval tests that call the real Anthropic API. "
        "Run explicitly with: pytest tests/evals/ -m eval",
    )


@pytest.fixture(scope="session")
def anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        pytest.skip(
            "ANTHROPIC_API_KEY is not set — skipping live prompt eval. "
            "Export ANTHROPIC_API_KEY=sk-ant-... and re-run."
        )
    return key
