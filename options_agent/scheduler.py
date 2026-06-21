"""WP-8.4 — Scheduler + blackout windows.

Drives both cycle loops at the correct cadences with lock-and-skip overlap
protection, observable skip counters, and kill-switch awareness.

Scheduling library: APScheduler 3.x BackgroundScheduler
  - CronTrigger for entry at configured ET times (config.entry_times)
  - IntervalTrigger for monitor at config.monitor_interval_minutes
  - coalesce=True + misfire_grace_time: collapse catch-up fires (skip, not queue)

Lock design:
  - Separate threading.Lock for entry and monitor; entry never blocks monitor.
    A slow LLM reasoning step in the entry cycle cannot delay stop-loss checks.
  - Lock-and-skip: if the lock is held the new fire is skipped and the skip
    counter incremented. After _SKIP_WARN_THRESHOLD consecutive skips a WARN
    alert fires — persistent skips mean cycles run longer than their interval.
  - Skip counter resets to zero on the next successful (non-skipped) run.

Kill-switch:
  - Entry: checked at scheduler level before acquiring the entry lock. Under
    HALT or FLATTEN, the entry cycle is not invoked at all. This surfaces the
    halted state at the scheduler layer, not only inside the cycle itself.
  - Monitor: not blocked at scheduler level — the monitor must run under both
    HALT and FLATTEN (WP-7.1 semantics). The cycle handles FLATTEN internally.

Market-hours enforcement:
  - Both cycles call market_is_open() at cycle-top via risk/gates.py, which
    uses exchange_calendars to handle holidays and early closes correctly.
  - The scheduler fires at configured times; the cycles gate themselves. On an
    early-close day a 15:00 ET entry fires but immediately short-circuits
    (MARKET_CLOSED) — the cycle is the correct enforcement point.
"""

from __future__ import annotations

import logging
import signal
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.engine import Engine

from options_agent.config import Config
from options_agent.contracts.alerts import AlertEvent, AlertEventType, AlertSeverity
from options_agent.execution.broker import BrokerClient
from options_agent.obs.alerts import AlertDispatcher
from options_agent.obs.killswitch import get_current_state, is_halted
from options_agent.orchestrator import run_entry_cycle, run_monitor_cycle
from options_agent.state.db import build_engine, get_connection

logger = logging.getLogger(__name__)

_SKIP_WARN_THRESHOLD = 3


def _dispatch_safe(dispatcher: AlertDispatcher | None, event: AlertEvent) -> None:
    """Dispatch without propagating — scheduler jobs must never raise from alerting."""
    if dispatcher is not None:
        try:
            dispatcher.dispatch(event)
        except Exception as exc:
            logger.error("CycleScheduler: alert dispatch failed — %s", exc)


