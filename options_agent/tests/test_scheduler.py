"""Tests for WP-8.4: scheduler.py — CycleScheduler.

Tests call _run_monitor() and _run_entry() directly so there is no dependency
on APScheduler timing.  The APScheduler is constructed but never started.
"""

from __future__ import annotations

from datetime import time
from unittest.mock import patch

import pytest

from options_agent.config import Config
from options_agent.contracts.alerts import AlertEventType, AlertSeverity
from options_agent.contracts.state import KillSwitchState
from options_agent.obs.alerts import NullChannel
from options_agent.scheduler import _SKIP_WARN_THRESHOLD, CycleScheduler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config(
        monitor_interval_minutes=2,
        entry_times=[time(10, 30), time(13, 0), time(14, 30)],
    )


@pytest.fixture
def null_dispatcher(engine):
    """AlertDispatcher backed by NullChannel, using the test engine."""
    from options_agent.obs.alerts import AlertDispatcher

    with AlertDispatcher(NullChannel(), engine) as d:
        yield d


@pytest.fixture
def scheduler(config, engine, null_dispatcher):
    return CycleScheduler(config, engine=engine, dispatcher=null_dispatcher)


# ---------------------------------------------------------------------------
# Monitor — lock-and-skip
# ---------------------------------------------------------------------------


def test_monitor_runs_when_unlocked(scheduler):
    with patch("options_agent.scheduler.run_monitor_cycle") as mock_cycle:
        scheduler._run_monitor()
    mock_cycle.assert_called_once()
    assert scheduler._monitor_skip_count == 0


def test_monitor_skips_when_locked(scheduler):
    scheduler._monitor_lock.acquire()
    try:
        with patch("options_agent.scheduler.run_monitor_cycle") as mock_cycle:
            scheduler._run_monitor()
        mock_cycle.assert_not_called()
        assert scheduler._monitor_skip_count == 1
    finally:
        scheduler._monitor_lock.release()


def test_monitor_skip_count_increments(scheduler):
    scheduler._monitor_lock.acquire()
    try:
        for i in range(1, 4):
            with patch("options_agent.scheduler.run_monitor_cycle"):
                scheduler._run_monitor()
            assert scheduler._monitor_skip_count == i
    finally:
        scheduler._monitor_lock.release()


def test_monitor_skip_count_resets_on_run(scheduler):
    # Simulate two skips, then a successful run.
    scheduler._monitor_lock.acquire()
    try:
        for _ in range(2):
            with patch("options_agent.scheduler.run_monitor_cycle"):
                scheduler._run_monitor()
    finally:
        scheduler._monitor_lock.release()
    assert scheduler._monitor_skip_count == 2

    with patch("options_agent.scheduler.run_monitor_cycle"):
        scheduler._run_monitor()
    assert scheduler._monitor_skip_count == 0


def test_monitor_warn_alert_at_threshold(scheduler):
    """After _SKIP_WARN_THRESHOLD consecutive skips a WARN alert is dispatched."""
    with patch("options_agent.scheduler._dispatch_safe") as mock_dispatch:
        scheduler._monitor_lock.acquire()
        try:
            for _ in range(_SKIP_WARN_THRESHOLD):
                with patch("options_agent.scheduler.run_monitor_cycle"):
                    scheduler._run_monitor()
        finally:
            scheduler._monitor_lock.release()

    skip_dispatches = [
        call
        for call in mock_dispatch.call_args_list
        if call.args[1].event_type == AlertEventType.SCHEDULER_SKIP
        and call.args[1].severity == AlertSeverity.WARN
    ]
    assert len(skip_dispatches) >= 1


def test_monitor_no_warn_below_threshold(scheduler, null_dispatcher):
    null_channel = null_dispatcher._channel
    scheduler._monitor_lock.acquire()
    try:
        for _ in range(_SKIP_WARN_THRESHOLD - 1):
            with patch("options_agent.scheduler.run_monitor_cycle"):
                scheduler._run_monitor()
    finally:
        scheduler._monitor_lock.release()

    warn_alerts = [
        e for e in null_channel.sent if e.event_type == AlertEventType.SCHEDULER_SKIP
    ]
    assert len(warn_alerts) == 0


# ---------------------------------------------------------------------------
# Entry — kill-switch gating
# ---------------------------------------------------------------------------


def test_entry_runs_when_ks_none(scheduler, engine):
    with patch("options_agent.scheduler.run_entry_cycle") as mock_cycle:
        scheduler._run_entry()
    mock_cycle.assert_called_once()


def test_entry_skips_under_halt(scheduler, engine):
    with (
        patch("options_agent.scheduler.get_current_state") as mock_ks,
        patch("options_agent.scheduler.run_entry_cycle") as mock_cycle,
    ):
        mock_ks.return_value = KillSwitchState.HALT
        scheduler._run_entry()
    mock_cycle.assert_not_called()


