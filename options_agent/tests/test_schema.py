"""Tests for WP-2.1: database schema creation and Alembic migration lifecycle."""

from __future__ import annotations

import os
import tempfile

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from options_agent.state.db import build_engine, get_connection, metadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_TABLES = {"positions", "orders", "journal_records", "outcome_records"}

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
}


def _alembic_cfg(db_path: str) -> Config:
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    ini = os.path.join(project_root, "alembic.ini")
    cfg = Config(ini)
    cfg.set_main_option("script_location", os.path.join(project_root, "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


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


# ---------------------------------------------------------------------------
# Alembic migration: upgrade → verify → downgrade → verify
# ---------------------------------------------------------------------------


def test_migration_upgrade_creates_all_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        command.upgrade(_alembic_cfg(db_path), "head")
        eng = create_engine(f"sqlite:///{db_path}")
        tables = set(inspect(eng).get_table_names())
        eng.dispose()
        assert _ALL_TABLES <= tables
    finally:
        os.unlink(db_path)


def test_migration_downgrade_removes_all_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        eng = create_engine(f"sqlite:///{db_path}")
        tables = set(inspect(eng).get_table_names())
        eng.dispose()
        # alembic_version table may remain; application tables must be gone
        assert not (_ALL_TABLES & tables)
    finally:
        os.unlink(db_path)


def test_migration_schema_matches_metadata():
    """Columns after migration must match what metadata.create_all would produce."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        command.upgrade(_alembic_cfg(db_path), "head")
        eng = create_engine(f"sqlite:///{db_path}")
        insp = inspect(eng)
        assert {c["name"] for c in insp.get_columns("positions")} == _POSITIONS_COLS
        assert {c["name"] for c in insp.get_columns("orders")} == _ORDERS_COLS
        assert {c["name"] for c in insp.get_columns("journal_records")} == _JOURNAL_COLS
        assert {c["name"] for c in insp.get_columns("outcome_records")} == _OUTCOME_COLS
        eng.dispose()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# build_engine + get_connection
# ---------------------------------------------------------------------------


def test_build_engine_sqlite():
    eng = build_engine("sqlite:///:memory:")
    assert eng is not None
    eng.dispose()


def test_get_connection_commits_on_success():
    eng = build_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    with get_connection(eng) as conn:
        conn.execute(
            text(
                "INSERT INTO positions (id, underlying, strategy, legs, quantity, "
                "entry_net_amount, current_mark, marked_at, unrealized_pnl, "
                "exit_plan, status, opened_at, nearest_expiration, "
                "est_max_loss, est_max_profit, opening_order_id) "
                "VALUES ('p1', 'SPY', 'bull_put_spread', '[]', 1, -100.0, -80.0, "
                "'2026-06-07T14:30:00+00:00', 20.0, '{}', 'OPEN', "
                "'2026-06-07T14:30:00+00:00', '2026-07-18', 500.0, 100.0, 'o1')"
            )
        )
    with get_connection(eng) as conn:
        row = conn.execute(text("SELECT id FROM positions WHERE id = 'p1'")).fetchone()
    assert row is not None
    eng.dispose()
