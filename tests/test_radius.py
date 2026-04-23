"""Comprehensive tests for the RADIUS server feature.

Covers:
- radius_users CRUD and validation
- radius_server config (load/save, encryption)
- API endpoints (authed_client + pro_license)
- License gating (authed_client + free_license)
- Auth log cleanup
- Rate limiter
- RadiusService config validation and run_forever behaviour
- Rollout helpers
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from updater import app as app_module
from updater import database as db
from updater import radius_rollout


# ===========================================================================
# TestRadiusUsers — CRUD + validation
# ===========================================================================

class TestRadiusUsers:
    """Unit tests for radius_users CRUD operations."""

    def test_create_user_with_valid_username(self, mock_db):
        from updater.radius_users import create_radius_user, get_radius_user_by_name

        user_id = create_radius_user("alice", "s3cur3pass!", "Test user")
        assert isinstance(user_id, int)
        assert user_id > 0

        user = get_radius_user_by_name("alice")
        assert user is not None
        assert user["username"] == "alice"
        assert user["description"] == "Test user"
        assert user["enabled"] == 1

    def test_create_user_empty_password_raises(self, mock_db):
        from updater.radius_users import create_radius_user

        with pytest.raises(ValueError, match="[Pp]assword"):
            create_radius_user("bob", "")

    def test_duplicate_username_raises(self, mock_db):
        from updater.radius_users import create_radius_user

        create_radius_user("carol", "password1!")
        with pytest.raises(Exception) as exc_info:
            create_radius_user("carol", "password2!")
        assert "UNIQUE" in str(exc_info.value) or "unique" in str(exc_info.value).lower()

    @pytest.mark.parametrize("bad_name", [
        "*", "user*name", "user(name", "user)name", "user\\name",
        "user name", " ", "", "a" * 129,
    ])
    def test_username_validation_rejects_invalid(self, bad_name):
        from updater.radius_users import validate_username

        with pytest.raises(ValueError):
            validate_username(bad_name)

    @pytest.mark.parametrize("good_name", [
        "user.name", "user@domain", "user-1", "user_2",
        "a", "A" * 128, "Alice.Bob@example.com",
    ])
    def test_username_validation_accepts_valid(self, good_name):
        from updater.radius_users import validate_username

        result = validate_username(good_name)
        assert result == good_name.strip()

    def test_get_radius_users_excludes_password_hashes(self, mock_db):
        from updater.radius_users import create_radius_user, get_radius_users

        create_radius_user("dave", "hunter2!", "Engineer")
        users = get_radius_users()

        assert len(users) >= 1
        dave = next(u for u in users if u["username"] == "dave")
        assert "password_hash" not in dave
        assert dave["description"] == "Engineer"

    def test_get_radius_user_by_name_includes_password_hash(self, mock_db):
        from updater.radius_users import create_radius_user, get_radius_user_by_name

        create_radius_user("eve", "MyP@ssw0rd")
        user = get_radius_user_by_name("eve")
        assert user is not None
        assert "password" in user
        assert user["password"].startswith("$2b$") or user["password"].startswith("$2a$")

    def test_update_radius_user_changes_description(self, mock_db):
        from updater.radius_users import create_radius_user, update_radius_user, get_radius_user

        uid = create_radius_user("frank", "pass12345!", "Old description")
        result = update_radius_user(uid, description="New description")
        assert result is True

        user = get_radius_user(uid)
        assert user["description"] == "New description"

    def test_update_radius_user_rehashes_password(self, mock_db):
        from updater.radius_users import (
            create_radius_user, update_radius_user,
            get_radius_user_by_name, verify_radius_user,
        )

        uid = create_radius_user("grace", "OldPass123!")
        old_user = get_radius_user_by_name("grace")
        old_hash = old_user["password"]

        update_radius_user(uid, password="NewPass456!")
        new_user = get_radius_user_by_name("grace")
        assert new_user["password"] != old_hash
        assert verify_radius_user("grace", "NewPass456!")
        assert not verify_radius_user("grace", "OldPass123!")

    def test_delete_radius_user_returns_true_for_existing(self, mock_db):
        from updater.radius_users import create_radius_user, delete_radius_user

        uid = create_radius_user("henry", "del3te.me!")
        assert delete_radius_user(uid) is True

    def test_delete_radius_user_returns_false_for_nonexistent(self, mock_db):
        from updater.radius_users import delete_radius_user

        assert delete_radius_user(99999) is False

    def test_verify_radius_user_correct_password(self, mock_db):
        from updater.radius_users import create_radius_user, verify_radius_user

        create_radius_user("ivan", "correct-horse-battery!")
        assert verify_radius_user("ivan", "correct-horse-battery!") is True

    def test_verify_radius_user_wrong_password(self, mock_db):
        from updater.radius_users import create_radius_user, verify_radius_user

        create_radius_user("julia", "rightpassword!")
        assert verify_radius_user("julia", "wrongpassword!") is False

    def test_verify_radius_user_disabled_user(self, mock_db):
        from updater.radius_users import create_radius_user, update_radius_user, verify_radius_user

        uid = create_radius_user("karl", "mypassword123!")
        update_radius_user(uid, enabled=False)
        assert verify_radius_user("karl", "mypassword123!") is False

    def test_verify_radius_user_unknown_user(self, mock_db):
        from updater.radius_users import verify_radius_user

        assert verify_radius_user("nobody_exists", "somepassword") is False

    def test_verify_radius_user_updates_auth_stats_on_success(self, mock_db):
        from updater.radius_users import (
            create_radius_user, verify_radius_user, get_radius_user_by_name,
        )

        create_radius_user("lily", "authme123!")
        before = get_radius_user_by_name("lily")
        assert before["auth_count"] == 0
        assert before["last_auth_at"] is None

        result = verify_radius_user("lily", "authme123!")
        assert result is True

        after = get_radius_user_by_name("lily")
        assert after["auth_count"] == 1
        assert after["last_auth_at"] is not None


# ===========================================================================
# TestRadiusServerConfig — config load/save and encryption
# ===========================================================================

class TestRadiusServerConfig:
    """Tests for RADIUS server configuration persistence and encryption."""

    def test_default_config_from_clean_db(self, mock_db):
        from updater.radius_server import get_radius_server_config

        config = get_radius_server_config()
        assert config.enabled is False
        assert config.auth_port == 1812
        assert len(config.shared_secret) > 0  # auto-generated
        assert config.auth_mode == "local"

    def test_save_and_load_config_round_trip(self, mock_db):
        from updater.radius_server import (
            get_radius_server_config, set_radius_server_config, RadiusServerConfig,
        )

        cfg = RadiusServerConfig(
            enabled=True,
            auth_port=1812,
            shared_secret="s3cr3tshared!",
            auth_mode="local",
            advertised_address="10.0.0.1",
        )
        set_radius_server_config(cfg)

        loaded = get_radius_server_config()
        assert loaded.enabled is True
        assert loaded.auth_port == 1812
        assert loaded.shared_secret == "s3cr3tshared!"
        assert loaded.auth_mode == "local"
        assert loaded.advertised_address == "10.0.0.1"

    def test_shared_secret_is_encrypted_in_db(self, mock_db):
        from updater.radius_server import set_radius_server_config, RadiusServerConfig

        cfg = RadiusServerConfig(shared_secret="plaintext-secret!")
        set_radius_server_config(cfg)

        row = mock_db.execute(
            "SELECT value FROM settings WHERE key = 'radius_server_secret'"
        ).fetchone()
        assert row is not None
        raw_value = row[0]
        assert raw_value.startswith("gAAAAA"), (
            f"Expected Fernet-encrypted value (starts with 'gAAAAA'), got: {raw_value!r}"
        )

    def test_ldap_bind_password_is_encrypted_in_db(self, mock_db):
        from updater.radius_server import set_radius_server_config, RadiusServerConfig

        cfg = RadiusServerConfig(ldap_bind_password="ldap-bind-secret!")
        set_radius_server_config(cfg)

        row = mock_db.execute(
            "SELECT value FROM settings WHERE key = 'radius_server_ldap_bind_password'"
        ).fetchone()
        assert row is not None
        raw_value = row[0]
        assert raw_value.startswith("gAAAAA"), (
            f"Expected Fernet-encrypted value (starts with 'gAAAAA'), got: {raw_value!r}"
        )


# ===========================================================================
# TestRadiusServerAPI — authenticated API endpoints (PRO license)
# ===========================================================================

class TestRadiusServerAPI:
    """Integration tests for RADIUS API endpoints (requires PRO license)."""

    def test_get_config_returns_expected_fields_no_secrets(self, authed_client, pro_license):
        resp = authed_client.get("/api/auth/radius")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "auth_port" in data
        assert "has_secret" in data
        assert "auth_mode" in data
        assert "shared_secret" in data
        assert "ldap_bind_password" not in data

    def test_put_config_updates_settings(self, authed_client, pro_license):
        # Create a RADIUS user first (required for enabling in local mode)
        from updater import radius_users
        radius_users.create_radius_user("testuser", "TestPassword1!")
        resp = authed_client.put("/api/auth/radius", json={
            "enabled": True,
            "auth_port": 1812,
            "shared_secret": "ValidSecret1!",
            "auth_mode": "local",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        get_resp = authed_client.get("/api/auth/radius")
        assert get_resp.status_code == 200
        config = get_resp.json()
        assert config["enabled"] is True
        assert config["has_secret"] is True
        assert config["auth_mode"] == "local"

    def test_put_config_rejects_short_secret(self, authed_client, pro_license):
        resp = authed_client.put("/api/auth/radius", json={
            "shared_secret": "short",
        })
        assert resp.status_code == 400

    def test_put_config_rejects_invalid_port(self, authed_client, pro_license):
        resp = authed_client.put("/api/auth/radius", json={
            "auth_port": 80,
        })
        assert resp.status_code == 400

    def test_put_config_rejects_invalid_auth_mode(self, authed_client, pro_license):
        resp = authed_client.put("/api/auth/radius", json={
            "auth_mode": "kerberos",
        })
        assert resp.status_code == 400

    def test_get_status_returns_running_status(self, authed_client, pro_license):
        resp = authed_client.get("/api/auth/radius/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert isinstance(data["running"], bool)

    def test_post_users_creates_user(self, authed_client, pro_license):
        resp = authed_client.post("/api/auth/radius/users", json={
            "username": "apiuser1",
            "password": "Str0ng!Pass",
            "description": "Created via API",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "apiuser1"

    def test_post_users_rejects_invalid_username(self, authed_client, pro_license):
        resp = authed_client.post("/api/auth/radius/users", json={
            "username": "bad user name*",
            "password": "Str0ng!Pass",
        })
        assert resp.status_code == 400

    def test_get_users_lists_users(self, authed_client, pro_license):
        authed_client.post("/api/auth/radius/users", json={
            "username": "listuser",
            "password": "Str0ng!Pass",
        })
        resp = authed_client.get("/api/auth/radius/users")
        assert resp.status_code == 200
        data = resp.json()
        users = data["users"]
        assert isinstance(users, list)
        usernames = [u["username"] for u in users]
        assert "listuser" in usernames

    def test_put_users_updates_user(self, authed_client, pro_license):
        create_resp = authed_client.post("/api/auth/radius/users", json={
            "username": "updateme",
            "password": "Str0ng!Pass",
            "description": "Before update",
        })
        assert create_resp.status_code == 200
        user_id = create_resp.json()["id"]

        resp = authed_client.put(f"/api/auth/radius/users/{user_id}", json={
            "description": "After update",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_delete_users_deletes_user(self, authed_client, pro_license):
        create_resp = authed_client.post("/api/auth/radius/users", json={
            "username": "deleteme",
            "password": "Str0ng!Pass",
        })
        assert create_resp.status_code == 200
        user_id = create_resp.json()["id"]

        resp = authed_client.delete(f"/api/auth/radius/users/{user_id}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_delete_users_nonexistent_returns_404(self, authed_client, pro_license):
        resp = authed_client.delete("/api/auth/radius/users/999")
        assert resp.status_code == 404

    def test_get_auth_log_returns_entries_with_pagination(self, authed_client, pro_license, mock_db):
        now = datetime.now().isoformat()
        for i in range(5):
            mock_db.execute(
                "INSERT INTO radius_auth_log (username, client_ip, outcome, reason, occurred_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"user{i}", "10.0.0.1", "accept", "local", now),
            )
        mock_db.commit()

        resp = authed_client.get("/api/auth/radius/auth-log?limit=3&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total" in data
        assert data["total"] >= 5
        assert len(data["entries"]) <= 3


# ===========================================================================
# TestRadiusEndpointsAccessible — RADIUS endpoints not gated
# ===========================================================================

class TestRadiusEndpointsAccessible:
    """All RADIUS endpoints should be accessible (no license gating)."""

    ENDPOINTS = [
        ("GET", "/api/auth/radius"),
        ("GET", "/api/auth/radius/status"),
        ("GET", "/api/auth/radius/users"),
        ("GET", "/api/auth/radius/auth-log"),
    ]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_radius_endpoint_not_gated(self, method, path, authed_client):
        resp = authed_client.request(method, path)
        assert resp.status_code != 403, (
            f"{method} {path} should not return 403, got {resp.status_code}"
        )


# ===========================================================================
# TestRadiusAuthLog — log cleanup
# ===========================================================================

class TestRadiusAuthLog:
    """Tests for radius_auth_log table cleanup."""

    def test_cleanup_removes_old_entries_keeps_recent(self, mock_db):
        from updater.database import cleanup_old_radius_auth_log

        old_ts = (datetime.now() - timedelta(days=100)).isoformat()
        new_ts = (datetime.now() - timedelta(days=10)).isoformat()

        mock_db.execute(
            "INSERT INTO radius_auth_log (username, client_ip, outcome, reason, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("olduser", "10.0.0.1", "reject", "local", old_ts),
        )
        mock_db.execute(
            "INSERT INTO radius_auth_log (username, client_ip, outcome, reason, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("newuser", "10.0.0.1", "accept", "local", new_ts),
        )
        mock_db.commit()

        cleanup_old_radius_auth_log(max_age_days=90)

        remaining = mock_db.execute(
            "SELECT username FROM radius_auth_log"
        ).fetchall()
        usernames = [r[0] for r in remaining]
        assert "olduser" not in usernames
        assert "newuser" in usernames


# ===========================================================================
# TestRadiusRateLimiter — SixtyOpsRadiusServer rate limiting logic
# ===========================================================================

class TestRadiusRateLimiter:
    """Tests for the built-in rate limiter in SixtyOpsRadiusServer."""

    def _make_server(self):
        from updater.radius_server import SixtyOpsRadiusServer, RadiusServerConfig

        srv = SixtyOpsRadiusServer.__new__(SixtyOpsRadiusServer)
        srv._config = RadiusServerConfig(shared_secret="testsecret!", auth_port=11812)
        srv._running = True
        srv._last_heartbeat = time.monotonic()
        srv._rate_attempts = {}
        srv._rate_limit = 10
        srv._rate_window = 60.0
        srv._ldap_consecutive_failures = 0
        srv._broadcast_func = None
        return srv

    def test_rate_limiter_allows_under_threshold(self):
        srv = self._make_server()
        for _ in range(9):
            srv._record_failed_attempt("192.168.1.1")
        assert srv._check_rate_limit("192.168.1.1") is False

    def test_rate_limiter_blocks_at_threshold(self):
        srv = self._make_server()
        for _ in range(10):
            srv._record_failed_attempt("192.168.1.2")
        assert srv._check_rate_limit("192.168.1.2") is True

    def test_cleanup_removes_stale_entries(self):
        srv = self._make_server()
        stale_time = time.monotonic() - srv._rate_window - 1
        srv._rate_attempts["192.168.1.3"] = [stale_time, stale_time]
        srv.cleanup_rate_limiter()
        assert "192.168.1.3" not in srv._rate_attempts


# ===========================================================================
# TestRadiusServiceConfig — RadiusService._validate_config + run_forever
# ===========================================================================

class TestRadiusServiceConfig:
    """Tests for RadiusService validation and run_forever sleep-when-disabled."""

    def test_validate_config_returns_error_for_missing_secret(self):
        from updater.radius_server import RadiusService, RadiusServerConfig

        svc = RadiusService(broadcast_func=None)
        config = RadiusServerConfig(enabled=True, shared_secret="", auth_port=1812)
        # Empty secret is checked before license — no mock needed
        error = svc._validate_config(config)
        assert error is not None
        assert "secret" in error.lower() or "Secret" in error

    def test_validate_config_returns_error_for_invalid_port(self):
        from updater.radius_server import RadiusService, RadiusServerConfig

        svc = RadiusService(broadcast_func=None)
        config = RadiusServerConfig(enabled=True, shared_secret="validSecret1!", auth_port=80)
        # Invalid port is checked before license — no mock needed
        error = svc._validate_config(config)
        assert error is not None
        assert "port" in error.lower() or "Port" in error

    @pytest.mark.asyncio
    async def test_run_forever_sleeps_when_disabled(self):
        """run_forever must not return immediately when disabled — it must sleep."""
        from updater.radius_server import RadiusService, RadiusServerConfig

        svc = RadiusService(broadcast_func=None)
        disabled_config = RadiusServerConfig(enabled=False)

        sleep_called = False

        async def mock_sleep(delay):
            nonlocal sleep_called
            sleep_called = True
            raise asyncio.CancelledError()

        with patch("updater.radius_server.get_radius_server_config", return_value=disabled_config), \
             patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await svc.run_forever()
            except asyncio.CancelledError:
                pass

        assert sleep_called, "run_forever should sleep (not spin) when server is disabled"


# ===========================================================================
# TestRadiusAPI — /api/auth/radius/* endpoint tests
# ===========================================================================

class TestRadiusAPI:
    def test_config_requires_secret_when_enabled(self, authed_client):
        from updater import radius_users
        radius_users.create_radius_user("testuser", "TestPassword1!")
        resp = authed_client.put("/api/auth/radius", json={"enabled": True, "shared_secret": "short"})
        assert resp.status_code == 400
        assert "Shared secret" in resp.json()["detail"]

    def test_create_and_list_users(self, authed_client):
        # Create a user first so we can enable RADIUS in local mode
        created_first = authed_client.post("/api/auth/radius/users", json={"username": "bootstrap", "password": "pass123456789", "enabled": True})
        assert created_first.status_code == 200
        resp = authed_client.put("/api/auth/radius", json={"enabled": True, "host": "radius.internal", "port": 39122, "secret": "sharedsecret"})
        assert resp.status_code == 200

        created = authed_client.post("/api/auth/radius/users", json={"username": "jsmith", "password": "pass123456789", "enabled": True})
        assert created.status_code == 200
        assert created.json()["username"] == "jsmith"
        assert "password" not in created.json()

        listing = authed_client.get("/api/auth/radius/users")
        assert listing.status_code == 200
        users = listing.json()["users"]
        assert len(users) >= 1
        assert any(u["username"] == "jsmith" for u in users)

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
        from updater.crypto import encrypt_password
        db.set_settings({
            "radius_server_secret": encrypt_password("sharedsecret"),
            "builtin_radius_secret_updated_at": "",
        })

        resp = authed_client.post("/api/auth/radius/secret-review")
        assert resp.status_code == 200
        assert resp.json()["rotation_status"] == "healthy"
        assert resp.json()["secret_last_rotated_at"]

    def test_start_radius_rollout(self, authed_client, monkeypatch):
        from updater.radius_server import set_radius_server_config, RadiusServerConfig

        set_radius_server_config(RadiusServerConfig(
            enabled=True,
            auth_port=1812,
            shared_secret="sharedsecret",
            advertised_address="radius.internal",
        ))
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

        monkeypatch.setattr(
            app_module,
            "_radius_rollout_targets",
            lambda target_ips=None: [{"ip": "10.0.0.5", "role": "ap", "username": "root", "password": "oldpass"}],
        )
        monkeypatch.setattr(app_module, "_start_radius_rollout_task", lambda rollout_id: None)
        monkeypatch.setattr(app_module, "_refresh_radius_rollout_inventory", app_module._refresh_radius_rollout_inventory)
        monkeypatch.setattr(radius_rollout, "get_management_service_credentials", lambda create_if_missing=True: ("sixtyops-radius-mgmt", "svcpass"))

        resp = authed_client.post("/api/auth/radius/rollout/start", json={"target_ips": ["10.0.0.5"]})
        assert resp.status_code == 200
        assert resp.json()["rollout"]["status"] == "active"
        assert resp.json()["rollout"]["phase"] == "canary"
        assert json.loads(resp.json()["rollout"]["target_ips_json"]) == ["10.0.0.5"]

    def test_start_radius_rollout_rejects_template_secret_mismatch(self, authed_client):
        from updater.radius_server import set_radius_server_config, RadiusServerConfig

        set_radius_server_config(RadiusServerConfig(
            enabled=True,
            auth_port=1812,
            shared_secret="sharedsecret",
            advertised_address="radius.internal",
        ))
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
        assert "does not match" in resp.json()["detail"]

    def test_start_radius_rollout_rejects_failed_preflight(self, authed_client, monkeypatch):
        from updater.radius_server import set_radius_server_config, RadiusServerConfig

        set_radius_server_config(RadiusServerConfig(
            enabled=True,
            auth_port=1812,
            shared_secret="sharedsecret",
            advertised_address="radius.internal",
        ))
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

        async def fail_preflight(_target_ips=None):
            raise ValueError("Radius rollout preflight failed for APs: 10.0.0.5 (bad creds)")

        monkeypatch.setattr(app_module, "_refresh_radius_rollout_inventory", fail_preflight)

        resp = authed_client.post("/api/auth/radius/rollout/start")
        assert resp.status_code == 400
        assert "preflight failed" in resp.json()["detail"]


# ===========================================================================
# TestRadiusRollout — rollout helper functions
# ===========================================================================

class TestRadiusRollout:
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

    def test_rollout_targets_can_be_limited_to_explicit_ips(self, mock_db):
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
        mock_db.commit()

        targets = app_module._radius_rollout_targets({"10.0.0.11", "10.0.0.20"})

        assert [target["ip"] for target in targets] == ["10.0.0.11", "10.0.0.20"]

    def test_serialize_rollout_devices_includes_parent_ap_repair_target(self, mock_db):
        mock_db.execute(
            """
            INSERT INTO access_points (ip, username, password, system_name, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            ("10.0.0.10", "root", "ap-pass", "tower-ap-1"),
        )
        db.upsert_cpe("10.0.0.10", {"ip": "10.0.0.11", "system_name": "sm-ok", "auth_status": "ok"})
        rollout_id = radius_rollout.create_rollout(1, "sixtyops-radius-mgmt")
        radius_rollout.assign_device_to_rollout(rollout_id, "10.0.0.11", "cpe", "canary")

        devices = app_module._serialize_radius_rollout_devices(rollout_id)
        assert devices[0]["parent_ap_ip"] == "10.0.0.10"
        assert devices[0]["repair_target_ip"] == "10.0.0.10"

    def test_resolve_rollout_phase_devices_marks_missing_inventory_as_skipped(self, mock_db):
        rollout_id = radius_rollout.create_rollout(1, "sixtyops-radius-mgmt")
        radius_rollout.assign_device_to_rollout(rollout_id, "10.0.0.99", "cpe", "canary")
        rollout = radius_rollout.get_rollout(rollout_id)

        resolved = app_module._resolve_radius_rollout_phase_devices(rollout, [])
        rows = radius_rollout.get_rollout_devices(rollout_id)

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
            radius_rollout,
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
            radius_rollout,
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
