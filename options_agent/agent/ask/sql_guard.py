"""WP-9.8: SELECT-only SQL guardrail for the Ask-the-journal analyst.

Two-layer defense: sqlglot AST validation (validate_select_only) rejects
non-SELECT / multi-statement input before it ever reaches the database, and
every query additionally runs on a connection opened via
state.db.build_engine(url, read_only=True) — PRAGMA query_only=ON at the
SQLite level — as a backstop against anything the AST check misses.

Targets SQLite only: the only dialect the console deploys against (see
state/db.py's build_engine dialect branching and docker-compose.yml, where
the Postgres service is profile:test only and explicitly not used by the
agent). execute_guarded_select raises SqlGuardError if handed a non-SQLite
connection rather than silently degrading (e.g. skipping the timeout).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import sqlglot
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Connection
from sqlglot import exp
from sqlglot.errors import ParseError

# Ticket-suggested defaults (WP-9.8 Phase 3 decision): 500 rows / 5s per query.
DEFAULT_ROW_CAP = 500
DEFAULT_TIMEOUT_SECS = 5.0

# SQLite invokes the progress-handler callback every N virtual-machine
# instructions. 1000 balances timeout responsiveness against the per-call
# overhead of a Python callback firing repeatedly inside the query loop.
_PROGRESS_HANDLER_N_INSTRUCTIONS = 1000

# Allow-list, not deny-list: sqlglot has no single expression type per unsafe
# statement kind (VACUUM/REINDEX/EXPLAIN/PRAGMA/ATTACH all parse to different,
# inconsistent node types — some even fall back to a generic Command). Only
# accepting known read-only top-level node types is the one approach that
# doesn't require enumerating every unsafe statement kind up front.
_SELECT_LIKE: tuple[type, ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
)


class SqlGuardError(Exception):
    """A submitted query failed validation, or errored/timed out on execution."""


@dataclass(frozen=True)
class GuardedQueryResult:
    columns: list[str]
    rows: list[dict[str, object]]
    truncated: bool
    row_cap: int


def validate_select_only(sql: str) -> None:
    """Raise SqlGuardError unless sql is exactly one SELECT-like statement.

    "SELECT-like" includes UNION/INTERSECT/EXCEPT of SELECTs (still strictly
    read-only), but nothing else — no trailing semicolons, no multi-statement
    input, no DDL/DML/PRAGMA/ATTACH/transaction-control statements.
    """
    if not sql or not sql.strip():
        raise SqlGuardError("Empty query.")
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="sqlite") if s is not None]
    except ParseError as exc:
        raise SqlGuardError(f"Could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise SqlGuardError(
            "Exactly one SQL statement is allowed per query; found "
            f"{len(statements)}. Remove any trailing semicolons or extra statements."
        )

    stmt = statements[0]
    if not isinstance(stmt, _SELECT_LIKE):
        raise SqlGuardError(
            f"Only SELECT statements are allowed; got {type(stmt).__name__}."
        )


@contextmanager
def _sqlite_deadline(
    raw_conn: sqlite3.Connection, timeout_secs: float
) -> Iterator[None]:
    """Abort the in-flight query once timeout_secs has elapsed.

    SQLite has no native statement-timeout setting; set_progress_handler's
    callback is polled periodically during query execution and can abort it
    by returning non-zero, which is the closest primitive SQLite offers.
    """
    deadline = time.monotonic() + timeout_secs

    def _handler() -> int:
        return 1 if time.monotonic() > deadline else 0

    raw_conn.set_progress_handler(_handler, _PROGRESS_HANDLER_N_INSTRUCTIONS)
    try:
        yield
    finally:
        raw_conn.set_progress_handler(None, 0)


def execute_guarded_select(
    conn: Connection,
    sql: str,
    *,
    row_cap: int = DEFAULT_ROW_CAP,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
) -> GuardedQueryResult:
    """Validate and run sql on a read-only SQLite connection.

    conn should be opened via state.db.build_engine(url, read_only=True) —
    this function does not itself verify that (same trust-the-caller contract
    as ui/app.py), but it does verify the underlying driver is sqlite3, since
    the timeout mechanism is SQLite-specific.

    Raises SqlGuardError if the query is not a single SELECT-like statement,
    if it errors during execution, or if it exceeds timeout_secs.
    """
    validate_select_only(sql)

    raw_conn = conn.connection.driver_connection
    if not isinstance(raw_conn, sqlite3.Connection):
        raise SqlGuardError(
            f"run_sql only supports SQLite connections; got {type(raw_conn).__name__}."
        )

    with _sqlite_deadline(raw_conn, timeout_secs):
        try:
            cursor = conn.exec_driver_sql(sql)
        except sa_exc.DBAPIError as exc:
            if "interrupted" in str(exc).lower():
                raise SqlGuardError(
                    f"Query exceeded the {timeout_secs:.0f}s statement timeout."
                ) from exc
            raise SqlGuardError(f"Query failed: {exc.orig}") from exc

        columns = list(cursor.keys())
        rows = cursor.fetchmany(row_cap + 1)

    truncated = len(rows) > row_cap
    if truncated:
        rows = rows[:row_cap]
    return GuardedQueryResult(
        columns=columns,
        rows=[dict(zip(columns, row, strict=True)) for row in rows],
        truncated=truncated,
        row_cap=row_cap,
    )
