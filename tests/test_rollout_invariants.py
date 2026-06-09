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
from datetime import datetime, timedelta, timezone
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
        # on_job_completed stamps canary_completed_at with the real wall clock,
        # but the soak gate compares it against this harness's simulated clock.
        # Re-stamp from self.now so the soak is measured on one consistent clock;
        # otherwise the test depends on the real date and breaks once wall-clock
        # time reaches the hard-coded tick dates.
        if rollout["phase"] == "canary":
            db.set_canary_completed_at(rollout["id"], self.now.isoformat())


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


# ── Canary soak waiver: skip the soak when the firmware is already proven ──

def _seed_proven_peer(ip: str, version: str, *, model: str = None,
                      healthy: bool = True, stale: bool = False):
    """Seed an enabled AP already running `version`. `healthy=False` marks it
    errored; `stale=True` makes its last poll too old to count as proof."""
    db.upsert_access_point(ip, "root", "pass", enabled=True,
                           firmware_version=version, model=model)
    last_seen = datetime.now(timezone.utc) - (
        timedelta(days=10) if stale else timedelta(minutes=5)
    )
    db.update_ap_status(
        ip, last_seen=last_seen.isoformat(),
        last_error=(None if healthy else "link down"),
    )


@pytest.mark.asyncio
async def test_pct10_soak_waived_when_proof_appears_after_canary(mock_db, monkeypatch):
    """If the firmware becomes proven on the fleet AFTER this rollout's canary ran
    (so the canary wasn't skipped), the pct10 soak is still waived: pct10 runs the
    next window instead of waiting out the 6-day hold, logged as a waiver."""
    _seed_aps(5)              # no proven peer yet -> canary runs normally
    scheduler, start_update = _make_scheduler()
    settings = _settings(hold_days=6)
    db.set_settings(settings)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)       # canary runs (nothing proven yet)
    assert db.get_active_rollout()["phase"] == "canary"
    h.complete_job()
    assert db.get_active_rollout()["phase"] == "pct10"

    # A peer is now on the target version -> firmware proven.
    target = scheduler._target_versions(settings, None)["tna-30x"]
    _seed_proven_peer("10.0.0.200", target)

    await h.tick(day=3)       # inside the 6-day soak, but now proven -> RUN
    assert start_update.call_count == 2
    assert scheduler._state == "running"
    assert scheduler._soak_waiver_reason and "waived" in scheduler._soak_waiver_reason.lower()
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'canary_soak_waived'"
        ).fetchall()
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_pct10_holds_when_only_peer_is_unhealthy(mock_db, monkeypatch):
    """Fail-closed: a peer on the target version but reporting an error proves
    nothing — the soak still holds."""
    _seed_aps(5)
    scheduler, start_update = _make_scheduler()
    settings = _settings(hold_days=6)
    target = scheduler._target_versions(settings, None)["tna-30x"]
    _seed_proven_peer("10.0.0.200", target, healthy=False)
    db.set_settings(settings)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)
    h.complete_job()
    await h.tick(day=3)        # inside soak, peer unhealthy -> HOLD
    assert start_update.call_count == 1
    assert scheduler._state == "blocked_canary_hold"
    assert scheduler._soak_waiver_reason is None


@pytest.mark.asyncio
async def test_pct10_holds_when_only_peer_is_stale(mock_db, monkeypatch):
    """Fail-closed: a peer on the target version not seen recently is not proof."""
    _seed_aps(5)
    scheduler, start_update = _make_scheduler()
    settings = _settings(hold_days=6)
    target = scheduler._target_versions(settings, None)["tna-30x"]
    _seed_proven_peer("10.0.0.200", target, stale=True)
    db.set_settings(settings)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)
    h.complete_job()
    await h.tick(day=3)
    assert start_update.call_count == 1
    assert scheduler._state == "blocked_canary_hold"


