"""Tests for WP-2.1: database schema creation and Alembic migration lifecycle."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from alembic import command
from options_agent.state.db import (
    build_engine,
    get_connection,
    journal_records_table,
    metadata,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_TABLES = {
    "positions",
    "orders",
    "journal_records",
    "outcome_records",
    "alert_delivery_failures",
}

_POSITIONS_COLS = {
    "id",
    "underlying",
    "strategy",
    "legs",
    "quantity",
    "entry_net_amount",
    "current_mark",
    "marked_at",
    "unrealized_pnl",
    "realized_pnl",
    "exit_plan",
    "status",
    "opened_at",
    "closed_at",
    "nearest_expiration",
    "est_max_loss",
    "est_max_profit",
    "opening_order_id",
    "asset_class",
    "equity_legs",
    "assigned_from_position_id",
}

_ORDERS_COLS = {
    "id",
    "broker_order_id",
    "position_id",
    "role",
    "status",
    "broker_status_raw",
    "submitted_at",
    "filled_at",
    "legs_filled",
    "net_fill_price",
    "filled_qty",
    "exit_reason",
}

_JOURNAL_COLS = {
    "cycle_id",
    "timestamp",
    "action_taken",
    "decision",
    "context_snapshot",
    "position_ids",
    "order_ids",
    "strategy",
    "underlying",
    "net_delta_at_open",
    "earnings_within_dte",
    "conviction",
    "iv_rank_at_open",
    "limits_version",
    "prompt_version",
    "model_id",
    "rejection_rule_ids",
    "data_quality_flags",
}

_OUTCOME_COLS = {
    "id",
    "position_id",
    "event_type",
    "recorded_at",
    "contracts_closed",
    "realized_pnl",
    "fill_price",
    "closing_order_id",
    "exit_reason",
}

_ALERT_DELIVERY_FAILURES_COLS = {
    "id",
    "event_type",
    "severity",
    "detail",
    "attempted_at",
    "attempts",
    "last_error",
}


@pytest.fixture
def make_alembic_cfg(monkeypatch: pytest.MonkeyPatch):
    """Return a factory that builds an Alembic Config pointing at a SQLite file.

    Unsets DB_URL for the duration of each migration test so alembic/env.py's
    unconditional DB_URL override cannot redirect the migration to Postgres.
    """
    monkeypatch.delenv("DB_URL", raising=False)

    def _make(db_path: str) -> Config:
        project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        ini = os.path.join(project_root, "alembic.ini")
        cfg = Config(ini)
        cfg.set_main_option("script_location", os.path.join(project_root, "alembic"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        return cfg

    return _make


# ---------------------------------------------------------------------------
# Schema creation (metadata.create_all)
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    metadata.create_all(eng)
    yield eng
    eng.dispose()


def test_create_all_produces_all_tables(mem_engine):
    tables = set(inspect(mem_engine).get_table_names())
    assert _ALL_TABLES <= tables


def test_positions_columns_complete(mem_engine):
    cols = {c["name"] for c in inspect(mem_engine).get_columns("positions")}
    assert cols == _POSITIONS_COLS


def test_orders_columns_complete(mem_engine):
    cols = {c["name"] for c in inspect(mem_engine).get_columns("orders")}
    assert cols == _ORDERS_COLS


def test_journal_records_columns_complete(mem_engine):
    cols = {c["name"] for c in inspect(mem_engine).get_columns("journal_records")}
    assert cols == _JOURNAL_COLS


def test_outcome_records_columns_complete(mem_engine):
    cols = {c["name"] for c in inspect(mem_engine).get_columns("outcome_records")}
    assert cols == _OUTCOME_COLS


def test_alert_delivery_failures_columns_complete(mem_engine):
    cols = {
        c["name"] for c in inspect(mem_engine).get_columns("alert_delivery_failures")
    }
    assert cols == _ALERT_DELIVERY_FAILURES_COLS


def test_positions_primary_key(mem_engine):
    pk = inspect(mem_engine).get_pk_constraint("positions")
    assert pk["constrained_columns"] == ["id"]


def test_orders_primary_key(mem_engine):
    pk = inspect(mem_engine).get_pk_constraint("orders")
    assert pk["constrained_columns"] == ["id"]


def test_journal_records_primary_key(mem_engine):
    pk = inspect(mem_engine).get_pk_constraint("journal_records")
    assert pk["constrained_columns"] == ["cycle_id"]


def test_outcome_records_primary_key(mem_engine):
    pk = inspect(mem_engine).get_pk_constraint("outcome_records")
    assert pk["constrained_columns"] == ["id"]


def test_orders_foreign_key_to_positions(mem_engine):
    fks = inspect(mem_engine).get_foreign_keys("orders")
    assert any(
        fk["referred_table"] == "positions"
        and fk["constrained_columns"] == ["position_id"]
        for fk in fks
    )


def test_outcome_records_foreign_key_to_positions(mem_engine):
    fks = inspect(mem_engine).get_foreign_keys("outcome_records")
    assert any(
        fk["referred_table"] == "positions"
        and fk["constrained_columns"] == ["position_id"]
        for fk in fks
    )


# ---------------------------------------------------------------------------
# Alembic migration: upgrade → verify → downgrade → verify
# ---------------------------------------------------------------------------


def test_migration_upgrade_creates_all_tables(make_alembic_cfg):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        command.upgrade(make_alembic_cfg(db_path), "head")
        eng = create_engine(f"sqlite:///{db_path}")
        tables = set(inspect(eng).get_table_names())
        eng.dispose()
        assert _ALL_TABLES <= tables
    finally:
        os.unlink(db_path)


def test_migration_downgrade_removes_all_tables(make_alembic_cfg):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        cfg = make_alembic_cfg(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        eng = create_engine(f"sqlite:///{db_path}")
        tables = set(inspect(eng).get_table_names())
        eng.dispose()
        # alembic_version table may remain; application tables must be gone
        assert not (_ALL_TABLES & tables)
    finally:
        os.unlink(db_path)


def test_migration_columns_match_metadata(make_alembic_cfg):
    """Migration-created schema must have identical columns to metadata.create_all.

    Compares both paths directly so drift between db.py and
    001_initial_schema.py is caught without a hardcoded intermediary.
    """
    # metadata path
    meta_eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    metadata.create_all(meta_eng)
    meta_cols = {
        tbl: {c["name"] for c in inspect(meta_eng).get_columns(tbl)}
        for tbl in _ALL_TABLES
    }
    meta_eng.dispose()

    # migration path
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        command.upgrade(make_alembic_cfg(db_path), "head")
        mig_eng = create_engine(f"sqlite:///{db_path}")
        mig_cols = {
            tbl: {c["name"] for c in inspect(mig_eng).get_columns(tbl)}
            for tbl in _ALL_TABLES
        }
        mig_eng.dispose()
    finally:
        os.unlink(db_path)

    assert meta_cols == mig_cols


# ---------------------------------------------------------------------------
# Migration 008: data_quality_flags column + phantom-net-delta backfill
# ---------------------------------------------------------------------------

_PHANTOM_NET_DELTA_CYCLE_IDS = (
    "05c9b8da-6d8e-4a79-b035-72af0f792ec1",
    "e3b3290b-5861-43fe-b9d3-82d50a706856",
    "fbb3bef4-c711-4ef2-8f80-fa0aee22c2ba",
    "6a3a2b02-e7bf-4e03-afad-7e0621579e4f",
)

_JOURNAL_ROW_INSERT = (
    "INSERT INTO journal_records (cycle_id, timestamp, action_taken, decision, "
    "context_snapshot, position_ids, order_ids, limits_version, prompt_version, "
    "model_id, rejection_rule_ids) VALUES (:cycle_id, '2026-07-09T17:00:00+00:00', "
    "'OPENED', '{}', '{}', '[]', '[]', 'v1', 'v1', 'm1', '[]')"
)


def test_migration_008_backfills_only_the_four_phantom_cycles(make_alembic_cfg):
    """Migration 008 flags exactly the 4 known phantom-net-delta cycles.

    Seeds rows at revision 007 (pre-flag schema) for the 4 hardcoded cycle
    IDs plus an unrelated control row, upgrades to head, and asserts only the
    4 phantom rows carry the flag — the control row stays NULL.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        cfg = make_alembic_cfg(db_path)
        command.upgrade(cfg, "007")

        control_id = "cycle-unaffected-001"
        eng = create_engine(f"sqlite:///{db_path}")
        with eng.begin() as conn:
            for cycle_id in (*_PHANTOM_NET_DELTA_CYCLE_IDS, control_id):
                conn.execute(text(_JOURNAL_ROW_INSERT), {"cycle_id": cycle_id})
        eng.dispose()

        command.upgrade(cfg, "008")

        # Read through the mapped Table (not a raw text() SELECT) so
        # sqlalchemy's JSON type applies its own decode pass — this matches
        # state/journal.py's read path, which layers a second, explicit
        # json.loads() on top for the same reason rejection_rule_ids etc. do
        # (see memory: pre-existing double-JSON-encoded JSON columns).
        eng = create_engine(f"sqlite:///{db_path}")
        with eng.connect() as conn:
            rows = conn.execute(
                select(
                    journal_records_table.c.cycle_id,
                    journal_records_table.c.data_quality_flags,
                )
            ).fetchall()
        eng.dispose()

        flags_by_id = {row.cycle_id: row.data_quality_flags for row in rows}
        for cycle_id in _PHANTOM_NET_DELTA_CYCLE_IDS:
            assert json.loads(flags_by_id[cycle_id]) == ["phantom_net_delta"]
        assert flags_by_id[control_id] is None
    finally:
        os.unlink(db_path)