class CycleScheduler:
    """Drives entry + monitor cycles on configured cadences.

    Usage::

        scheduler = CycleScheduler(config, engine=engine, dispatcher=dispatcher)
        with scheduler:
            scheduler.run_forever()   # blocks until SIGINT / SIGTERM
    """

    def __init__(
        self,
        config: Config,
        *,
        engine: Engine | None = None,
        broker: BrokerClient | None = None,
        dispatcher: AlertDispatcher | None = None,
    ) -> None:
        self._config = config
        self._engine = engine if engine is not None else build_engine(config.db_url)
        self._broker = broker
        self._dispatcher = dispatcher

        # Separate locks — entry must never block the monitor safety loop.
        self._entry_lock = threading.Lock()
        self._monitor_lock = threading.Lock()
        self._entry_skip_count = 0
        self._monitor_skip_count = 0

        self._apscheduler = BackgroundScheduler(timezone=config.timezone)
        self._add_jobs()

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background scheduler."""
        self._apscheduler.start()
        logger.info(
            "CycleScheduler started: monitor_interval=%dmin entry_times=%s tz=%s",
            self._config.monitor_interval_minutes,
            [str(t) for t in self._config.entry_times],
            self._config.timezone,
        )

    def stop(self, wait: bool = True) -> None:
        """Shut down the scheduler, optionally waiting for running jobs to finish."""
        self._apscheduler.shutdown(wait=wait)
        logger.info("CycleScheduler stopped")

    def run_forever(self) -> None:
        """Block the calling thread until SIGINT or SIGTERM, then stop cleanly."""
        stop_event = threading.Event()

        def _handle(signum: int, _frame: object) -> None:
            logger.info("CycleScheduler: signal %d — shutting down", signum)
            stop_event.set()

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        stop_event.wait()
        self.stop()

    def __enter__(self) -> CycleScheduler:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ── Job registration ─────────────────────────────────────────────────────

    def _add_jobs(self) -> None:
        interval_secs = self._config.monitor_interval_minutes * 60
        self._apscheduler.add_job(
            self._run_monitor,
            IntervalTrigger(minutes=self._config.monitor_interval_minutes),
            id="monitor",
            coalesce=True,
            misfire_grace_time=interval_secs,
        )

        tz = self._config.timezone
        for entry_time in self._config.entry_times:
            job_id = f"entry_{entry_time.hour:02d}{entry_time.minute:02d}"
            self._apscheduler.add_job(
                self._run_entry,
                CronTrigger(
                    hour=entry_time.hour,
                    minute=entry_time.minute,
                    timezone=tz,
                ),
                id=job_id,
                coalesce=True,
                misfire_grace_time=300,
            )

    # ── Cycle runners ────────────────────────────────────────────────────────

    def _run_monitor(self) -> None:
        if not self._monitor_lock.acquire(blocking=False):
            self._monitor_skip_count += 1
            logger.warning(
                "CycleScheduler: monitor skipped — previous cycle still running "
                "(consecutive_skips=%d)",
                self._monitor_skip_count,
            )
            if self._monitor_skip_count >= _SKIP_WARN_THRESHOLD:
                _dispatch_safe(
                    self._dispatcher,
                    AlertEvent(
                        event_type=AlertEventType.SCHEDULER_SKIP,
                        severity=AlertSeverity.WARN,
                        detail=(
                            f"Monitor cycle skipped {self._monitor_skip_count} "
                            "consecutive times — cycles may be running longer than the "
                            f"{self._config.monitor_interval_minutes}min interval"
                        ),
                    ),
                )
            return

        try:
            self._monitor_skip_count = 0
            run_monitor_cycle(
                self._config,
                broker=self._broker,
                engine=self._engine,
                dispatcher=self._dispatcher,
            )
        except Exception as exc:
            logger.error("CycleScheduler: monitor cycle unhandled exception — %s", exc)
        finally:
            self._monitor_lock.release()

    def _run_entry(self) -> None:
        # Kill-switch check at scheduler level: entry is not invoked under HALT/FLATTEN.
        # The monitor is not gated here — it runs under both states (WP-7.1 semantics).
        try:
            with get_connection(self._engine) as conn:
                ks_state = get_current_state(conn)
        except Exception as exc:
            logger.critical(
                "CycleScheduler: kill-switch read failed — skipping entry "
                "(fail closed): %s",
                exc,
            )
            return

        if is_halted(ks_state):
            logger.info("CycleScheduler: entry skipped — kill switch %s", ks_state)
            return

        if not self._entry_lock.acquire(blocking=False):
            self._entry_skip_count += 1
            logger.warning(
                "CycleScheduler: entry skipped — previous cycle still running "
                "(consecutive_skips=%d)",
                self._entry_skip_count,
            )
            if self._entry_skip_count >= _SKIP_WARN_THRESHOLD:
                _dispatch_safe(
                    self._dispatcher,
                    AlertEvent(
                        event_type=AlertEventType.SCHEDULER_SKIP,
                        severity=AlertSeverity.WARN,
                        detail=(
                            f"Entry cycle skipped {self._entry_skip_count} consecutive "
                            "times — LLM reasoning may be taking longer than expected"
                        ),
                    ),
                )
            return

        try:
            self._entry_skip_count = 0
            run_entry_cycle(
                self._config,
                broker=self._broker,
                engine=self._engine,
                dispatcher=self._dispatcher,
            )
        except Exception as exc:
            logger.error("CycleScheduler: entry cycle unhandled exception — %s", exc)
        finally:
            self._entry_lock.release()


__all__ = ["CycleScheduler"]
