"""Tests for the manual config-push paths: device_types filter on preview/apply."""

import json

import pytest

from updater.config_utils import filter_templates_by_device_type


@pytest.fixture
def push_db(mock_db):
    """In-memory DB seeded with a tower-site, an AP, and a switch, plus stored
    configs so the preview endpoint has something to merge against."""
    mock_db.execute("INSERT INTO tower_sites (id, name) VALUES (1, 'Tower-A')")
    mock_db.execute(
        "INSERT INTO access_points (ip, tower_site_id, username, password, enabled) "
        "VALUES ('10.0.0.1', 1, 'admin', 'pass', 1)"
    )
    mock_db.execute(
        "INSERT INTO switches (ip, tower_site_id, username, password, enabled) "
        "VALUES ('10.0.1.1', 1, 'admin', 'pass', 1)"
    )
    mock_db.execute(
        "INSERT INTO device_configs (ip, config_json, config_hash) VALUES (?, ?, ?)",
        ("10.0.0.1", json.dumps({"services": {"snmp": {"community": "old"}}}), "h1"),
    )
    mock_db.execute(
        "INSERT INTO device_configs (ip, config_json, config_hash) VALUES (?, ?, ?)",
        ("10.0.1.1", json.dumps({"services": {"snmp": {"community": "old"}}}), "h2"),
    )
    mock_db.commit()
    return mock_db


def _create_template(db_conn, name, fragment, device_types=None, category="custom"):
    db_conn.execute(
        "INSERT INTO config_templates (name, category, config_fragment, device_types, enabled) "
        "VALUES (?, ?, ?, ?, 1)",
        (name, category, json.dumps(fragment), device_types),
    )
    db_conn.commit()
    row = db_conn.execute("SELECT id FROM config_templates WHERE name = ?", (name,)).fetchone()
    return row["id"]


