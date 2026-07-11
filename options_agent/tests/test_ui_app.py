"""WP-9.1: FastAPI console skeleton (options_agent.ui.app)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from options_agent.config import Config
from options_agent.state.db import build_engine, metadata
from options_agent.ui.app import create_app


def _migrated_engine():
    """A schema-complete engine, including the alembic_version bookkeeping
    table health() checks for — metadata.create_all() alone (SQLAlchemy DDL
    from the Table objects) does not create it; only alembic itself does."""
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(sa.text("INSERT INTO alembic_version VALUES ('test-head')"))
    return engine


def test_health_ok_when_db_reachable():
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_error_when_db_unreachable():
    broken_engine = MagicMock()
    broken_engine.connect.side_effect = RuntimeError("db unreachable")

    app = create_app(engine=broken_engine)
    client = TestClient(app)

    resp = client.get("/api/health")

    assert resp.status_code == 503
    assert resp.json() == {"status": "error"}


def test_health_error_when_db_has_no_schema():
    # A reachable DB that has never been migrated (or crashed mid-migration
    # before alembic_version was written): SELECT 1 would report this as
    # healthy since it touches no table. health() must not.
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)  # app tables exist; alembic_version does not

    app = create_app(engine=engine)
    client = TestClient(app)

    resp = client.get("/api/health")

    assert resp.status_code == 503
    assert resp.json() == {"status": "error"}


def test_root_returns_404_when_spa_not_built():
    # No options_agent/ui/static/ in the source tree — it's produced by the
    # Docker multi-stage build. The skeleton must not crash without it.
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    resp = client.get("/")

    assert resp.status_code == 404


def test_root_serves_built_spa(monkeypatch, tmp_path):
    index = tmp_path / "index.html"
    index.write_text("<html><body>console shell</body></html>")
    monkeypatch.setattr("options_agent.ui.app.STATIC_DIR", tmp_path)

    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    resp = client.get("/")

    assert resp.status_code == 200
    assert "console shell" in resp.text


def test_create_app_engine_is_read_only_by_default_wiring(monkeypatch, tmp_path):
    # create_app(engine=...) is the test-injection seam; production wiring
    # (no engine passed) must build a read-only engine, not a writable one.
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path / 'wiring.db'}")

    app = create_app()
    engine = app.state.engine
    try:
        with pytest.raises(DBAPIError):
            metadata.create_all(engine)
    finally:
        engine.dispose()


def test_overview_mode_defaults_to_paper_when_no_config_given():
    # Config.alpaca_paper defaults to True — the safe default when a test
    # injects only an engine, matching create_app's own fallback.
    app = create_app(engine=_migrated_engine())
    client = TestClient(app)

    resp = client.get("/api/overview")

    assert resp.json()["mode"] == "paper"


def test_overview_mode_reflects_config_alpaca_paper_false():
    app = create_app(
        engine=_migrated_engine(),
        config=Config(alpaca_paper=False, use_real_data_tools=True),
    )
    client = TestClient(app)

    resp = client.get("/api/overview")

    assert resp.json()["mode"] == "live"
