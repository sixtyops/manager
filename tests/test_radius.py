"""Tests for the built-in FreeRADIUS integration."""

import json
import subprocess
from datetime import datetime

import pytest

from updater import app as app_module
from updater import builtin_radius
from updater import database as db


class TestBuiltinRadiusUsers:
    def test_reserved_usernames_blocked(self, mock_db):
        with pytest.raises(ValueError, match="Reserved usernames"):
            builtin_radius.create_user("admin", "secret")

        with pytest.raises(ValueError, match="Reserved usernames"):
            builtin_radius.create_user("root", "secret")


class TestBuiltinRadiusSecretRotation:
    def test_new_secret_sets_rotation_timestamp(self, mock_db):
        builtin_radius.set_config(builtin_radius.BuiltinRadiusConfig(enabled=True, port=1812, secret="sharedsecret"))

        summary = builtin_radius.get_secret_rotation_summary()
        assert summary["rotation_status"] == "healthy"
        assert summary["rotation_recommended"] is False
        assert summary["secret_last_rotated_at"]
        assert summary["secret_age_days"] == 0

    def test_legacy_secret_without_timestamp_reports_unknown(self, mock_db):
        db.set_settings({
            "builtin_radius_secret": "sharedsecret",
            "builtin_radius_secret_updated_at": "",
        })

        summary = builtin_radius.get_secret_rotation_summary()
        assert summary["rotation_status"] == "unknown"
        assert summary["secret_last_rotated_at"] is None
        assert summary["secret_age_days"] is None

    def test_unchanged_secret_preserves_rotation_timestamp(self, mock_db):
        original_timestamp = "2024-01-15T12:00:00"
        db.set_settings({
            "builtin_radius_enabled": "true",
            "builtin_radius_port": "1812",
            "builtin_radius_secret": "sharedsecret",
            "builtin_radius_secret_updated_at": original_timestamp,
        })

        builtin_radius.set_config(builtin_radius.BuiltinRadiusConfig(enabled=True, port=1812, secret="sharedsecret"))

        summary = builtin_radius.get_secret_rotation_summary()
        assert summary["secret_last_rotated_at"] == original_timestamp


