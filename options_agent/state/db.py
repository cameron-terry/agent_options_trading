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
    Index,
    Integer,
    MetaData,
    String,
    Table,
    event,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

metadata = MetaData()

# ---------------------------------------------------------------------------
# iv_history — append-once-per-trading-day; one ATM IV observation per symbol.
#
# Accumulates the daily ATM IV scalar used to compute iv_rank and iv_percentile
# (WP-3.4). Observation discipline: one row per (symbol, date); a re-run on
# the same market day updates atm_iv rather than inserting a duplicate (the
# unique constraint enforces this via data/iv_rank.record_daily_iv()).
#
# Compute rank/percentile on read from the trailing 252-row window; never store
# the derived metrics — they change every day as the window rolls, and storing
# them creates a staleness risk with no compensating benefit.
#
# "current IV" definition (must be consistent across stored history and live
# compute): ATM call at the nearest-to-30-DTE expiration. See data/iv_rank.py.
# ---------------------------------------------------------------------------
iv_history_table = Table(
    "iv_history",
    metadata,
    Column("symbol", String, nullable=False),
    Column("observation_date", Date, nullable=False),
    Column("atm_iv", Float, nullable=False),
    sa.UniqueConstraint("symbol", "observation_date", name="uq_iv_history_symbol_date"),
    Index("ix_iv_history_symbol_date", "symbol", "observation_date"),
)

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
    Column("exit_plan", JSON, nullable=True),  # ExitPlan; None for EQUITY positions
    Column("status", String, nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False, index=True),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column("nearest_expiration", Date, nullable=False),
    Column("est_max_loss", Float, nullable=False),
    Column("est_max_profit", Float, nullable=False),
    Column("opening_order_id", String, nullable=False),
    # WP-1.5: assignment/expiry support
    Column("asset_class", String, nullable=True),
    Column("equity_legs", JSON, nullable=True),
    Column("assigned_from_position_id", String, nullable=True),
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
    # WP-5.5: ExitReason enum value; NULL for opening orders and pre-WP-5.5 records.
    Column("exit_reason", String, nullable=True),
)

# ---------------------------------------------------------------------------
# fill_events — append-only; one row per broker fill execution.
#
# broker_exec_id is a unique idempotency key: "{broker_order_id}@{cumulative_qty}".
# filled_qty is the INCREMENTAL quantity for this execution (not cumulative).
# occurred_at is broker-reported fill time; observed_at is reconcile-pass time.
# ---------------------------------------------------------------------------
fill_events_table = Table(
    "fill_events",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "order_id",
        String,
        sa.ForeignKey("orders.id"),
        nullable=False,
        index=True,
    ),
    Column("broker_exec_id", String, nullable=False, unique=True),
    Column("leg_symbol", String, nullable=False),
    Column("filled_qty", Integer, nullable=False),
    Column("fill_price", Float, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
)

# ---------------------------------------------------------------------------
# Mutability boundary: positions and orders are mutable-in-place (status
# transitions, fill updates). journal_records, outcome_records, and
# fill_events are append-only — application code must never UPDATE those tables.
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
    # WP-5.5: ExitReason enum value; NULL for pre-WP-5.5 records and non-monitor closes.
    Column("exit_reason", String, nullable=True),
)


# ---------------------------------------------------------------------------
# kill_switch_log — append-only; one row per state change.
#
# Current state = latest row by created_at (tie-broken by id, UUID descending).
# Append-only by design: a safety/audit record that is UPDATE'd in place is no
# longer an audit record — the change history is exactly what post-incident
# analysis needs.
#
# The reason column is required: in an emergency the operator must record why
# the switch was set, which feeds post-mortems and WP-7.2 alerting.
# ---------------------------------------------------------------------------
kill_switch_log_table = Table(
    "kill_switch_log",
    metadata,
    Column("id", String, primary_key=True),
    Column("state", String, nullable=False),  # KillSwitchState value
    Column("set_by", String, nullable=False),  # operator name or script identifier
    Column("reason", String, nullable=False),  # required context for audit
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
)


