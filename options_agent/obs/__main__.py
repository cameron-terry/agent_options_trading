"""Observability CLI: python -m options_agent.obs

Kill-switch management and journal review in one entry point.

Commands
--------
  status              Show current kill-switch state and last few log entries.
  set HALT|FLATTEN    Arm the kill switch (zero friction — no confirmation).
  resume              Clear to NONE (requires acknowledgement + --reason).
  history [--n N]     Show last N kill-switch log entries (default 20).
  review              Print hit rate, P&L attribution, and cycle funnel.

Examples
--------
  python -m options_agent.obs status
  python -m options_agent.obs set HALT --reason "broker reconcile mismatch"
  python -m options_agent.obs set FLATTEN --reason "vega band breached"
  python -m options_agent.obs resume --reason "issue resolved, positions reviewed"
  python -m options_agent.obs history --n 10
  python -m options_agent.obs review
  python -m options_agent.obs review --since 2026-06-01
  python -m options_agent.obs review --prompt-version v2.0.0
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from options_agent.config import Config
from options_agent.contracts.state import ActionTaken, KillSwitchState
from options_agent.obs.killswitch import (
    get_current_state,
    list_history,
    resume,
    set_state,
)
from options_agent.obs.review import (
    CycleFunnelReport,
    HitRateReport,
    PnLAttributionReport,
    cycle_funnel,
    hit_rate_by_strategy,
    pnl_attribution,
)
from options_agent.state.db import build_engine, get_connection, metadata
from options_agent.state.journal import query_journal, query_outcome_records

_console = Console()


def _load_config() -> Config:
    """Load config.toml from CWD if present; otherwise use all defaults."""
    toml_path = Path("config.toml")
    if toml_path.exists():
        return Config.from_toml(toml_path)
    db_url = os.environ.get("DB_URL", "sqlite:///options_agent.db")
    return Config(db_url=db_url)


def _fmt_entry(entry) -> str:  # type: ignore[no-untyped-def]
    ts = entry.created_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"  {ts}  {entry.state:<8}  by={entry.set_by!r}  reason={entry.reason!r}"


# ---------------------------------------------------------------------------
# Kill-switch commands (unchanged)
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config()
    engine = build_engine(config.db_url)
    metadata.create_all(engine)
    with get_connection(engine) as conn:
        state = get_current_state(conn)
        history = list_history(conn, limit=5)

    indicator = {
        KillSwitchState.NONE: "✓ NONE — system operating normally",
        KillSwitchState.HALT: "⚠  HALT — new entries blocked; monitor running",
        KillSwitchState.FLATTEN: "🛑 FLATTEN — closing all positions; no new entries",
    }[state]
    print(f"\nKill-switch state: {indicator}\n")

    if history:
        print("Recent history (newest first):")
        for entry in history:
            print(_fmt_entry(entry))
    else:
        print("No kill-switch history recorded.")
    print()
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    try:
        state = KillSwitchState(args.state.upper())
    except ValueError:
        print(f"ERROR: invalid state {args.state!r}. Must be HALT or FLATTEN.")
        return 1

    if state == KillSwitchState.NONE:
        print("ERROR: use 'resume' to clear the kill switch, not 'set NONE'.")
        return 1

    config = _load_config()
    engine = build_engine(config.db_url)
    metadata.create_all(engine)
    with get_connection(engine) as conn:
        entry = set_state(conn, state, set_by=args.set_by, reason=args.reason)

    ts = entry.created_at.strftime("%H:%M:%S %Z")
    print(f"\nKill switch set to {entry.state} at {ts}")
    print(f"  by: {entry.set_by}")
    print(f"  reason: {entry.reason}\n")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    config = _load_config()
    engine = build_engine(config.db_url)
    metadata.create_all(engine)

    with get_connection(engine) as conn:
        current = get_current_state(conn)

    if current == KillSwitchState.NONE:
        print("\nKill switch is already NONE — system is already running normally.\n")
        return 0

    print(f"\nCurrent state: {current}")
    print("You are about to resume trading (set kill switch to NONE).")
    print(f"  Operator: {args.set_by}")
    print(f"  Reason:   {args.reason}")

    if not args.yes:
        answer = input("\nConfirm resume? [y/N] ").strip().lower()
        if answer != "y":
            print("Resume cancelled.")
            return 1

    with get_connection(engine) as conn:
        entry = resume(conn, set_by=args.set_by, reason=args.reason)

    ts = entry.created_at.strftime("%H:%M:%S %Z")
    print(f"\nKill switch cleared to NONE at {ts}")
    print(f"  by: {entry.set_by}")
    print(f"  reason: {entry.reason}\n")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    config = _load_config()
    engine = build_engine(config.db_url)
    metadata.create_all(engine)
    with get_connection(engine) as conn:
        history = list_history(conn, limit=args.n)

    if not history:
        print("\nNo kill-switch history recorded.\n")
        return 0

    print(f"\nKill-switch history (last {len(history)}):")
    for entry in history:
        print(_fmt_entry(entry))
    print()
    return 0


# ---------------------------------------------------------------------------
# Review command — rich rendering
# ---------------------------------------------------------------------------


def _pnl_str(v: float) -> str:
    if math.isnan(v):
        return "[dim]—[/dim]"
    color = "green" if v > 0 else "red" if v < 0 else "white"
    return f"[{color}]{v:+.2f}[/{color}]"


def _pct_str(v: float) -> str:
    if math.isnan(v):
        return "[dim]—[/dim]"
    color = "green" if v >= 0.5 else "yellow" if v >= 0.35 else "red"
    return f"[{color}]{v:.1%}[/{color}]"


def _render_funnel(report: CycleFunnelReport) -> None:
    _console.print()
    _console.rule("[bold]Cycle Funnel[/bold]")
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    t.add_column("Stage", style="dim", min_width=22)
    t.add_column("Count", justify="right")
    t.add_column("% of prev", justify="right")

    def row(label: str, count: int, denom: int) -> None:
        pct = f"{count / denom:.1%}" if denom else "—"
        t.add_row(label, str(count), pct)

    row("Total cycles", report.total, report.total)
    row("  Gated (pre-LLM)", report.gated, report.total)
    row("  Reasoned (LLM called)", report.reasoned, report.total)
    row("    No-action (agent)", report.no_action_agent, report.reasoned or 1)
    row("    Proposed", report.proposed, report.reasoned or 1)
    row("      Rejected", report.rejected, report.proposed or 1)
    row("      Sized to zero", report.sized_to_zero, report.proposed or 1)
    row("      Exec failed", report.execution_failed, report.proposed or 1)
    row("      Opened", report.opened, report.proposed or 1)

    _console.print(t)


def _render_hit_rate(report: HitRateReport) -> None:
    _console.print()
    _console.rule("[bold]Hit Rate by Strategy[/bold]")
    _console.print(
        "[dim]Hit = realized_pnl > 0 on a fully-closed position. "
        "Read hit rate alongside expectancy — credit strategies win often "
        "and lose big; standalone hit rate misleads.[/dim]"
    )

    if not report.by_strategy and report.overall.trade_count == 0:
        _console.print("[yellow]  No closed trades yet.[/yellow]")
        if report.open_summary.open_position_count:
            _console.print(
                f"  Open: {report.open_summary.open_position_count} position(s), "
                f"realized-to-date {_pnl_str(report.open_summary.realized_to_date)}"
            )
        return

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    t.add_column("Strategy", min_width=22)
    t.add_column("Trades", justify="right")
    t.add_column("Hits", justify="right")
    t.add_column("Hit rate", justify="right")
    t.add_column("Avg win", justify="right")
    t.add_column("Avg loss", justify="right")
    t.add_column("Expectancy", justify="right")
    t.add_column("Net P&L", justify="right")

    rows = list(report.by_strategy.values()) + [report.overall]
    for s in rows:
        is_total = s.strategy == "_all"
        label = "[bold]TOTAL[/bold]" if is_total else s.strategy
        t.add_row(
            label,
            str(s.trade_count),
            str(s.hit_count),
            _pct_str(s.hit_rate),
            _pnl_str(s.avg_win),
            _pnl_str(s.avg_loss),
            _pnl_str(s.expectancy),
            _pnl_str(s.total_pnl),
            end_section=is_total,
        )

    _console.print(t)

    if report.open_summary.open_position_count:
        _console.print(
            f"  [dim]Open: {report.open_summary.open_position_count} position(s), "
            f"realized-to-date {_pnl_str(report.open_summary.realized_to_date)}[/dim]"
        )


def _render_pnl_attribution(report: PnLAttributionReport) -> None:
    _console.print()
    _console.rule("[bold]P&L Attribution[/bold]")

    if not report.by_underlying and not report.by_strategy:
        _console.print("[yellow]  No closed trades yet.[/yellow]")
        return

    # By underlying
    _console.print("[bold dim]By underlying[/bold dim]")
    tu = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    tu.add_column("Underlying", min_width=12)
    tu.add_column("Trades", justify="right")
    tu.add_column("Net P&L", justify="right")
    for u in report.by_underlying.values():
        tu.add_row(u.underlying, str(u.trade_count), _pnl_str(u.net_pnl))
    _console.print(tu)

    # By strategy
    _console.print("[bold dim]By strategy[/bold dim]")
    ts = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    ts.add_column("Strategy", min_width=22)
    ts.add_column("Trades", justify="right")
    ts.add_column("Net P&L", justify="right")
    for s in report.by_strategy.values():
        ts.add_row(s.strategy, str(s.trade_count), _pnl_str(s.net_pnl))
    ts.add_row(
        "[bold]TOTAL[/bold]",
        "",
        _pnl_str(report.total_realized_pnl),
        end_section=True,
    )
    _console.print(ts)

    if report.open_summary.open_position_count:
        _console.print(
            f"  [dim]Open: {report.open_summary.open_position_count} position(s), "
            f"realized-to-date {_pnl_str(report.open_summary.realized_to_date)}[/dim]"
        )


def cmd_review(args: argparse.Namespace) -> int:
    since: datetime | None = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
        except ValueError:
            print(
                f"ERROR: --since must be ISO 8601 date, e.g. 2026-06-01."
                f" Got {args.since!r}"
            )
            return 1

    prompt_ver: str | None = args.prompt_version or None

    config = _load_config()
    engine = build_engine(config.db_url)
    metadata.create_all(engine)

    with get_connection(engine) as conn:
        records = query_journal(conn, date_from=since)
        # Collect position_ids touched by any OPENED / CLOSED / ROLLED cycle
        opened_action_types = {
            ActionTaken.OPENED,
            ActionTaken.CLOSED,
            ActionTaken.ROLLED,
        }
        position_ids = [
            pid
            for r in records
            if r.action_taken in opened_action_types
            for pid in r.position_ids
        ]
        outcomes = query_outcome_records(conn, position_ids=position_ids or None)

    filter_desc = []
    if since:
        filter_desc.append(f"since {since.date()}")
    if prompt_ver:
        filter_desc.append(f"prompt={prompt_ver}")
    title = "Journal Review"
    if filter_desc:
        title += f" ({', '.join(filter_desc)})"
    _console.print(
        f"\n[bold]{title}[/bold]  —  {len(records)} cycles, {len(outcomes)} outcomes"
    )

    funnel_report = cycle_funnel(records, since=since)
    hit_report = hit_rate_by_strategy(
        records, outcomes, since=since, prompt_version=prompt_ver
    )
    attr_report = pnl_attribution(
        records, outcomes, since=since, prompt_version=prompt_ver
    )

    _render_funnel(funnel_report)
    _render_hit_rate(hit_report)
    _render_pnl_attribution(attr_report)
    _console.print()
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m options_agent.obs",
        description="Observability CLI — kill-switch management and journal review",
    )
    parser.add_argument(
        "--set-by",
        default=os.environ.get("USER", "operator"),
        help="Operator name recorded in the audit log (default: $USER)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show current kill-switch state and recent history")

    set_p = sub.add_parser("set", help="Arm the kill switch (HALT or FLATTEN)")
    set_p.add_argument("state", choices=["HALT", "FLATTEN"])
    set_p.add_argument("--reason", required=True, help="Why the switch is being set")

    resume_p = sub.add_parser("resume", help="Clear kill switch and resume trading")
    resume_p.add_argument(
        "--reason",
        required=True,
        help="Acknowledgement that the issue is resolved",
    )
    resume_p.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt",
    )

    hist_p = sub.add_parser("history", help="Show recent kill-switch log entries")
    hist_p.add_argument(
        "--n", type=int, default=20, help="Number of entries (default 20)"
    )

    review_p = sub.add_parser(
        "review", help="Print hit rate, P&L attribution, and cycle funnel"
    )
    review_p.add_argument(
        "--since",
        metavar="DATE",
        help="ISO 8601 date (e.g. 2026-06-01) — only records from this date onward",
    )
    review_p.add_argument(
        "--prompt-version",
        metavar="VERSION",
        help="Filter to a prompt version (e.g. v2.0.0) for before/after comparison",
    )

    args = parser.parse_args()

    dispatch = {
        "status": cmd_status,
        "set": cmd_set,
        "resume": cmd_resume,
        "history": cmd_history,
        "review": cmd_review,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
