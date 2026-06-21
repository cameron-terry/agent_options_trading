"""Tests for WP-7.2: alerting channel integration."""

from __future__ import annotations

import threading

import pytest

from options_agent.contracts.alerts import (
    DEFAULT_SEVERITY,
    AlertEvent,
    AlertEventType,
    AlertSeverity,
)
from options_agent.obs.alerts import AlertDispatcher, DiscordChannel, NullChannel
from options_agent.state.db import alert_delivery_failures_table, get_connection

# ---------------------------------------------------------------------------
# NullChannel
# ---------------------------------------------------------------------------


def test_null_channel_records_events() -> None:
    ch = NullChannel()
    ev = AlertEvent(
        event_type=AlertEventType.FILL, severity=AlertSeverity.INFO, detail="fill test"
    )
    ch.send(ev)
    assert ch.sent == [ev]


def test_null_channel_is_thread_safe() -> None:
    ch = NullChannel()
    ev = AlertEvent(
        event_type=AlertEventType.FILL, severity=AlertSeverity.INFO, detail="x"
    )

    def _send() -> None:
        for _ in range(100):
            ch.send(ev)

    threads = [threading.Thread(target=_send) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ch.sent) == 500


# ---------------------------------------------------------------------------
# DiscordChannel construction guard
# ---------------------------------------------------------------------------


def test_discord_channel_raises_on_empty_url() -> None:
    with pytest.raises(ValueError, match="DISCORD_WEBHOOK_URL"):
        DiscordChannel("")


def test_discord_channel_send_posts_well_formed_embed() -> None:
    """DiscordChannel.send() must POST a valid Discord embed JSON payload."""
    import json
    from unittest.mock import MagicMock, patch

    channel = DiscordChannel("https://discord.com/api/webhooks/test/token")
    ev = AlertEvent(
        event_type=AlertEventType.KILL_SWITCH_CHANGE,
        severity=AlertSeverity.CRITICAL,
        detail="HALT engaged",
        symbol="SPY",
        order_id="ord-abc",
    )

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("options_agent.obs.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = mock_resp
        channel.send(ev)

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]

    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert req.full_url == "https://discord.com/api/webhooks/test/token"

    payload = json.loads(req.data)
    assert "embeds" in payload and len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == "[CRITICAL] KILL_SWITCH_CHANGE"
    assert embed["description"] == "HALT engaged"
    assert embed["color"] == 15_548_997  # red — CRITICAL

    field_names = [f["name"] for f in embed["fields"]]
    assert "Event" in field_names
    assert "Severity" in field_names
    assert "Symbol" in field_names
    assert "Order ID" in field_names


