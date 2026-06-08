"""Initial schema: positions, orders, journal_records, outcome_records.

Revision ID: 001
Revises:
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("underlying", sa.String(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("legs", sa.JSON(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_net_amount", sa.Float(), nullable=False),
        sa.Column("current_mark", sa.Float(), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("exit_plan", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("nearest_expiration", sa.Date(), nullable=False),
        sa.Column("est_max_loss", sa.Float(), nullable=False),
        sa.Column("est_max_profit", sa.Float(), nullable=False),
        sa.Column("opening_order_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_positions_opened_at", "positions", ["opened_at"])

    op.create_table(
        "orders",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("broker_order_id", sa.String(), nullable=False),
        sa.Column("position_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("broker_status_raw", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legs_filled", sa.JSON(), nullable=False),
        sa.Column("net_fill_price", sa.Float(), nullable=True),
        sa.Column("filled_qty", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_position_id", "orders", ["position_id"])

    op.create_table(
        "journal_records",
        sa.Column("cycle_id", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_taken", sa.String(), nullable=False),
        sa.Column("decision", sa.JSON(), nullable=False),
        sa.Column("context_snapshot", sa.JSON(), nullable=False),
        sa.Column("position_ids", sa.JSON(), nullable=False),
        sa.Column("order_ids", sa.JSON(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=True),
        sa.Column("underlying", sa.String(), nullable=True),
        sa.Column("net_delta_at_open", sa.Float(), nullable=True),
        sa.Column("earnings_within_dte", sa.Boolean(), nullable=True),
        sa.Column("conviction", sa.Float(), nullable=True),
        sa.Column("iv_rank_at_open", sa.Float(), nullable=True),
        sa.Column("limits_version", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("rejection_rule_ids", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("cycle_id"),
    )
    op.create_index("ix_journal_records_timestamp", "journal_records", ["timestamp"])
    op.create_index(
        "ix_journal_records_action_taken", "journal_records", ["action_taken"]
    )
    op.create_index("ix_journal_records_strategy", "journal_records", ["strategy"])
    op.create_index("ix_journal_records_underlying", "journal_records", ["underlying"])

    op.create_table(
        "outcome_records",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("position_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contracts_closed", sa.Integer(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False),
        sa.Column("fill_price", sa.Float(), nullable=True),
        sa.Column("closing_order_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outcome_records_position_id", "outcome_records", ["position_id"]
    )
    op.create_index(
        "ix_outcome_records_recorded_at", "outcome_records", ["recorded_at"]
    )


def downgrade() -> None:
    op.drop_table("outcome_records")
    op.drop_table("journal_records")
    op.drop_table("orders")
    op.drop_table("positions")
