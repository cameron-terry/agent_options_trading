"""WP-9.7: kill-switch console + alert-delivery health.

Covers the card's acceptance criteria directly:
  - arm/resume round-trips through kill_switch_log and is visible via
    obs.killswitch.get_current_state (the same read the CLI's `status`
    command uses)
  - state changes dispatch the CRITICAL alert
  - HALT arming is low-friction; RESUME and FLATTEN require typed
    confirmation; reason is mandatory on all
  - no other console endpoint can write (source-scan + read/write
    isolation test)
  - alert-delivery health panel lists alert_delivery_failures rows
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient

from options_agent.contracts.alerts import AlertEventType, AlertSeverity
from options_agent.contracts.state import KillSwitchState
from options_agent.obs.alerts import AlertDispatcher, NullChannel
from options_agent.obs.killswitch import get_current_state, set_state
from options_agent.state.db import (
    alert_delivery_failures_table,
    build_engine,
    get_connection,
    metadata,
)
from options_agent.ui.app import create_app

UI_DIR = Path(__file__).parent.parent / "ui"


def _engine() -> sa.engine.Engine:
    eng = build_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _client(**kwargs) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(create_app(**kwargs))


# ---------------------------------------------------------------------------
# GET /api/killswitch
# ---------------------------------------------------------------------------


def test_status_empty_db_reports_none_state_and_empty_lists() -> None:
    client = _client(engine=_engine())

    resp = client.get("/api/killswitch")

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "NONE"
    assert body["history"] == []
    assert body["alert_failures"] == []


def test_status_lists_alert_delivery_failures_newest_first() -> None:
    engine = _engine()
    older = datetime(2026, 7, 1, tzinfo=UTC)
    newer = datetime(2026, 7, 12, tzinfo=UTC)
    with get_connection(engine) as conn:
        for ts, detail in [(older, "older failure"), (newer, "newer failure")]:
            conn.execute(
                alert_delivery_failures_table.insert().values(
                    id=str(uuid.uuid4()),
                    event_type=str(AlertEventType.KILL_SWITCH_CHANGE),
                    severity=str(AlertSeverity.CRITICAL),
                    detail=detail,
                    attempted_at=ts,
                    attempts=2,
                    last_error="HTTPError 500",
                )
            )

    client = _client(engine=engine)
    resp = client.get("/api/killswitch")

    failures = resp.json()["alert_failures"]
    assert [f["detail"] for f in failures] == ["newer failure", "older failure"]


# ---------------------------------------------------------------------------
# POST /api/killswitch — confirmation UX
# ---------------------------------------------------------------------------


def test_arm_halt_requires_no_confirmation() -> None:
    client = _client(engine=_engine())

    resp = client.post(
        "/api/killswitch", json={"action": "HALT", "reason": "reconcile mismatch"}
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "HALT"


def test_reason_is_mandatory_even_for_halt() -> None:
    client = _client(engine=_engine())

    resp = client.post("/api/killswitch", json={"action": "HALT", "reason": ""})

    assert resp.status_code == 422


def test_flatten_without_confirmation_is_rejected() -> None:
    client = _client(engine=_engine())

    resp = client.post(
        "/api/killswitch", json={"action": "FLATTEN", "reason": "vega band breached"}
    )

    assert resp.status_code == 422


def test_flatten_with_wrong_confirmation_is_rejected() -> None:
    client = _client(engine=_engine())

    resp = client.post(
        "/api/killswitch",
        json={
            "action": "FLATTEN",
            "reason": "vega band breached",
            "confirmation": "flatten",
        },
    )

    assert resp.status_code == 422


def test_flatten_with_correct_confirmation_succeeds() -> None:
    client = _client(engine=_engine())

    resp = client.post(
        "/api/killswitch",
        json={
            "action": "FLATTEN",
            "reason": "vega band breached",
            "confirmation": "FLATTEN",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "FLATTEN"


def test_resume_without_confirmation_is_rejected() -> None:
    engine = _engine()
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="test", reason="setup")
    client = _client(engine=engine)

    resp = client.post(
        "/api/killswitch", json={"action": "RESUME", "reason": "issue resolved"}
    )

    assert resp.status_code == 422


def test_resume_with_correct_confirmation_succeeds() -> None:
    engine = _engine()
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="test", reason="setup")
    client = _client(engine=engine)

    resp = client.post(
        "/api/killswitch",
        json={
            "action": "RESUME",
            "reason": "issue resolved",
            "confirmation": "RESUME",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "NONE"


# ---------------------------------------------------------------------------
# Round-trip visibility (CLI parity) + history
# ---------------------------------------------------------------------------


def test_arm_round_trips_through_kill_switch_log_and_matches_cli_read() -> None:
    """Visible to `python -m options_agent.obs status` == get_current_state()."""
    engine = _engine()
    client = _client(engine=engine)

    resp = client.post(
        "/api/killswitch", json={"action": "HALT", "reason": "manual test"}
    )
    assert resp.status_code == 200

    with get_connection(engine) as conn:
        assert get_current_state(conn).value == "HALT"


def test_history_reflects_arm_then_resume_newest_first() -> None:
    client = _client(engine=_engine())

    client.post("/api/killswitch", json={"action": "HALT", "reason": "first"})
    client.post(
        "/api/killswitch",
        json={"action": "RESUME", "reason": "second", "confirmation": "RESUME"},
    )

    resp = client.get("/api/killswitch")
    history = resp.json()["history"]
    assert [h["state"] for h in history[:2]] == ["NONE", "HALT"]
    assert [h["reason"] for h in history[:2]] == ["second", "first"]


# ---------------------------------------------------------------------------
# Alert dispatch
# ---------------------------------------------------------------------------


def test_killswitch_change_dispatches_critical_alert() -> None:
    engine = _engine()
    channel = NullChannel()
    dispatcher = AlertDispatcher(channel, engine)
    try:
        client = _client(
            engine=engine, write_engine=engine, alert_dispatcher=dispatcher
        )

        resp = client.post(
            "/api/killswitch", json={"action": "HALT", "reason": "vega breach"}
        )
        assert resp.status_code == 200

        dispatcher.shutdown()  # flush the worker queue before asserting
        assert len(channel.sent) == 1
        event = channel.sent[0]
        assert event.event_type == AlertEventType.KILL_SWITCH_CHANGE
        assert event.severity == AlertSeverity.CRITICAL
        assert "vega breach" in event.detail
    finally:
        dispatcher.shutdown()


# ---------------------------------------------------------------------------
# Write isolation — "no other console endpoint can write"
# ---------------------------------------------------------------------------


def test_get_routes_never_see_writes_made_through_write_engine() -> None:
    """GET /api/killswitch reads only `engine`; POST writes only `write_engine`.

    Two independent in-memory engines stand in for "the read-only engine
    every other route uses" and "the write-capable engine only the
    kill-switch router uses" — if a GET handler ever accidentally read from
    or a POST handler ever wrote to the wrong one, this test would catch it
    by seeing state leak across engines that share no storage.
    """
    read_engine = _engine()
    write_engine = _engine()
    client = _client(
        engine=read_engine,
        write_engine=write_engine,
        alert_dispatcher=AlertDispatcher(NullChannel(), write_engine),
    )

    resp = client.post(
        "/api/killswitch", json={"action": "HALT", "reason": "isolation check"}
    )
    assert resp.status_code == 200

    with get_connection(write_engine) as conn:
        assert get_current_state(conn).value == "HALT"
    with get_connection(read_engine) as conn:
        assert get_current_state(conn).value == "NONE"  # untouched by the POST

    # GET must reflect the untouched read engine, not the written-to one.
    status = client.get("/api/killswitch").json()
    assert status["state"] == "NONE"


def test_no_other_ui_module_references_write_engine() -> None:
    """Source-scan enforcement: only app.py (wiring) and killswitch.py (the
    router that uses it) may mention write_engine. Prevents a future
    endpoint from acquiring write access by copy-pasting a wiring line.
    """
    allowed = {"app.py", "killswitch.py", "__main__.py"}
    offenders = []
    for path in UI_DIR.glob("*.py"):
        if path.name in allowed:
            continue
        if "write_engine" in path.read_text():
            offenders.append(path.name)

    assert offenders == []
