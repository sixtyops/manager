"""Keep scheduled admission closed until the operator records readiness."""

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
