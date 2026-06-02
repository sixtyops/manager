"""Rollout timing invariants — the guarantees that keep a phased rollout from
updating the whole fleet at once.

These drive the scheduler across multiple ticks within one maintenance window
and across successive windows — the exact scenario the existing scheduler tests
never exercised, which is how the "all phases in one night" cascade shipped
unnoticed. Each test here fails against the pre-fix scheduler.

Invariants covered:
  1. One phase-job per maintenance window (no cascade).
  2. Phases progress one-per-window across successive windows.
  3. Canary soak is measured from fleet-canary completion (not firmware release).
  4. A held phase logs a `phase_held` event (observable).
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from updater import database as db
from updater import rollout_gate
from updater.scheduler import AutoUpdateScheduler

FW = "tna-30x-1.12.3-r55002.bin"  # target version 1.12.3.55002


def _discard_task(coro):
    """Swallow the broadcast task on_job_completed schedules (no running loop)."""
    coro.close()

    class _Dummy:
        def add_done_callback(self, _cb):
            return None

    return _Dummy()


def _seed_aps(n: int):
    for i in range(n):
        db.upsert_access_point(
            f"10.0.0.{10 + i}", "root", "pass", enabled=True, firmware_version="1.0.0"
        )


def _settings(hold_days: int = 0) -> dict:
    return {
        "schedule_enabled": "true",
        "timezone": "America/Chicago",
        "schedule_start_hour": "3",
        "schedule_end_hour": "4",
        "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
        "selected_firmware_30x": FW,
        "weather_check_enabled": "false",
        "firmware_canary_hold_days": str(hold_days),
    }


def _make_scheduler():
    start_update = AsyncMock(side_effect=lambda **kw: f"job-{start_update.call_count}")
    scheduler = AutoUpdateScheduler(AsyncMock(), start_update)
    return scheduler, start_update


class _Harness:
    """Drives _check_and_run at a controllable clock with the env gates stubbed
    out, so the tests isolate the phase-gate / soak logic."""

    def __init__(self, scheduler, monkeypatch):
        self.scheduler = scheduler
        self.now = datetime(2026, 6, 2, 3, 15)  # inside the 03:00-04:00 window
        async def _time(_tz):
            return (True, self.now)
        monkeypatch.setattr("updater.scheduler.services.validate_time_sources", _time)
        monkeypatch.setattr(
            "updater.scheduler.services.is_in_schedule_window", lambda *a, **k: True
        )
        monkeypatch.setattr(
            "updater.scheduler.services.minutes_until_window_end", lambda *a, **k: 45
        )

    async def tick(self, day: int = None):
        if day is not None:
            self.now = datetime(2026, 6, day, 3, 15)
        await self.scheduler._check_and_run()

    def complete_job(self):
        """Simulate the in-flight job finishing successfully for its devices."""
        rollout = db.get_active_rollout()
        pending = [d for d in db.get_rollout_devices(rollout["id"]) if d["status"] == "pending"]
        statuses = {d["ip"]: "success" for d in pending}
        with patch("updater.scheduler.asyncio.create_task", _discard_task):
            self.scheduler.on_job_completed(
                self.scheduler._current_job_id, len(statuses), 0, device_statuses=statuses
            )


@pytest.mark.asyncio
async def test_one_phase_per_window_no_cascade(mock_db, monkeypatch):
    """The bug: after canary finishes, the next phase must NOT run in the same
    window. Pre-fix this cascaded through all four phases in one window."""
    _seed_aps(5)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick()  # window 1: canary
    assert start_update.call_count == 1
    rollout = db.get_active_rollout()
    assert rollout["phase"] == "canary"

    h.complete_job()  # canary done -> advances to pct10
    assert db.get_active_rollout()["phase"] == "pct10"

    await h.tick()  # SAME window — must hold
    assert start_update.call_count == 1, "second phase ran in the same window (cascade)"
    assert db.get_active_rollout()["phase"] == "pct10"
    assert scheduler._state == "waiting"


@pytest.mark.asyncio
async def test_phases_progress_one_per_window(mock_db, monkeypatch):
    """Across successive windows the rollout walks canary -> pct10 -> pct50 ->
    pct100, exactly one phase per window."""
    _seed_aps(5)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    expected = ["canary", "pct10", "pct50", "pct100"]
    seen = []
    for day in (2, 3, 4, 5):
        await h.tick(day=day)
        rollout = db.get_active_rollout()
        if rollout is None:  # rollout completed
            break
        seen.append(rollout["phase"])
        h.complete_job()

    assert seen == expected, f"phases did not advance one-per-window: {seen}"
    assert start_update.call_count == 4


@pytest.mark.asyncio
async def test_canary_soak_measured_from_fleet_completion(mock_db, monkeypatch):
    """pct10 is held until canary has soaked for hold_days measured from when
    canary completed HERE — not from the firmware's release date."""
    _seed_aps(5)
    db.set_settings(_settings(hold_days=6))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)  # canary runs
    h.complete_job()
    rollout = db.get_active_rollout()
    assert rollout["phase"] == "pct10"
    assert rollout["canary_completed_at"] is not None

    # 5 days later — still inside the 6-day soak: pct10 must NOT run.
    await h.tick(day=7)
    assert start_update.call_count == 1
    assert scheduler._state == "blocked_canary_hold"
    assert db.get_active_rollout()["phase"] == "pct10"

    # 7 days after canary — soak cleared: pct10 runs.
    await h.tick(day=9)
    assert start_update.call_count == 2
    assert scheduler._state == "running"


