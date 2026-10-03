"""Dashboard WebSocket tests, including coverage reused from parked PR 153."""

import asyncio
import json
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect


@pytest.fixture(autouse=True)
def isolated_websocket_state(mock_db, monkeypatch):
    """Give every test fresh connection/job sets and no real scheduler."""
    from updater import app as app_mod
    monkeypatch.setattr(app_mod, "active_websockets", set())
    monkeypatch.setattr(app_mod, "update_jobs", {})
    monkeypatch.setattr(app_mod, "get_scheduler", lambda: None)


def test_websocket_rejects_unauthenticated_client(client):
    """Unauthenticated clients must not receive live dashboard state."""
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws"):
            pass

    assert exc.value.code == 4001


def test_websocket_sends_initial_dashboard_state(authed_client):
    """A fresh connection receives topology and license state immediately."""
    with authed_client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        second = ws.receive_json()

    assert first["type"] == "topology_update"
    assert first["topology"]["sites"] == []
    assert second["type"] == "license_state"
    assert second["is_pro"] is True


def test_websocket_reconnect_replays_current_state(authed_client):
    """Reconnects should get the same current snapshot as a fresh client."""
    with authed_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "topology_update"

    with authed_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "topology_update"
        assert ws.receive_json()["type"] == "license_state"


def test_websocket_replays_running_job_before_license_state(authed_client):
    """Operators joining mid-update should see the active job before idle state."""
    from updater.app import DeviceStatus, UpdateJob, update_jobs

    update_jobs["job-1"] = UpdateJob(
        job_id="job-1", firmware_names={"default": "tachyon.bin"},
        devices={"192.0.2.10": DeviceStatus(
            ip="192.0.2.10", status="running", progress_message="Uploading firmware",
            old_version="1.0.0", new_version="1.1.0", bank1_version="1.0.0",
            bank2_version="1.1.0", active_bank=1, role="ap", model="TNA-303L")},
        ap_cpe_map={"192.0.2.10": []}, device_roles={"192.0.2.10": "ap"}, status="running")
    with authed_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "topology_update"
        started = ws.receive_json()
        device = ws.receive_json()
        license_state = ws.receive_json()

    assert started["type"] == "job_started"
    assert started["job_id"] == "job-1"
    assert started["device_count"] == 1
    assert device["type"] == "device_update"
    assert device["job_id"] == "job-1"
    assert device["ip"] == "192.0.2.10"
    assert device["message"] == "Uploading firmware"
    assert license_state["type"] == "license_state"


