"""WP-9.1: read_only + WAL support in state.db.build_engine."""

from __future__ import annotations

import os
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from options_agent.state.db import (
    build_engine,
    get_connection,
    iv_history_table,
    metadata,
)


def _seed_row(engine: sa.engine.Engine) -> None:
    metadata.create_all(engine)
    with get_connection(engine) as conn:
        conn.execute(
            iv_history_table.insert().values(
                symbol="SPY", observation_date=date(2026, 7, 1), atm_iv=0.18
            )
        )


def test_sqlite_read_only_engine_reads_but_rejects_writes(tmp_path):
    url = f"sqlite:///{tmp_path / 'console_ro.db'}"

    writable = build_engine(url)
    _seed_row(writable)

    read_only = build_engine(url, read_only=True)
    with read_only.connect() as conn:
        rows = conn.execute(sa.select(iv_history_table)).fetchall()
    assert len(rows) == 1

    with pytest.raises(DBAPIError):
        with read_only.connect() as conn:
            conn.execute(
                iv_history_table.insert().values(
                    symbol="QQQ", observation_date=date(2026, 7, 2), atm_iv=0.20
                )
            )
            conn.commit()

    writable.dispose()
    read_only.dispose()


def test_sqlite_memory_read_only_engine_rejects_ddl():
    engine = build_engine("sqlite:///:memory:", read_only=True)

    with pytest.raises(DBAPIError):
        metadata.create_all(engine)

    engine.dispose()


def test_sqlite_file_backed_engine_enables_wal(tmp_path):
    url = f"sqlite:///{tmp_path / 'console_wal.db'}"

    engine = build_engine(url)
    metadata.create_all(engine)
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()

    assert mode == "wal"
    engine.dispose()


def test_sqlite_memory_engine_does_not_enable_wal():
    # WAL mode is unsupported for :memory: databases; build_engine must skip
    # the pragma there rather than issuing a pointless/confusing statement.
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()

    assert mode == "memory"
    engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("DB_URL", "").startswith("postgresql"),
    reason="Postgres-only; CI's postgres matrix leg sets DB_URL",
)
def test_postgres_read_only_engine_rejects_writes():
    url = os.environ["DB_URL"]

    writable = build_engine(url)
    metadata.drop_all(writable)
    _seed_row(writable)

    read_only = build_engine(url, read_only=True)
    with read_only.connect() as conn:
        rows = conn.execute(sa.select(iv_history_table)).fetchall()
    assert len(rows) == 1

    with pytest.raises(DBAPIError):
        with read_only.connect() as conn:
            conn.execute(
                iv_history_table.insert().values(
                    symbol="QQQ", observation_date=date(2026, 7, 2), atm_iv=0.20
                )
            )
            conn.commit()

    metadata.drop_all(writable)
    writable.dispose()
    read_only.dispose()
