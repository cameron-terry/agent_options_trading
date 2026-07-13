"""WP-9.8: SELECT-only SQL guardrail tests (agent/sql_guard.py).

No Anthropic API calls anywhere in this file — validate_select_only and
execute_guarded_select are pure/DB-only and are tested directly.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from options_agent.agent.sql_guard import (
    SqlGuardError,
    execute_guarded_select,
    validate_select_only,
)
from options_agent.state.db import build_engine, metadata

# ──────────────────────────────────────────────────────────────────────────────
# validate_select_only — adversarial fixture suite
# ──────────────────────────────────────────────────────────────────────────────

_REJECTED_QUERIES = [
    ("insert", "INSERT INTO positions (id) VALUES ('x')"),
    ("update", "UPDATE positions SET status = 'CLOSED'"),
    ("delete", "DELETE FROM positions"),
    ("create_table", "CREATE TABLE evil (id INT)"),
    ("drop_table", "DROP TABLE positions"),
    ("alter_table", "ALTER TABLE positions ADD COLUMN evil TEXT"),
    ("pragma", "PRAGMA table_info(positions)"),
    ("attach", "ATTACH DATABASE 'other.db' AS other"),
    ("vacuum", "VACUUM"),
    ("reindex", "REINDEX"),
    ("begin", "BEGIN"),
    ("commit", "COMMIT"),
    (
        "multi_statement_select_then_drop",
        "SELECT * FROM journal_records; DROP TABLE journal_records;",
    ),
    ("multi_statement_two_selects", "SELECT 1; SELECT 2;"),
    ("empty", ""),
    ("whitespace_only", "   \n\t "),
    ("unparseable_garbage", "SELECT FROM WHERE ;;; garbage :::"),
]


@pytest.mark.parametrize(
    "name,sql", _REJECTED_QUERIES, ids=[name for name, _ in _REJECTED_QUERIES]
)
def test_validate_select_only_rejects(name: str, sql: str) -> None:
    with pytest.raises(SqlGuardError):
        validate_select_only(sql)


_ACCEPTED_QUERIES = [
    ("simple_select", "SELECT * FROM journal_records"),
    ("select_with_limit", "SELECT cycle_id FROM journal_records LIMIT 10"),
    (
        "select_with_subquery",
        "SELECT * FROM journal_records WHERE cycle_id = "
        "(SELECT cycle_id FROM journal_records LIMIT 1)",
    ),
    (
        "select_with_cte",
        "WITH recent AS (SELECT * FROM journal_records) SELECT * FROM recent",
    ),
    (
        "union",
        "SELECT cycle_id FROM journal_records"
        " UNION SELECT position_id FROM outcome_records",
    ),
    ("trailing_semicolon", "SELECT * FROM journal_records;"),
    (
        "trailing_comment_disguising_nothing",
        "SELECT * FROM journal_records -- ; DROP TABLE journal_records",
    ),
]


@pytest.mark.parametrize(
    "name,sql", _ACCEPTED_QUERIES, ids=[name for name, _ in _ACCEPTED_QUERIES]
)
def test_validate_select_only_accepts(name: str, sql: str) -> None:
    validate_select_only(sql)  # must not raise


# ──────────────────────────────────────────────────────────────────────────────
# execute_guarded_select — row cap, timeout, error surfacing
# ──────────────────────────────────────────────────────────────────────────────


def _seeded_engine(n_rows: int) -> sa.Engine:
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as conn:
        for i in range(n_rows):
            conn.exec_driver_sql(
                "INSERT INTO journal_records (cycle_id, timestamp, action_taken,"
                " decision, context_snapshot, position_ids, order_ids,"
                " limits_version, prompt_version, model_id, rejection_rule_ids)"
                " VALUES (?, '2026-01-01', 'NO_ACTION_AGENT', '{}', '{}', '[]',"
                " '[]', '1', '1', 'm', '[]')",
                (f"c{i}",),
            )
    return engine


def test_execute_guarded_select_rejects_non_select() -> None:
    engine = _seeded_engine(1)
    with engine.connect() as conn, pytest.raises(SqlGuardError):
        execute_guarded_select(conn, "DELETE FROM journal_records")


def test_execute_guarded_select_returns_rows_under_cap() -> None:
    engine = _seeded_engine(3)
    with engine.connect() as conn:
        result = execute_guarded_select(
            conn, "SELECT cycle_id FROM journal_records ORDER BY cycle_id", row_cap=500
        )
    assert result.truncated is False
    assert result.row_cap == 500
    assert [r["cycle_id"] for r in result.rows] == ["c0", "c1", "c2"]
    assert result.columns == ["cycle_id"]


def test_execute_guarded_select_truncates_over_cap() -> None:
    engine = _seeded_engine(12)
    with engine.connect() as conn:
        result = execute_guarded_select(
            conn, "SELECT cycle_id FROM journal_records", row_cap=10
        )
    assert result.truncated is True
    assert len(result.rows) == 10


def test_execute_guarded_select_surfaces_execution_errors() -> None:
    engine = _seeded_engine(1)
    with engine.connect() as conn, pytest.raises(SqlGuardError, match="Query failed"):
        execute_guarded_select(conn, "SELECT * FROM no_such_table")


def test_execute_guarded_select_enforces_timeout() -> None:
    engine = _seeded_engine(1)
    # A recursive CTE that would otherwise run for a very long time; an
    # effectively-zero timeout guarantees the progress-handler deadline has
    # already passed by the first callback tick, regardless of machine speed.
    slow_query = (
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt"
        " WHERE x < 100000000) SELECT count(*) FROM cnt"
    )
    with engine.connect() as conn, pytest.raises(SqlGuardError, match="timeout"):
        execute_guarded_select(conn, slow_query, timeout_secs=0.0)


def test_execute_guarded_select_rejects_non_sqlite_connection() -> None:
    from unittest.mock import MagicMock

    fake_conn = MagicMock()
    fake_conn.connection.driver_connection = object()
    with pytest.raises(SqlGuardError, match="only supports SQLite"):
        execute_guarded_select(fake_conn, "SELECT 1")
