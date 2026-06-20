"""Kill-switch CLI: python -m options_agent.obs

Primary interface for arming and clearing the kill switch.  Raw SQL is the
documented break-glass fallback when this script is unavailable — see
docs/runbook_kill_switch.md for the three-tier escalation procedure.

Commands
--------
  status              Show current state and last few log entries.
  set HALT|FLATTEN    Arm the kill switch (zero friction — no confirmation).
  resume              Clear to NONE (requires acknowledgement + --reason).
  history [--n N]     Show last N log entries (default 20).

Examples
--------
  python -m options_agent.obs status
  python -m options_agent.obs set HALT --reason "broker reconcile mismatch"
  python -m options_agent.obs set FLATTEN --reason "vega band breached"
  python -m options_agent.obs resume --reason "issue resolved, positions reviewed"
  python -m options_agent.obs history --n 10
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from options_agent.config import Config
from options_agent.contracts.state import KillSwitchState
from options_agent.obs.killswitch import (
    get_current_state,
    list_history,
    resume,
    set_state,
)
from options_agent.state.db import build_engine, get_connection, metadata


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

    # Show context before requiring acknowledgement.
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


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m options_agent.obs",
        description="Kill-switch management CLI",
    )
    parser.add_argument(
        "--set-by",
        default=os.environ.get("USER", "operator"),
        help="Operator name recorded in the audit log (default: $USER)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show current state and recent history")

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

    args = parser.parse_args()

    dispatch = {
        "status": cmd_status,
        "set": cmd_set,
        "resume": cmd_resume,
        "history": cmd_history,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