@pytest.mark.asyncio
async def test_held_phase_logs_event(mock_db, monkeypatch):
    """Holding the next phase emits an observable schedule_log event."""
    _seed_aps(5)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick()
    h.complete_job()
    await h.tick()  # same window -> held

    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'phase_held'"
        ).fetchall()
    assert len(rows) >= 1


# ── Canary soak: timestamp fallback for rollouts that predate canary_completed_at ──
# These are pure-logic checks on the gate (no scheduler/DB needed).

_NOW = datetime(2026, 6, 10, 3, 15)
_SOAK = timedelta(days=6)


def test_soak_falls_back_to_last_phase_completed_at():
    """A rollout already at pct10 when this shipped has no canary_completed_at;
    the soak must still apply, measured from last_phase_completed_at (the
    canary -> pct10 advance), not be silently skipped."""
    rollout = {
        "status": "active",
        "phase": "pct10",
        "last_phase_window": None,
        "canary_completed_at": None,
        "last_phase_completed_at": (_NOW - timedelta(days=2)).isoformat(),  # within soak
    }
    may_run, reason = rollout_gate.phase_run_decision(rollout, "2026-06-10", _NOW, _SOAK)
    assert may_run is False
    assert reason == "canary_soak"


def test_soak_fallback_clears_after_period():
    """Once last_phase_completed_at + soak has elapsed, pct10 may run."""
    rollout = {
        "status": "active",
        "phase": "pct10",
        "last_phase_window": None,
        "canary_completed_at": None,
        "last_phase_completed_at": (_NOW - timedelta(days=7)).isoformat(),  # soak elapsed
    }
    may_run, reason = rollout_gate.phase_run_decision(rollout, "2026-06-10", _NOW, _SOAK)
    assert may_run is True
    assert reason is None


def test_soak_holds_when_no_timestamp_exists():
    """Fail-closed: with neither canary_completed_at nor last_phase_completed_at,
    hold pct10 rather than skip the soak."""
    rollout = {
        "status": "active",
        "phase": "pct10",
        "last_phase_window": None,
        "canary_completed_at": None,
        "last_phase_completed_at": None,
    }
    may_run, reason = rollout_gate.phase_run_decision(rollout, "2026-06-10", _NOW, _SOAK)
    assert may_run is False
    assert reason == "canary_soak"