class TestBuiltinRadiusAPI:
    def test_config_requires_secret_when_enabled(self, authed_client):
        resp = authed_client.put("/api/auth/radius", json={"enabled": True, "host": "radius.internal", "port": 39122, "secret": ""})
        assert resp.status_code == 400
        assert "Shared secret" in resp.json()["detail"]

    def test_create_and_list_users(self, authed_client):
        resp = authed_client.put("/api/auth/radius", json={"enabled": True, "host": "radius.internal", "port": 39122, "secret": "sharedsecret"})
        assert resp.status_code == 200
        assert resp.json()["secret_set"] is True

        reserved = authed_client.post("/api/auth/radius/users", json={"username": "admin", "password": "x"})
        assert reserved.status_code == 400

        created = authed_client.post("/api/auth/radius/users", json={"username": "jsmith", "password": "pass123456789", "enabled": True})
        assert created.status_code == 200
        assert created.json()["username"] == "jsmith"
        assert "password" not in created.json()

        listing = authed_client.get("/api/auth/radius/users")
        assert listing.status_code == 200
        users = listing.json()["users"]
        assert len(users) == 1
        assert users[0]["username"] == "jsmith"

    def test_create_and_list_client_overrides(self, authed_client):
        created = authed_client.post(
            "/api/auth/radius/clients",
            json={"client_spec": "10.0.10.0/24", "shortname": "tower-subnet", "enabled": True},
        )
        assert created.status_code == 200
        assert created.json()["client_spec"] == "10.0.10.0/24"

        listing = authed_client.get("/api/auth/radius/clients")
        assert listing.status_code == 200
        clients = listing.json()["clients"]
        assert len(clients) == 1
        assert clients[0]["shortname"] == "tower-subnet"

    def test_mark_legacy_secret_reviewed(self, authed_client):
        db.set_settings({
            "builtin_radius_secret": "sharedsecret",
            "builtin_radius_secret_updated_at": "",
        })

        resp = authed_client.post("/api/auth/radius/secret-review")
        assert resp.status_code == 200
        assert resp.json()["rotation_status"] == "healthy"
        assert resp.json()["secret_last_rotated_at"]

    def test_start_radius_rollout(self, authed_client, monkeypatch):
        db.set_settings({
            "builtin_radius_enabled": "true",
            "builtin_radius_host": "radius.internal",
            "builtin_radius_secret": "sharedsecret",
        })
        db.save_config_template(
            name="Radius Auth",
            category="radius",
            config_fragment=json.dumps({
                "system": {
                    "auth": {
                        "method": "radius",
                        "radius": {
                            "auth_server1": "10.0.0.1",
                            "auth_port": 1812,
                            "auth_secret": "sharedsecret",
                        },
                    },
                },
            }),
            form_data=json.dumps({
                "method": "radius",
                "server": "radius.internal",
                "port": "1812",
                "secret": "sharedsecret",
            }),
            description="",
        )

        monkeypatch.setattr(app_module, "_radius_rollout_targets", lambda: [{"ip": "10.0.0.5", "role": "ap", "username": "root", "password": "oldpass"}])
        monkeypatch.setattr(app_module, "_start_radius_rollout_task", lambda rollout_id: None)
        monkeypatch.setattr(app_module, "_refresh_radius_rollout_inventory", app_module._refresh_radius_rollout_inventory)
        monkeypatch.setattr(builtin_radius, "get_management_service_credentials", lambda create_if_missing=True: ("sixtyops-radius-mgmt", "svcpass"))

        class _Runtime:
            async def reload(self):
                return None

        monkeypatch.setattr(builtin_radius, "get_runtime", lambda: _Runtime())

        resp = authed_client.post("/api/auth/radius/rollout/start")
        assert resp.status_code == 200
        assert resp.json()["rollout"]["status"] == "active"
        assert resp.json()["rollout"]["phase"] == "canary"

    def test_start_radius_rollout_rejects_template_secret_mismatch(self, authed_client):
        db.set_settings({
            "builtin_radius_enabled": "true",
            "builtin_radius_host": "radius.internal",
            "builtin_radius_secret": "sharedsecret",
            "builtin_radius_port": "1812",
        })
        db.save_config_template(
            name="Radius Auth",
            category="radius",
            config_fragment=json.dumps({
                "system": {
                    "auth": {
                        "method": "radius",
                        "radius": {
                            "auth_server1": "radius.internal",
                            "auth_port": 1812,
                            "auth_secret": "wrongsecret",
                        },
                    },
                },
            }),
            form_data=json.dumps({
                "method": "radius",
                "server": "radius.internal",
                "port": "1812",
                "secret": "wrongsecret",
            }),
            description="",
        )

        resp = authed_client.post("/api/auth/radius/rollout/start")
        assert resp.status_code == 400
        assert "does not match the built-in Radius secret" in resp.json()["detail"]

    def test_start_radius_rollout_rejects_failed_preflight(self, authed_client, monkeypatch):
        db.set_settings({
            "builtin_radius_enabled": "true",
            "builtin_radius_host": "radius.internal",
            "builtin_radius_secret": "sharedsecret",
        })
        db.save_config_template(
            name="Radius Auth",
            category="radius",
            config_fragment=json.dumps({
                "system": {
                    "auth": {
                        "method": "radius",
                        "radius": {
                            "auth_server1": "radius.internal",
                            "auth_port": 1812,
                            "auth_secret": "sharedsecret",
                        },
                    },
                },
            }),
            form_data=json.dumps({
                "method": "radius",
                "server": "radius.internal",
                "port": "1812",
                "secret": "sharedsecret",
            }),
            description="",
        )

        async def fail_preflight():
            raise ValueError("Radius rollout preflight failed for APs: 10.0.0.5 (bad creds)")

        monkeypatch.setattr(app_module, "_refresh_radius_rollout_inventory", fail_preflight)

        resp = authed_client.post("/api/auth/radius/rollout/start")
        assert resp.status_code == 400
        assert "preflight failed" in resp.json()["detail"]