# ---------------------------------------------------------------------------
# alert_delivery_failures — append-only; one row per exhausted-retry send attempt.
#
# Written by AlertDispatcher when all retry attempts for an alert are exhausted.
# The individual alert is dropped (delivery is best-effort), but the fact that
# delivery failed is durable and queryable so WP-7 review can surface
# "N undelivered CRITICAL alerts last week" without relying on log lines.
#
# event_type/severity stored as String (AlertEventType/AlertSeverity values);
# no FK to journal_records — delivery failures may originate outside the
# entry-cycle context (e.g. kill-switch changes fired from the CLI).
# ---------------------------------------------------------------------------
alert_delivery_failures_table = Table(
    "alert_delivery_failures",
    metadata,
    Column("id", String, primary_key=True),
    Column("event_type", String, nullable=False),
    Column("severity", String, nullable=False),
    Column("detail", String, nullable=False),
    Column("attempted_at", DateTime(timezone=True), nullable=False, index=True),
    Column("attempts", Integer, nullable=False),
    Column("last_error", String, nullable=False),
)


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def build_engine(url: str, *, read_only: bool = False) -> Engine:
    """Create a SQLAlchemy engine from a connection URL.

    SQLite connections get check_same_thread=False so the engine can be shared
    across the main thread and any background tasks (e.g. AlertDispatcher's
    worker thread), and foreign_keys=ON so FK constraints are enforced
    (matching Postgres behaviour). Writable, file-based SQLite connections
    also get journal_mode=WAL so a writer (the scheduler) doesn't block a
    reader (the WP-9 console) — setting it is idempotent and persists in the
    DB file, so it only needs to happen on some writable connection, not
    specifically the first: the scheduler's own build_engine(db_url) call
    (read_only=False) runs on every startup, ahead of the console.

    read_only=True engines never issue journal_mode=WAL (or any other pragma
    that changes the file) — only PRAGMA query_only, so this engine performs
    no write of its own. Note this is not an absolute read-only guarantee:
    opening a *brand new* SQLite file still creates it on connect regardless
    of query_only (a SQLite driver behaviour, not a statement we issue) — in
    practice this never happens because the writable/scheduler engine always
    creates the file first.

    :memory: URLs additionally use StaticPool so that all engine.connect()
    calls share the same DBAPI connection. Without StaticPool, each new
    connection opens a fresh empty database — AlertDispatcher's worker thread
    would write to a different (empty) :memory: DB than the one the test just
    populated with create_all(). File-based SQLite and Postgres are unaffected.

    read_only=True enforces read-only access at the session/connection level
    (SQLite: PRAGMA query_only; Postgres: default_transaction_read_only) —
    same DB_URL and credentials as the writable engine, no separate DB role
    to provision. Used by the WP-9 console; a write attempted through this
    engine fails.
    """
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
        engine = sa.create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # type: ignore[misc]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if read_only:
                cursor.execute("PRAGMA query_only=ON")
            elif ":memory:" not in url:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    engine = sa.create_engine(url)
    if read_only:

        @event.listens_for(engine, "connect")
        def _set_postgres_read_only(dbapi_conn, _record):  # type: ignore[misc]
            cursor = dbapi_conn.cursor()
            cursor.execute("SET default_transaction_read_only = on")
            cursor.close()
            # psycopg2 opens an implicit transaction around the SET above;
            # without committing it here, SQLAlchemy's pool-checkin ROLLBACK
            # (or any later rollback) discards the setting along with it —
            # default_transaction_read_only is session-level but still lives
            # inside the enclosing transaction until committed.
            dbapi_conn.commit()

    return engine


@contextmanager
def get_connection(engine: Engine) -> Generator[Connection, None, None]:
    """Yield a transactional connection; commit on success, rollback on error."""
    with engine.begin() as conn:
        yield conn
