"""Alerting channel integration for WP-7.2.

Public API
----------
AlertChannel      — Protocol; implement for any notification backend.
DiscordChannel    — Discord incoming webhook implementation.
NullChannel       — No-op channel; records sent events in memory for tests
                    and for running with alerts deliberately disabled.
AlertDispatcher   — Non-blocking dispatcher: background worker queue,
                    bounded retry, durable failure recording.

Design decisions
----------------
Non-blocking: dispatch() enqueues and returns immediately. The cycle thread
is never stalled by a slow or failing webhook.

Never propagates: channel failures are caught inside the worker and recorded
durably in alert_delivery_failures — never raised into the cycle.

Bounded retry: up to max_attempts (default 2) with retry_delay_s backoff.
On exhaustion the alert is dropped; the failure is written to DB so WP-7
review can surface "N undelivered CRITICALs last week."

Dropped-alert vs. broken-alerting distinction: the individual alert is
best-effort/dropped on exhaustion, but the *fact* that delivery failed is
durable (DB row). Logs are the medium alerting exists to not depend on —
a log-only failure record defeats the purpose of the alerting layer.

Shutdown flush: shutdown() puts a sentinel and joins the worker thread so a
CRITICAL fired just before process exit is not silently lost.

Testability: inject a NullChannel so tests assert on AlertEvent production
without coupling to real async timing or a live webhook. The async-is-hard-
to-test problem dissolves when the channel is an injected interface.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time as _time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.engine import Engine

from options_agent.contracts.alerts import AlertEvent, AlertSeverity
from options_agent.state.db import alert_delivery_failures_table, get_connection

logger = logging.getLogger(__name__)

_DISCORD_COLORS: dict[AlertSeverity, int] = {
    AlertSeverity.INFO: 3_447_003,  # blue
    AlertSeverity.WARN: 16_750_848,  # orange
    AlertSeverity.CRITICAL: 15_548_997,  # red
}

_SENTINEL = None  # put to queue to signal shutdown


@runtime_checkable
class AlertChannel(Protocol):
    """Interface for a notification backend.

    Implementations must raise on delivery failure — the dispatcher owns
    retry logic and failure recording. Never swallow errors silently.
    """

    def send(self, event: AlertEvent) -> None: ...


class NullChannel:
    """No-op channel that records events in memory.

    Use in tests and when alerting is deliberately disabled.
    sent is thread-safe: the worker thread appends while test threads read.
    """

    def __init__(self) -> None:
        self.sent: list[AlertEvent] = []
        self._lock = threading.Lock()

    def send(self, event: AlertEvent) -> None:
        with self._lock:
            self.sent.append(event)


class DiscordChannel:
    """Discord incoming webhook channel.

    Reads DISCORD_WEBHOOK_URL from the environment at construction time.
    The URL is never stored in config.toml or logged.

    Raises urllib.error.URLError / urllib.error.HTTPError on delivery
    failure so the dispatcher can catch and retry.
    """

    def __init__(self, webhook_url: str) -> None:
        if not webhook_url:
            raise ValueError(
                "DISCORD_WEBHOOK_URL must be non-empty to use DiscordChannel. "
                "Set the environment variable before constructing DiscordChannel."
            )
        self._url = webhook_url

    def send(self, event: AlertEvent) -> None:
        color = _DISCORD_COLORS.get(event.severity, _DISCORD_COLORS[AlertSeverity.INFO])
        fields: list[dict[str, str | bool]] = [
            {"name": "Event", "value": str(event.event_type), "inline": True},
            {"name": "Severity", "value": str(event.severity), "inline": True},
        ]
        if event.symbol:
            fields.append({"name": "Symbol", "value": event.symbol, "inline": True})
        if event.order_id:
            fields.append({"name": "Order ID", "value": event.order_id, "inline": True})

        payload = {
            "embeds": [
                {
                    "title": f"[{event.severity}] {event.event_type}",
                    "description": event.detail,
                    "color": color,
                    "timestamp": event.timestamp.isoformat(),
                    "fields": fields,
                }
            ]
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass  # 2xx/3xx → success; 4xx/5xx → HTTPError raised automatically


class AlertDispatcher:
    """Non-blocking alert dispatcher with bounded retry and durable failure recording.

    The worker thread starts in __init__ and runs until shutdown() is called.
    Use as a context manager or call shutdown() explicitly on process exit.

    Usage::

        dispatcher = AlertDispatcher(channel, engine)
        dispatcher.dispatch(event)
        dispatcher.shutdown()

        # or as a context manager:
        with AlertDispatcher(channel, engine) as dispatcher:
            dispatcher.dispatch(event)
    """

    def __init__(
        self,
        channel: AlertChannel,
        engine: Engine,
        *,
        max_attempts: int = 2,
        retry_delay_s: float = 1.0,
    ) -> None:
        self._channel = channel
        self._engine = engine
        self._max_attempts = max_attempts
        self._retry_delay_s = retry_delay_s
        self._queue: queue.Queue[AlertEvent | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="alert-dispatcher"
        )
        self._thread.start()

    def dispatch(self, event: AlertEvent) -> None:
        """Enqueue an event for delivery. Returns immediately; never blocks."""
        self._queue.put(event)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Flush pending alerts and stop the worker thread.

        Puts a sentinel and waits up to *timeout* seconds. All events queued
        before this call are delivered (or their failure recorded) before the
        thread exits — a CRITICAL fired just before process exit is not lost.
        """
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=timeout)

    def __enter__(self) -> AlertDispatcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # Internal worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._deliver(item)

    def _deliver(self, event: AlertEvent) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._channel.send(event)
                return  # success — done
            except Exception as exc:
                last_error = exc
                if attempt < self._max_attempts:
                    _time.sleep(self._retry_delay_s)

        self._record_failure(event, last_error)

    def _record_failure(self, event: AlertEvent, error: Exception | None) -> None:
        try:
            with get_connection(self._engine) as conn:
                conn.execute(
                    alert_delivery_failures_table.insert().values(
                        id=str(uuid.uuid4()),
                        event_type=str(event.event_type),
                        severity=str(event.severity),
                        detail=event.detail,
                        attempted_at=datetime.now(UTC),
                        attempts=self._max_attempts,
                        last_error=str(error) if error else "",
                    )
                )
        except Exception as db_exc:
            # DB failure on failure recording: log only — do not raise.
            # This is the last-resort path; if the DB is also down there is
            # nothing durable left to write to.
            logger.error("Failed to record alert delivery failure: %s", db_exc)
