from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
)
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()

# ---------------------------------------------------------------------------
# positions — one strategy-level position per row; mutable (status, mark, pnl)
#
# Nested compound fields (legs, exit_plan) stored as JSON blobs: the system
# never queries by individual leg, and the strategy-level is the unit WP-5
# and WP-7 reason about.
# ---------------------------------------------------------------------------
positions_table = Table(
    "positions",
    metadata,
    Column("id", String, primary_key=True),
    Column("underlying", String, nullable=False),
    Column("strategy", String, nullable=False),
    Column("legs", JSON, nullable=False),  # list[PositionLeg]
    Column("quantity", Integer, nullable=False),
    Column("entry_net_amount", Float, nullable=False),
    Column("current_mark", Float, nullable=False),
    Column("marked_at", DateTime(timezone=True), nullable=False),
    Column("unrealized_pnl", Float, nullable=False),
    Column("realized_pnl", Float, nullable=True),
    Column("exit_plan", JSON, nullable=False),  # ExitPlan
    Column("status", String, nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False, index=True),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column("nearest_expiration", Date, nullable=False),
    Column("est_max_loss", Float, nullable=False),
    Column("est_max_profit", Float, nullable=False),
    Column("opening_order_id", String, nullable=False),
)

# ---------------------------------------------------------------------------
# orders — broker-facing order entity; mutable (status, fill details)
#
# legs_filled stored as JSON blob (list[LegFill]); per-leg detail is
# needed as a unit (slippage analysis), not individually filtered.
# ---------------------------------------------------------------------------
orders_table = Table(
    "orders",
    metadata,
    Column("id", String, primary_key=True),
    Column("broker_order_id", String, nullable=False),
    Column(
        "position_id",
        String,
        sa.ForeignKey("positions.id"),
        nullable=False,
        index=True,
    ),
    Column("role", String, nullable=False),
    Column("status", String, nullable=False),
    Column("broker_status_raw", String, nullable=False),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("filled_at", DateTime(timezone=True), nullable=True),
    Column("legs_filled", JSON, nullable=False),  # list[LegFill]
    Column("net_fill_price", Float, nullable=True),
    Column("filled_qty", Integer, nullable=False),
)

# ---------------------------------------------------------------------------
# Mutability boundary: positions and orders are mutable-in-place (status
# transitions, fill updates). journal_records and outcome_records are
# append-only — application code must never UPDATE those two tables.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# journal_records — append-only; write once at end of each entry cycle.
#
# Hybrid layout: nested decision/context stored as JSON blobs (source of
# truth, immutable); denormalized analytics fields (strategy, underlying,
# action_taken, conviction, rejection_rule_ids, etc.) are first-class indexed
# columns derived from those blobs at write time so WP-7 can GROUP BY / WHERE
# without unpacking JSON.
#
# rejection_rule_ids stored as JSON list for SQLite/Postgres portability;
# GIN-indexed array column is a Postgres-era optimization deferred to WP-7.
# ---------------------------------------------------------------------------
journal_records_table = Table(
    "journal_records",
    metadata,
    Column("cycle_id", String, primary_key=True),
    Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
    # Denormalized primary grouping key — top-level for WP-7 analytics
    Column("action_taken", String, nullable=False, index=True),
    # Full source-of-truth blobs
    Column("decision", JSON, nullable=False),
    Column("context_snapshot", JSON, nullable=False),
    # Position/order linkage (soft references — positions may not exist yet on
    # NO_ACTION/REJECTED cycles)
    Column("position_ids", JSON, nullable=False),  # list[str]
    Column("order_ids", JSON, nullable=False),  # list[str]
    # Denormalized analytics index — derived from decision/context at write time
    Column("strategy", String, nullable=True, index=True),
    Column("underlying", String, nullable=True, index=True),
    Column("net_delta_at_open", Float, nullable=True),
    Column("earnings_within_dte", Boolean, nullable=True),
    Column("conviction", Float, nullable=True),
    Column("iv_rank_at_open", Float, nullable=True),
    # Versioning — top-level so before/after analysis can filter without
    # unpacking nested snapshots
    Column("limits_version", String, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("model_id", String, nullable=False),
    # Rejection index — JSON list for portability; non-empty only when
    # action_taken == REJECTED
    Column("rejection_rule_ids", JSON, nullable=False),  # list[str]
)

# ---------------------------------------------------------------------------
# outcome_records — append-only; one row per terminal-ish position event.
#
# Multiple rows per position are normal (partial close, roll, full close).
# Join spine: position_id → positions.id → journal_records.position_ids.
# Not linked to cycle_id: monitor-driven closes have no entry-cycle record.
# ---------------------------------------------------------------------------
outcome_records_table = Table(
    "outcome_records",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "position_id",
        String,
        sa.ForeignKey("positions.id"),
        nullable=False,
        index=True,
    ),
    Column("event_type", String, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False, index=True),
    Column("contracts_closed", Integer, nullable=False),
    Column("realized_pnl", Float, nullable=False),
    Column("fill_price", Float, nullable=True),
    Column("closing_order_id", String, nullable=True),
)


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def build_engine(url: str) -> Engine:
    """Create a SQLAlchemy engine from a connection URL.

    SQLite connections get check_same_thread=False so the engine can be shared
    across the main thread and any background reconcile tasks, and
    foreign_keys=ON so FK constraints are enforced (matching Postgres behaviour).
    """
    if url.startswith("sqlite"):
        engine = sa.create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # type: ignore[misc]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return sa.create_engine(url)


@contextmanager
def get_connection(engine: Engine) -> Generator[Connection, None, None]:
    """Yield a transactional connection; commit on success, rollback on error."""
    with engine.begin() as conn:
        yield conn