class TestPreviewDeviceTypeFilter:
    """`/api/config-push/preview` must report templates that don't apply to
    the target's device type so operators see the mismatch *before* clicking
    apply."""

    def test_ap_only_template_is_skipped_when_target_is_switch(self, operator_client, push_db):
        tid = _create_template(
            push_db, "AP-only-snmp",
            {"services": {"snmp": {"community": "new"}}},
            device_types='["ap"]',
        )
        resp = operator_client.post(
            "/api/config-push/preview",
            json={"ip": "10.0.1.1", "template_ids": [tid]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["device_type"] == "switch"
        assert len(data["skipped_templates"]) == 1
        skipped = data["skipped_templates"][0]
        assert skipped["template_name"] == "AP-only-snmp"
        assert skipped["reason"] == "device_type_mismatch"
        assert skipped["allowed_device_types"] == ["ap"]
        # Only template was excluded → no merge happened
        assert data["changed"] is False

    def test_switch_only_template_is_skipped_when_target_is_ap(self, operator_client, push_db):
        tid = _create_template(
            push_db, "Switch-only-vlan",
            {"vlan": {"id": 10}},
            device_types='["switch"]',
        )
        resp = operator_client.post(
            "/api/config-push/preview",
            json={"ip": "10.0.0.1", "template_ids": [tid]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["device_type"] == "ap"
        assert [s["template_name"] for s in data["skipped_templates"]] == ["Switch-only-vlan"]
        assert data["changed"] is False

    def test_no_device_types_template_applies_to_all(self, operator_client, push_db):
        tid = _create_template(
            push_db, "Global-snmp",
            {"services": {"snmp": {"community": "new"}}},
            device_types=None,
        )
        resp = operator_client.post(
            "/api/config-push/preview",
            json={"ip": "10.0.1.1", "template_ids": [tid]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["skipped_templates"] == []
        assert data["changed"] is True

    def test_partial_filter_keeps_applicable_templates(self, operator_client, push_db):
        """Some templates apply, some don't — preview merges the applicable
        ones and reports the rest as skipped."""
        global_id = _create_template(
            push_db, "Global", {"services": {"snmp": {"community": "new"}}},
        )
        switch_only_id = _create_template(
            push_db, "Switch-only", {"vlan": {"id": 10}}, device_types='["switch"]',
        )
        resp = operator_client.post(
            "/api/config-push/preview",
            json={"ip": "10.0.0.1", "template_ids": [global_id, switch_only_id]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["device_type"] == "ap"
        assert [s["template_name"] for s in data["skipped_templates"]] == ["Switch-only"]
        # Global SNMP merged
        assert data["changed"] is True

    def test_unknown_ip_falls_back_to_unfiltered_merge(self, operator_client, push_db):
        """If we can't identify the device's type (stray IP not in inventory),
        be permissive — same as previous behavior — and surface no skips."""
        # Stash a config for an IP that's not in any device table
        push_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash) VALUES (?, ?, ?)",
            ("10.99.99.99", json.dumps({"services": {}}), "h-unknown"),
        )
        push_db.commit()
        tid = _create_template(
            push_db, "AP-only-anywhere",
            {"services": {"snmp": {"community": "x"}}},
            device_types='["ap"]',
        )
        resp = operator_client.post(
            "/api/config-push/preview",
            json={"ip": "10.99.99.99", "template_ids": [tid]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["device_type"] is None
        assert data["skipped_templates"] == []
        # Merge still happened (permissive fallback)
        assert data["changed"] is True


class TestPushApplyDeviceTypeFilter:
    """`POST /api/config-push` must build a templates list that retains
    `device_types`, so the per-device filter inside `_run_config_push` can
    skip templates that don't apply to a given target."""

    def test_apply_returns_job_with_skipped_counter_field(self, operator_client, push_db):
        """Smoke check that the apply endpoint accepts the request and the
        job tracker exposes the new `skipped` field."""
        tid = _create_template(
            push_db, "AP-only-snmp",
            {"services": {"snmp": {"community": "new"}}},
            device_types='["ap"]',
        )
        resp = operator_client.post(
            "/api/config-push",
            json={
                "template_ids": [tid],
                "targets": [{"type": "ap", "ip": "10.0.0.1"}],
            },
        )
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]
        # The job tracker should expose a `skipped` field even before the
        # background task completes. (The actual push is mocked in
        # integration tests — here we're verifying the wire shape.)
        status = operator_client.get(f"/api/config-push/jobs/{job_id}").json()
        assert "skipped" in status


class TestRunConfigPushFilters:
    """Direct invocation of the background push function so we can verify
    `device_types` filtering without spinning up a real device. Uses a
    mocked TachyonClient so no network connections are attempted."""

    @pytest.mark.asyncio
    async def test_run_config_push_skips_devices_with_no_applicable_templates(
        self, push_db, monkeypatch
    ):
        from unittest.mock import AsyncMock, MagicMock, patch
        from updater.app import _run_config_push

        templates = [
            {
                "id": 1,
                "name": "Switch-only",
                "fragment": {"vlan": {"id": 10}},
                "device_types": '["switch"]',
            },
        ]
        devices = [
            {"ip": "10.0.0.1", "role": "ap", "username": "admin", "password": "pass", "model": None},
            {"ip": "10.0.1.1", "role": "switch", "username": "admin", "password": "pass", "model": None},
        ]
        job_info = {"cancelled": False, "success": 0, "failed": 0, "skipped": 0, "total": 2, "done": False}

        ips_connected = []

        def fake_client(ip, username, password):
            ips_connected.append(ip)
            instance = MagicMock()
            instance.login = AsyncMock(return_value=True)
            instance.get_config = AsyncMock(return_value={"services": {}})
            instance.apply_config = AsyncMock(return_value={"success": True})
            instance.get_hardware_id = MagicMock(return_value="tn-110")
            return instance

        with patch("updater.app.TachyonClient", side_effect=fake_client), \
             patch("updater.app.broadcast", new=AsyncMock()):
            await _run_config_push("test-job", devices, templates, job_info)

        # Switch was pushed, AP was filtered out before connecting
        assert ips_connected == ["10.0.1.1"]
        assert job_info["skipped"] == 1
        assert job_info["success"] == 1
        assert job_info["failed"] == 0
        assert job_info["done"] is True
