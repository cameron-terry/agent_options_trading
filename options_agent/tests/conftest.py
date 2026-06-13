"""Shared test fixtures for the options_agent test suite."""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.pool import NullPool, StaticPool

from options_agent.state.db import metadata


@pytest.fixture
def engine():
    """SQLAlchemy engine for state tests.

    Reads DB_URL from the environment; falls back to an in-memory SQLite
    database.  Running pytest with DB_URL=postgresql://... exercises the
    Postgres dialect — the CI matrix sets this automatically.

    Isolation: tables are created before each test and dropped after, so
    tests never share row state regardless of backend.
    """
    url = os.environ.get("DB_URL") or "sqlite:///:memory:"

    if url.startswith("sqlite"):
        # StaticPool keeps a single connection alive for the lifetime of the
        # engine, which is required for :memory: databases (each new connection
        # would otherwise get a blank database).
        eng = sa.create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # type: ignore[misc]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    else:
        # NullPool prevents connection-pool state from leaking between tests
        # when multiple tests hit the same Postgres instance in sequence.
        eng = sa.create_engine(url, poolclass=NullPool)

    metadata.create_all(eng)
    yield eng
    metadata.drop_all(eng)
    eng.dispose()
