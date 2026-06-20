"""Add exit_reason column to orders and outcome_records for WP-5.5.

exit_reason records which monitor rule triggered a closing order (STOP_LOSS,
PROFIT_TARGET, DTE, FLATTEN). Stored on the Order at submit time so it can
be carried through to the OutcomeRecord when reconcile confirms the fill.

Nullable in both tables: opening orders have no exit reason; pre-WP-5.5
records will have NULL and are excluded from WP-7 exit-rule analytics by a
WHERE exit_reason IS NOT NULL filter.

Revision ID: 005
Revises: 004
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("exit_reason", sa.String(), nullable=True))
    op.add_column(
        "outcome_records", sa.Column("exit_reason", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("outcome_records", "exit_reason")
    op.drop_column("orders", "exit_reason")
