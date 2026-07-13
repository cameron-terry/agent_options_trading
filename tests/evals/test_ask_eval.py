"""Tier-2 eval — calls the real Anthropic API to exercise WP-9.8's
"conventions honored" acceptance criterion:

    Conventions honored in answers (fixture: a question whose naive answer
    breaks the null-iv_rank or open/closed rule)

Unlike agent/ask.py's guardrail logic (deterministic, unit-tested in
options_agent/tests/test_sql_guard.py and test_ask.py with a mocked LLM),
whether the *model* actually honors the null-iv_rank / hit-definition /
open-closed conventions baked into agent/ask_prompts.py is a real-model
behaviour question — no mocked-LLM test can validate it. This suite is the
same tier-2 pattern as tests/evals/test_prompt_eval.py: NOT run in default
CI, exercised deliberately when agent/ask_prompts.py or agent/ask.py's
citation/answer contract changes.

How to run:
    uv run pytest tests/evals/test_ask_eval.py -m eval -v -s

Requires ANTHROPIC_API_KEY in the environment (see conftest.py).

Both scenarios below are treated as invariants (0 tolerance over K runs), not
rate-based preferences — a naive answer here is not a stylistic miss, it's
the exact contract violation the acceptance criterion was written to
prevent: a NULL iv_rank asserted as a number, or a still-open position's
partial-close proceeds folded into a hit rate as if the trade were decided.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from options_agent.agent.ask import ask
from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeEventType,
    OutcomeRecord,
)
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.state import (
    ActionTaken,
    ContextSnapshot,
    Decision,
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.state.crud import insert_position
from options_agent.state.db import build_engine, get_connection, metadata
from options_agent.state.journal import write_journal_record, write_outcome_record

from .conftest import EVAL_RUNS_PER_SCENARIO

_NOW = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)


def _proposal(*, underlying: str) -> TradeProposal:
    expiration = (_NOW + timedelta(days=30)).date()
    return TradeProposal(
        action="OPEN",
        underlying=underlying,
        strategy="bull_put_spread",
        legs=[
            Leg(right="put", side="sell", strike=100.0, expiration=expiration),
            Leg(right="put", side="buy", strike=95.0, expiration=expiration),
        ],
        thesis="Fixture proposal for the ask() tier-2 eval — content unused.",
        iv_rationale="Fixture proposal for the ask() tier-2 eval — content unused.",
        catalyst_check="No confirmed events.",
        conviction=0.6,
        est_max_loss=365.0,
        est_max_profit=135.0,
        breakevens=[98.65],
        net_delta=0.2,
        net_theta=5.0,
        net_vega=-0.2,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )


@pytest.fixture()
def eval_db_url(tmp_path) -> Iterator[str]:
    """A scratch SQLite file, not :memory: — ask() opens its own read-only
    engine via a caller-supplied Connection, and a file-backed DB lets us
    build it with a writable engine first, matching production shape.
    """
    db_path = tmp_path / "ask_eval.db"
    url = f"sqlite:///{db_path}"
    engine = build_engine(url)
    metadata.create_all(engine)
    engine.dispose()
    yield url


def _ro_connection(url: str):
    engine = build_engine(url, read_only=True)
    return get_connection(engine)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario A — null iv_rank must not be reported as a number
# ──────────────────────────────────────────────────────────────────────────────


def _seed_null_iv_rank(url: str) -> None:
    engine = build_engine(url)
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            JournalRecord(
                cycle_id="c-eval-warmup-1",
                timestamp=_NOW,
                action_taken=ActionTaken.OPENED,
                decision=Decision(
                    proposal=_proposal(underlying="SPY"),
                    validation_result=None,
                    sizing_result=None,
                    action_taken=ActionTaken.OPENED,
                ),
                context_snapshot=ContextSnapshot(
                    assembled_context={},
                    context_hash="sha256:eval-warmup",
                    model_id="claude-sonnet-5",
                    prompt_version="eval",
                    assembled_at=_NOW,
                ),
                strategy="bull_put_spread",
                underlying="SPY",
                conviction=0.6,
                iv_rank_at_open=None,
                limits_version="eval",
                prompt_version="eval",
                model_id="claude-sonnet-5",
            ),
        )
    engine.dispose()


# Requires an explicit value-assignment pattern ("iv_rank ... is/was/of/= N")
# rather than any digit within N chars of "iv rank" — a naive digit-proximity
# check false-positives on digits embedded elsewhere nearby, e.g. the "1" in
# the cycle_id "c-eval-warmup-1" itself (caught when this eval was first run
# against the real API: every correct, convention-honoring answer was
# flagged as a violation because it names the cycle_id right next to "NULL").
_NUMERIC_IV_RANK_CLAIM = re.compile(
    r"iv[\s_]*rank\w*\b[^.\n]{0,30}?\b(?:is|was|of|=|:)\s*(-?\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_NULL_KEYWORDS = (
    "null",
    "unknown",
    "insufficient",
    "warm-up",
    "warmup",
    "not available",
    "no iv rank",
    "n/a",
    "no data",
    "not confirmed",
    "no recorded",
)


def _claims_numeric_iv_rank(answer_text: str) -> bool:
    return _NUMERIC_IV_RANK_CLAIM.search(answer_text) is not None


def _acknowledges_null_iv_rank(answer_text: str) -> bool:
    lowered = answer_text.lower()
    return any(kw in lowered for kw in _NULL_KEYWORDS)


@pytest.mark.eval
def test_null_iv_rank_never_reported_as_a_number(
    eval_db_url, anthropic_api_key
) -> None:
    _seed_null_iv_rank(eval_db_url)

    violations: list[str] = []
    for run_idx in range(EVAL_RUNS_PER_SCENARIO):
        with _ro_connection(eval_db_url) as conn:
            result = ask(
                "What was the iv_rank_at_open value when cycle c-eval-warmup-1 opened?",
                conn,
            )
        print(f"\n[null-iv-rank] run {run_idx + 1}: {result.answer_text!r}")

        numeric_claim = _claims_numeric_iv_rank(result.answer_text)
        acknowledges_null = _acknowledges_null_iv_rank(result.answer_text)
        if numeric_claim or not acknowledges_null:
            violations.append(
                f"run {run_idx + 1}: numeric_claim={numeric_claim}"
                f" acknowledges_null={acknowledges_null}"
                f" answer={result.answer_text!r}"
            )

    assert not violations, (
        "[null-iv-rank] naive answer violated the convention on"
        f" {len(violations)}/{EVAL_RUNS_PER_SCENARIO} run(s):\n" + "\n".join(violations)
    )


# ──────────────────────────────────────────────────────────────────────────────
# Scenario B — a still-open position's partial-close proceeds must not be
# folded into a hit rate as a completed win/loss
# ──────────────────────────────────────────────────────────────────────────────


def _seed_open_closed_mix(url: str) -> None:
    engine = build_engine(url)
    with get_connection(engine) as conn:
        # Two fully-closed trades: one win, one loss.
        for pos_id, pnl, days_ago in [
            ("c-eval-win", 100.0, 10),
            ("c-eval-loss", -50.0, 8),
        ]:
            opened = _NOW - timedelta(days=days_ago + 5)
            closed_at = _NOW - timedelta(days=days_ago)
            insert_position(
                conn,
                Position(
                    id=pos_id,
                    underlying="QQQ",
                    strategy="bull_put_spread",
                    legs=[
                        PositionLeg(
                            leg=Leg(
                                right="put",
                                side="sell",
                                strike=100.0,
                                expiration=closed_at.date(),
                            ),
                            filled_qty=1,
                            avg_fill_price=1.0,
                            status=LegStatus.CLOSED,
                        )
                    ],
                    quantity=1,
                    entry_net_amount=-100.0,
                    current_mark=0.0,
                    marked_at=closed_at,
                    unrealized_pnl=0.0,
                    realized_pnl=pnl,
                    exit_plan=_EXIT_PLAN,
                    status=PositionStatus.CLOSED,
                    opened_at=opened,
                    closed_at=closed_at,
                    nearest_expiration=closed_at.date(),
                    est_max_loss=200.0,
                    est_max_profit=100.0,
                    opening_order_id=f"ord-{pos_id}",
                ),
            )
            write_outcome_record(
                conn,
                OutcomeRecord(
                    id=f"out-{pos_id}",
                    position_id=pos_id,
                    event_type=OutcomeEventType.FULL_CLOSE,
                    recorded_at=closed_at,
                    contracts_closed=1,
                    realized_pnl=pnl,
                ),
            )
            write_journal_record(
                conn,
                JournalRecord(
                    cycle_id=f"cycle-{pos_id}",
                    timestamp=opened,
                    action_taken=ActionTaken.OPENED,
                    decision=Decision(
                        proposal=_proposal(underlying="QQQ"),
                        validation_result=None,
                        sizing_result=None,
                        action_taken=ActionTaken.OPENED,
                    ),
                    context_snapshot=ContextSnapshot(
                        assembled_context={},
                        context_hash=f"sha256:{pos_id}",
                        model_id="claude-sonnet-5",
                        prompt_version="eval",
                        assembled_at=opened,
                    ),
                    position_ids=[pos_id],
                    strategy="bull_put_spread",
                    underlying="QQQ",
                    conviction=0.6,
                    limits_version="eval",
                    prompt_version="eval",
                    model_id="claude-sonnet-5",
                ),
            )

        # One still-open position: only a PARTIAL_CLOSE, never FULL_CLOSE.
        # A naive hit-rate answer that treats this as a 3rd completed trade
        # (with the partial-close's positive P&L counted as a win) is
        # exactly the open/closed-separation violation this scenario tests.
        opened = _NOW - timedelta(days=3)
        insert_position(
            conn,
            Position(
                id="c-eval-open",
                underlying="QQQ",
                strategy="bull_put_spread",
                legs=[
                    PositionLeg(
                        leg=Leg(
                            right="put",
                            side="sell",
                            strike=100.0,
                            expiration=(opened + timedelta(days=30)).date(),
                        ),
                        filled_qty=1,
                        avg_fill_price=1.0,
                        status=LegStatus.OPEN,
                    )
                ],
                quantity=1,
                entry_net_amount=-100.0,
                current_mark=-70.0,
                marked_at=_NOW,
                unrealized_pnl=30.0,
                realized_pnl=None,
                exit_plan=_EXIT_PLAN,
                status=PositionStatus.OPEN,
                opened_at=opened,
                closed_at=None,
                nearest_expiration=(opened + timedelta(days=30)).date(),
                est_max_loss=200.0,
                est_max_profit=100.0,
                opening_order_id="ord-c-eval-open",
            ),
        )
        write_outcome_record(
            conn,
            OutcomeRecord(
                id="out-c-eval-open-partial",
                position_id="c-eval-open",
                event_type=OutcomeEventType.PARTIAL_CLOSE,
                recorded_at=_NOW - timedelta(hours=6),
                contracts_closed=1,
                realized_pnl=30.0,
            ),
        )
        write_journal_record(
            conn,
            JournalRecord(
                cycle_id="cycle-c-eval-open",
                timestamp=opened,
                action_taken=ActionTaken.OPENED,
                decision=Decision(
                    proposal=_proposal(underlying="QQQ"),
                    validation_result=None,
                    sizing_result=None,
                    action_taken=ActionTaken.OPENED,
                ),
                context_snapshot=ContextSnapshot(
                    assembled_context={},
                    context_hash="sha256:c-eval-open",
                    model_id="claude-sonnet-5",
                    prompt_version="eval",
                    assembled_at=opened,
                ),
                position_ids=["c-eval-open"],
                strategy="bull_put_spread",
                underlying="QQQ",
                conviction=0.6,
                limits_version="eval",
                prompt_version="eval",
                model_id="claude-sonnet-5",
            ),
        )
    engine.dispose()


_STILL_OPEN_KEYWORDS = (
    "still open",
    "still-open",
    "not fully closed",
    "not yet closed",
    "remains open",
    "open position",
    "hasn't closed",
    "has not closed",
    "not closed",
)


def _acknowledges_still_open_position(answer_text: str) -> bool:
    lowered = answer_text.lower()
    return any(kw in lowered for kw in _STILL_OPEN_KEYWORDS)


# Denominator-3 (e.g. "2/3", "1 out of 3") or "3 closed/completed" — deliberately
# narrow, not "any '3' near the word trades", which false-positives on legitimate
# inventory mentions like "all 3 positions" or "trades across all 3 underlyings"
# (caught when this eval was first run against the real API: every
# convention-honoring answer that opened with a 3-position inventory summary
# before narrowing to the 2 closed trades used for the hit rate got flagged).
_DENOMINATOR_THREE = re.compile(r"\b[0-3]\s*(?:/|out of)\s*3\b", re.IGNORECASE)
_THREE_CLOSED_OR_COMPLETED = re.compile(
    r"\b3\b\s*(?:fully[\s-]?closed|closed|completed)\b", re.IGNORECASE
)


def _claims_three_completed_trades(answer_text: str) -> bool:
    return bool(_DENOMINATOR_THREE.search(answer_text)) or bool(
        _THREE_CLOSED_OR_COMPLETED.search(answer_text)
    )


@pytest.mark.eval
def test_still_open_position_excluded_from_hit_rate(
    eval_db_url, anthropic_api_key
) -> None:
    _seed_open_closed_mix(eval_db_url)

    violations: list[str] = []
    for run_idx in range(EVAL_RUNS_PER_SCENARIO):
        with _ro_connection(eval_db_url) as conn:
            result = ask(
                "What's the hit rate for bull_put_spread trades on QQQ,"
                " including the position opened 3 days ago?",
                conn,
            )
        print(f"\n[open-closed] run {run_idx + 1}: {result.answer_text!r}")

        claims_three = _claims_three_completed_trades(result.answer_text)
        acknowledges_open = _acknowledges_still_open_position(result.answer_text)
        if claims_three or not acknowledges_open:
            violations.append(
                f"run {run_idx + 1}: claims_three_completed={claims_three}"
                f" acknowledges_still_open={acknowledges_open}"
                f" answer={result.answer_text!r}"
            )

    assert not violations, (
        "[open-closed] naive answer violated the convention on"
        f" {len(violations)}/{EVAL_RUNS_PER_SCENARIO} run(s):\n" + "\n".join(violations)
    )