def test_websocket_replays_completed_job_history(authed_client, mock_db):
    """Completed update history should be sent to clients on connect."""
    mock_db.execute(
        """
        INSERT INTO job_history (
            job_id, started_at, completed_at, duration, bank_mode,
            success_count, failed_count, skipped_count, cancelled_count,
            devices_json, ap_cpe_map_json, device_roles_json, timezone
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("job-history-1", datetime(2026, 5, 1, 1, 0).isoformat(),
         datetime(2026, 5, 1, 1, 5).isoformat(), 300.4, "both", 1, 0, 0, 0,
         '{"192.0.2.10": {"status": "completed"}}', '{"192.0.2.10": []}',
         '{"192.0.2.10": "ap"}', "America/Chicago"),
    )
    mock_db.commit()

    with authed_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "topology_update"
        history = ws.receive_json()
        license_state = ws.receive_json()

    assert history["type"] == "job_history"
    assert history["job_id"] == "job-history-1"
    assert history["duration"] == 300
    assert history["success_count"] == 1
    assert history["timezone"] == "America/Chicago"
    assert license_state["type"] == "license_state"


def test_websocket_sends_scheduler_and_rollout_status(authed_client, monkeypatch):
    """Scheduler state should be replayed so reconnects do not show stale rollout UI."""
    from updater import app as app_mod

    scheduler = MagicMock()
    scheduler.get_status.return_value = {
        "enabled": True,
        "running": False,
        "rollout": {"status": "paused", "phase": "10%"},
    }

    monkeypatch.setattr(app_mod, "get_scheduler", lambda: scheduler)
    with authed_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "topology_update"
        assert ws.receive_json()["type"] == "license_state"
        scheduler_status = ws.receive_json()
        rollout_status = ws.receive_json()

    assert scheduler_status["type"] == "scheduler_status"
    assert scheduler_status["enabled"] is True
    assert scheduler_status["rollout"]["status"] == "paused"
    assert rollout_status == {
        "type": "rollout_status",
        "rollout": {"status": "paused", "phase": "10%"},
    }


def test_websocket_removes_connection_on_disconnect(authed_client):
    """Disconnected clients should not remain in the broadcast set."""
    from updater.app import active_websockets

    with authed_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert len(active_websockets) == 1

    assert len(active_websockets) == 0


@pytest.mark.asyncio
async def test_broadcast_preserves_payload_and_sequential_order_for_clients():
    """Extend PR153's fanout control to cover sequential message order."""
    from updater.app import active_websockets, broadcast
    clients = [AsyncMock(), AsyncMock()]
    active_websockets.update(clients)
    messages = [{"type": "device_update", "ip": "192.0.2.10", "status": "running"},
                {"type": "device_update", "ip": "192.0.2.10", "status": "done"}]
    for message in messages:
        await broadcast(message)
    for client in clients:
        assert [json.loads(call.args[0]) for call in client.send_text.await_args_list] == messages


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["connect", "disconnect", "reconnect"])
async def test_membership_change_during_suspended_broadcast(change):
    """A connection change cannot abort the current connection snapshot."""
    from updater import app as app_mod
    entered, release = asyncio.Event(), asyncio.Event()
    received = []
    async def suspended_send(text):
        entered.set()
        await release.wait()
        received.append(json.loads(text))
    original = AsyncMock()
    original.send_text.side_effect = suspended_send
    replacement = AsyncMock()
    app_mod.active_websockets.add(original)
    first_message = {"type": "device_update", "status": "running"}
    task = asyncio.create_task(app_mod.broadcast(first_message))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if change in ("disconnect", "reconnect"):
            app_mod.active_websockets.discard(original)
        if change in ("connect", "reconnect"):
            app_mod.active_websockets.add(replacement)
        # A joining client cannot receive a partial in-flight broadcast.
        replacement.send_text.assert_not_called()
        release.set()
        await asyncio.wait_for(task, timeout=1)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert received == [first_message]
    replacement.send_text.assert_not_called()
    second_message = {"type": "device_update", "status": "completed"}
    await app_mod.broadcast(second_message)
    if change in ("connect", "reconnect"):
        assert json.loads(replacement.send_text.call_args.args[0]) == second_message
    else:
        assert not app_mod.active_websockets
    assert original.send_text.call_count == (2 if change == "connect" else 1)


@pytest.mark.asyncio
async def test_remaining_client_receives_message_when_peer_disconnects():
    """Removing the suspended peer must not skip the other snapshot member."""
    from updater import app as app_mod
    entered, release = asyncio.Event(), asyncio.Event()
    inflight = []
    clients = [AsyncMock(), AsyncMock()]
    for client in clients:
        async def send(text, client=client):
            if not inflight:
                inflight.append(client)
                entered.set()
                await release.wait()
        client.send_text.side_effect = send
    app_mod.active_websockets.update(clients)
    task = asyncio.create_task(app_mod.broadcast({"type": "job_completed", "success": True}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        removed = inflight[0]
        survivor = next(client for client in clients if client is not removed)
        # Real backpressure keeps this sequential broadcast suspended.
        survivor.send_text.assert_not_called()
        app_mod.active_websockets.discard(removed)
        release.set()
        await asyncio.wait_for(task, timeout=1)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    survivor.send_text.assert_awaited_once()
    assert survivor in app_mod.active_websockets and removed not in app_mod.active_websockets

