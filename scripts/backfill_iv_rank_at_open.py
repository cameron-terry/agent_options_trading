"""Backfill journal_records.iv_rank_at_open / net_delta_at_open / earnings_within_dte
for historical OPENED and SIZED_TO_ZERO rows (WP-7).

Prior to this WP-7 fix, the orchestrator never populated these three
denormalized analytics columns for the SIZED_TO_ZERO branch, and never
populated iv_rank_at_open / earnings_within_dte for either trade-action
branch — even though the source data (proposal.net_delta, and the
underlying's SymbolSnapshot in context_snapshot) was already captured in
every row. This script derives the missing values from each row's own
context_snapshot/decision blobs and patches them in with a direct UPDATE.

journal_records is write-once by design (state/journal.py has no update
path) — this script deliberately bypasses that module and issues a raw
SQLAlchemy Core UPDATE, following the precedent in state/crud.py's
update_position()/patch_order_fill_metrics().

Usage:
    uv run python scripts/backfill_iv_rank_at_open.py [--dry-run]

Reads db_url from config.toml (or the DB_URL env override, same convention
as scripts/backfill_iv_history.py) in the current directory.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from options_agent.config import Config
from options_agent.contracts.state import ActionTaken
from options_agent.state.db import build_engine, get_connection, journal_records_table
from options_agent.state.journal import query_journal


def _derive_fields(
    record, event_blackout_days: int
) -> tuple[float | None, float | None, bool | None]:
    """Return (iv_rank_at_open, net_delta_at_open, earnings_within_dte)."""
    snapshot = (
        record.context_snapshot.assembled_context.get("universe", {})
        .get("symbol_snapshots", {})
        .get(record.underlying or "", {})
    )
    iv_rank_at_open = snapshot.get("iv_rank")
    days_to_earnings = snapshot.get("days_to_earnings")
    earnings_within_dte = (
        days_to_earnings is not None and days_to_earnings <= event_blackout_days
        if snapshot
        else None
    )

    proposal = record.decision.proposal
    net_delta_at_open = proposal.net_delta if proposal is not None else None

    return iv_rank_at_open, net_delta_at_open, earnings_within_dte


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing to the database.",
    )
    args = parser.parse_args()

    config_path = Path("config.toml")
    if not config_path.exists():
        print(
            "ERROR: config.toml not found. Run from the project root.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = Config.from_toml(config_path)
    db_url = os.environ.get("DB_URL", config.db_url)
    engine = build_engine(db_url)

    print(f"WP-7 iv_rank_at_open backfill — target: {db_url}")
    print(f"event_blackout_days = {config.limits.event_blackout_days}")
    print()

    with get_connection(engine) as conn:
        records = [
            r
            for action in (ActionTaken.OPENED, ActionTaken.SIZED_TO_ZERO)
            for r in query_journal(conn, action_type=action)
        ]

    print(f"Found {len(records)} OPENED/SIZED_TO_ZERO rows.")
    print(
        f"{'cycle_id':<38} {'action':<15} {'underlying':<10}"
        f" {'iv_rank':>9} {'net_delta':>10} {'earn_dte':>9}"
    )
    print("-" * 96)

    updated = 0
    for record in records:
        iv_rank_at_open, net_delta_at_open, earnings_within_dte = _derive_fields(
            record, config.limits.event_blackout_days
        )
        iv_rank_str = "null" if iv_rank_at_open is None else f"{iv_rank_at_open:.4f}"
        net_delta_str = (
            "null" if net_delta_at_open is None else f"{net_delta_at_open:.4f}"
        )
        earn_dte_str = (
            "null" if earnings_within_dte is None else str(earnings_within_dte)
        )
        print(
            f"{record.cycle_id:<38} {record.action_taken.value:<15}"
            f" {record.underlying or '':<10}"
            f" {iv_rank_str:>9} {net_delta_str:>10} {earn_dte_str:>9}"
        )

        if args.dry_run:
            continue

        with get_connection(engine) as conn:
            result = conn.execute(
                journal_records_table.update()
                .where(journal_records_table.c.cycle_id == record.cycle_id)
                .values(
                    iv_rank_at_open=iv_rank_at_open,
                    net_delta_at_open=net_delta_at_open,
                    earnings_within_dte=earnings_within_dte,
                )
            )
            if result.rowcount == 1:
                updated += 1

    print()
    if args.dry_run:
        print(f"DRY RUN — no rows written ({len(records)} would be updated).")
    else:
        print(f"Backfilled {updated}/{len(records)} rows.")


if __name__ == "__main__":
    main()
