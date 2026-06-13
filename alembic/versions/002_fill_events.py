"""Add fill_events table for reconcile idempotency (WP-1.4).

Revision ID: 002
Revises: 001
Create Date: 2026-06-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "fill_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("broker_exec_id", sa.String(), nullable=False),
        sa.Column("leg_symbol", sa.String(), nullable=False),
        sa.Column("filled_qty", sa.Integer(), nullable=False),
        sa.Column("fill_price", sa.Float(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("broker_exec_id"),
    )
    op.create_index("ix_fill_events_order_id", "fill_events", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_fill_events_order_id", table_name="fill_events")
    op.drop_table("fill_events")
