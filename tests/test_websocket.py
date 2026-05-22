"""Tests for the dashboard WebSocket endpoint."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from starlette.websockets import WebSocketDisconnect


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

    update_jobs.clear()
    update_jobs["job-1"] = UpdateJob(
        job_id="job-1",
        firmware_names={"default": "tachyon.bin"},
        devices={
            "192.0.2.10": DeviceStatus(
                ip="192.0.2.10",
                status="running",
                progress_message="Uploading firmware",
                old_version="1.0.0",
                new_version="1.1.0",
                bank1_version="1.0.0",
                bank2_version="1.1.0",
                active_bank=1,
                role="ap",
                model="TNA-303L",
            )
        },
        ap_cpe_map={"192.0.2.10": []},
        device_roles={"192.0.2.10": "ap"},
        status="running",
    )

    try:
        with authed_client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "topology_update"
            started = ws.receive_json()
            device = ws.receive_json()
            license_state = ws.receive_json()
    finally:
        update_jobs.clear()

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
        (
            "job-history-1",
            datetime(2026, 5, 1, 1, 0, 0).isoformat(),
            datetime(2026, 5, 1, 1, 5, 0).isoformat(),
            300.4,
            "both",
            1,
            0,
            0,
            0,
            '{"192.0.2.10": {"status": "completed"}}',
            '{"192.0.2.10": []}',
            '{"192.0.2.10": "ap"}',
            "America/Chicago",
        ),
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


def test_websocket_sends_scheduler_and_rollout_status(authed_client):
    """Scheduler state should be replayed so reconnects do not show stale rollout UI."""
    from updater import app as app_mod

    scheduler = MagicMock()
    scheduler.get_status.return_value = {
        "enabled": True,
        "running": False,
        "rollout": {"status": "paused", "phase": "canary"},
    }

    original = app_mod.get_scheduler
    app_mod.get_scheduler = MagicMock(return_value=scheduler)
    try:
        with authed_client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "topology_update"
            assert ws.receive_json()["type"] == "license_state"
            scheduler_status = ws.receive_json()
            rollout_status = ws.receive_json()
    finally:
        app_mod.get_scheduler = original

    assert scheduler_status["type"] == "scheduler_status"
    assert scheduler_status["enabled"] is True
    assert scheduler_status["rollout"]["status"] == "paused"
    assert rollout_status == {
        "type": "rollout_status",
        "rollout": {"status": "paused", "phase": "canary"},
    }


def test_websocket_removes_connection_on_disconnect(authed_client):
    """Disconnected clients should not remain in the broadcast set."""
    from updater.app import active_websockets

    active_websockets.clear()
    with authed_client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert len(active_websockets) == 1

    assert len(active_websockets) == 0


@pytest.mark.asyncio
async def test_broadcast_sends_same_json_to_concurrent_clients():
    """Broadcast should deliver the same payload to every connected client."""
    import json
    from unittest.mock import AsyncMock

    from updater.app import active_websockets, broadcast

    first = AsyncMock()
    second = AsyncMock()
    active_websockets.clear()
    active_websockets.add(first)
    active_websockets.add(second)

    try:
        await broadcast({"type": "device_update", "ip": "192.0.2.10", "status": "done"})
    finally:
        active_websockets.clear()

    first.send_text.assert_called_once()
    second.send_text.assert_called_once()
    assert json.loads(first.send_text.call_args.args[0]) == {
        "type": "device_update",
        "ip": "192.0.2.10",
        "status": "done",
    }
    assert second.send_text.call_args.args[0] == first.send_text.call_args.args[0]