@pytest.mark.asyncio
async def test_canary_skipped_entirely_when_proven(mock_db, monkeypatch):
    """When the firmware is already proven on the fleet, the rollout skips the
    canary phase outright and runs the 10% wave in the first window (no 1-device
    canary first)."""
    _seed_aps(20)  # plenty, so 10% is clearly more than one device
    scheduler, start_update = _make_scheduler()
    settings = _settings(hold_days=6)
    target = scheduler._target_versions(settings, None)["tna-30x"]
    _seed_proven_peer("10.0.0.200", target)
    db.set_settings(settings)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)
    assert start_update.call_count == 1
    rollout = db.get_active_rollout()
    assert rollout["phase"] == "pct10"             # straight to 10%, canary skipped
    assert len(db.get_rollout_devices(rollout["id"], phase="canary")) == 0
    assert len(db.get_rollout_devices(rollout["id"], phase="pct10")) >= 2  # ~10% of 20
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'canary_skipped'"
        ).fetchall()
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_canary_not_skipped_when_unproven(mock_db, monkeypatch):
    """With no qualifying proven peer, the canary phase runs normally (1 device)
    — the skip is fail-closed."""
    _seed_aps(5)
    scheduler, start_update = _make_scheduler()
    settings = _settings(hold_days=6)
    target = scheduler._target_versions(settings, None)["tna-30x"]
    _seed_proven_peer("10.0.0.200", target, healthy=False)  # errored -> not proof
    db.set_settings(settings)
    h = _Harness(scheduler, monkeypatch)

    await h.tick(day=2)
    rollout = db.get_active_rollout()
    assert rollout["phase"] == "canary"            # canary ran, not skipped
    assert len(db.get_rollout_devices(rollout["id"], phase="canary")) == 1
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT 1 FROM schedule_log WHERE event = 'canary_skipped'"
        ).fetchall()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_proven_signal_requires_every_pending_family(mock_db, monkeypatch):
    """Per-model, fail-closed: if one pending family is proven but another pending
    family is not, the soak is NOT waived — an unproven model never rides a proven
    model's clearance."""
    _seed_aps(3)  # tna-30x devices on 1.0.0 (pending)
    db.upsert_access_point("10.0.0.50", "root", "pass", enabled=True,
                           firmware_version="1.0.0", model="TNA-303L")  # pending tna-303l
    scheduler, _ = _make_scheduler()
    settings = _settings(hold_days=6)
    settings["selected_firmware_303l"] = "tna-303l-1.12.4-r7782.bin"
    targets = scheduler._target_versions(settings, None)
    _seed_proven_peer("10.0.0.200", targets["tna-30x"])  # proves tna-30x only
    db.set_settings(settings)
    rollout = {"firmware_file": FW, "firmware_file_303l": "tna-303l-1.12.4-r7782.bin"}

    proven, detail = scheduler._proven_soak_signal(settings, rollout)
    assert proven is False and detail is None     # tna-303l pending but unproven

    # Prove tna-303l too -> every pending family covered -> waived.
    _seed_proven_peer("10.0.0.201", targets["tna-303l"], model="TNA-303L")
    proven, detail = scheduler._proven_soak_signal(settings, rollout)
    assert proven is True and detail


@pytest.mark.asyncio
async def test_proven_signal_respects_rollout_scope(mock_db, monkeypatch):
    """A proven device OUTSIDE the rollout's scope must not waive the soak for
    in-scope devices — scoped maintenance keeps its own canary. The proof and
    pending sets are both built from the resolved schedule_scope."""
    _seed_aps(2)  # 10.0.0.10, 10.0.0.11 — in scope, on 1.0.0 (pending)
    scheduler, _ = _make_scheduler()
    settings = _settings(hold_days=6)
    settings["schedule_scope"] = "aps"
    settings["schedule_scope_data"] = "10.0.0.10,10.0.0.11"
    target = scheduler._target_versions(settings, None)["tna-30x"]
    _seed_proven_peer("10.0.0.200", target)  # proven, but OUTSIDE the scope
    db.set_settings(settings)
    rollout = {"firmware_file": FW}

    proven, detail = scheduler._proven_soak_signal(settings, rollout)
    assert proven is False and detail is None  # out-of-scope proof does not count

    # Bring an IN-scope device onto the target -> now proven within scope.
    _seed_proven_peer("10.0.0.10", target)
    proven, detail = scheduler._proven_soak_signal(settings, rollout)
    assert proven is True and detail


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


