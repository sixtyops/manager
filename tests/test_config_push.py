"""Tests for the manual config-push paths: device_types filter on preview/apply."""

import json
from unittest.mock import AsyncMock, patch

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
        detail = resp.json()["detail"]
        assert detail["code"] == "safety_snapshot_failed"
        assert "force=true" in detail["message"]
        assert "device unreachable" in detail["snapshot_error"]

    def test_rollback_refuses_when_get_config_returns_empty(self, operator_client, rollback_db):
        ctx, _ = self._patch_client(get_config_result=None)
        with ctx:
            resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "safety_snapshot_failed"
        assert "empty config" in detail["snapshot_error"].lower()

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
        # First read (safety snapshot) fails; the post-apply read-back matches.
        instance.get_config.side_effect = [RuntimeError("boom"), {"v": "old"}]
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
        from updater import database as db
        ctx, instance = self._patch_client(get_config_result={"v": "current"})
        instance.get_config.side_effect = [{"v": "current"}, {"v": "old"}]
        with ctx:
            resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["safety_snapshot_saved"] is True
        assert body["rollback_verified"] is True
        # The pre-rollback snapshot landed in device_configs (DAL decrypts on read)
        latest = db.get_latest_device_config("10.0.0.5")
        assert json.loads(latest["config_json"]) == {"v": "current"}


