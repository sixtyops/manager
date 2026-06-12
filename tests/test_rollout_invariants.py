"""Rollout timing invariants — the guarantees that keep a fleet rollout from
updating the whole fleet at once.

The canary phase was removed: the first fleet wave (pct10) is the de-facto canary
and is protected by halt-on-first-failure. Two timing rules remain (rollout_gate):

  1. One wave-job per maintenance window (no cascade).
  2. The Firmware Hold gates the first wave (pct10) of newly-released firmware
     until N days after the firmware's Tachyon release date — or earlier once a
     device of that model family is confirmed working on it.

These drive the scheduler across multiple ticks within one maintenance window and
across successive windows — the exact scenario that lets the "all phases in one
night" cascade slip through if Rule 1 ever regresses.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from updater import database as db
from updater import rollout_gate
from updater.scheduler import AutoUpdateScheduler

# The release-date Firmware Hold is evaluated against the real wall clock (not the
# harness's simulated scheduler clock), so dated test firmware must be relative to
# the actual "today" to stay deterministic as time passes.
def _dated(prefix: str, days_ago: int) -> str:
    d = (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")
    return f"{prefix}-{d}-x.bin"


# Undated firmware (no release date in the name) — the hold falls back to the
# registry/legacy path and is treated as cleared, so these exercise the wave/window
# rules without a hold getting in the way.
FW = "tna-30x-1.12.3-r55002.bin"  # target version 1.12.3.55002
# Released yesterday — with hold_days=6 its hold has NOT elapsed (first wave held).
FW_HELD = _dated("tna-30x-1.12.3-r55002", 1)
# Released 30 days ago — its 6-day hold has objectively elapsed.
FW_AGED = _dated("tna-30x-1.12.3-r55002", 30)
# A 303L firmware also released yesterday (held).
FW_303L_HELD = _dated("tna-303l-1.12.4-r7782", 1)


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


def _seed_confirmed(ip: str, version: str, *, model: str = None):
    """Seed an enabled, healthy AP confirmed working on `version` (on-version, no
    error, seen now, plus a recorded confirmation event)."""
    db.upsert_access_point(ip, "root", "pass", enabled=True,
                           firmware_version=version, model=model)
    db.update_ap_status(
        ip, last_seen=datetime.now(timezone.utc).isoformat(), last_error=None
    )
    db.mark_device_firmware_confirmed(ip, version)


def _settings(hold_days: int = 0, firmware: str = FW) -> dict:
    return {
        "schedule_enabled": "true",
        "timezone": "America/Chicago",
        "schedule_start_hour": "3",
        "schedule_end_hour": "4",
        "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
        "selected_firmware_30x": firmware,
        "weather_check_enabled": "false",
        "firmware_canary_hold_days": str(hold_days),
    }


def _make_scheduler():
    start_update = AsyncMock(side_effect=lambda **kw: f"job-{start_update.call_count}")
    scheduler = AutoUpdateScheduler(AsyncMock(), start_update)
    return scheduler, start_update


class _Harness:
    """Drives _check_and_run at a controllable clock with the env gates stubbed
    out, so the tests isolate the wave-gate / hold logic."""

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

    def complete_job(self, failed: int = 0):
        """Simulate the in-flight job finishing for its devices (all success, or
        `failed` of them failing → halt-on-first-failure)."""
        rollout = db.get_active_rollout()
        pending = [d for d in db.get_rollout_devices(rollout["id"]) if d["status"] == "pending"]
        statuses = {d["ip"]: "success" for d in pending}
        fail_n = failed
        for d in pending:
            if fail_n <= 0:
                break
            statuses[d["ip"]] = "failed"
            fail_n -= 1
        success = sum(1 for s in statuses.values() if s == "success")
        with patch("updater.scheduler.asyncio.create_task", _discard_task):
            self.scheduler.on_job_completed(
                self.scheduler._current_job_id, success, failed, device_statuses=statuses
            )


# ── Rule 1: one wave per maintenance window ──

@pytest.mark.asyncio
async def test_one_phase_per_window_no_cascade(mock_db, monkeypatch):
    """After the 10% wave finishes, the next wave must NOT run in the same window
    (the anti-cascade rule)."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick()  # window 1: pct10
    assert start_update.call_count == 1
    rollout = db.get_active_rollout()
    assert rollout["phase"] == "pct10"

    h.complete_job()  # pct10 done -> advances to pct50
    assert db.get_active_rollout()["phase"] == "pct50"

    await h.tick()  # SAME window — must hold
    assert start_update.call_count == 1, "second wave ran in the same window (cascade)"
    assert db.get_active_rollout()["phase"] == "pct50"
    assert scheduler._state == "waiting"