def test_migration_008_downgrade_removes_data_quality_flags_column(make_alembic_cfg):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        cfg = make_alembic_cfg(db_path)
        command.upgrade(cfg, "008")
        command.downgrade(cfg, "007")
        eng = create_engine(f"sqlite:///{db_path}")
        cols = {c["name"] for c in inspect(eng).get_columns("journal_records")}
        eng.dispose()
        assert "data_quality_flags" not in cols
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# build_engine + get_connection
# ---------------------------------------------------------------------------


_POS_INSERT = (
    "INSERT INTO positions (id, underlying, strategy, legs, quantity, "
    "entry_net_amount, current_mark, marked_at, unrealized_pnl, "
    "exit_plan, status, opened_at, nearest_expiration, "
    "est_max_loss, est_max_profit, opening_order_id) "
    "VALUES ('p1', 'SPY', 'bull_put_spread', '[]', 1, -100.0, -80.0, "
    "'2026-06-07T14:30:00+00:00', 20.0, '{}', 'OPEN', "
    "'2026-06-07T14:30:00+00:00', '2026-07-18', 500.0, 100.0, 'o1')"
)


def test_build_engine_sqlite():
    eng = build_engine("sqlite:///:memory:")
    assert eng is not None
    eng.dispose()


def test_build_engine_postgres_url_takes_non_sqlite_path():
    # Verifies the non-sqlite branch calls create_engine without connect_args
    # injection. Patches sa.create_engine so no DBAPI driver is required.
    pg_url = "postgresql+psycopg2://user:pass@localhost/test"
    with patch("options_agent.state.db.sa.create_engine") as mock_ce:
        mock_ce.return_value = MagicMock()
        build_engine(pg_url)
        mock_ce.assert_called_once_with(pg_url)


