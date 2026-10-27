"""Verify config containment with the production safety capability."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from updater import app as app_module, config_write_guard, database as db
from updater.poller import NetworkPoller
from tests.test_config_push import push_db, _create_template


@pytest.fixture
def device_client(monkeypatch):
    monkeypatch.setattr(app_module, "_config_push_jobs", {})
    monkeypatch.setattr(app_module, "_config_pushing_ips", set())
    client = MagicMock()
    client.login = AsyncMock(return_value=True)
    client.connect = AsyncMock(return_value=True)
    client.get_config = AsyncMock(return_value={"services": {"snmp": {"community": "old"}}})
    client.apply_config = AsyncMock(return_value={"success": True})
    client.get_hardware_id.return_value = "tn-110"
    monkeypatch.setattr(app_module, "TachyonClient", lambda *a, **kw: client)
    monkeypatch.setattr("updater.poller.get_driver", lambda vendor: lambda *a, **kw: client)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    return client


def saved_rollout(status):
    rid = db.create_config_push_rollout("[]", "Saved policy", "[]")
    db.assign_config_push_device(rid, "10.0.0.1", "ap", "canary")
    db.update_config_push_rollout_status(rid, status)
    return rid


def targets():
    return [{"ip": "10.0.0.1", "role": "ap", "username": "admin", "password": "pass", "model": None}]


def templates():
    return [{"id": 1, "name": "SNMP", "fragment": {"services": {"snmp": {"community": "new"}}}}]


@pytest.mark.parametrize("status", ["active", "paused"])
def test_saved_rollout_routes_refuse_and_keep_status(operator_client, push_db, device_client, status):
    rid = saved_rollout(status)
    for suffix in ["start", f"{rid}/advance", f"{rid}/resume"]:
        response = operator_client.post(f"/api/config-push/rollout/{suffix}", json={})
        assert response.status_code == 409
        assert response.json()["detail"] == config_write_guard.CONFIG_WRITES_PAUSED_REASON
        assert db.get_config_push_rollout(rid)["status"] == status
    response = operator_client.get("/api/config-push/rollout")
    assert response.status_code == 200
    assert response.json()["rollout"]["id"] == rid
    assert operator_client.post(f"/api/config-push/rollout/{rid}/cancel").status_code == 200
    assert db.get_config_push_rollout(rid)["status"] == "cancelled"
    device_client.apply_config.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "paused"])
async def test_saved_rollout_runner_makes_zero_applies(push_db, device_client, status):
    rid = saved_rollout(status)
    await app_module._run_config_push_phase(rid, templates(), targets())
    device_client.apply_config.assert_not_called()
    assert db.get_config_push_rollout_devices(rid)[0]["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("catchup", [False, True])
async def test_scheduled_and_catchup_keep_polling_without_enforcement(push_db, device_client, monkeypatch, catchup):
    db.set_setting("config_auto_enforce", "true")
    db.set_setting("config_poll_enabled", "true")
    db.set_setting("timezone", "auto")
    db.set_setting("config_enforce_hour", str(datetime.now().hour))
    poller = NetworkPoller()
    poller._last_config_poll_hydrated = True
    poller._last_config_poll = datetime.now() - timedelta(hours=30) if catchup else None
    poller.poll_all_configs = AsyncMock()
    enforce = AsyncMock(wraps=poller._auto_enforce_compliance)
    monkeypatch.setattr(poller, "_auto_enforce_compliance", enforce)
    await poller._maybe_poll_configs()
    poller.poll_all_configs.assert_awaited_once()
    enforce.assert_not_called()
    device_client.apply_config.assert_not_called()
    assert db.get_setting("config_auto_enforce") == "true"


@pytest.mark.asyncio
async def test_direct_enforcement_and_rollback_make_zero_applies(push_db, device_client):
    db.set_setting("config_auto_enforce", "true")
    poller = NetworkPoller()
    await poller._auto_enforce_compliance()
    await poller._run_enforce_phases()
    result = await poller._enforce_device("10.0.0.1", "ap", [], "canary", asyncio.Semaphore(1))
    assert result[0] is False
    assert await poller._auto_rollback_device("10.0.0.1", "ap", 1) is False
    device_client.apply_config.assert_not_called()


def test_enforcement_status_explains_safety_pause(operator_client, push_db):
    db.set_setting("config_auto_enforce", "true")
    response = operator_client.get("/api/config-enforce/status")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["paused_for_safety"] is True
    assert response.json()["pause_reason"] == config_write_guard.CONFIG_WRITES_PAUSED_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_manual_push_applies_to_selected_devices(push_db, device_client, count):
    devices = targets()
    if count == 2:
        devices.append({**devices[0], "ip": "10.0.1.1", "role": "switch"})
    job = {"cancelled": False, "success": 0, "failed": 0, "skipped": 0, "total": count, "done": False}
    await app_module._run_config_push("manual-test", devices, templates(), job)
    assert job["success"] == count
    assert device_client.apply_config.await_count == count * 2
    assert sum(not call.kwargs.get("dry_run", False) for call in device_client.apply_config.await_args_list) == count


def test_manual_push_route_still_accepts(operator_client, push_db, device_client):
    tid = _create_template(push_db, "Manual SNMP", {"services": {"snmp": {"community": "new"}}})
    response = operator_client.post("/api/config-push", json={"template_ids": [tid], "targets": [{"type": "ap", "ip": "10.0.0.1"}]})
    assert response.status_code == 200, response.text


def test_manual_restore_still_applies(operator_client, push_db, device_client, monkeypatch):
    monkeypatch.setattr(app_module, "get_poller", lambda: None)
    snapshot_id = db.get_latest_device_config("10.0.0.1")["id"]
    response = operator_client.post("/api/config-push/rollback/10.0.0.1", json={"config_id": snapshot_id})
    assert response.status_code == 200, response.text
    device_client.apply_config.assert_any_await({"services": {"snmp": {"community": "old"}}})


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["phase", "enforce", "rollback"])
async def test_pause_after_dry_run_stops_next_apply(push_db, device_client, monkeypatch, path):
    allowed = True
    monkeypatch.setattr(config_write_guard, "automatic_config_writes_allowed", lambda: allowed)

    async def dry_run_then_pause(*args, **kwargs):
        nonlocal allowed
        assert kwargs.get("dry_run") is True
        allowed = False
        return {"success": True}

    device_client.apply_config.side_effect = dry_run_then_pause
    poller = NetworkPoller()
    if path == "phase":
        rid = saved_rollout("active")
        await app_module._run_config_push_phase(rid, templates(), targets())
        assert db.get_config_push_rollout_devices(rid)[0]["status"] == "pending"
    elif path == "enforce":
        await poller._enforce_device("10.0.0.1", "ap", [{"id": 1, "config_fragment": {}}], "canary", asyncio.Semaphore(1))
    else:
        await poller._auto_rollback_device("10.0.0.1", "ap", db.get_latest_device_config("10.0.0.1")["id"])
    device_client.apply_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_lets_submitted_apply_finish_and_records_result(push_db, device_client, monkeypatch):
    monkeypatch.setattr(config_write_guard, "automatic_config_writes_allowed", lambda: True)
    rid = saved_rollout("active")
    applying = asyncio.Event()
    finish = asyncio.Event()

    async def apply(*args, **kwargs):
        if not kwargs.get("dry_run"):
            applying.set()
            await finish.wait()
        return {"success": True}

    device_client.apply_config.side_effect = apply
    task = asyncio.create_task(app_module._run_config_push_phase(rid, templates(), targets()))
    monkeypatch.setattr(app_module, "_config_push_rollout_task", task)
    await asyncio.wait_for(applying.wait(), timeout=2)
    monkeypatch.setattr(config_write_guard, "automatic_config_writes_allowed", lambda: False)
    await app_module.cancel_config_push_rollout(rid, session={})
    assert not task.cancelled()
    finish.set()
    await asyncio.wait_for(task, timeout=2)
    assert db.get_config_push_rollout_devices(rid)[0]["status"] == "updated"
    assert db.get_config_push_rollout(rid)["status"] == "cancelled"
    assert device_client.apply_config.await_count == 2


@pytest.mark.asyncio
async def test_queued_enforcement_checks_guard_after_capacity(push_db, device_client, monkeypatch):
    allowed = True
    monkeypatch.setattr(config_write_guard, "automatic_config_writes_allowed", lambda: allowed)
    sem = asyncio.Semaphore(0)
    task = asyncio.create_task(NetworkPoller()._enforce_device("10.0.0.1", "ap", [], "canary", sem))
    await asyncio.sleep(0)
    allowed = False
    sem.release()
    assert (await asyncio.wait_for(task, timeout=2))[0] is False
    device_client.apply_config.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["enforce", "rollback"])
async def test_submitted_automatic_apply_records_success_after_pause(push_db, device_client, monkeypatch, path):
    allowed = True
    monkeypatch.setattr(config_write_guard, "automatic_config_writes_allowed", lambda: allowed)

    async def apply_then_pause(*args, **kwargs):
        nonlocal allowed
        if not kwargs.get("dry_run"):
            allowed = False
        return {"success": True}

    device_client.apply_config.side_effect = apply_then_pause
    poller = NetworkPoller()
    if path == "enforce":
        result = await poller._enforce_device("10.0.0.1", "ap", [{"id": 1, "config_fragment": {}}], "canary", asyncio.Semaphore(1))
        assert result[0] is True
    else:
        assert await poller._auto_rollback_device("10.0.0.1", "ap", db.get_latest_device_config("10.0.0.1")["id"]) is True
    assert db.get_config_enforce_log(ip="10.0.0.1")[0]["status"] == "success"
    assert device_client.apply_config.await_count == 2


def test_ui_shows_safety_pause_and_retains_stop(operator_client, push_db):
    response = operator_client.get("/")
    assert response.status_code == 200
    assert "Phased config rollout is paused for safety" in response.text
    assert "Auto-enforce paused for safety" in response.text
    assert 'id="configAutoEnforceToggle" type="button" disabled' in response.text
    assert 'onclick="cancelConfigPushRollout()' in response.text