class TestRollbackLockAndVerify:
    """Rollback takes the per-IP config push lock and verifies the result by
    reading the config back (issue #311, closes #37 and #39)."""

    @pytest.fixture
    def rollback_db(self, mock_db):
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

    def _patch_client(self, read_back):
        from contextlib import ExitStack
        from unittest.mock import MagicMock
        instance = MagicMock()
        instance.login = AsyncMock(return_value=True)
        # First read is the safety snapshot; second is the post-apply read-back.
        instance.get_config = AsyncMock(side_effect=[{"v": "new"}, read_back])
        instance.apply_config = AsyncMock(return_value={"success": True})
        instance.get_hardware_id = MagicMock(return_value="tn-110")
        poller_stub = MagicMock()
        poller_stub.poll_configs_for_ips = AsyncMock(return_value=None)
        stack = ExitStack()
        stack.enter_context(patch("updater.app.TachyonClient", return_value=instance))
        stack.enter_context(patch("updater.app.get_poller", return_value=poller_stub))
        return stack, instance

    def test_success_reports_verified_and_releases_lock(self, operator_client, rollback_db):
        from updater import app as app_module
        from updater.app import _compute_config_hash
        ctx, instance = self._patch_client(read_back={"v": "old"})
        with ctx:
            resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success"
        assert body["rollback_verified"] is True
        assert body["config_hash"] == _compute_config_hash({"v": "old"})
        assert "10.0.0.5" not in app_module._config_pushing_ips

    def test_hash_mismatch_is_reported_not_success(self, operator_client, rollback_db):
        from updater import app as app_module
        # Device accepted the apply but dropped a field.
        ctx, instance = self._patch_client(read_back={})
        instance.get_config.side_effect = [{"v": "new"}, {"v": "old", "extra": 1}]
        with ctx:
            resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "rollback_not_verified"
        assert detail["rollback_verified"] is False
        assert detail["expected_hash"] != detail["actual_hash"]
        assert "not verified" in detail["message"]
        row = rollback_db.execute(
            "SELECT target_id FROM audit_log WHERE action = 'config.rollback.unverified'"
        ).fetchone()
        assert row is not None and row["target_id"] == "10.0.0.5"
        assert "10.0.0.5" not in app_module._config_pushing_ips

    def test_read_back_failure_is_reported_not_success(self, operator_client, rollback_db):
        ctx, instance = self._patch_client(read_back=None)
        with ctx:
            resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "rollback_not_verified"
        assert detail["actual_hash"] is None
        assert "empty config" in detail["message"]

    def test_rollback_refused_while_push_or_rollback_holds_lock(self, operator_client, rollback_db):
        from updater import app as app_module
        ctx, instance = self._patch_client(read_back={"v": "old"})
        app_module._config_pushing_ips.add("10.0.0.5")
        try:
            with ctx:
                resp = operator_client.post("/api/config-push/rollback/10.0.0.5", json={})
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["code"] == "config_change_in_progress"
            # Fail closed: no device contact at all.
            instance.login.assert_not_awaited()
            instance.apply_config.assert_not_awaited()
            # The refused request must not release the other writer's lock.
            assert "10.0.0.5" in app_module._config_pushing_ips
        finally:
            app_module._config_pushing_ips.discard("10.0.0.5")

    @staticmethod
    async def _drain(task, gate):
        """Release the gate and finish the task, so that a failed assertion
        does not leave the task blocked while it holds the IP lock."""
        import asyncio
        gate.set()
        if task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except asyncio.TimeoutError:
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=30)
            if done:
                try:
                    task.result()
                except (Exception, asyncio.CancelledError):
                    pass  # The test body reports the failure.
        except (Exception, asyncio.CancelledError):
            pass  # The test body reports the failure.

    @pytest.mark.asyncio
    async def test_concurrent_rollbacks_on_one_ip_serialize(self, rollback_db):
        """Two rollbacks race on one IP: one applies, the other is refused
        before it touches the device."""
        import asyncio
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        from updater import app as app_module

        gate = asyncio.Event()
        entered = asyncio.Event()
        instance = MagicMock()
        instance.login = AsyncMock(return_value=True)
        instance.get_hardware_id = MagicMock(return_value="tn-110")
        reads = iter([{"v": "new"}, {"v": "old"}])

        async def slow_get_config():
            entered.set()
            await asyncio.wait_for(gate.wait(), timeout=30)
            return next(reads)

        instance.get_config = AsyncMock(side_effect=slow_get_config)
        instance.apply_config = AsyncMock(return_value={"success": True})
        poller_stub = MagicMock()
        poller_stub.poll_configs_for_ips = AsyncMock(return_value=None)

        request = MagicMock()
        request.json = AsyncMock(return_value={})
        request.headers = {}
        request.client = None
        session = {"username": "op"}

        with patch("updater.app.TachyonClient", return_value=instance), \
                patch("updater.app.get_poller", return_value=poller_stub):
            first = asyncio.create_task(
                app_module.rollback_device_config("10.0.0.5", request, session, None)
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=30)
                with pytest.raises(HTTPException) as exc:
                    await asyncio.wait_for(
                        app_module.rollback_device_config("10.0.0.5", request, session, None),
                        timeout=30,
                    )
                assert exc.value.status_code == 409
                assert exc.value.detail["code"] == "config_change_in_progress"
                gate.set()
                result = await asyncio.wait_for(first, timeout=30)
            finally:
                await self._drain(first, gate)

        assert result["rollback_verified"] is True
        assert instance.login.await_count == 1
        assert instance.apply_config.await_count == 2  # dry-run + apply, once
        assert "10.0.0.5" not in app_module._config_pushing_ips

    @pytest.mark.asyncio
    async def test_rollback_refused_during_config_push_write(self, rollback_db):
        """A config push holds the lock on one IP during the device write.
        A rollback on that IP gets HTTP 409 and does not contact the device."""
        import asyncio
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        from updater import app as app_module

        gate = asyncio.Event()
        writing = asyncio.Event()
        clients = []

        async def slow_apply(config, dry_run=False, **kwargs):
            # The dry run returns at once. The real write pauses at the gate.
            if not dry_run:
                writing.set()
                await asyncio.wait_for(gate.wait(), timeout=30)
            return {"success": True}

        def fake_client(ip, username, password):
            instance = MagicMock()
            instance.login = AsyncMock(return_value=True)
            instance.get_config = AsyncMock(return_value={"v": "new"})
            instance.apply_config = AsyncMock(side_effect=slow_apply)
            instance.get_hardware_id = MagicMock(return_value="tn-110")
            clients.append(instance)
            return instance

        templates = [{"id": 1, "name": "T", "fragment": {"v": "pushed"}, "device_types": None}]
        devices = [{"ip": "10.0.0.5", "role": "ap", "username": "admin",
                    "password": "pass", "model": None}]
        job_info = {"cancelled": False, "success": 0, "failed": 0, "skipped": 0,
                    "total": 1, "done": False}

        request = MagicMock()
        request.json = AsyncMock(return_value={})
        request.headers = {}
        request.client = None
        session = {"username": "op"}

        with patch("updater.app.TachyonClient", side_effect=fake_client), \
                patch("updater.app.broadcast", new=AsyncMock()):
            push = asyncio.create_task(
                app_module._run_config_push("push-job", devices, templates, job_info)
            )
            try:
                await asyncio.wait_for(writing.wait(), timeout=30)
                push_client = clients[0]
                assert push_client.apply_config.await_count == 2  # dry run + paused write
                assert "10.0.0.5" in app_module._config_pushing_ips
                with pytest.raises(HTTPException) as exc:
                    await asyncio.wait_for(
                        app_module.rollback_device_config("10.0.0.5", request, session, None),
                        timeout=30,
                    )
                assert exc.value.status_code == 409
                assert exc.value.detail["code"] == "config_change_in_progress"
                # Fail closed: the rollback made no client, made no device
                # call, and kept the push lock.
                assert len(clients) == 1
                assert push_client.login.await_count == 1
                assert push_client.get_config.await_count == 1
                assert push_client.apply_config.await_count == 2
                assert "10.0.0.5" in app_module._config_pushing_ips
                gate.set()
                await asyncio.wait_for(push, timeout=30)
            finally:
                await self._drain(push, gate)

        assert job_info["success"] == 1
        assert clients[0].apply_config.await_count == 2  # push dry run + write only
        assert "10.0.0.5" not in app_module._config_pushing_ips


def test_phased_rollout_routes_are_removed(operator_client):
    paths = [
        ("GET", "/api/config-push/rollout"),
        ("POST", "/api/config-push/rollout/start"),
        ("POST", "/api/config-push/rollout/1/advance"),
        ("POST", "/api/config-push/rollout/1/resume"),
        ("POST", "/api/config-push/rollout/1/cancel"),
    ]
    openapi = operator_client.get("/openapi.json").json()["paths"]
    assert not any(path.startswith("/api/config-push/rollout") for path in openapi)
    for method, path in paths:
        request = getattr(operator_client, method.lower())
        response = request(path, json={}) if method == "POST" else request(path)
        assert response.status_code == 404, path