def test_soak_is_absolute_duration_not_local_offset():
    """Regression: canary_completed_at is stored in UTC, but `now` arrives in the
    operator's local timezone. The soak must clear exactly `canary_soak` after the
    UTC completion instant — not `canary_soak + local_utc_offset`.

    Pre-fix the naive UTC stamp was relabeled with now's local tz, so a 6-day soak
    ran ~5h long in US Central and could miss the maintenance window it should have
    cleared in (the production "1.2 days remaining" when ~1.0 was real)."""
    central_cdt = timezone(timedelta(hours=-5))  # US Central, summer (CDT), UTC-5
    rollout = {
        "status": "active",
        "phase": "pct10",
        "last_phase_window": None,
        # 2026-06-04 08:04:20 UTC (= 03:04 Central), stored naive-UTC as in prod.
        "canary_completed_at": "2026-06-04T08:04:20",
        "last_phase_completed_at": None,
    }
    soak = timedelta(days=6)  # clears at 2026-06-10 08:04:20 UTC

    # 6 days + ~1 min later, seen in Central (03:05 CDT == 08:05 UTC) -> CLEARED.
    just_after = datetime(2026, 6, 10, 3, 5, tzinfo=central_cdt)
    cleared, remaining = rollout_gate.canary_soak_cleared(rollout, just_after, soak)
    assert cleared is True  # pre-fix: held, ~5h short (UTC relabeled as Central)
    assert remaining is None
    may_run, reason = rollout_gate.phase_run_decision(rollout, "2026-06-10", just_after, soak)
    assert may_run is True

    # Still inside the soak (02:00 CDT == 07:00 UTC, ~1h before the UTC clear) -> HOLD.
    just_before = datetime(2026, 6, 10, 2, 0, tzinfo=central_cdt)
    cleared, remaining = rollout_gate.canary_soak_cleared(rollout, just_before, soak)
    assert cleared is False
    assert remaining is not None


# ── Gate: soak_proven clears Rule 2 only (pure-logic) ──

def _pct10(within_soak: bool = True, window=None):
    return {
        "status": "active",
        "phase": "pct10",
        "last_phase_window": window,
        "canary_completed_at": (_NOW - timedelta(days=1 if within_soak else 7)).isoformat(),
        "last_phase_completed_at": None,
    }


def test_gate_waives_soak_when_proven():
    """soak_proven clears the canary soak even inside the hold window."""
    may_run, reason = rollout_gate.phase_run_decision(
        _pct10(), "2026-06-10", _NOW, _SOAK, soak_proven=True
    )
    assert may_run is True
    assert reason == "canary_soak_waived"


def test_gate_holds_soak_when_not_proven():
    """Default (soak_proven=False) holds inside the soak window (fail-closed)."""
    may_run, reason = rollout_gate.phase_run_decision(_pct10(), "2026-06-10", _NOW, _SOAK)
    assert may_run is False
    assert reason == "canary_soak"


def test_waiver_never_overrides_one_phase_per_window():
    """Even when proven, a phase already run this window waits for the next one —
    the waiver clears the soak, never the anti-cascade rule."""
    rollout = _pct10(window="2026-06-10")
    may_run, reason = rollout_gate.phase_run_decision(
        rollout, "2026-06-10", _NOW, _SOAK, soak_proven=True
    )
    assert may_run is False
    assert reason == "already_ran_this_window"


def test_waiver_only_affects_pct10():
    """soak_proven is ignored for non-pct10 phases (they aren't soak-gated)."""
    for phase in ("canary", "pct50", "pct100"):
        rollout = {"status": "active", "phase": phase, "last_phase_window": None,
                   "canary_completed_at": None, "last_phase_completed_at": None}
        may_run, reason = rollout_gate.phase_run_decision(
            rollout, "2026-06-10", _NOW, _SOAK, soak_proven=True
        )
        assert may_run is True
        assert reason is None  # a normal run, not a waiver
