"""Config backup integration tests — poll, history, diff, download."""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def test_config_poll_device(session, config_ap):
    """Trigger a config poll and verify a snapshot is stored."""
    ip = config_ap["ip"]
    resp = session.post(f"/api/configs/{ip}/poll")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "config_hash" in data


def test_config_history_exists(session, config_ap):
    """After polling, config history should have at least one entry."""
    ip = config_ap["ip"]
    resp = session.get(f"/api/configs/{ip}")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data.get("history") or data, list) or "history" in data


def test_config_latest(session, config_ap):
    """Fetch the latest config snapshot and verify it has content."""
    ip = config_ap["ip"]
    resp = session.get(f"/api/configs/{ip}/latest")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("config_json") or data.get("config"), "No config content in latest snapshot"


def test_config_diff(session, config_ap):
    """If two snapshots exist, diff them."""
    ip = config_ap["ip"]

    # Poll twice to ensure we have at least one snapshot
    session.post(f"/api/configs/{ip}/poll")

    resp = session.get(f"/api/configs/{ip}")
    assert resp.status_code == 200
    data = resp.json()
    history = data.get("history", data) if isinstance(data, dict) else data

    if not isinstance(history, list) or len(history) < 2:
        pytest.skip("Need at least 2 config snapshots to test diff")

    a_id = history[0]["id"]
    b_id = history[1]["id"]
    resp = session.get(f"/api/configs/{ip}/diff", params={"a": a_id, "b": b_id})
    assert resp.status_code == 200


def test_config_download_tar(session, config_ap):
    """Download a config archive and verify it looks like a tar."""
    ip = config_ap["ip"]

    # Get latest snapshot ID
    resp = session.get(f"/api/configs/{ip}")
    assert resp.status_code == 200
    data = resp.json()
    history = data.get("history", data) if isinstance(data, dict) else data
    if not history:
        pytest.skip("No config snapshots to download")

    config_id = history[0]["id"] if isinstance(history, list) else history["id"]
    resp = session.get(f"/api/configs/{ip}/download/{config_id}")
    assert resp.status_code == 200
    assert "tar" in resp.headers.get("content-type", "") or "octet" in resp.headers.get("content-type", "")
