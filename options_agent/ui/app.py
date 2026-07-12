"""FastAPI app factory — WP-9.1 skeleton, extended with WP-9.2's Overview API
and WP-9.3's Decision explorer API.

Ships /api/health, /api/overview, /api/positions, /api/cycles,
/api/cycles/{cycle_id}, and static SPA serving. Further data endpoints
(/api/review/*, etc.) land in later WP-9 cards. The engine passed to
create_app must be read-only (see state.db.build_engine(url,
read_only=True)) — this module does not itself enforce that, it trusts its
caller.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine

from options_agent.config import Config
from options_agent.contracts.state import ActionTaken
from options_agent.state.db import build_engine, get_connection
from options_agent.ui.cycles import (
    CycleDetail,
    CycleListItem,
    get_cycle_detail,
    get_cycles,
)
from options_agent.ui.overview import (
    OverviewResponse,
    PositionSummary,
    get_overview,
    get_positions,
)

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
    config = config or Config()
    mode: Literal["paper", "live"] = "paper" if config.alpaca_paper else "live"

    app = FastAPI(title="Options Agent Console")
    app.state.engine = engine

    @app.get("/api/health")
    def health() -> JSONResponse:
        # Query alembic's own bookkeeping table rather than a bare `SELECT 1`
        # — a constant expression touches no table and reports healthy even
        # against a completely unmigrated, zero-table DB. alembic_version is
        # written by the migration framework itself, so its presence doesn't
        # couple this check to any particular application table (WP-9.2+ can
        # add/remove tables without touching this).
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT version_num FROM alembic_version"))
        except Exception:
            logger.exception("Health check query failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/api/overview")
    def overview() -> OverviewResponse:
        with get_connection(engine) as conn:
            return get_overview(conn, now=datetime.now(UTC), mode=mode)

    @app.get("/api/positions")
    def positions() -> list[PositionSummary]:
        with get_connection(engine) as conn:
            return get_positions(conn, now=datetime.now(UTC))

    @app.get("/api/cycles")
    def cycles(
        symbol: str | None = None,
        action_type: ActionTaken | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[CycleListItem]:
        with get_connection(engine) as conn:
            return get_cycles(
                conn,
                symbol=symbol,
                action_type=action_type,
                date_from=date_from,
                date_to=date_to,
                now=datetime.now(UTC),
            )

    @app.get("/api/cycles/{cycle_id}")
    def cycle_detail(cycle_id: str) -> CycleDetail:
        with get_connection(engine) as conn:
            detail = get_cycle_detail(conn, cycle_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="cycle not found")
        return detail

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="spa")

    return app
