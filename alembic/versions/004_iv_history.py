"""Add iv_history table for daily ATM IV accumulation (WP-3.4).

Revision ID: 004
Revises: 003
Create Date: 2026-06-14
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
        "iv_history",
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("atm_iv", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "symbol", "observation_date", name="uq_iv_history_symbol_date"
        ),
    )
    op.create_index(
        "ix_iv_history_symbol_date", "iv_history", ["symbol", "observation_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_iv_history_symbol_date", table_name="iv_history")
    op.drop_table("iv_history")
