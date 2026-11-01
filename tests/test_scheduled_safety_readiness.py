"""Keep scheduled admission closed until the operator records readiness."""

import sqlite3
from unittest.mock import patch

import pytest

from updater import database as db
from updater import rollout_gate
from tests.test_rollout_invariants import _Harness, _make_scheduler, _seed_aps, _settings, FW


@pytest.mark.asyncio
@pytest.mark.parametrize("readiness", [None, "false", "", "TRUE", "1", "invalid"])
@pytest.mark.parametrize("phase", [None, "pct10", "pct50", "pct100", "paused"])
async def test_unready_tick_preserves_rollout_and_admits_no_job(
    mock_db, monkeypatch, readiness, phase
):
    _seed_aps(10)
    settings = _settings()
    settings.pop("scheduled_rollout_safety_ready")
    db.set_settings(settings)
    if readiness is not None:
        db.set_setting("scheduled_rollout_safety_ready", readiness)
    if phase:
        rollout_id = db.create_rollout(FW)
        if phase == "paused":
            db.pause_rollout(rollout_id, "Review recovery proof")
        else:
            mock_db.execute("UPDATE rollouts SET phase = ? WHERE id = ?", (phase, rollout_id))
            mock_db.commit()
    before = db.get_current_rollout()
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)

    with patch.object(db, "create_rollout", wraps=db.create_rollout) as create, \
         patch.object(db, "complete_rollout_phase", wraps=db.complete_rollout_phase) as advance:
        await harness.tick()
        await harness.tick(day=3)
    create.assert_not_called()
    advance.assert_not_called()
    start.assert_not_awaited()
    assert db.get_current_rollout() == before
    assert scheduler._state == "blocked_safety"
    assert scheduler.get_status()["safety_block_reason"] == rollout_gate.SCHEDULED_ROLLOUT_SAFETY_REASON
    assert scheduler.compute_next_attempt("192.0.2.1", "ap")["next_attempt_iso"] is None


@pytest.mark.asyncio
async def test_recorded_readiness_can_be_revoked_between_waves(mock_db, monkeypatch):
    _seed_aps(10)
    db.set_settings(_settings())
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)
    await harness.tick()
    harness.complete_job()
    before = db.get_active_rollout()
    assert before["phase"] == "pct50"
    db.set_setting("scheduled_rollout_safety_ready", "false")

    await harness.tick(day=3)

    assert start.call_count == 1
    assert db.get_active_rollout() == before
    assert scheduler._state == "blocked_safety"


def test_readiness_defaults_closed_on_install_and_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "manager.db")
    db.init_db()
    assert db.get_setting("scheduled_rollout_safety_ready") == "false"
    with db.get_db() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'scheduled_rollout_safety_ready'")
    db.set_setting("schedule_enabled", "true")
    db.init_db()
    assert db.get_setting("scheduled_rollout_safety_ready") == "false"
    assert db.get_setting("schedule_enabled") == "true"
    db.set_setting("scheduled_rollout_safety_ready", "true")
    db.init_db()
    assert db.get_setting("scheduled_rollout_safety_ready") == "true"


def test_admin_records_readiness_and_schedule_toggle_preserves_it(authed_client, mock_db):
    response = authed_client.post("/api/settings/save", json={"scheduled_rollout_safety_ready": "true"})
    assert response.status_code == 200
    response = authed_client.post("/api/settings/save", json={"schedule_enabled": "true"})
    assert response.status_code == 200
    assert db.get_setting("scheduled_rollout_safety_ready") == "true"
    assert rollout_gate.scheduled_admission_decision(db.get_all_settings()) == (True, None)
    response = authed_client.post("/api/settings/save", json={"scheduled_rollout_safety_ready": "invalid"})
    assert response.status_code == 400
    assert db.get_setting("scheduled_rollout_safety_ready") == "true"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary,empty_wave", [
    ("time", False), ("time", True), ("weather", False), ("weather", True),
    ("broadcast", False),
])
async def test_revocation_during_tick_holds_new_admission(
    mock_db, monkeypatch, boundary, empty_wave
):
    _seed_aps(10)
    db.set_settings(_settings())
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)
    if empty_wave:
        rollout_id = db.create_rollout(FW)
        monkeypatch.setattr(scheduler, "_get_devices_for_phase", lambda *a: [])
        monkeypatch.setattr(scheduler, "_get_switches_for_phase", lambda *a: [])
    if boundary == "time":
        async def revoke_time(*args):
            db.set_setting("scheduled_rollout_safety_ready", "false")
            return True, harness.now
        monkeypatch.setattr("updater.scheduler.services.validate_time_sources", revoke_time)
    elif boundary == "weather":
        db.set_setting("weather_check_enabled", "true")
        async def revoke_weather(*args):
            db.set_setting("scheduled_rollout_safety_ready", "false")
            return True, {}
        monkeypatch.setattr("updater.scheduler.services.check_weather_ok", revoke_weather)
    else:
        async def revoke_broadcast(*args):
            db.set_setting("scheduled_rollout_safety_ready", "false")
        monkeypatch.setattr(scheduler, "_broadcast_status", revoke_broadcast)

    with patch.object(db, "complete_rollout_phase", wraps=db.complete_rollout_phase) as advance:
        await harness.tick()
    start.assert_not_awaited()
    advance.assert_not_called()
    assert scheduler._state == "blocked_safety"
    if boundary in {"time", "weather"} and not empty_wave:
        assert db.get_current_rollout() is None
    if empty_wave:
        assert db.get_rollout(rollout_id)["phase"] == "pct10"


def test_unreadable_current_readiness_holds_admission():
    with patch.object(db, "get_setting", side_effect=RuntimeError("Read failed")):
        assert rollout_gate.current_scheduled_admission_decision() == (
            False, rollout_gate.SCHEDULED_ROLLOUT_SAFETY_REASON,
        )


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("newer", "Rollout format 3 exceeds this image's supported format 2"),
        (
            "unmarked_b2",
            "Rollout compatibility marker is missing while saved rollout data is present",
        ),
        ("unreadable", "Rollout compatibility could not be read"),
    ],
)
def test_incompatible_database_closes_current_admission(
    tmp_path, monkeypatch, state, reason
):
    path = tmp_path / "manager.db"
    if state != "unreadable":
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("CREATE TABLE rollouts (status TEXT)")
        if state == "newer":
            connection.execute(
                "INSERT INTO settings VALUES ('rollout_format_version', '3')"
            )
        else:
            connection.execute("CREATE TABLE rollout_devices (rollout_id INTEGER, ip TEXT)")
            connection.execute("INSERT INTO rollouts VALUES ('active')")
            connection.execute("INSERT INTO rollout_devices VALUES (1, '192.0.2.10')")
        connection.commit()
        connection.close()
    monkeypatch.setattr(db, "DB_PATH", path)

    with patch.object(db, "get_setting", return_value="true"):
        assert rollout_gate.current_scheduled_admission_decision() == (False, reason)


@pytest.mark.asyncio
async def test_revocation_during_job_keeps_current_wave(mock_db, monkeypatch):
    _seed_aps(10)
    db.set_settings(_settings())
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)
    await harness.tick()
    before = db.get_active_rollout()
    db.set_setting("scheduled_rollout_safety_ready", "false")
    harness.complete_job()
    assert db.get_active_rollout()["phase"] == before["phase"]
    assert scheduler._state == "blocked_safety"
    await harness.tick(day=3)
    assert start.call_count == 1