def test_discord_channel_send_info_color_and_no_optional_fields() -> None:
    """INFO severity gets blue color; missing symbol/order_id omitted from fields."""
    import json
    from unittest.mock import MagicMock, patch

    channel = DiscordChannel("https://discord.com/api/webhooks/test/token")
    ev = AlertEvent(
        event_type=AlertEventType.FILL,
        severity=AlertSeverity.INFO,
        detail="SPY fill 2.35",
    )

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("options_agent.obs.alerts.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = mock_resp
        channel.send(ev)

    payload = json.loads(mock_urlopen.call_args[0][0].data)
    embed = payload["embeds"][0]
    assert embed["color"] == 3_447_003  # blue — INFO

    field_names = [f["name"] for f in embed["fields"]]
    assert "Symbol" not in field_names
    assert "Order ID" not in field_names


# ---------------------------------------------------------------------------
# AlertEvent defaults
# ---------------------------------------------------------------------------


def test_alert_event_timestamp_defaults_to_utc() -> None:

    ev = AlertEvent(
        event_type=AlertEventType.FILL, severity=AlertSeverity.INFO, detail="test"
    )
    assert ev.timestamp.tzinfo is not None
    assert ev.timestamp.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_alert_event_optional_fields_default_to_none() -> None:
    ev = AlertEvent(
        event_type=AlertEventType.KILL_SWITCH_CHANGE,
        severity=AlertSeverity.CRITICAL,
        detail="HALT",
    )
    assert ev.symbol is None
    assert ev.order_id is None


# ---------------------------------------------------------------------------
# DEFAULT_SEVERITY mapping
# ---------------------------------------------------------------------------


def test_default_severity_mapping() -> None:
    assert DEFAULT_SEVERITY[AlertEventType.EXIT_SUBMITTED] == AlertSeverity.INFO
    assert DEFAULT_SEVERITY[AlertEventType.FILL] == AlertSeverity.INFO
    assert DEFAULT_SEVERITY[AlertEventType.REJECTION] == AlertSeverity.WARN
    assert DEFAULT_SEVERITY[AlertEventType.KILL_SWITCH_CHANGE] == AlertSeverity.CRITICAL
    assert DEFAULT_SEVERITY[AlertEventType.SCHEDULER_SKIP] == AlertSeverity.WARN


# ---------------------------------------------------------------------------
# AlertDispatcher — happy path
# ---------------------------------------------------------------------------


def test_dispatcher_delivers_fill_event(engine) -> None:
    ch = NullChannel()
    ev = AlertEvent(
        event_type=AlertEventType.FILL,
        severity=AlertSeverity.INFO,
        detail="SPY filled at 2.50",
        symbol="SPY",
    )
    with AlertDispatcher(ch, engine) as d:  # type: ignore[arg-type]
        d.dispatch(ev)
    assert len(ch.sent) == 1
    assert ch.sent[0].event_type == AlertEventType.FILL


def test_dispatcher_delivers_multiple_events(engine) -> None:
    ch = NullChannel()
    events = [
        AlertEvent(
            event_type=AlertEventType.FILL,
            severity=AlertSeverity.INFO,
            detail=f"fill {i}",
        )
        for i in range(5)
    ]
    with AlertDispatcher(ch, engine) as d:  # type: ignore[arg-type]
        for ev in events:
            d.dispatch(ev)
    assert len(ch.sent) == 5


def test_dispatcher_delivers_rejection_event(engine) -> None:
    ch = NullChannel()
    ev = AlertEvent(
        event_type=AlertEventType.REJECTION,
        severity=AlertSeverity.WARN,
        detail="Rejected: max loss exceeded",
        symbol="AAPL",
    )
    with AlertDispatcher(ch, engine) as d:  # type: ignore[arg-type]
        d.dispatch(ev)
    assert ch.sent[0].event_type == AlertEventType.REJECTION


def test_dispatcher_delivers_kill_switch_event(engine) -> None:
    ch = NullChannel()
    ev = AlertEvent(
        event_type=AlertEventType.KILL_SWITCH_CHANGE,
        severity=AlertSeverity.CRITICAL,
        detail="HALT engaged by operator",
    )
    with AlertDispatcher(ch, engine) as d:  # type: ignore[arg-type]
        d.dispatch(ev)
    assert ch.sent[0].severity == AlertSeverity.CRITICAL


# ---------------------------------------------------------------------------
# AlertDispatcher — channel failure handling
# ---------------------------------------------------------------------------


class _FailingChannel:
    """Channel that always raises — simulates a down webhook."""

    def send(self, event: AlertEvent) -> None:
        raise RuntimeError("webhook unavailable")


def test_dispatcher_records_failure_in_db_on_exhaustion(engine) -> None:
    ev = AlertEvent(
        event_type=AlertEventType.KILL_SWITCH_CHANGE,
        severity=AlertSeverity.CRITICAL,
        detail="HALT engaged",
    )
    with AlertDispatcher(
        _FailingChannel(),
        engine,
        max_attempts=2,
        retry_delay_s=0.0,  # type: ignore[arg-type]
    ) as d:
        d.dispatch(ev)

    with get_connection(engine) as conn:  # type: ignore[arg-type]
        rows = conn.execute(alert_delivery_failures_table.select()).fetchall()
    assert len(rows) == 1
    assert rows[0].event_type == str(AlertEventType.KILL_SWITCH_CHANGE)
    assert rows[0].severity == str(AlertSeverity.CRITICAL)
    assert rows[0].attempts == 2
    assert "webhook unavailable" in rows[0].last_error


def test_dispatcher_retries_up_to_max_attempts(engine) -> None:
    call_count = 0

    class _CountingFailChannel:
        def send(self, event: AlertEvent) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("fail")

    ev = AlertEvent(
        event_type=AlertEventType.REJECTION,
        severity=AlertSeverity.WARN,
        detail="rejected",
    )
    with AlertDispatcher(
        _CountingFailChannel(),
        engine,  # type: ignore[arg-type]
        max_attempts=2,
        retry_delay_s=0.0,
    ) as d:
        d.dispatch(ev)

    assert call_count == 2


def test_dispatcher_does_not_raise_into_caller_on_failure(engine) -> None:
    """A broken channel must never surface into the cycle."""
    ev = AlertEvent(
        event_type=AlertEventType.REJECTION,
        severity=AlertSeverity.WARN,
        detail="rejected",
    )
    with AlertDispatcher(
        _FailingChannel(),
        engine,  # type: ignore[arg-type]
        max_attempts=1,
        retry_delay_s=0.0,
    ) as d:
        d.dispatch(ev)
    # reaching here without exception is the assertion


def test_failed_event_detail_recorded_accurately(engine) -> None:
    ev = AlertEvent(
        event_type=AlertEventType.FILL,
        severity=AlertSeverity.INFO,
        detail="SPY fill detail text",
        symbol="SPY",
        order_id="ord-123",
    )
    with AlertDispatcher(
        _FailingChannel(),
        engine,
        max_attempts=1,
        retry_delay_s=0.0,  # type: ignore[arg-type]
    ) as d:
        d.dispatch(ev)

    with get_connection(engine) as conn:  # type: ignore[arg-type]
        row = conn.execute(alert_delivery_failures_table.select()).fetchone()
    assert row is not None
    assert row.detail == "SPY fill detail text"


# ---------------------------------------------------------------------------
# AlertDispatcher — shutdown flush
# ---------------------------------------------------------------------------


def test_dispatcher_flushes_all_events_on_shutdown(engine) -> None:
    """Events dispatched before shutdown() must be delivered, not dropped."""
    ch = NullChannel()
    events = [
        AlertEvent(
            event_type=AlertEventType.FILL,
            severity=AlertSeverity.INFO,
            detail=f"fill {i}",
        )
        for i in range(10)
    ]
    d = AlertDispatcher(ch, engine)  # type: ignore[arg-type]
    for ev in events:
        d.dispatch(ev)
    d.shutdown(timeout=5.0)
    assert len(ch.sent) == 10


def test_dispatcher_context_manager_flushes_on_exit(engine) -> None:
    ch = NullChannel()
    ev = AlertEvent(
        event_type=AlertEventType.KILL_SWITCH_CHANGE,
        severity=AlertSeverity.CRITICAL,
        detail="FLATTEN engaged",
    )
    with AlertDispatcher(ch, engine) as d:  # type: ignore[arg-type]
        d.dispatch(ev)
    assert len(ch.sent) == 1
