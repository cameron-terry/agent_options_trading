"""Repair double-JSON-encoded columns (2026-07-18 journal data audit).

Several JSON columns were written by application code that called
json.dumps() on the value before handing it to a sa.JSON()-typed column,
which serializes again — the stored value is a JSON string whose *contents*
are another JSON document (e.g. journal_records.position_ids stored as
'"[\\"e8e1ff2e-...\\"]"' instead of '["e8e1ff2e-..."]'). The application-code
bug (options_agent/state/journal.py, options_agent/state/crud.py) is fixed
in the same change that introduces this migration; this migration repairs
already-written rows.

Detection: reading a column through its sa.JSON() type applies one decode
pass automatically. A correctly-encoded value comes back as a native
list/dict. A double-encoded value comes back as a str (the un-decoded inner
JSON document) because the outer decode only stripped one layer. Any row
that decodes to a str is repaired by decoding it once more and writing the
native object back (the column's JSON type re-encodes it exactly once on
write). This makes the migration idempotent: already-clean rows decode to
a list/dict, never a str, and are left untouched on re-run.

Includes journal_records.data_quality_flags, whose only populated rows
(4 phantom-net-delta cycles) were backfilled by migration 008 using the
same buggy json.dumps-before-JSON-column pattern.

Revision ID: 009
Revises: 008
Create Date: 2026-07-19
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# (table_name, pk_column_name, json_column_name)
_AFFECTED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("journal_records", "cycle_id", "decision"),
    ("journal_records", "cycle_id", "context_snapshot"),
    ("journal_records", "cycle_id", "position_ids"),
    ("journal_records", "cycle_id", "order_ids"),
    ("journal_records", "cycle_id", "rejection_rule_ids"),
    ("journal_records", "cycle_id", "data_quality_flags"),
    ("positions", "id", "legs"),
    ("positions", "id", "exit_plan"),
    ("positions", "id", "equity_legs"),
    ("orders", "id", "legs_filled"),
)


def _repair_column(
    conn: sa.engine.Connection, table_name: str, pk_col: str, json_col: str
) -> None:
    table = sa.table(
        table_name,
        sa.column(pk_col, sa.String()),
        sa.column(json_col, sa.JSON()),
    )
    rows = conn.execute(sa.select(table.c[pk_col], table.c[json_col])).fetchall()
    for pk_value, decoded_once in rows:
        if not isinstance(decoded_once, str):
            continue  # already a native list/dict (or None) — clean row
        native_value = json.loads(decoded_once)
        conn.execute(
            table.update()
            .where(table.c[pk_col] == pk_value)
            .values(**{json_col: native_value})
        )


def upgrade() -> None:
    conn = op.get_bind()
    for table_name, pk_col, json_col in _AFFECTED_COLUMNS:
        _repair_column(conn, table_name, pk_col, json_col)


def downgrade() -> None:
    # Repairing double-encoded JSON is not reversible to a known prior state
    # (the "before" shape was a bug, not a schema this migration introduced).
    pass
