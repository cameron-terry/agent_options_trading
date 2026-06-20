"""Add kill_switch_log table for WP-7.1 kill-switch flag.

Append-only audit log: one row per state change; current state = latest row.

Revision ID: 004
Revises: 003
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "kill_switch_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("set_by", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kill_switch_log_created_at", "kill_switch_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_kill_switch_log_created_at", table_name="kill_switch_log")
    op.drop_table("kill_switch_log")