def test_entry_skips_under_flatten(scheduler, engine):
    with (
        patch("options_agent.scheduler.get_current_state") as mock_ks,
        patch("options_agent.scheduler.run_entry_cycle") as mock_cycle,
    ):
        mock_ks.return_value = KillSwitchState.FLATTEN
        scheduler._run_entry()
    mock_cycle.assert_not_called()


def test_entry_skips_on_ks_read_failure(scheduler):
    """Kill-switch DB read failure → entry skipped (fail closed)."""
    with (
        patch(
            "options_agent.scheduler.get_connection",
            side_effect=RuntimeError("db down"),
        ),
        patch("options_agent.scheduler.run_entry_cycle") as mock_cycle,
    ):
        scheduler._run_entry()
    mock_cycle.assert_not_called()


# ---------------------------------------------------------------------------
# Entry — lock-and-skip
# ---------------------------------------------------------------------------


def test_entry_skips_when_locked(scheduler):
    scheduler._entry_lock.acquire()
    try:
        with (
            patch("options_agent.scheduler.get_current_state") as mock_ks,
            patch("options_agent.scheduler.run_entry_cycle") as mock_cycle,
        ):
            mock_ks.return_value = KillSwitchState.NONE
            scheduler._run_entry()
        mock_cycle.assert_not_called()
        assert scheduler._entry_skip_count == 1
    finally:
        scheduler._entry_lock.release()


def test_entry_skip_count_resets_on_run(scheduler):
    scheduler._entry_lock.acquire()
    try:
        for _ in range(2):
            with (
                patch("options_agent.scheduler.get_current_state") as mock_ks,
                patch("options_agent.scheduler.run_entry_cycle"),
            ):
                mock_ks.return_value = KillSwitchState.NONE
                scheduler._run_entry()
    finally:
        scheduler._entry_lock.release()
    assert scheduler._entry_skip_count == 2

    with (
        patch("options_agent.scheduler.get_current_state") as mock_ks,
        patch("options_agent.scheduler.run_entry_cycle"),
    ):
        mock_ks.return_value = KillSwitchState.NONE
        scheduler._run_entry()
    assert scheduler._entry_skip_count == 0


def test_entry_warn_alert_at_threshold(scheduler):
    with patch("options_agent.scheduler._dispatch_safe") as mock_dispatch:
        scheduler._entry_lock.acquire()
        try:
            for _ in range(_SKIP_WARN_THRESHOLD):
                with (
                    patch("options_agent.scheduler.get_current_state") as mock_ks,
                    patch("options_agent.scheduler.run_entry_cycle"),
                ):
                    mock_ks.return_value = KillSwitchState.NONE
                    scheduler._run_entry()
        finally:
            scheduler._entry_lock.release()

    skip_dispatches = [
        call
        for call in mock_dispatch.call_args_list
        if call.args[1].event_type == AlertEventType.SCHEDULER_SKIP
        and call.args[1].severity == AlertSeverity.WARN
    ]
    assert len(skip_dispatches) >= 1


# ---------------------------------------------------------------------------
# Lock independence: entry lock must not block monitor
# ---------------------------------------------------------------------------


def test_monitor_runs_while_entry_locked(scheduler):
    """Monitor fires even when the entry lock is held (separate locks)."""
    scheduler._entry_lock.acquire()
    try:
        with patch("options_agent.scheduler.run_monitor_cycle") as mock_monitor:
            scheduler._run_monitor()
        mock_monitor.assert_called_once()
    finally:
        scheduler._entry_lock.release()


def test_entry_runs_while_monitor_locked(scheduler):
    """Entry fires even when the monitor lock is held (separate locks)."""
    scheduler._monitor_lock.acquire()
    try:
        with (
            patch("options_agent.scheduler.get_current_state") as mock_ks,
            patch("options_agent.scheduler.run_entry_cycle") as mock_entry,
        ):
            mock_ks.return_value = KillSwitchState.NONE
            scheduler._run_entry()
        mock_entry.assert_called_once()
    finally:
        scheduler._monitor_lock.release()


# ---------------------------------------------------------------------------
# Job registration
# ---------------------------------------------------------------------------


def test_jobs_registered(config, engine):
    sched = CycleScheduler(config, engine=engine)
    job_ids = {job.id for job in sched._apscheduler.get_jobs()}
    assert "monitor" in job_ids
    # One cron job per entry_time
    for t in config.entry_times:
        assert f"entry_{t.hour:02d}{t.minute:02d}" in job_ids


def test_monitor_coalesce_enabled(config, engine):
    sched = CycleScheduler(config, engine=engine)
    monitor_job = sched._apscheduler.get_job("monitor")
    assert monitor_job is not None
    assert monitor_job.coalesce is True
