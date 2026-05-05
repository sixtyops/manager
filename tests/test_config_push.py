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


class TestRollbackSafetySnapshot:
    """Pre-rollback safety snapshot must be mandatory (issue #42).
    If get_config fails or returns empty, the rollback must refuse with 409
    unless the operator explicitly passes force=true."""

    @pytest.fixture
    def rollback_db(self, mock_db):
        """DB with an AP that has two snapshots so rollback has a target."""
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) "
            "VALUES ('10.0.0.5', 'admin', 'pass', 1)"
        )
        mock_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.5", json.dumps({"v": "old"}), "h-old", "2026-01-01T00:00:00"),
        )
        mock_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("10.0.0.5", json.dumps({"v": "new"}), "h-new", "2026-01-02T00:00:00"),
        )
        mock_db.commit()
        return mock_db

    def _patch_client(self, get_config_result=None, get_config_raises=None):
        from contextlib import ExitStack
        from unittest.mock import AsyncMock, MagicMock, patch
        instance = MagicMock()
        instance.login = AsyncMock(return_value=True)
        if get_config_raises is not None:
            instance.get_config = AsyncMock(side_effect=get_config_raises)
        else:
            instance.get_config = AsyncMock(return_value=get_config_result)
        instance.apply_config = AsyncMock(return_value={"success": True})
        instance.get_hardware_id = MagicMock(return_value="tn-110")
        # Stub poller so the post-rollback re-poll doesn't blow up on the
        # default MagicMock from conftest (which isn't awaitable).
        poller_stub = MagicMock()
        poller_stub.poll_configs_for_ips = AsyncMock(return_value=None)
        stack = ExitStack()
        stack.enter_context(patch("updater.app.TachyonClient", return_value=instance))
        stack.enter_context(patch("updater.app.get_poller", return_value=poller_stub))
        return stack, instance

    def test_rollback_refuses_when_get_config_raises(self, operator_client, rollback_db):
        ctx, _ = self._patch_client(get_config_raises=ConnectionError("device unreachable"))
        with ctx:
            resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
        assert resp.status_code == 409, resp.text
        assert "safety snapshot" in resp.json()["detail"].lower()
        assert "force=true" in resp.json()["detail"]

    def test_rollback_refuses_when_get_config_returns_empty(self, operator_client, rollback_db):
        ctx, _ = self._patch_client(get_config_result=None)
        with ctx:
            resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
        assert resp.status_code == 409, resp.text
        assert "empty config" in resp.json()["detail"].lower()

    def test_rollback_refuses_when_force_is_false(self, operator_client, rollback_db):
        ctx, _ = self._patch_client(get_config_raises=RuntimeError("boom"))
        with ctx:
            resp = operator_client.post(
                "/api/config-push/rollback/10.0.0.5", json={"force": False}
            )
        assert resp.status_code == 409, resp.text

    def test_rollback_proceeds_with_force_when_snapshot_fails(
        self, operator_client, rollback_db
    ):
        ctx, instance = self._patch_client(get_config_raises=RuntimeError("boom"))
        with ctx:
            resp = operator_client.post(
                "/api/config-push/rollback/10.0.0.5", json={"force": True}
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success"
        assert body["safety_snapshot_saved"] is False
        # Apply was still attempted (dry-run + apply)
        assert instance.apply_config.await_count == 2
        # Audit trail records the forced override
        row = rollback_db.execute(
            "SELECT action, target_id, details FROM audit_log "
            "WHERE action = 'config.rollback.force'"
        ).fetchone()
        assert row is not None
        assert row["target_id"] == "10.0.0.5"
        assert "boom" in row["details"]

    def test_rollback_succeeds_and_saves_snapshot_on_happy_path(
        self, operator_client, rollback_db
    ):
        ctx, instance = self._patch_client(get_config_result={"v": "current"})
        with ctx:
            resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["safety_snapshot_saved"] is True
        # The pre-rollback snapshot landed in device_configs
        snapshots = rollback_db.execute(
            "SELECT config_json FROM device_configs WHERE ip = '10.0.0.5' "
            "ORDER BY id DESC"
        ).fetchall()
        # Latest row is the safety snapshot we just took
        assert json.loads(snapshots[0]["config_json"]) == {"v": "current"}
