"""Scheduler admission and completion for the derived firmware queue."""

from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from updater import database as db
from updater.scheduler import AutoUpdateScheduler


ZONE = ZoneInfo("America/Chicago")
FW_30X = "tna-30x-2.0.0-r200.bin"
FW_TNS = "tns-100-2.0.0-r200.bin"
HASH_30X = "a" * 64
HASH_TNS = "b" * 64


def _queue_fixture(mock_db, *, firmware_30x=FW_30X, now=None):
    now = now or datetime(2026, 6, 23, 3, 10, tzinfo=ZONE)
    db.set_settings({
        "schedule_scope": "all",
        "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
        "schedule_start_hour": "3",
        "schedule_end_hour": "6",
        "schedule_timezone": "America/Chicago",
        "firmware_window_ramp_minutes": "[30, 120]",
        "firmware_update_cooldown_days": "0",
        "firmware_canary_hold_days": "0",
        "selected_firmware_30x": firmware_30x,
        "selected_firmware_303l": "",
        "selected_firmware_tns100": FW_TNS,
    })
    db.register_firmware(firmware_30x, sha256=HASH_30X)
    db.register_firmware(FW_TNS, sha256=HASH_TNS)
    db.upsert_access_point(
        "10.0.0.1", "root", "secret", enabled=True, model="TNA-301",
        firmware_version="1.0.0",
    )
    db.upsert_switch(
        "10.0.1.1", "admin", "secret", enabled=True, model="TNS-100",
        firmware_version="1.0.0",
    )
    db.create_rollout(firmware_30x, firmware_file_tns100=FW_TNS)
    return now


async def _admit(scheduler, settings, now, start):
    scheduler._scheduled_admission_allowed = lambda: True
    await scheduler._run_derived_queue(
        settings, now,
        ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        3, 6, "America/Chicago", settings["selected_firmware_30x"], "", FW_TNS,
        False,
    )


@pytest.mark.asyncio
async def test_duplicate_tick_reuses_job_and_timing(mock_db):
    now = _queue_fixture(mock_db)
    start = AsyncMock(return_value="queue-job")
    scheduler = AutoUpdateScheduler(AsyncMock(), start)
    settings = db.get_all_settings()

    await _admit(scheduler, settings, now, start)
    first = db.get_firmware_sha256(FW_30X)
    with db.get_db() as conn:
        timing = conn.execute(
            "SELECT ordinal, cutoff_utc FROM firmware_window_timing "
            "WHERE family = 'tna-30x' AND target_sha256 = ?", (first,),
        ).fetchone()
    await scheduler._check_and_run()

    assert start.await_count == 1
    assert timing["ordinal"] == 1
    assert timing["cutoff_utc"] == "2026-06-23T08:30:00+00:00"
    assert start.await_args.kwargs["concurrency"] == 1
    assert [entry["role"] for entry in start.await_args.kwargs["device_entries"]] == ["ap", "switch"]


@pytest.mark.asyncio
async def test_paused_legacy_rollout_does_not_change_serial_queue_admission(mock_db):
    now = _queue_fixture(mock_db)
    legacy_id = db.get_active_rollout()["id"]
    db.pause_rollout(legacy_id, "legacy wave paused")
    scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock(return_value="serial-job"))

    await _admit(scheduler, db.get_all_settings(), now, scheduler.start_update_func)

    assert scheduler.start_update_func.await_count == 1
    assert scheduler.start_update_func.await_args.kwargs["concurrency"] == 1
    assert db.get_rollout(legacy_id)["status"] == "paused"


def test_scheduler_status_hides_legacy_wave_rollout(mock_db, monkeypatch):
    rollout_id = db.create_rollout(FW_30X)
    monkeypatch.setattr(db, "get_rollout_devices", lambda _rollout_id: [{"ip": "10.0.0.1"}])
    scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())

    assert scheduler.get_status()["rollout"] is None


