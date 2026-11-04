"""Timing and failure invariants for scheduled firmware queue admission.

The scheduler derives pending devices from current inventory, applies Firmware
Hold per family, and persists each target's cutoff across restarts. One failure
stops later queue admission across families. Maintenance window identity remains
timezone-aware across midnight and daylight-saving changes.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from updater import database as db
from updater.core.gates import MaintenanceSchedule, window_instance_key
from tests.fixtures.fleet import make_fleet, set_version, set_health, confirm
from updater import rollout_gate
from updater import app as app_module
from updater.scheduler import AutoUpdateScheduler

# The release-date Firmware Hold is evaluated against the real wall clock (not the
# harness's simulated scheduler clock), so dated test firmware must be relative to
# the actual "today" to stay deterministic as time passes.
def _dated(prefix: str, days_ago: int) -> str:
    d = (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")
    return f"{prefix}-{d}-x.bin"


# Undated firmware (no release date in the name) — the hold falls back to the
# registry/legacy path and is treated as cleared.
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
    make_fleet(sites=1, aps_per_site=n, switches_per_site=0,
               ap_models=("TNA-301",))


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
        "scheduled_rollout_safety_ready": "true",
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
    # Use synthetic CPE inventory without a poller. Dedicated scheduler tests
    # cover the fresh-login result path.
    scheduler._precheck_cpe_credentials = AsyncMock(return_value=())
    return scheduler, start_update


def test_rollout_creation_keeps_membership_history_inert(mock_db):
    rollout_id = db.create_rollout(FW)
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO rollout_devices (rollout_id, ip, device_type, phase_assigned, status)
               VALUES (?, ?, ?, ?, ?)""",
            (rollout_id, "192.0.2.10", "ap", "pct10", "updated"),
        )
        conn.execute(
            """INSERT INTO firmware_window_stops
               (window_key, job_id, device_ip, gate, reason, stopped_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ('["firmware","2026-06-02"]', "job-1", "192.0.2.10", "flash", "failed", "2026-06-02T03:00:00"),
        )

    db._migrate(mock_db)
    db._migrate(mock_db)

    with db.get_db() as conn:
        membership = conn.execute(
            "SELECT ip, status FROM rollout_devices WHERE rollout_id = ?", (rollout_id,)
        ).fetchone()
        stop = conn.execute("SELECT device_ip, resolved_at FROM firmware_window_stops").fetchone()
    assert tuple(membership) == ("192.0.2.10", "updated")
    assert tuple(stop) == ("192.0.2.10", None)
    assert db.get_rollout_devices(db.create_rollout("another.bin")) == []


def test_overnight_window_keeps_its_start_date_key():
    schedule = MaintenanceSchedule(
        schedule_id=7, days=frozenset({0}), start_hour=23, end_hour=2, tz="UTC"
    )
    before_midnight = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 6, 2, 1, 30, tzinfo=timezone.utc)
    assert window_instance_key(schedule, before_midnight) == (7, "2026-06-01")
    assert window_instance_key(schedule, after_midnight) == (7, "2026-06-01")


class _Harness:
    """Drive scheduler ticks at a fixed clock with environmental checks stubbed."""

    def __init__(self, scheduler, monkeypatch):
        self.scheduler = scheduler
        self.now = datetime(2026, 6, 2, 3, 15, tzinfo=ZoneInfo("America/Chicago"))

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
            self.now = datetime(2026, 6, day, 3, 15, tzinfo=ZoneInfo("America/Chicago"))
        import hashlib
        for key in ("selected_firmware_30x", "selected_firmware_303l", "selected_firmware_tns100"):
            filename = db.get_setting(key)
            if filename and not db.get_firmware_sha256(filename):
                db.register_firmware(filename, sha256=hashlib.sha256(filename.encode()).hexdigest())
        await self.scheduler._check_and_run()

    def complete_job(self, failed: int = 0):
        context = self.scheduler._queue_job_context[self.scheduler._current_job_id]
        entries = context["entries"]
        statuses = {entry["ip"]: "success" for entry in entries}
        if failed and entries:
            statuses[entries[0]["ip"]] = "failed"
        with patch("updater.scheduler.asyncio.create_task", _discard_task):
            self.scheduler.on_job_completed(
                self.scheduler._current_job_id, len(entries) - failed, failed,
                device_statuses=statuses,
            )


@pytest.mark.asyncio
async def test_scheduler_dispatches_exact_serial_queue(mock_db, monkeypatch):
    _seed_aps(2)
    parent_ip = "192.0.2.10"
    db.upsert_cpe(parent_ip, {
        "ip": "192.0.2.50", "model": "TNA-301", "firmware_version": "1.0.0",
        "auth_status": "ok",
    })
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()

    await _Harness(scheduler, monkeypatch).tick()

    start_update.assert_awaited_once()
    kwargs = start_update.await_args.kwargs
    assert [entry["role"] for entry in kwargs["device_entries"]] == ["cpe", "ap", "ap"]
    assert kwargs["concurrency"] == 1
    assert kwargs["bank_mode"] == "one"
    assert kwargs["device_entries"][0]["parent_ap"] == parent_ip


@pytest.mark.asyncio
async def test_scheduler_tick_leaves_paused_legacy_rollout_untouched(mock_db, monkeypatch):
    _seed_aps(1)
    db.set_settings(_settings(hold_days=0))
    legacy_id = db.create_rollout("old-firmware.bin")
    db.pause_rollout(legacy_id, "Paused legacy rollout")
    legacy_before = db.get_rollout(legacy_id)
    scheduler, start_update = _make_scheduler()

    with patch.object(db, "get_active_rollout", side_effect=AssertionError("legacy read")) as get_active, \
         patch.object(db, "get_last_rollout_for_firmware_set", side_effect=AssertionError("legacy read")) as get_last, \
         patch.object(db, "create_rollout", side_effect=AssertionError("legacy write")) as create, \
         patch.object(db, "cancel_rollout", side_effect=AssertionError("legacy write")) as cancel:
        await _Harness(scheduler, monkeypatch).tick()

    start_update.assert_awaited_once()
    assert start_update.await_args.kwargs["device_entries"]
    get_active.assert_not_called()
    get_last.assert_not_called()
    create.assert_not_called()
    cancel.assert_not_called()
    assert db.get_rollout(legacy_id) == legacy_before


@pytest.mark.asyncio
async def test_queue_failure_stops_later_windows_and_target_changes(mock_db, monkeypatch):
    _seed_aps(2)
    db.set_settings(_settings(hold_days=0))
    scheduler, start_update = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)

    await harness.tick()
    harness.complete_job(failed=1)
    stop = db.get_firmware_window_stop(("firmware", "2026-06-02"))
    assert stop and stop["device_ip"]
    assert db.get_unresolved_firmware_window_stops()

    settings = db.get_all_settings()
    settings["selected_firmware_30x"] = _dated("tna-30x-1.12.4-r55003", 30)
    db.set_settings(settings)
    await harness.tick(day=3)

    assert start_update.await_count == 1
    assert db.get_unresolved_firmware_window_stops()[0]["device_ip"] == stop["device_ip"]
    assert scheduler._state == "blocked_safety"


@pytest.mark.asyncio
async def test_held_family_waits_without_blocking_an_unheld_family(mock_db, monkeypatch):
    fleet = make_fleet(sites=1, aps_per_site=2, switches_per_site=0,
                       ap_models=("TNA-301", "TNA-303L"))
    settings = _settings(hold_days=6, firmware=FW_AGED)
    settings["selected_firmware_303l"] = FW_303L_HELD
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()

    await _Harness(scheduler, monkeypatch).tick()

    assert start_update.await_count == 1
    entries = start_update.await_args.kwargs["device_entries"]
    assert [entry["ip"] for entry in entries] == [fleet["aps"][0]["ip"]]
    assert scheduler._state == "running"


# ── Pure-logic gate tests (no scheduler/DB) ──

_NOW = "2026-06-10"


def test_firmware_window_key_uses_schedule_zone_for_naive_scheduler_time():
    """A naive injected time represents schedule-local time on every host zone."""
    key = rollout_gate.firmware_window_key(
        {
            "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
            "schedule_start_hour": 3,
            "schedule_end_hour": 4,
            "schedule_timezone": "America/Chicago",
        },
        datetime(2026, 6, 2, 3, 15),
    )

    assert key == ("firmware", "2026-06-02")

# Current-schema managed confirmation boundaries. Freeze both clocks used by
# the real Hold path: scheduler health freshness and database release age.
_HOLD_NOW = datetime(2026, 6, 2, 3, 15, tzinfo=timezone.utc)
_HOLD_VERSION = "1.12.3.55002"


@pytest.fixture
def hold_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _HOLD_NOW.astimezone(tz) if tz else _HOLD_NOW.replace(tzinfo=None)

    monkeypatch.setattr("updater.scheduler.datetime", Clock)
    monkeypatch.setattr("updater.database.datetime", Clock)
    return _HOLD_NOW


def _fleet_hold_settings(fleet, role):
    settings = _settings(hold_days=6, firmware="tna-30x-1.12.3-r55002-20260601-x.bin")
    settings.update(schedule_scope="sites", schedule_scope_data=str(fleet["sites"][0]))
    if role == "switch":
        # Same version across families must not make AP proof count for switches.
        settings["selected_firmware_tns100"] = "tns-100-1.12.3-r55002-20260601-x.bin"
    return settings


def test_managed_fleet_uses_current_schema(mock_db):
    assert db.get_devices(enabled_only=False) == []
    fleet = make_fleet()
    assert len(fleet["sites"]) == 2
    assert len(fleet["aps"]) == 10
    assert len(fleet["switches"]) == 2
    assert {d["model"] for d in fleet["aps"]} == {"TNA-301", "TNA-303L"}
    for device in fleet["aps"] + fleet["switches"]:
        row = db.get_device(device["ip"])
        assert {k: row[k] for k in device} == device
        assert row["enabled"] == 1
        assert row["vendor"] == "tachyon"
        assert row["firmware_version"] == "1.0.0"
    assert len(db.get_devices(enabled_only=False)) == 12


@pytest.mark.parametrize("role", ["ap", "switch"])
@pytest.mark.parametrize("case", [
    "valid", "wrong_version", "unconfirmed", "stale", "missing_seen",
    "invalid_seen", "error", "disabled", "other_site", "wrong_family",
    "at_cutoff", "before_cutoff", "after_cutoff",
])
@pytest.mark.asyncio
async def test_managed_confirmation_hold_boundaries(mock_db, monkeypatch, hold_clock, role, case):
    fleet = make_fleet(aps_per_site=3, switches_per_site=3, ap_models=("TNA-301",))
    settings = _fleet_hold_settings(fleet, role)
    devices = fleet["aps"] if role == "ap" else fleet["switches"]
    prover = devices[3] if case == "other_site" else devices[0]
    family = "tna-30x" if role == "ap" else "tns-100"
    set_version(prover, "1.1.0" if case == "wrong_version" else _HOLD_VERSION)
    if case != "missing_seen":
        seen = hold_clock
        if case == "stale":
            seen -= timedelta(days=2)
        elif case in ("at_cutoff", "before_cutoff", "after_cutoff"):
            seen -= timedelta(hours=24)
            seen += timedelta(microseconds={"at_cutoff": 0, "before_cutoff": -1,
                                            "after_cutoff": 1}[case])
        set_health(prover, "invalid" if case == "invalid_seen" else seen.isoformat(),
                   error="synthetic poll failure" if case == "error" else None)
    if case != "unconfirmed":
        confirm(prover, _HOLD_VERSION)
    if case == "disabled":
        assert db.bulk_set_enabled(role, [prover["ip"]], False) == 1
    if case == "wrong_family":
        update = db.update_ap_status if role == "ap" else db.update_switch_status
        update(prover["ip"], model="TNA-303L" if role == "ap" else "TNA-301")
    if role == "switch":
        # Valid AP proof leaves only the switch family's gate to test.
        ap = fleet["aps"][0]
        set_version(ap, _HOLD_VERSION)
        set_health(ap, hold_clock.isoformat())
        confirm(ap, _HOLD_VERSION)

    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    accepted = case in ("valid", "at_cutoff", "after_cutoff")
    held, holds = scheduler._held_families(settings, None)
    assert (family not in held) is accepted
    assert holds[family]["confirmed_by"] == (prover["ip"] if accepted else None)
    h = _Harness(scheduler, monkeypatch)
    await h.tick()
    should_run = accepted or role == "switch"
    assert start_update.call_count == int(should_run)
    assert scheduler._state == ("running" if should_run else "blocked_firmware_hold")
    if accepted:
        call = start_update.call_args.kwargs
        candidates = [
            entry["ip"] for entry in call["device_entries"]
            if entry["role"] == role
        ]
        assert candidates and prover["ip"] not in candidates


@pytest.mark.parametrize("failed_read", ["get_all_access_points_dict", "get_all_switches_dict"])
@pytest.mark.asyncio
async def test_confirmation_fleet_read_failure_keeps_wave_held(mock_db, monkeypatch, hold_clock, failed_read):
    fleet = make_fleet(aps_per_site=3, switches_per_site=3, ap_models=("TNA-301",))
    settings = _fleet_hold_settings(fleet, "switch")
    for prover in (fleet["aps"][0], fleet["switches"][0]):
        set_version(prover, _HOLD_VERSION)
        set_health(prover, hold_clock.isoformat())
        confirm(prover, _HOLD_VERSION)
    db.set_settings(settings)
    scheduler, start_update = _make_scheduler()
    real_read = getattr(db, failed_read)

    def read(*, enabled_only=True):
        # Fail the confirmation read; leave the pending-work read available.
        # No Hold or scope helper is replaced.
        if enabled_only:
            raise RuntimeError("synthetic fleet read failure")
        return real_read(enabled_only=enabled_only)

    monkeypatch.setattr(db, failed_read, read)
    assert scheduler._confirmed_family_devices(settings, {}, scheduler._target_versions(settings, None)) == {}
    held, _ = scheduler._held_families(settings, None)
    assert held == {"tna-30x", "tns-100"}
    await _Harness(scheduler, monkeypatch).tick()
    assert start_update.call_count == 0
    assert scheduler._state == "blocked_firmware_hold"
