"""WP-9.1: FastAPI console skeleton (options_agent.ui.app)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from options_agent.state.db import build_engine, metadata
from options_agent.ui.app import create_app


def _memory_engine():
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def test_health_ok_when_db_reachable():
    app = create_app(engine=_memory_engine())
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


def test_root_returns_404_when_spa_not_built():
    # No options_agent/ui/static/ in the source tree — it's produced by the
    # Docker multi-stage build. The skeleton must not crash without it.
    app = create_app(engine=_memory_engine())
    client = TestClient(app)

    resp = client.get("/")

    assert resp.status_code == 404


def test_root_serves_built_spa(monkeypatch, tmp_path):
    index = tmp_path / "index.html"
    index.write_text("<html><body>console shell</body></html>")
    monkeypatch.setattr("options_agent.ui.app.STATIC_DIR", tmp_path)

    app = create_app(engine=_memory_engine())
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
