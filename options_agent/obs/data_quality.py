"""Known JournalRecord.data_quality_flags values and their descriptions.

Retroactive data-quality annotations (WP-7): a curated, append-only registry
of flag values that can appear in JournalRecord.data_quality_flags, each
with a human-readable description consumers can surface without needing a
free-text per-row note. Add a new entry here whenever a data bug is found
and backfilled onto historical rows (see alembic/versions for the migration
that applies each flag).
"""

from __future__ import annotations

DATA_QUALITY_FLAG_DESCRIPTIONS: dict[str, str] = {
    "phantom_net_delta": (
        "context_snapshot.assembled_context.portfolio.net_dollar_delta is "
        "unreliable on this cycle: SPY condor legs fell out of the "
        "entry-filtered chain and aggregate_portfolio_greeks() zeroed their "
        "contribution (see greek_warnings on the same snapshot), producing a "
        "net delta of tens of thousands of dollars. Fixed going forward by "
        "PR #89 (held-leg Greek fetch); this flag marks the 4 historical "
        "cycles (2026-07-09 17:00 through 2026-07-10 19:00) written before "
        "the fix landed. Any thesis text or risk-budget reasoning on this "
        "cycle that cites net_dollar_delta should be treated as unreliable."
    ),
}
