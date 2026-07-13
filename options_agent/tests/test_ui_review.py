"""Tests for WP-9.5: Performance & bias API (/api/review/*)."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from options_agent.config import Config
from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeEventType,
    OutcomeRecord,
)
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import ValidationRuleId
from options_agent.contracts.state import (
    ActionTaken,
    AssetClass,
    ContextSnapshot,
    Decision,
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.obs.review import (
    cycle_funnel,
    detect_bias,
    hit_rate_by_strategy,
    pnl_attribution,
)
from options_agent.risk.limits import Limits
from options_agent.state.crud import insert_position
from options_agent.state.db import build_engine, get_connection, metadata
from options_agent.state.journal import (
    query_journal,
    query_outcome_records,
    write_journal_record,
    write_outcome_record,
)
from options_agent.ui.app import create_app

_NOW = datetime(2026, 7, 12, 14, 0, 0, tzinfo=UTC)
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)


def _proposal(underlying: str, *, net_delta: float) -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying=underlying,
        strategy="bull_put_spread",
        legs=[
            Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15))
        ],
        thesis="thesis",
        iv_rationale="iv rationale",
        catalyst_check="no catalyst",
        conviction=0.7,
        est_max_loss=2225.0,
        est_max_profit=275.0,
        breakevens=[447.50],
        net_delta=net_delta,
        net_theta=8.50,
        net_vega=-0.30,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )


def _journal_record(
    *,
    cycle_id: str,
    action: ActionTaken,
    underlying: str | None = None,
    strategy: str | None = None,
    timestamp: datetime = _NOW,
    position_ids: list[str] | None = None,
    rejection_rule_ids: list[ValidationRuleId] | None = None,
    net_delta_at_open: float | None = None,
    earnings_within_dte: bool | None = None,
    prompt_version: str = "v1.0.0",
) -> JournalRecord:
    context_snapshot = ContextSnapshot(
        assembled_context={},
        context_hash="sha256:abcdef1234567890",
        model_id="claude-sonnet-4-6",
        prompt_version=prompt_version,
        assembled_at=timestamp,
    )
    decision = Decision(
        proposal=_proposal(underlying or "SPY", net_delta=net_delta_at_open or 0.0)
        if action == ActionTaken.OPENED
        else None,
        validation_result=None,
        sizing_result=None,
        action_taken=action,
    )
    return JournalRecord(
        cycle_id=cycle_id,
        timestamp=timestamp,
        action_taken=action,
        decision=decision,
        context_snapshot=context_snapshot,
        position_ids=position_ids or [],
        order_ids=[],
        strategy=strategy,
        underlying=underlying,
        limits_version="v1.0.0",
        prompt_version=prompt_version,
        model_id="claude-sonnet-4-6",
        rejection_rule_ids=rejection_rule_ids or [],
        net_delta_at_open=net_delta_at_open,
        earnings_within_dte=earnings_within_dte,
    )


def _position(*, position_id: str, underlying: str, strategy: str) -> Position:
    return Position(
        id=position_id,
        underlying=underlying,
        strategy=strategy,
        legs=[
            PositionLeg(
                leg=Leg(
                    right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15)
                ),
                filled_qty=1,
                avg_fill_price=0.55,
                status=LegStatus.OPEN,
            )
        ],
        quantity=1,
        entry_net_amount=-275.0,
        current_mark=-150.0,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=PositionStatus.CLOSED,
        opened_at=_NOW,
        closed_at=_NOW,
        nearest_expiration=date(2026, 8, 15),
        est_max_loss=2225.0,
        est_max_profit=275.0,
        opening_order_id="open-ord-001",
        asset_class=AssetClass.OPTION_STRATEGY,
        equity_legs=[],
    )


def _outcome(
    *,
    id: str,
    position_id: str,
    realized_pnl: float,
    event_type: OutcomeEventType = OutcomeEventType.FULL_CLOSE,
    recorded_at: datetime = _NOW,
) -> OutcomeRecord:
    return OutcomeRecord(
        id=id,
        position_id=position_id,
        event_type=event_type,
        recorded_at=recorded_at,
        contracts_closed=2,
        realized_pnl=realized_pnl,
        fill_price=-0.47,
        closing_order_id=f"ord-close-{id}",
    )


@pytest.fixture
def engine():
    eng = build_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def seeded(engine):
    """A small, deliberately mixed journal: two closed winners/losers, four
    rejections across two rules, and gated/no-action/sized-to-zero/exec-failed
    cycles so every funnel stage is non-zero."""
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _journal_record(
                cycle_id="c-open-spy",
                action=ActionTaken.OPENED,
                underlying="SPY",
                strategy="bull_put_spread",
                position_ids=["pos-spy"],
                net_delta_at_open=0.30,
                earnings_within_dte=False,
                prompt_version="v1.0.0",
            ),
        )
        write_journal_record(
            conn,
            _journal_record(
                cycle_id="c-open-qqq",
                action=ActionTaken.OPENED,
                underlying="QQQ",
                strategy="iron_condor",
                position_ids=["pos-qqq"],
                net_delta_at_open=0.10,
                earnings_within_dte=True,
                prompt_version="v2.0.0",
            ),
        )
        write_journal_record(
            conn,
            _journal_record(
                cycle_id="c-rej-1",
                action=ActionTaken.REJECTED,
                underlying="AAPL",
                rejection_rule_ids=[
                    ValidationRuleId.EVENT_BLACKOUT,
                    ValidationRuleId.LIQUIDITY_SPREAD,
                ],
            ),
        )
        write_journal_record(
            conn,
            _journal_record(
                cycle_id="c-rej-2",
                action=ActionTaken.REJECTED,
                underlying="AAPL",
                rejection_rule_ids=[ValidationRuleId.EVENT_BLACKOUT],
            ),
        )
        write_journal_record(
            conn,
            _journal_record(cycle_id="c-gated", action=ActionTaken.NO_ACTION_GATED),
        )
        write_journal_record(
            conn,
            _journal_record(
                cycle_id="c-no-action",
                action=ActionTaken.NO_ACTION_AGENT,
                underlying="NVDA",
            ),
        )
        write_journal_record(
            conn,
            _journal_record(
                cycle_id="c-sized-zero",
                action=ActionTaken.SIZED_TO_ZERO,
                underlying="MSFT",
            ),
        )
        write_journal_record(
            conn,
            _journal_record(
                cycle_id="c-exec-failed",
                action=ActionTaken.EXECUTION_FAILED,
                underlying="TSLA",
            ),
        )

        insert_position(
            conn,
            _position(
                position_id="pos-spy", underlying="SPY", strategy="bull_put_spread"
            ),
        )
        insert_position(
            conn,
            _position(position_id="pos-qqq", underlying="QQQ", strategy="iron_condor"),
        )
        write_outcome_record(
            conn, _outcome(id="o-spy", position_id="pos-spy", realized_pnl=150.0)
        )
        write_outcome_record(
            conn, _outcome(id="o-qqq", position_id="pos-qqq", realized_pnl=-80.0)
        )
    return engine


def _client(engine, *, bias_min_sample_size: int = 10) -> TestClient:
    config = Config(limits=Limits(bias_min_sample_size=bias_min_sample_size))
    app = create_app(config=config, engine=engine)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------


def test_funnel_matches_cycle_funnel_pure_function(seeded) -> None:
    with get_connection(seeded) as conn:
        records = query_journal(conn, date_from=None)
    expected = cycle_funnel(records, since=None)

    resp = _client(seeded).get("/api/review/funnel")
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == expected.total
    assert body["gated"] == expected.gated
    assert body["reasoned"] == expected.reasoned
    assert body["no_action_agent"] == expected.no_action_agent
    assert body["proposed"] == expected.proposed
    assert body["rejected"] == expected.rejected
    assert body["sized_to_zero"] == expected.sized_to_zero
    assert body["execution_failed"] == expected.execution_failed
    assert body["opened"] == expected.opened


def test_funnel_rejections_by_rule_counts(seeded) -> None:
    body = _client(seeded).get("/api/review/funnel").json()
    counts = {r["rule_id"]: r["count"] for r in body["rejections_by_rule"]}
    assert counts == {"EVENT_BLACKOUT": 2, "LIQUIDITY_SPREAD": 1}


def test_funnel_ignores_prompt_version_matching_cli_behavior(seeded) -> None:
    """cycle_funnel() has no prompt_version filter, and neither does the CLI's
    cmd_review call into it — passing one to the endpoint must be a no-op so
    the parity invariant against the CLI output holds."""
    without = _client(seeded).get("/api/review/funnel").json()
    with_filter = (
        _client(seeded)
        .get("/api/review/funnel", params={"prompt_version": "v1.0.0"})
        .json()
    )
    assert without == with_filter


def test_funnel_since_filter_excludes_earlier_records(seeded) -> None:
    later = _NOW.isoformat()
    resp = _client(seeded).get("/api/review/funnel", params={"since": later})
    body = resp.json()
    assert body["total"] == 8  # all seeded records are at exactly _NOW

    after = "2026-07-12T14:00:01Z"
    resp2 = _client(seeded).get("/api/review/funnel", params={"since": after})
    assert resp2.json()["total"] == 0


# ---------------------------------------------------------------------------
# Hit rate
# ---------------------------------------------------------------------------


def test_hit_rate_matches_pure_function_when_sufficient(seeded) -> None:
    with get_connection(seeded) as conn:
        records = query_journal(conn, date_from=None)
        outcomes = query_outcome_records(conn, position_ids=["pos-spy", "pos-qqq"])
    expected = hit_rate_by_strategy(records, outcomes, since=None, prompt_version=None)

    # min_sample_size=1 so the display gate never suppresses values —
    # isolates the parity check to the underlying numbers.
    body = _client(seeded, bias_min_sample_size=1).get("/api/review/hit-rate").json()

    assert body["overall"]["trade_count"] == expected.overall.trade_count
    assert body["overall"]["hit_count"] == expected.overall.hit_count
    assert body["overall"]["total_pnl"] == pytest.approx(expected.overall.total_pnl)
    assert body["overall"]["hit_rate"] == pytest.approx(expected.overall.hit_rate)
    assert body["overall"]["expectancy"] == pytest.approx(expected.overall.expectancy)

    for strategy, stats in expected.by_strategy.items():
        out = body["by_strategy"][strategy]
        assert out["trade_count"] == stats.trade_count
        assert out["total_pnl"] == pytest.approx(stats.total_pnl)
        assert out["hit_rate"] == pytest.approx(stats.hit_rate)


def test_hit_rate_gates_fields_below_min_sample_size(seeded) -> None:
    # Default bias_min_sample_size=10; only 1-2 closed trades per bucket.
    body = _client(seeded, bias_min_sample_size=10).get("/api/review/hit-rate").json()

    assert body["overall"]["trade_count"] == 2
    assert body["overall"]["sufficient"] is False
    assert body["overall"]["hit_rate"] is None
    assert body["overall"]["avg_win"] is None
    assert body["overall"]["avg_loss"] is None
    assert body["overall"]["expectancy"] is None
    # trade_count and total_pnl are never suppressed by the gate.
    assert body["overall"]["total_pnl"] == pytest.approx(70.0)

    for stats in body["by_strategy"].values():
        assert stats["sufficient"] is False
        assert stats["hit_rate"] is None


def test_hit_rate_prompt_version_filter_passes_through(seeded) -> None:
    body = (
        _client(seeded, bias_min_sample_size=1)
        .get("/api/review/hit-rate", params={"prompt_version": "v1.0.0"})
        .json()
    )
    # Only the SPY (v1.0.0) trade should count; QQQ was v2.0.0.
    assert body["overall"]["trade_count"] == 1
    assert body["overall"]["total_pnl"] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_attribution_matches_pure_function(seeded) -> None:
    with get_connection(seeded) as conn:
        records = query_journal(conn, date_from=None)
        outcomes = query_outcome_records(conn, position_ids=["pos-spy", "pos-qqq"])
    expected = pnl_attribution(records, outcomes, since=None, prompt_version=None)

    body = _client(seeded).get("/api/review/attribution").json()

    assert body["total_realized_pnl"] == pytest.approx(expected.total_realized_pnl)
    for underlying, stats in expected.by_underlying.items():
        out = body["by_underlying"][underlying]
        assert out["net_pnl"] == pytest.approx(stats.net_pnl)
        assert out["trade_count"] == stats.trade_count
    for strategy, stats in expected.by_strategy.items():
        out = body["by_strategy"][strategy]
        assert out["net_pnl"] == pytest.approx(stats.net_pnl)
        assert out["trade_count"] == stats.trade_count


# ---------------------------------------------------------------------------
# Bias
# ---------------------------------------------------------------------------


def test_bias_matches_pure_function_when_sufficient(seeded) -> None:
    with get_connection(seeded) as conn:
        records = query_journal(conn, date_from=None)
        outcomes = query_outcome_records(conn, position_ids=["pos-spy", "pos-qqq"])
    expected = detect_bias(
        records, outcomes, since=None, prompt_version=None, min_sample_size=1
    )

    body = _client(seeded, bias_min_sample_size=1).get("/api/review/bias").json()

    assert body["delta_skew"]["sample_size"] == expected.delta_skew.sample_size
    assert body["delta_skew"]["sufficient"] == expected.delta_skew.sufficient
    assert body["delta_skew"]["mean_net_delta"] == pytest.approx(
        expected.delta_skew.mean_net_delta
    )
    assert body["delta_skew"]["direction"] == expected.delta_skew.direction


def test_bias_insufficient_data_by_default(seeded) -> None:
    # Default bias_min_sample_size=10; only 2 OPENED records with delta set.
    body = _client(seeded, bias_min_sample_size=10).get("/api/review/bias").json()

    assert body["min_sample_size"] == 10
    assert body["delta_skew"]["sufficient"] is False
    assert body["delta_skew"]["mean_net_delta"] is None
    assert body["delta_skew"]["direction"] == "insufficient_data"


# ---------------------------------------------------------------------------
# NaN never reaches the wire — every /api/review/* response must be valid JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/review/funnel",
        "/api/review/hit-rate",
        "/api/review/attribution",
        "/api/review/bias",
    ],
)
def test_no_literal_nan_in_response_body(seeded, path) -> None:
    resp = _client(seeded).get(path)
    assert "NaN" not in resp.text
    # A stricter check: json.loads with the stdlib default (which *does*
    # accept the NaN token) would mask the bug above — parse would still
    # succeed either way, so the substring check is the real assertion.
    parsed = json.loads(resp.text)
    assert not any(isinstance(v, float) and math.isnan(v) for v in _flatten(parsed))


def _flatten(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten(v)
    else:
        yield obj