class TestBuiltinRadiusRollout:
    def test_rollout_targets_include_auth_ok_cpes_with_parent_ap_credentials(self, mock_db):
        mock_db.execute(
            """
            INSERT INTO access_points (ip, username, password, system_name, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            ("10.0.0.10", "root", "ap-pass", "tower-ap-1"),
        )
        mock_db.execute(
            """
            INSERT INTO switches (ip, username, password, system_name, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            ("10.0.0.20", "admin", "sw-pass", "tower-sw-1"),
        )
        db.upsert_cpe("10.0.0.10", {"ip": "10.0.0.11", "system_name": "sm-ok", "auth_status": "ok"})
        db.upsert_cpe("10.0.0.10", {"ip": "10.0.0.12", "system_name": "sm-fail", "auth_status": "failed"})
        mock_db.commit()

        targets = app_module._radius_rollout_targets()

        assert [target["role"] for target in targets] == ["ap", "cpe", "switch"]
        cpe_target = next(target for target in targets if target["role"] == "cpe")
        assert cpe_target["ip"] == "10.0.0.11"
        assert cpe_target["username"] == "root"
        assert cpe_target["password"] == "ap-pass"
        assert cpe_target["parent_ap_ip"] == "10.0.0.10"

    def test_serialize_rollout_devices_includes_parent_ap_repair_target(self, mock_db):
        mock_db.execute(
            """
            INSERT INTO access_points (ip, username, password, system_name, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            ("10.0.0.10", "root", "ap-pass", "tower-ap-1"),
        )
        db.upsert_cpe("10.0.0.10", {"ip": "10.0.0.11", "system_name": "sm-ok", "auth_status": "ok"})
        rollout_id = builtin_radius.create_rollout(1, "sixtyops-radius-mgmt")
        builtin_radius.assign_device_to_rollout(rollout_id, "10.0.0.11", "cpe", "canary")

        devices = app_module._serialize_radius_rollout_devices(rollout_id)
        assert devices[0]["parent_ap_ip"] == "10.0.0.10"
        assert devices[0]["repair_target_ip"] == "10.0.0.10"

    def test_resolve_rollout_phase_devices_marks_missing_inventory_as_skipped(self, mock_db):
        rollout_id = builtin_radius.create_rollout(1, "sixtyops-radius-mgmt")
        builtin_radius.assign_device_to_rollout(rollout_id, "10.0.0.99", "cpe", "canary")
        rollout = builtin_radius.get_rollout(rollout_id)

        resolved = app_module._resolve_radius_rollout_phase_devices(rollout, [])
        rows = builtin_radius.get_rollout_devices(rollout_id)

        assert resolved == []
        assert rows[0]["status"] == "skipped"
        assert rows[0]["error"] == "Device missing from inventory"

    @pytest.mark.asyncio
    async def test_cpe_rollout_failure_requests_inline_ap_credential_update(self, mock_db, monkeypatch):
        recorded = []

        class FailingClient:
            def __init__(self, ip, username, password, timeout=10):
                self.ip = ip

            async def login(self):
                return "bad password"

        monkeypatch.setattr(app_module, "TachyonClient", FailingClient)
        monkeypatch.setattr(
            builtin_radius,
            "mark_rollout_device",
            lambda rollout_id, ip, status, error="": recorded.append((rollout_id, ip, status, error)),
        )

        success, error = await app_module._push_radius_to_device(
            7,
            {
                "ip": "10.0.0.11",
                "role": "cpe",
                "username": "root",
                "password": "ap-pass",
                "parent_ap_ip": "10.0.0.10",
            },
            {"system": {}},
            "sixtyops-radius-mgmt",
            "svc-pass",
        )

        assert success is False
        assert "Update the AP credentials inline and resume rollout." in error
        assert recorded[-1][2] == "failed"

    @pytest.mark.asyncio
    async def test_cpe_rollout_success_does_not_persist_fake_cpe_credentials(self, mock_db, monkeypatch):
        update_calls = []

        class SuccessfulClient:
            def __init__(self, ip, username, password, timeout=10):
                self.ip = ip

            async def login(self):
                return True

            async def get_config(self):
                return {"system": {}}

            async def apply_config(self, merged, dry_run=False):
                return {"success": True}

        async def no_sleep(_seconds):
            return None

        monkeypatch.setattr(app_module, "TachyonClient", SuccessfulClient)
        monkeypatch.setattr(app_module.asyncio, "sleep", no_sleep)
        monkeypatch.setattr(
            builtin_radius,
            "mark_rollout_device",
            lambda rollout_id, ip, status, error="": None,
        )
        monkeypatch.setattr(
            db,
            "update_device_credentials",
            lambda device_type, ip, username, password: update_calls.append((device_type, ip, username)),
        )

        success, error = await app_module._push_radius_to_device(
            8,
            {
                "ip": "10.0.0.11",
                "role": "cpe",
                "username": "root",
                "password": "ap-pass",
                "parent_ap_ip": "10.0.0.10",
            },
            {"system": {"auth": {"method": "radius"}}},
            "sixtyops-radius-mgmt",
            "svc-pass",
        )

        assert success is True
        assert error == ""
        assert update_calls == []


class TestBuiltinRadiusFiles:
    def test_writes_clients_and_authorize_files(self, mock_db, tmp_path, monkeypatch):
        mock_db.execute(
            """
            INSERT INTO access_points (ip, username, password, system_name, model, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            ("10.0.0.5", "root", "ignored", "tower-ap-1", "TNA-301"),
        )
        mock_db.commit()

        client_file = tmp_path / "clients.conf"
        users_file = tmp_path / "mods-config" / "files" / "authorize"
        monkeypatch.setattr(builtin_radius, "RADIUS_CLIENTS_FILE", client_file)
        monkeypatch.setattr(builtin_radius, "RADIUS_USERS_FILE", users_file)

        builtin_radius.set_config(builtin_radius.BuiltinRadiusConfig(enabled=True, port=1812, secret="sharedsecret"))
        builtin_radius.create_user("jsmith", "pass123456789")
        builtin_radius.create_client_override("10.0.10.0/24", "tower-subnet")
        builtin_radius._write_freeradius_files()

        clients_text = client_file.read_text()
        users_text = users_file.read_text()
        assert "10.0.0.5" in clients_text
        assert "10.0.10.0/24" in clients_text
        assert 'secret = "sharedsecret"' in clients_text
        assert 'jsmith Cleartext-Password := "pass123456789"' in users_text
        assert 'sixtyops-radius-mgmt Cleartext-Password :=' in users_text


class TestBuiltinRadiusStats:
    def test_manual_client_override_used_for_stats(self, mock_db, monkeypatch):
        builtin_radius.create_client_override("10.0.10.0/24", "tower-subnet")

        today = datetime.now().date().isoformat()
        log_text = "\n".join([
            f"{today}T12:00:00.000000Z (0) Login OK: [jsmith/<via Auth-Type = PAP>] (from client sixtyops_1 port 0 cli 10.0.10.5)",
        ])

        monkeypatch.setattr(
            builtin_radius,
            "_run_docker",
            lambda args: subprocess.CompletedProcess(args, 0, stdout="", stderr=log_text),
        )

        builtin_radius.sync_auth_history()
        stats = builtin_radius.get_stats()
        assert stats["known_clients"] == 1
        assert stats["active_devices_24h"] == 1
        assert stats["recent_logins"][0]["client_name"] == "tower-subnet"

    def test_parses_freeradius_logs(self, mock_db, monkeypatch):
        mock_db.execute(
            """
            INSERT INTO access_points (ip, username, password, system_name, model, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            ("10.0.0.5", "root", "ignored", "tower-ap-1", "TNA-301"),
        )
        mock_db.commit()

        builtin_radius.create_user("jsmith", "pass123456789")

        today = datetime.now().date().isoformat()
        log_text = "\n".join([
            f"{today}T12:05:00.000000Z (1) Login incorrect (pap: Password mismatch): [jsmith/wrong] (from client sixtyops_1 port 0 cli 10.0.0.5)",
            f"{today}T12:00:00.000000Z (0) Login OK: [jsmith/<via Auth-Type = PAP>] (from client sixtyops_1 port 0 cli 10.0.0.5)",
        ])

        monkeypatch.setattr(
            builtin_radius,
            "_run_docker",
            lambda args: subprocess.CompletedProcess(args, 0, stdout="", stderr=log_text),
        )

        builtin_radius.sync_auth_history()
        stats = builtin_radius.get_stats()
        assert stats["admin_accounts"] == 1
        assert stats["known_clients"] == 1
        assert stats["logins_today"] == 2
        assert stats["auth_success_rate"] == 50.0
        assert stats["recent_logins"][0]["outcome"] == "reject"
        assert mock_db.execute("SELECT COUNT(*) FROM radius_auth_log").fetchone()[0] == 2


class TestBuiltinRadiusRuntime:
    def test_public_summary_exposes_container_health(self, mock_db, monkeypatch):
        builtin_radius.set_config(builtin_radius.BuiltinRadiusConfig(enabled=True, port=1812, secret="sharedsecret"))
        monkeypatch.setattr(
            builtin_radius,
            "_run_docker",
            lambda args: subprocess.CompletedProcess(args, 0, stdout="true|running|healthy\n", stderr=""),
        )
        builtin_radius._runtime = builtin_radius.BuiltinRadiusRuntime()

        summary = builtin_radius.get_public_config_summary()
        assert summary["running"] is True
        assert summary["healthy"] is True
        assert summary["container_status"] == "running"
        assert summary["health_status"] == "healthy"

    @pytest.mark.asyncio
    async def test_ensure_healthy_restarts_unhealthy_container(self, mock_db, monkeypatch):
        builtin_radius.set_config(builtin_radius.BuiltinRadiusConfig(enabled=True, port=1812, secret="sharedsecret"))
        runtime = builtin_radius.BuiltinRadiusRuntime()
        restarted = []

        monkeypatch.setattr(builtin_radius, "_docker_socket_available", lambda: True)
        monkeypatch.setattr(
            builtin_radius,
            "_run_docker",
            lambda args: subprocess.CompletedProcess(args, 0, stdout="true|running|unhealthy\n", stderr=""),
        )
        monkeypatch.setattr(
            builtin_radius,
            "_run_compose",
            lambda args: restarted.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        )

        await runtime.ensure_healthy()
        assert restarted == [["restart", builtin_radius.RADIUS_SERVICE_NAME]]
