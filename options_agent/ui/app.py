"""FastAPI app factory — WP-9.1 skeleton, extended with WP-9.2's Overview API,
WP-9.3's Decision explorer API, WP-9.4's live activity stream, WP-9.5's
Performance & bias API, WP-9.7's kill-switch console, and WP-9.8's
Ask-the-journal analyst.

Ships /api/health, /api/overview, /api/positions, /api/cycles,
/api/cycles/{cycle_id}, /api/events, /api/review/*, /api/killswitch,
/api/ask, and static SPA serving. The `engine` passed to create_app must be
read-only (see state.db.build_engine(url, read_only=True)) — this module
does not itself enforce that, it trusts its caller. `write_engine` is the
one exception: a write-capable engine used exclusively by the killswitch
router (see ui/killswitch.py's module docstring for the write-isolation
invariant this maintains).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine

from options_agent.config import Config
from options_agent.contracts.state import ActionTaken
from options_agent.obs.alerts import AlertDispatcher, DiscordChannel, NullChannel
from options_agent.state.db import build_engine, get_connection
from options_agent.ui.ask import AskRequest, AskResponse, get_ask_answer
from options_agent.ui.cycles import (
    CycleDetail,
    CycleListItem,
    get_cycle_detail,
    get_cycles,
)
from options_agent.ui.events import event_stream
from options_agent.ui.killswitch import (
    KillSwitchActionRequest,
    KillSwitchHistoryEntry,
    KillSwitchStatusResponse,
    apply_killswitch_action,
    get_killswitch_status,
)
from options_agent.ui.overview import (
    OverviewResponse,
    PositionSummary,
    get_overview,
    get_positions,
)
from options_agent.ui.review import (
    AttributionResponse,
    BiasResponse,
    FunnelResponse,
    HitRateResponse,
    get_attribution,
    get_bias,
    get_funnel,
    get_hit_rate,
    get_prompt_versions,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    *,
    config: Config | None = None,
    engine: Engine | None = None,
    write_engine: Engine | None = None,
    alert_dispatcher: AlertDispatcher | None = None,
) -> FastAPI:
    """Build the console FastAPI app.

    engine is injectable for tests; production callers (__main__.py) pass a
    loaded Config and let this factory build the read-only engine from
    DB_URL/config.db_url.

    write_engine and alert_dispatcher back the kill-switch router only (the
    console's one write path) — also injectable for tests. In production
    (no engine passed at all) write_engine defaults to a fresh writable
    engine on the same DB_URL/config.db_url. When a test injects `engine`
    but not `write_engine`, write_engine defaults to that same injected
    engine rather than silently building a second one against
    config.db_url's default (which would touch a real on-disk
    "options_agent.db" file as an untracked side effect for the many
    existing tests that inject only `engine` and never exercise the
    kill-switch write path). Every other route in this module reads through
    `engine`; only ui/killswitch.py's handlers use `write_engine`.
    """
    config = config or Config()
    db_url = os.environ.get("DB_URL", config.db_url)
    production_wiring = engine is None
    if engine is None:
        engine = build_engine(db_url, read_only=True)
    if write_engine is None:
        write_engine = (
            build_engine(db_url, read_only=False) if production_wiring else engine
        )
    if alert_dispatcher is None:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        channel = DiscordChannel(webhook_url) if webhook_url else NullChannel()
        alert_dispatcher = AlertDispatcher(channel, write_engine)
    mode: Literal["paper", "live"] = "paper" if config.alpaca_paper else "live"

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        yield
        # Flush any in-flight alert before the process exits — same
        # shutdown-flush guarantee AlertDispatcher's own docstring promises
        # for the scheduler's context-manager usage in __main__.py.
        alert_dispatcher.shutdown()

    app = FastAPI(title="Options Agent Console", lifespan=_lifespan)
    app.state.engine = engine
    app.state.write_engine = write_engine
    app.state.alert_dispatcher = alert_dispatcher

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

    @app.get("/api/events")
    def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            event_stream(engine, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/review/funnel")
    def review_funnel(
        since: datetime | None = None, prompt_version: str | None = None
    ) -> FunnelResponse:
        with get_connection(engine) as conn:
            return get_funnel(conn, since=since, prompt_version=prompt_version)

    @app.get("/api/review/hit-rate")
    def review_hit_rate(
        since: datetime | None = None, prompt_version: str | None = None
    ) -> HitRateResponse:
        with get_connection(engine) as conn:
            return get_hit_rate(
                conn,
                since=since,
                prompt_version=prompt_version,
                min_sample_size=config.limits.bias_min_sample_size,
            )

    @app.get("/api/review/attribution")
    def review_attribution(
        since: datetime | None = None, prompt_version: str | None = None
    ) -> AttributionResponse:
        with get_connection(engine) as conn:
            return get_attribution(conn, since=since, prompt_version=prompt_version)

    @app.get("/api/review/bias")
    def review_bias(
        since: datetime | None = None, prompt_version: str | None = None
    ) -> BiasResponse:
        with get_connection(engine) as conn:
            return get_bias(
                conn,
                since=since,
                prompt_version=prompt_version,
                min_sample_size=config.limits.bias_min_sample_size,
            )

    @app.get("/api/review/prompt-versions")
    def review_prompt_versions() -> list[str]:
        with get_connection(engine) as conn:
            return get_prompt_versions(conn)

    @app.post("/api/ask")
    def ask_endpoint(body: AskRequest) -> AskResponse:
        with get_connection(engine) as conn:
            return get_ask_answer(conn, body.question)

    @app.get("/api/killswitch")
    def killswitch_status() -> KillSwitchStatusResponse:
        with get_connection(engine) as conn:
            return get_killswitch_status(conn)

    @app.post("/api/killswitch")
    def killswitch_action(body: KillSwitchActionRequest) -> KillSwitchHistoryEntry:
        # The console's only write: a dedicated write-capable engine, never
        # used by any other handler in this module (see ui/killswitch.py).
        with get_connection(write_engine) as conn:
            return apply_killswitch_action(conn, body, dispatcher=alert_dispatcher)

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="spa")

    return app
