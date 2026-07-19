"""Backfill Position.est_max_loss/profit against actual fill price (WP-1).

Prior to this WP-1 fix, Position.est_max_loss/est_max_profit were copied
straight from the proposal's chain-mid estimate at position creation and
never corrected against the real fill price. This one-shot script recomputes
them for existing OPEN/PENDING_OPEN positions using the same payoff-analysis
math the live fix now applies automatically at fill confirmation
(risk/structure.py:apply_fill_metrics), driven by each position's opening
order's net_fill_price.

Safety: read-only by default. Pass --apply to write corrected values; without
it, the script only prints what it *would* change. Idempotent — recomputing
an already-correct position is a no-op diff.

Usage:
    uv run python scripts/backfill_position_fill_metrics.py            # dry run
    uv run python scripts/backfill_position_fill_metrics.py --apply    # write

Honors the DB_URL env override the same way ui.app.create_app() does — a
config.toml is optional; DB_URL alone is sufficient (the running console
container, for instance, has no config.toml, only DB_URL).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from options_agent.config import Config
from options_agent.risk.structure import apply_fill_metrics
from options_agent.state.crud import get_order, list_open_positions, update_position
from options_agent.state.db import build_engine, get_connection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write corrected values (default: dry run, prints only)",
    )
    args = parser.parse_args()

    config_path = Path("config.toml")
    db_url = os.environ.get("DB_URL")
    if db_url is None:
        if not config_path.exists():
            print(
                "ERROR: no DB_URL env var and no config.toml in the current "
                "directory. Set DB_URL explicitly, or run from a directory "
                "containing config.toml.",
                file=sys.stderr,
            )
            sys.exit(1)
        db_url = Config.from_toml(config_path).db_url
    engine = build_engine(db_url)

    print(f"WP-1 est_max_loss/profit fill-time backfill — target: {db_url}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN (pass --apply to write)'}")
    print()
    print(
        f"{'Position':<12} {'Order':<12} {'Fill':>8} "
        f"{'Old Loss':>10} {'New Loss':>10} {'Old Profit':>11} {'New Profit':>11}"
    )
    print("-" * 80)

    changed = 0
    skipped_no_fill = 0

    with get_connection(engine) as conn:
        positions = list_open_positions(conn)

        for pos in positions:
            order = get_order(conn, pos.opening_order_id)
            if order is None or order.net_fill_price is None:
                print(
                    f"{pos.id:<12} {pos.opening_order_id:<12}"
                    "    -- no fill price, skipped"
                )
                skipped_no_fill += 1
                continue

            new_loss, new_profit = apply_fill_metrics(
                [pos_leg.leg for pos_leg in pos.legs],
                order.net_fill_price,
                prior_est_max_loss=pos.est_max_loss,
                prior_est_max_profit=pos.est_max_profit,
                log_context=f"backfill position {pos.id}",
            )

            print(
                f"{pos.id:<12} {order.id:<12} {order.net_fill_price:>8.2f} "
                f"{pos.est_max_loss:>10.2f} {new_loss:>10.2f} "
                f"{pos.est_max_profit:>11.2f} {new_profit:>11.2f}"
            )

            if new_loss != pos.est_max_loss or new_profit != pos.est_max_profit:
                changed += 1
                if args.apply:
                    updated = pos.model_copy(
                        update={"est_max_loss": new_loss, "est_max_profit": new_profit}
                    )
                    update_position(conn, updated)

    print()
    print(
        f"{changed} position(s) {'corrected' if args.apply else 'would be corrected'}; "
        f"{skipped_no_fill} skipped (no fill price on opening order)."
    )
    if not args.apply and changed:
        print("Re-run with --apply to write these changes.")


if __name__ == "__main__":
    main()
