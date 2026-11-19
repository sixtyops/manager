"""Auto-Update controls admission; old readiness values have no effect."""

from unittest.mock import patch

import pytest

from updater import database as db
from updater import rollout_gate
from tests.test_rollout_invariants import _Harness, _make_scheduler, _seed_aps, _settings


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [None, "false", "true", "invalid"])
async def test_auto_update_admits_without_readiness(mock_db, monkeypatch, legacy):
    _seed_aps(10)
    db.set_settings(_settings())
    if legacy is not None:
        db.set_setting("scheduled_rollout_safety_ready", legacy)
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)
    await harness.tick()
    start.assert_awaited_once()
    assert "safety_block_reason" not in scheduler.get_status()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", ["false", "", "TRUE", "1", "invalid"])
async def test_auto_update_off_admits_no_job(mock_db, monkeypatch, enabled):
    _seed_aps(10)
    db.set_settings(_settings())
    db.set_setting("schedule_enabled", enabled)
    scheduler, start = _make_scheduler()
    await _Harness(scheduler, monkeypatch).tick()
    start.assert_not_awaited()
    assert scheduler._state == "disabled"


@pytest.mark.asyncio
async def test_unresolved_stop_still_blocks_without_readiness(mock_db, monkeypatch):
    _seed_aps(10)
    db.set_settings(_settings())
    db.stop_firmware_window(
        ("firmware", "2026-06-01"), "failed-job", "192.0.2.1", "flash", "Failed",
    )
    scheduler, start = _make_scheduler()
    await _Harness(scheduler, monkeypatch).tick()
    start.assert_not_awaited()
    assert scheduler._state == "blocked_safety"
    assert "resolve the failure" in scheduler._block_reason


@pytest.mark.asyncio
async def test_recorded_auto_update_can_be_revoked_before_a_later_window(mock_db, monkeypatch):
    _seed_aps(10)
    db.set_settings(_settings())
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)
    await harness.tick()
    harness.complete_job()
    before = db.get_active_rollout()
    db.set_setting("schedule_enabled", "false")

    await harness.tick(day=3)

    assert start.call_count == 1
    assert db.get_active_rollout() == before
    assert scheduler._state == "disabled"


def test_readiness_is_not_created_and_old_value_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "manager.db")
    db.init_db()
    assert "scheduled_rollout_safety_ready" not in db.get_all_settings()
    db.set_setting("scheduled_rollout_safety_ready", "invalid")
    db.init_db()
    assert db.get_setting("scheduled_rollout_safety_ready") == "invalid"
    db.set_setting("schedule_enabled", "true")
    assert rollout_gate.current_scheduled_admission_decision() == (True, None)


def test_deleted_setting_is_not_writable_or_validated(authed_client, mock_db):
    db.set_setting("scheduled_rollout_safety_ready", "false")
    response = authed_client.post("/api/settings/save", json={
        "scheduled_rollout_safety_ready": "invalid", "schedule_enabled": "true",
    })
    assert response.status_code == 200
    assert db.get_setting("scheduled_rollout_safety_ready") == "false"
    assert rollout_gate.current_scheduled_admission_decision() == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["time", "weather", "broadcast"])
async def test_revocation_during_tick_holds_new_admission(
    mock_db, monkeypatch, boundary
):
    _seed_aps(10)
    db.set_settings(_settings())
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)
    if boundary == "time":
        async def revoke_time(*args):
            db.set_setting("schedule_enabled", "false")
            return True, harness.now
        monkeypatch.setattr("updater.scheduler.services.validate_time_sources", revoke_time)
    elif boundary == "weather":
        db.set_setting("weather_check_enabled", "true")
        async def revoke_weather(*args):
            db.set_setting("schedule_enabled", "false")
            return True, {}
        monkeypatch.setattr("updater.scheduler.services.check_weather_ok", revoke_weather)
    else:
        async def revoke_broadcast(*args):
            db.set_setting("schedule_enabled", "false")
        monkeypatch.setattr(scheduler, "_broadcast_status", revoke_broadcast)

    await harness.tick()
    start.assert_not_awaited()
    assert scheduler._state == "disabled"
    if boundary in {"time", "weather"}:
        assert db.get_current_rollout() is None


def test_unreadable_current_auto_update_holds_admission():
    with patch.object(db, "get_setting", side_effect=RuntimeError("Read failed")):
        assert rollout_gate.current_scheduled_admission_decision() == (
            False, rollout_gate.SCHEDULE_DISABLED_REASON,
        )


@pytest.mark.asyncio
async def test_revocation_during_job_keeps_current_wave(mock_db, monkeypatch):
    _seed_aps(10)
    db.set_settings(_settings())
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)
    await harness.tick()
    db.set_setting("schedule_enabled", "false")
    harness.complete_job()
    assert db.get_active_rollout() is None
    assert scheduler._state == "waiting"
    await harness.tick(day=3)
    assert start.call_count == 1
    assert scheduler._state == "disabled"
