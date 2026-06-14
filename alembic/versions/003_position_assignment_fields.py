"""Add assignment/expiry fields to positions table (WP-1.5).

Adds three new columns and relaxes exit_plan to nullable so that equity
positions created by assignment can be represented without a sentinel value.

Revision ID: 003
Revises: 002
Create Date: 2026-06-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # batch_alter_table is required for SQLite (which cannot ALTER COLUMN directly).
    with op.batch_alter_table("positions") as batch_op:
        # Allow exit_plan to be NULL — EQUITY positions have no predefined exit plan.
        batch_op.alter_column("exit_plan", nullable=True)
        batch_op.add_column(sa.Column("asset_class", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("equity_legs", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("assigned_from_position_id", sa.String(), nullable=True)
        )

    # Backfill existing rows: all pre-migration positions are option strategies.
    op.execute(
        "UPDATE positions SET asset_class = 'option_strategy' WHERE asset_class IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("positions") as batch_op:
        batch_op.drop_column("assigned_from_position_id")
        batch_op.drop_column("equity_legs")
        batch_op.drop_column("asset_class")
        batch_op.alter_column("exit_plan", nullable=False)
