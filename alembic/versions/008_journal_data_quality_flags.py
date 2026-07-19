"""Add data_quality_flags to journal_records + backfill phantom-delta cycles
(WP-7 retroactive data-quality flag).

Adds a nullable JSON column for machine-readable data-quality annotations,
then backfills the 4 journal rows (2026-07-09 17:00 through 2026-07-10
19:00) whose context_snapshot.assembled_context.portfolio.net_dollar_delta
was corrupted by the missing-leg Greek-aggregation bug fixed in PR #89. See
options_agent/obs/data_quality.py for the flag's description.

Revision ID: 008
Revises: 007
Create Date: 2026-07-19
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# The 4 cycles identified by the 2026-07-18 journal data audit as having a
# phantom (contaminated) net_dollar_delta in their context_snapshot.
_PHANTOM_NET_DELTA_CYCLE_IDS = (
    "05c9b8da-6d8e-4a79-b035-72af0f792ec1",
    "e3b3290b-5861-43fe-b9d3-82d50a706856",
    "fbb3bef4-c711-4ef2-8f80-fa0aee22c2ba",
    "6a3a2b02-e7bf-4e03-afad-7e0621579e4f",
)


def upgrade() -> None:
    with op.batch_alter_table("journal_records") as batch_op:
        batch_op.add_column(sa.Column("data_quality_flags", sa.JSON(), nullable=True))

    conn = op.get_bind()
    flags_json = json.dumps(["phantom_net_delta"])
    journal_records = sa.table(
        "journal_records",
        sa.column("cycle_id", sa.String()),
        sa.column("data_quality_flags", sa.JSON()),
    )
    conn.execute(
        journal_records.update()
        .where(journal_records.c.cycle_id.in_(_PHANTOM_NET_DELTA_CYCLE_IDS))
        .values(data_quality_flags=flags_json)
    )


def downgrade() -> None:
    with op.batch_alter_table("journal_records") as batch_op:
        batch_op.drop_column("data_quality_flags")