@pytest.mark.asyncio
async def test_restart_reuses_cutoff_and_later_window_continues(mock_db):
    now = _queue_fixture(mock_db)
    settings = db.get_all_settings()
    first_scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock(return_value="first"))
    await _admit(first_scheduler, settings, now, first_scheduler.start_update_func)
    with db.get_db() as conn:
        first_cutoff = conn.execute(
            "SELECT cutoff_utc FROM firmware_window_timing WHERE family='tna-30x'"
        ).fetchone()["cutoff_utc"]

    restarted = AutoUpdateScheduler(AsyncMock(), AsyncMock(return_value="after-restart"))
    await _admit(restarted, settings, now, restarted.start_update_func)
    with db.get_db() as conn:
        same_window = conn.execute(
            "SELECT ordinal, cutoff_utc FROM firmware_window_timing WHERE family='tna-30x'"
        ).fetchone()
    assert (same_window["ordinal"], same_window["cutoff_utc"]) == (1, first_cutoff)

    next_window = datetime(2026, 6, 24, 3, 10, tzinfo=ZONE)
    continuation = AutoUpdateScheduler(AsyncMock(), AsyncMock(return_value="next-window"))
    await _admit(continuation, settings, next_window, continuation.start_update_func)
    with db.get_db() as conn:
        later = conn.execute(
            "SELECT ordinal, cutoff_utc FROM firmware_window_timing WHERE family='tna-30x'"
        ).fetchone()
    assert later["ordinal"] == 2
    assert later["cutoff_utc"] == "2026-06-24T10:00:00+00:00"
    assert continuation._current_job_id == "next-window"


@pytest.mark.asyncio
async def test_failure_stop_blocks_other_families_and_survives_target_change(mock_db):
    now = _queue_fixture(mock_db)
    start = AsyncMock(return_value="failed-job")
    scheduler = AutoUpdateScheduler(AsyncMock(), start)
    settings = db.get_all_settings()
    await _admit(scheduler, settings, now, start)
    scheduler.on_job_completed(
        "failed-job", success_count=0, failed_count=1,
        device_statuses={"10.0.0.1": "failed"},
    )

    stop = db.get_firmware_window_stop(("firmware", "2026-06-23"))
    assert stop["device_ip"] == "10.0.0.1"
    assert db.get_unresolved_firmware_window_stops()

    new_target = "tna-30x-2.1.0-r210.bin"
    db.register_firmware(new_target, sha256="c" * 64)
    changed = dict(settings, selected_firmware_30x=new_target)
    later = AutoUpdateScheduler(AsyncMock(), AsyncMock(return_value="must-not-start"))
    next_window = datetime(2026, 6, 24, 3, 10, tzinfo=ZONE)
    await _admit(later, changed, next_window, later.start_update_func)

    assert later.start_update_func.await_count == 0
    assert db.get_unresolved_firmware_window_stops()[0]["device_ip"] == "10.0.0.1"


@pytest.mark.asyncio
async def test_held_recovery_family_blocks_later_devices(mock_db, monkeypatch):
    now = _queue_fixture(mock_db)
    prior_stop = db.stop_firmware_window(
        ("firmware", "2026-06-22"), "prior-job", "10.0.0.1", "update", "device failed",
    )
    db.resolve_firmware_window_stop(prior_stop["id"], "operator", "reflash approved")
    scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock(return_value="must-not-start"))
    monkeypatch.setattr(
        scheduler, "_held_families",
        lambda settings, _rollout: ({"tna-30x"}, {
            "tna-30x": {"clears_at": "2026-06-25T00:00:00+00:00"},
        }),
    )

    await _admit(scheduler, db.get_all_settings(), now, scheduler.start_update_func)

    assert scheduler.start_update_func.await_count == 0
    assert scheduler._state == "blocked_firmware_hold"
    assert "Firmware hold" in scheduler._block_reason
