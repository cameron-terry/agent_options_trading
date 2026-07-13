"""Seed a scratch SQLite DB with representative console demo data (WP-9).

Populates journal records (today's cycles, spanning OPENED / REJECTED /
NO_ACTION_AGENT / NO_ACTION_GATED, plus one with a deliberately-unresolvable
position link to exercise the anomaly path), open positions across a spread
of distance-to-trigger states, and closed positions/outcomes for the equity
curve and realized-P&L math — enough for every WP-9 console screen (Overview,
Decision explorer, Performance, Ask) to have something real to render.

This is a shared fixture, not a one-off: reuse it for every WP-9.x screen's
docker visual-verification pass instead of hand-rolling a new scratch seed
script per ticket. That keeps demo data consistent across screens (the same
SPY position and c-2026-... cycle appear in Overview's activity feed *and*
the Decision explorer) and prevents regressions from silently narrowing what
gets exercised.

Never point this at the real agent_data volume — it writes fabricated data
via the same insert/write functions the trading loop uses, indistinguishable
from real history once written. Always target a scratch path.

Usage:
    uv run python scripts/seed_console_demo_data.py /tmp/scratch/dev.db
    uv run python scripts/seed_console_demo_data.py /tmp/scratch/dev.db --force

Runs `alembic upgrade head` against the target path first (via DB_URL env
override — see alembic/env.py), then seeds. --force removes an existing file
at the target path before migrating; without it, an existing file is left
alone and alembic runs its normal (idempotent) upgrade against it.

Then point the console's demo container at it. Config is a plain pydantic
BaseModel (not BaseSettings), so DB_URL alone does nothing here — the
container ships no config.toml (Dockerfile.console doesn't COPY one), so
options_agent.ui falls back to hardcoded defaults and silently ignores the
env var. Mount a config.toml with db_url pointing at the mounted path
instead:

    cp config.toml <scratch-dir>/config.toml
    # edit db_url in the copy to: sqlite:////app/demo-data/dev.db
    docker run -d --rm --name console-demo -p 127.0.0.1:8001:8000 \\
      -v <scratch-dir>:/app/demo-data \\
      -v <scratch-dir>/config.toml:/app/config.toml:ro \\
      agent_options_trading-console:latest
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeEventType,
    OutcomeRecord,
)
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import (
    RejectionReason,
    Severity,
    SizingConstraint,
    SizingResult,
    ValidationResult,
    ValidationRuleId,
)
from options_agent.contracts.state import (
    ActionTaken,
    ContextSnapshot,
    Decision,
    KillSwitchState,
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
    ToolCallRecord,
)
from options_agent.obs.killswitch import set_state
from options_agent.state.crud import insert_order, insert_position
from options_agent.state.db import build_engine, get_connection
from options_agent.state.journal import write_journal_record, write_outcome_record

REPO_ROOT = Path(__file__).resolve().parent.parent

NOW = datetime.now(UTC)
EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=7
)


def _migrate(db_path: Path, *, force: bool) -> None:
    if force and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "DB_URL": f"sqlite:///{db_path}"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )


def _bull_put_proposal(
    *, underlying: str, short_strike: float, long_strike: float, dte: int
) -> TradeProposal:
    expiration = (NOW + timedelta(days=dte)).date()
    return TradeProposal(
        action="OPEN",
        underlying=underlying,
        strategy="bull_put_spread",
        legs=[
            Leg(right="put", side="sell", strike=short_strike, expiration=expiration),
            Leg(right="put", side="buy", strike=long_strike, expiration=expiration),
        ],
        thesis=(
            f"{underlying} has held a multi-week consolidation with IV rank rich "
            "relative to realized. Selling the near-the-money put and buying "
            "further out collects premium with the short strike below support."
        ),
        iv_rationale=(
            "IV rank above the 60th percentile vs. realized; no earnings in tenor."
        ),
        catalyst_check="No earnings within the option's DTE window.",
        conviction=0.68,
        est_max_loss=(short_strike - long_strike) * 100 - 135.0,
        est_max_profit=135.0,
        breakevens=[short_strike - 1.35],
        net_delta=0.22,
        net_theta=7.0,
        net_vega=-0.25,
        exit_plan=EXIT_PLAN,
        informed_by=[],
    )


def _transcript() -> list[ToolCallRecord]:
    return [
        ToolCallRecord(
            tool_name="get_portfolio_state",
            tool_input={},
            result_json='{"equity": 51029, "options_bp": 24610, "positions": 3}',
        ),
        ToolCallRecord(
            tool_name="get_universe_snapshot",
            tool_input={},
            result_json=(
                '{"SPY": {"iv_rank": 62.0, "iv_pctile": 71}, '
                '"AAPL": {"days_to_earnings": 4}}'
            ),
        ),
        ToolCallRecord(
            tool_name="get_filtered_chain",
            tool_input={"symbol": "SPY", "strategy": "bull_put_spread"},
            result_json=(
                '{"puts": [{"strike": 530, "bid": 2.45, "ask": 2.52, "delta": -0.24}]}'
            ),
        ),
        ToolCallRecord(
            tool_name="get_events",
            tool_input={"symbols": ["SPY"]},
            result_json='{"SPY": {"earnings": null}}',
        ),
        ToolCallRecord(
            tool_name="get_journal_by_symbol",
            tool_input={"symbol": "SPY"},
            result_json='{"prior_cycles": 7, "opened": 3, "hits": 2}',
        ),
    ]


def _seed_open_position(
    conn,
    *,
    position_id: str,
    order_id: str,
    underlying: str,
    strategy: str,
    short_strike: float,
    long_strike: float,
    quantity: int,
    entry_net_amount: float,
    current_mark: float,
    unrealized_pnl: float,
    dte: int,
) -> None:
    expiration = (NOW + timedelta(days=dte)).date()
    insert_position(
        conn,
        Position(
            id=position_id,
            underlying=underlying,
            strategy=strategy,
            legs=[
                PositionLeg(
                    leg=Leg(
                        right="put",
                        side="sell",
                        strike=short_strike,
                        expiration=expiration,
                    ),
                    filled_qty=quantity,
                    avg_fill_price=abs(entry_net_amount) / 100 / quantity,
                    status=LegStatus.OPEN,
                ),
                PositionLeg(
                    leg=Leg(
                        right="put",
                        side="buy",
                        strike=long_strike,
                        expiration=expiration,
                    ),
                    filled_qty=quantity,
                    avg_fill_price=0.0,
                    status=LegStatus.OPEN,
                ),
            ],
            quantity=quantity,
            entry_net_amount=entry_net_amount,
            current_mark=current_mark,
            marked_at=NOW,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=None,
            exit_plan=EXIT_PLAN,
            status=PositionStatus.OPEN,
            opened_at=NOW - timedelta(days=3),
            closed_at=None,
            nearest_expiration=expiration,
            est_max_loss=abs(entry_net_amount) + 460.0,
            est_max_profit=abs(entry_net_amount),
            opening_order_id=order_id,
        ),
    )
    insert_order(
        conn,
        Order(
            id=order_id,
            broker_order_id=f"broker-{order_id}",
            position_id=position_id,
            role=OrderRole.OPEN,
            status=OrderStatus.FILLED,
            broker_status_raw="filled",
            submitted_at=NOW - timedelta(days=3),
            filled_at=NOW - timedelta(days=3),
            # Same sign convention as Position.entry_net_amount (positive =
            # debit paid, negative = credit received) — see execution/broker.py's
            # limit_price docstring ("positive = debit, negative = credit").
            limit_price=entry_net_amount / 100 / quantity,
            legs_filled=[],
            net_fill_price=entry_net_amount / 100 / quantity,
            filled_qty=quantity,
        ),
    )


def _seed_closed_history(conn) -> None:
    """A handful of closed positions over the past two weeks — equity curve,
    realized P&L tile, and hit-rate math all need more than one data point."""
    closed = [
        ("pos-hist-1", "QQQ", "iron_condor", 8, 7),
        ("pos-hist-2", "AMD", "bull_put_spread", -25, 6),
        ("pos-hist-3", "AAPL", "cash_secured_put", 62, 3),
        ("pos-hist-4", "MSFT", "bull_put_spread", 118, 10),
        ("pos-hist-5", "SPY", "iron_condor", -40, 12),
    ]
    for pos_id, underlying, strategy, pnl, days_ago in closed:
        opened = NOW - timedelta(days=days_ago + 5)
        closed_at = NOW - timedelta(days=days_ago)
        insert_position(
            conn,
            Position(
                id=pos_id,
                underlying=underlying,
                strategy=strategy,
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
                realized_pnl=float(pnl),
                exit_plan=EXIT_PLAN,
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
                realized_pnl=float(pnl),
            ),
        )


def seed(db_path: Path) -> None:
    engine = build_engine(f"sqlite:///{db_path}")

    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.NONE, set_by="demo-seed", reason="init")

        # --- Open positions spanning the distance-to-trigger spread the
        # Overview screen's mock shows: near-target, mid-target, near-stop,
        # and barely-moved. ---
        _seed_open_position(
            conn,
            position_id="pos-a7f3",
            order_id="ord-3fe2",
            underlying="SPY",
            strategy="bull_put_spread",
            short_strike=530.0,
            long_strike=525.0,
            quantity=2,
            entry_net_amount=-270.0,
            current_mark=-132.0,
            unrealized_pnl=276.0,
            dte=27,
        )
        _seed_open_position(
            conn,
            position_id="pos-qqq1",
            order_id="ord-qqq1",
            underlying="QQQ",
            strategy="iron_condor",
            short_strike=485.0,
            long_strike=480.0,
            quantity=1,
            entry_net_amount=-310.0,
            current_mark=-260.0,
            unrealized_pnl=50.0,
            dte=24,
        )
        _seed_open_position(
            conn,
            position_id="pos-amd1",
            order_id="ord-amd1",
            underlying="AMD",
            strategy="bull_put_spread",
            short_strike=150.0,
            long_strike=145.0,
            quantity=3,
            entry_net_amount=-345.0,
            current_mark=-444.0,
            unrealized_pnl=-99.0,
            dte=31,
        )
        _seed_open_position(
            conn,
            position_id="pos-aapl1",
            order_id="ord-aapl1",
            underlying="AAPL",
            strategy="cash_secured_put",
            short_strike=205.0,
            long_strike=200.0,
            quantity=1,
            entry_net_amount=-420.0,
            current_mark=-395.0,
            unrealized_pnl=25.0,
            dte=38,
        )

        # --- Today's cycles: one of each ActionTaken flavor the Decision
        # explorer and Overview activity feed need to render, anchored to
        # "now" so the Cycles Today tile always has data regardless of when
        # this script runs. ---
        spy_proposal = _bull_put_proposal(
            underlying="SPY", short_strike=530.0, long_strike=525.0, dte=27
        )
        write_journal_record(
            conn,
            JournalRecord(
                cycle_id="c-2026-demo-opened-spy",
                timestamp=NOW,
                action_taken=ActionTaken.OPENED,
                decision=Decision(
                    proposal=spy_proposal,
                    validation_result=ValidationResult(passed=True, reasons=[]),
                    sizing_result=SizingResult(
                        contracts=2,
                        sized_max_loss=730.0,
                        sized_max_profit=270.0,
                        risk_budget_used=0.014,
                        binding_constraint=SizingConstraint.RISK_BUDGET,
                    ),
                    action_taken=ActionTaken.OPENED,
                ),
                context_snapshot=ContextSnapshot(
                    assembled_context={
                        "iv_rank": 62,
                        "portfolio": {
                            "positions": [],
                            "account_equity": 51305.0,
                            "buying_power": 24610.0,
                            "options_buying_power": 24610.0,
                            "unrealized_pnl": 252.0,
                            "realized_pnl_today": 0.0,
                            "approval_level": 2,
                            "net_dollar_delta": 148.0,
                            "net_dollar_gamma": -12.0,
                            "net_dollar_theta": 41.0,
                            "net_dollar_vega": -135.0,
                        },
                    },
                    context_hash="sha256:9f3ac21e",
                    model_id="claude-sonnet-5",
                    prompt_version="v2.1.0",
                    assembled_at=NOW,
                    tool_calls_transcript=_transcript(),
                ),
                position_ids=["pos-a7f3"],
                order_ids=["ord-3fe2"],
                strategy="bull_put_spread",
                underlying="SPY",
                conviction=0.72,
                limits_version="v0.3.0",
                prompt_version="v2.1.0",
                model_id="claude-sonnet-5",
            ),
        )

        write_journal_record(
            conn,
            JournalRecord(
                cycle_id="c-2026-demo-noaction-nvda",
                timestamp=NOW - timedelta(minutes=30),
                action_taken=ActionTaken.NO_ACTION_AGENT,
                decision=Decision(
                    proposal=None,
                    validation_result=None,
                    sizing_result=None,
                    action_taken=ActionTaken.NO_ACTION_AGENT,
                ),
                context_snapshot=ContextSnapshot(
                    assembled_context={},
                    context_hash="sha256:11be22",
                    model_id="claude-sonnet-5",
                    prompt_version="v2.1.0",
                    assembled_at=NOW,
                    tool_calls_transcript=[],
                ),
                strategy=None,
                underlying="NVDA",
                conviction=None,
                limits_version="v0.3.0",
                prompt_version="v2.1.0",
                model_id="claude-sonnet-5",
            ),
        )

        write_journal_record(
            conn,
            JournalRecord(
                cycle_id="c-2026-demo-gated",
                timestamp=NOW - timedelta(minutes=60),
                action_taken=ActionTaken.NO_ACTION_GATED,
                decision=Decision(
                    proposal=None,
                    validation_result=None,
                    sizing_result=None,
                    action_taken=ActionTaken.NO_ACTION_GATED,
                ),
                context_snapshot=ContextSnapshot(
                    assembled_context={},
                    context_hash="sha256:4c0299",
                    model_id="claude-sonnet-5",
                    prompt_version="v2.1.0",
                    assembled_at=NOW,
                    tool_calls_transcript=[],
                ),
                strategy=None,
                underlying=None,
                conviction=None,
                limits_version="v0.3.0",
                prompt_version="v2.1.0",
                model_id="claude-sonnet-5",
            ),
        )

        rejected_proposal = _bull_put_proposal(
            underlying="AAPL", short_strike=205.0, long_strike=200.0, dte=20
        )
        write_journal_record(
            conn,
            JournalRecord(
                cycle_id="c-2026-demo-rejected-aapl",
                timestamp=NOW - timedelta(minutes=90),
                action_taken=ActionTaken.REJECTED,
                decision=Decision(
                    proposal=rejected_proposal,
                    validation_result=ValidationResult(
                        passed=False,
                        reasons=[
                            RejectionReason(
                                rule_id=ValidationRuleId.EVENT_BLACKOUT,
                                severity=Severity.ERROR,
                                human_message=(
                                    "Earnings in 4 days, inside the 5-day blackout "
                                    "window."
                                ),
                                observed=4,
                                limit=5,
                            )
                        ],
                    ),
                    sizing_result=None,
                    action_taken=ActionTaken.REJECTED,
                ),
                context_snapshot=ContextSnapshot(
                    assembled_context={},
                    context_hash="sha256:d21777",
                    model_id="claude-sonnet-5",
                    prompt_version="v2.1.0",
                    assembled_at=NOW,
                    tool_calls_transcript=[],
                ),
                strategy="bull_put_spread",
                underlying="AAPL",
                conviction=0.61,
                limits_version="v0.3.0",
                prompt_version="v2.1.0",
                model_id="claude-sonnet-5",
                rejection_rule_ids=[ValidationRuleId.EVENT_BLACKOUT],
            ),
        )

        # Deliberately references a position_id that was never inserted —
        # exercises the Decision explorer's anomaly path (broken history is
        # surfaced, not hidden or 500'd). See ui/cycles.py's PositionLink.
        write_journal_record(
            conn,
            JournalRecord(
                cycle_id="c-2026-demo-anomaly-amd",
                timestamp=NOW - timedelta(minutes=150),
                action_taken=ActionTaken.OPENED,
                decision=Decision(
                    proposal=_bull_put_proposal(
                        underlying="AMD", short_strike=150.0, long_strike=145.0, dte=31
                    ),
                    validation_result=ValidationResult(passed=True, reasons=[]),
                    sizing_result=None,
                    action_taken=ActionTaken.OPENED,
                ),
                context_snapshot=ContextSnapshot(
                    assembled_context={},
                    context_hash="sha256:aaaaaa",
                    model_id="claude-sonnet-5",
                    prompt_version="v2.1.0",
                    assembled_at=NOW,
                    tool_calls_transcript=[],
                ),
                position_ids=["pos-does-not-exist"],
                strategy="bull_put_spread",
                underlying="AMD",
                conviction=0.55,
                limits_version="v0.3.0",
                prompt_version="v2.1.0",
                model_id="claude-sonnet-5",
            ),
        )

        write_outcome_record(
            conn,
            OutcomeRecord(
                id="out-partial-spy",
                position_id="pos-a7f3",
                event_type=OutcomeEventType.PARTIAL_CLOSE,
                recorded_at=NOW - timedelta(hours=4),
                contracts_closed=1,
                realized_pnl=45.0,
            ),
        )

        _seed_closed_history(conn)

    engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path, help="Path to the scratch SQLite file")
    parser.add_argument(
        "--force", action="store_true", help="Delete an existing file at db_path first"
    )
    args = parser.parse_args()

    db_path: Path = args.db_path.resolve()
    if not args.force and db_path.exists():
        print(
            f"{db_path} already exists — pass --force to wipe and reseed it.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Migrating {db_path} ...")
    _migrate(db_path, force=args.force)

    print("Seeding demo data ...")
    seed(db_path)

    print(f"Done. Point the console at: DB_URL=sqlite:///{db_path}")


if __name__ == "__main__":
    main()