@pytest.mark.asyncio
async def test_phases_progress_one_per_window(mock_db, monkeypatch):
    """Across successive windows the rollout walks pct10 -> pct50 -> pct100,
    exactly one wave per window."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    expected = ["pct10", "pct50", "pct100"]
    seen = []
    for day in (2, 3, 4, 5):
        await h.tick(day=day)
        rollout = db.get_active_rollout()
        if rollout is None:  # rollout completed
            break
        seen.append(rollout["phase"])
        h.complete_job()

    assert seen == expected, f"waves did not advance one-per-window: {seen}"
    assert start_update.call_count == 3


@pytest.mark.asyncio
async def test_held_phase_logs_event(mock_db, monkeypatch):
    """Holding the next wave for the window rule emits an observable event."""
    _seed_aps(10)
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


# ── Rule 2: the Firmware Hold gates the first fleet wave ──

@pytest.mark.asyncio
async def test_release_date_hold_gates_first_wave(mock_db, monkeypatch):
    """Newly-released firmware (release date today, 6-day hold) must NOT roll the
    first wave — the fleet waits out the Firmware Hold."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=6, firmware=FW_HELD))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)  # release 2026-06-01 + 6 days clears 06-07; today is 06-02
    assert start_update.call_count == 0
    assert scheduler._state == "blocked_firmware_hold"
    rollout = db.get_active_rollout()
    assert rollout["phase"] == "pct10"
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'blocked_firmware_hold'"
        ).fetchall()
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_hold_clears_after_days_elapse(mock_db, monkeypatch):
    """Once the firmware's release date + hold days has elapsed, the first wave
    runs without any confirmed device."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=6, firmware=FW_AGED))  # released 2020 -> long clear
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)
    assert start_update.call_count == 1
    assert scheduler._state == "running"
    assert db.get_active_rollout()["phase"] == "pct10"


@pytest.mark.asyncio
async def test_hold_clears_via_confirmed_device(mock_db, monkeypatch):
    """A device confirmed working on the new firmware clears its family's hold
    early — the first wave runs inside the release-date hold window, and the
    clearing device is logged."""
    _seed_aps(10)
    settings = _settings(hold_days=6, firmware=FW_HELD)
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    target = scheduler._target_versions(settings, None)["tna-30x"]
    _seed_confirmed("10.0.0.200", target)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)  # still inside the release-date hold, but confirmed -> RUN
    assert start_update.call_count == 1
    assert scheduler._state == "running"
    assert scheduler._hold_clear_reason and "confirmed working" in scheduler._hold_clear_reason
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'firmware_hold_cleared'"
        ).fetchall()
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_hold_does_not_clear_for_unconfirmed_or_unhealthy_device(mock_db, monkeypatch):
    """Fail-closed: a same-family device on the target version but NOT recorded as
    confirmed (e.g. organic/observed only) does not clear the hold."""
    _seed_aps(10)
    settings = _settings(hold_days=6, firmware=FW_HELD)
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    target = scheduler._target_versions(settings, None)["tna-30x"]
    # On the target version + healthy, but no confirmation event recorded.
    db.upsert_access_point("10.0.0.200", "root", "pass", enabled=True, firmware_version=target)
    db.update_ap_status("10.0.0.200", last_seen=datetime.now(timezone.utc).isoformat(), last_error=None)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)
    assert start_update.call_count == 0
    assert scheduler._state == "blocked_firmware_hold"


@pytest.mark.asyncio
async def test_confirmed_clear_is_per_family(mock_db, monkeypatch):
    """A confirmed tna-30x device never clears a held tna-303l family (the
    per-family clearing invariant). Because the hold is enforced all-or-nothing,
    a still-held 303l family holds the whole first wave even though tna-30x is
    cleared — so no held firmware (incl. attached CPEs) ever slips out."""
    _seed_aps(10)  # tna-30x devices on 1.0.0
    db.upsert_access_point("10.0.0.50", "root", "pass", enabled=True,
                           firmware_version="1.0.0", model="TNA-303L")
    settings = _settings(hold_days=6, firmware=FW_HELD)
    settings["selected_firmware_303l"] = FW_303L_HELD
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    targets = scheduler._target_versions(settings, None)
    _seed_confirmed("10.0.0.200", targets["tna-30x"])  # confirms tna-30x only

    held, family_holds = scheduler._held_families(settings, None)
    assert "tna-303l" in held          # 303l still held (no confirmed device)
    assert "tna-30x" not in held       # 30x cleared by the confirmed device

    h = _Harness(scheduler, monkeypatch)
    await h.tick(day=2)
    # All-or-nothing: the held 303l family holds the whole first wave.
    assert start_update.call_count == 0
    assert scheduler._state == "blocked_firmware_hold"

    # Confirm a 303l device too -> both families cleared -> the wave runs.
    _seed_confirmed("10.0.0.51", targets["tna-303l"], model="TNA-303L")
    await h.tick(day=3)
    assert start_update.call_count == 1
    assert scheduler._state == "running"


# ── Halt-on-first-failure ──

@pytest.mark.asyncio
async def test_halt_on_first_failure_pauses_rollout(mock_db, monkeypatch):
    """A device that fails during a wave pauses the whole rollout — the next wave
    is never assigned (halt-on-first-failure)."""
    _seed_aps(10)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    h = _Harness(scheduler, monkeypatch)

    await h.tick()  # pct10 runs
    assert db.get_active_rollout()["phase"] == "pct10"
    h.complete_job(failed=1)  # one device fails

    rollout = db.get_active_rollout()
    assert rollout["status"] == "paused"
    assert rollout["phase"] == "pct10"  # never advanced to pct50

    await h.tick(day=3)  # next window — paused rollout does not run
    assert start_update.call_count == 1


# ── Pure-logic gate tests (no scheduler/DB) ──

_NOW = "2026-06-10"


def _pct10(window=None):
    return {"status": "active", "phase": "pct10", "last_phase_window": window}


def test_gate_holds_first_wave_when_held():
    """The hold blocks pct10 when the caller reports first_wave_held."""
    may_run, reason = rollout_gate.phase_run_decision(_pct10(), _NOW, first_wave_held=True)
    assert may_run is False
    assert reason == "firmware_hold"


def test_gate_runs_first_wave_when_clear():
    """pct10 runs when the hold is clear."""
    may_run, reason = rollout_gate.phase_run_decision(_pct10(), _NOW, first_wave_held=False)
    assert may_run is True
    assert reason is None


def test_firmware_hold_clear_never_overrides_one_phase_per_window():
    """Even with the hold clear, a wave already run this window waits for the next
    one — the hold clearance never bypasses the anti-cascade rule (Rule 1 first)."""
    rollout = _pct10(window=_NOW)
    may_run, reason = rollout_gate.phase_run_decision(rollout, _NOW, first_wave_held=False)
    assert may_run is False
    assert reason == "already_ran_this_window"


def test_hold_only_affects_pct10():
    """first_wave_held is ignored for non-pct10 waves (they aren't hold-gated)."""
    for phase in ("pct50", "pct100"):
        rollout = {"status": "active", "phase": phase, "last_phase_window": None}
        may_run, reason = rollout_gate.phase_run_decision(rollout, _NOW, first_wave_held=True)
        assert may_run is True
        assert reason is None


def test_gate_fail_closed_on_non_active_status():
    """Any non-active status holds (fail-closed)."""
    rollout = {"status": "paused", "phase": "pct10", "last_phase_window": None}
    may_run, reason = rollout_gate.phase_run_decision(rollout, _NOW, first_wave_held=False)
    assert may_run is False
    assert reason == "status_paused"


# ── Migration: in-flight canary rollouts remap to pct10 ──

def test_create_rollout_starts_at_pct10(mock_db):
    """New rollouts start at the first fleet wave, not the removed canary phase."""
    rid = db.create_rollout(FW)
    assert db.get_rollout(rid)["phase"] == "pct10"