def test_get_connection_commits_on_success():
    eng = build_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    with get_connection(eng) as conn:
        conn.execute(text(_POS_INSERT))
    with get_connection(eng) as conn:
        row = conn.execute(text("SELECT id FROM positions WHERE id = 'p1'")).fetchone()
    assert row is not None
    eng.dispose()


def test_get_connection_rolls_back_on_error():
    eng = build_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    # Insert a row in one transaction, then fail a second — row must not appear.
    with get_connection(eng) as conn:
        conn.execute(text(_POS_INSERT))
    try:
        with get_connection(eng) as conn:
            conn.execute(
                text(
                    "INSERT INTO positions (id, underlying, strategy, legs, quantity, "
                    "entry_net_amount, current_mark, marked_at, unrealized_pnl, "
                    "exit_plan, status, opened_at, nearest_expiration, "
                    "est_max_loss, est_max_profit, opening_order_id) "
                    "VALUES ('p2', 'QQQ', 'iron_condor', '[]', 1, -200.0, -180.0, "
                    "'2026-06-07T14:30:00+00:00', 20.0, '{}', 'OPEN', "
                    "'2026-06-07T14:30:00+00:00', '2026-07-18', 600.0, 150.0, 'o2')"
                )
            )
            raise RuntimeError("simulated failure")
    except RuntimeError:
        pass
    with get_connection(eng) as conn:
        row = conn.execute(text("SELECT id FROM positions WHERE id = 'p2'")).fetchone()
    assert row is None, "rolled-back row must not be visible after error"
    eng.dispose()
