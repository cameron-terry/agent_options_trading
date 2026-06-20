"""Add alert_delivery_failures table for WP-7.2 alerting channel integration.

Append-only audit record: one row per exhausted-retry alert send attempt.
Enables WP-7 review to surface undelivered CRITICAL alerts without relying
on log lines (logs are the medium alerting exists to not depend on).

Revision ID: 006
Revises: 005
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_delivery_failures",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alert_delivery_failures_attempted_at",
        "alert_delivery_failures",
        ["attempted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_alert_delivery_failures_attempted_at",
        table_name="alert_delivery_failures",
    )
    op.drop_table("alert_delivery_failures")
