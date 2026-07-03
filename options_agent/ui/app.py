"""FastAPI app factory — WP-9.1 skeleton.

Ships /api/health and static SPA serving only; data endpoints (/api/overview,
/api/cycles, etc.) land in later WP-9 cards. The engine passed to create_app
must be read-only (see state.db.build_engine(url, read_only=True)) — this
module does not itself enforce that, it trusts its caller.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine

from options_agent.config import Config
from options_agent.state.db import build_engine

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    *, config: Config | None = None, engine: Engine | None = None
) -> FastAPI:
    """Build the console FastAPI app.

    engine is injectable for tests; production callers (__main__.py) pass a
    loaded Config and let this factory build the read-only engine from
    DB_URL/config.db_url.
    """
    if engine is None:
        config = config or Config()
        db_url = os.environ.get("DB_URL", config.db_url)
        engine = build_engine(db_url, read_only=True)

    app = FastAPI(title="Options Agent Console")
    app.state.engine = engine

    @app.get("/api/health")
    def health() -> JSONResponse:
        # A real query, not just a connect attempt — surfaces a mid-migration
        # or otherwise unreachable DB at request time rather than serving
        # broken data from later endpoints.
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
        except Exception:
            logger.exception("Health check query failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok"})

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="spa")

    return app
