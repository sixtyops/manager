"""Tests for RBAC (role-based access control) and user management."""

import os
from unittest.mock import patch

import bcrypt
import pytest

from updater import database as db
from updater.auth import authenticate_local, ensure_admin_user, ensure_oidc_user


# ---------------------------------------------------------------------------
# Role enforcement on routes
# ---------------------------------------------------------------------------

class TestRoleEnforcement:
    """Verify that role-based access control is enforced on API routes."""

    def test_admin_can_access_settings(self, authed_client):
        resp = authed_client.get("/api/settings")
        assert resp.status_code == 200

    def test_admin_can_update_settings(self, authed_client):
        resp = authed_client.put("/api/settings", json={"timezone": "UTC"})
        assert resp.status_code == 200

    def test_operator_cannot_update_settings(self, operator_client):
        resp = operator_client.put("/api/settings", json={"timezone": "UTC"})
        assert resp.status_code == 403

    def test_viewer_cannot_update_settings(self, viewer_client):
        resp = viewer_client.put("/api/settings", json={"timezone": "UTC"})
        assert resp.status_code == 403

    def test_viewer_can_read_sites(self, viewer_client):
        resp = viewer_client.get("/api/sites")
        assert resp.status_code == 200

    def test_viewer_cannot_create_site(self, viewer_client):
        resp = viewer_client.post("/api/sites", data={"name": "test"})
        assert resp.status_code == 403

    def test_operator_can_create_site(self, operator_client):
        resp = operator_client.post("/api/sites", data={"name": "test-site"})
        assert resp.status_code == 200

    def test_viewer_cannot_upload_firmware(self, viewer_client):
        resp = viewer_client.post("/api/upload-firmware",
                                  files={"file": ("test.bin", b"data")})
        assert resp.status_code == 403

    def test_operator_cannot_manage_users(self, operator_client):
        resp = operator_client.get("/api/users")
        assert resp.status_code == 403

    def test_viewer_cannot_manage_users(self, viewer_client):
        resp = viewer_client.get("/api/users")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# User management API
# ---------------------------------------------------------------------------

class TestUserManagement:
    """Test CRUD operations on users."""

    def test_list_users(self, authed_client):
        resp = authed_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        users = data["users"]
        assert any(u["username"] == "admin" for u in users)

    def test_get_current_user(self, authed_client):
        resp = authed_client.get("/api/users/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert data["role"] == "admin"

    def test_operator_can_get_me(self, operator_client):
        resp = operator_client.get("/api/users/me")
        assert resp.status_code == 200
        assert resp.json()["role"] == "operator"

    def test_viewer_can_get_me(self, viewer_client):
        resp = viewer_client.get("/api/users/me")
        assert resp.status_code == 200
        assert resp.json()["role"] == "viewer"

    def test_create_user(self, authed_client):
        resp = authed_client.post("/api/users", json={
            "username": "newuser",
            "password": "securepass12345",
            "role": "operator",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "newuser"
        assert data["role"] == "operator"

    def test_create_user_short_password(self, authed_client):
        resp = authed_client.post("/api/users", json={
            "username": "shortpw",
            "password": "short",
            "role": "viewer",
        })
        assert resp.status_code == 400

    def test_create_user_invalid_role(self, authed_client):
        resp = authed_client.post("/api/users", json={
            "username": "badrole",
            "password": "securepass12345",
            "role": "superadmin",
        })
        assert resp.status_code == 400

    def test_create_duplicate_user(self, authed_client):
        authed_client.post("/api/users", json={
            "username": "dupuser",
            "password": "securepass12345",
            "role": "viewer",
        })
        resp = authed_client.post("/api/users", json={
            "username": "dupuser",
            "password": "securepass12345",
            "role": "viewer",
        })
        assert resp.status_code == 409

    def test_update_user_role(self, authed_client, mock_db):
        mock_db.execute(
            "INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
            ("target_user", "viewer", "local"),
        )
        mock_db.commit()
        user = db.get_user("target_user")

        resp = authed_client.put(f"/api/users/{user['id']}", json={"role": "operator"})
        assert resp.status_code == 200

        updated = db.get_user("target_user")
        assert updated["role"] == "operator"

    def test_update_user_password(self, authed_client, mock_db):
        mock_db.execute(
            "INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
            ("pwchange_user", "viewer", "local"),
        )
        mock_db.commit()
        user = db.get_user("pwchange_user")

        resp = authed_client.put(f"/api/users/{user['id']}", json={
            "password": "newpassword1234"
        })
        assert resp.status_code == 200

        updated = db.get_user("pwchange_user")
        assert updated["password_hash"] is not None
        assert bcrypt.checkpw(b"newpassword1234", updated["password_hash"].encode())

    def test_delete_user(self, authed_client, mock_db):
        mock_db.execute(
            "INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
            ("del_user", "viewer", "local"),
        )
        mock_db.commit()
        user = db.get_user("del_user")

        resp = authed_client.delete(f"/api/users/{user['id']}")
        assert resp.status_code == 200
        assert db.get_user("del_user") is None

    def test_delete_user_cleans_up_oidc_session_tokens(self, authed_client, mock_db):
        from datetime import datetime, timedelta

        mock_db.execute(
            "INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
            ("oidc-delete@example.com", "viewer", "oidc"),
        )
        mock_db.execute(
            "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
            (
                "oidc-delete-session",
                "oidc-delete@example.com",
                "127.0.0.1",
                (datetime.now() + timedelta(hours=1)).isoformat(),
            ),
        )
        mock_db.commit()
        db.set_setting("oidc_id_token_oidc-delete-session", "fake-id-token")

        user = db.get_user("oidc-delete@example.com")
        resp = authed_client.delete(f"/api/users/{user['id']}")

        assert resp.status_code == 200
        assert db.get_setting("oidc_id_token_oidc-delete-session") is None

    def test_cannot_delete_last_admin(self, authed_client, mock_db):
        """When another admin tries to delete the only admin, it should fail."""
        # Create a second admin so the authed user isn't the target
        mock_db.execute(
            "INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
            ("admin2", "admin", "local"),
        )
        mock_db.commit()
        admin2 = db.get_user("admin2")
        # Delete admin2, leaving only 'admin' — this should succeed
        resp = authed_client.delete(f"/api/users/{admin2['id']}")
        assert resp.status_code == 200
        # Now create a non-admin and make admin the sole admin
        mock_db.execute(
            "INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
            ("sole_admin_target", "admin", "local"),
        )
        mock_db.commit()
        target = db.get_user("sole_admin_target")
        # Only admin + sole_admin_target are admins; deleting target leaves 1 admin
        resp = authed_client.delete(f"/api/users/{target['id']}")
        assert resp.status_code == 200
        # Now there's only 1 admin left — deleting self should fail
        admin = db.get_user("admin")
        resp = authed_client.delete(f"/api/users/{admin['id']}")
        assert resp.status_code == 400

    def test_cannot_delete_self(self, authed_client):
        admin = db.get_user("admin")
        resp = authed_client.delete(f"/api/users/{admin['id']}")
        assert resp.status_code == 400
        assert "own account" in resp.json()["detail"].lower()

    def test_cannot_disable_self(self, authed_client):
        admin = db.get_user("admin")
        resp = authed_client.put(f"/api/users/{admin['id']}", json={"enabled": False})
        assert resp.status_code == 400
        assert "own account" in resp.json()["detail"].lower()

    def test_cannot_demote_last_admin(self, authed_client):
        admin = db.get_user("admin")
        resp = authed_client.put(f"/api/users/{admin['id']}", json={"role": "viewer"})
        assert resp.status_code == 400
        assert "last admin" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# ensure_admin_user migration
# ---------------------------------------------------------------------------

class TestEnsureAdminUser:
    def test_creates_admin_from_env_vars(self, mock_db):
        # Clear existing users
        mock_db.execute("DELETE FROM users")
        mock_db.commit()

        with patch.dict(os.environ, {"ADMIN_USERNAME": "myadmin", "ADMIN_PASSWORD": "mypass"}):
            ensure_admin_user()

        user = db.get_user("myadmin")
        assert user is not None
        assert user["role"] == "admin"
        assert user["auth_method"] == "local"

    def test_noop_if_users_exist(self, mock_db):
        # Admin already seeded by conftest
        with patch.dict(os.environ, {"ADMIN_USERNAME": "newadmin", "ADMIN_PASSWORD": "pass"}):
            ensure_admin_user()

        # Should NOT create a new user since users table already has entries
        assert db.get_user("newadmin") is None


# ---------------------------------------------------------------------------
# OIDC user auto-creation
# ---------------------------------------------------------------------------

class TestOIDCUserCreation:
    def test_creates_oidc_user_with_default_role(self, mock_db):
        user = ensure_oidc_user("oidcuser@example.com")
        assert user is not None
        assert user["username"] == "oidcuser@example.com"
        assert user["role"] == "viewer"
        assert user["auth_method"] == "oidc"

    def test_returns_existing_oidc_user(self, mock_db):
        db.create_user("existing@example.com", None, "operator", "oidc")
        user = ensure_oidc_user("existing@example.com")
        assert user["role"] == "operator"

    def test_custom_default_role(self, mock_db):
        db.set_setting("oidc_default_role", "operator")
        user = ensure_oidc_user("custom@example.com")
        assert user["role"] == "operator"

    def test_admin_group_forces_non_members_to_viewer(self, mock_db):
        from updater.oidc_config import OIDCConfig, set_oidc_config

        db.set_setting("oidc_default_role", "operator")
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="client",
            client_secret="secret",
            allowed_group="sixtyops-users",
            admin_group="sixtyops-admins",
        ))

        user = ensure_oidc_user("viewer@example.com", ["sixtyops-users"])
        assert user["role"] == "viewer"

    def test_admin_group_member_gets_admin(self, mock_db):
        from updater.oidc_config import OIDCConfig, set_oidc_config

        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="client",
            client_secret="secret",
            allowed_group="sixtyops-users",
            admin_group="sixtyops-admins",
        ))

        user = ensure_oidc_user("admin@example.com", ["sixtyops-users", "sixtyops-admins"])
        assert user["role"] == "admin"

    def test_existing_oidc_user_is_demoted_when_admin_group_membership_is_removed(self, mock_db):
        from updater.oidc_config import OIDCConfig, set_oidc_config

        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="client",
            client_secret="secret",
            allowed_group="sixtyops-users",
            admin_group="sixtyops-admins",
        ))

        user = ensure_oidc_user("demote@example.com", ["sixtyops-users", "sixtyops-admins"])
        assert user["role"] == "admin"

        user = ensure_oidc_user("demote@example.com", ["sixtyops-users"])
        assert user["role"] == "viewer"

    def test_existing_oidc_user_is_re_evaluated_when_admin_group_mapping_is_removed(self, mock_db):
        from updater.oidc_config import OIDCConfig, set_oidc_config

        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="client",
            client_secret="secret",
            allowed_group="sixtyops-users",
            admin_group="sixtyops-admins",
        ))
        user = ensure_oidc_user("operator@example.com", ["sixtyops-users", "sixtyops-admins"])
        assert user["role"] == "admin"

        db.set_setting("oidc_default_role", "operator")
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="client",
            client_secret="secret",
            allowed_group="sixtyops-users",
            admin_group="",
        ))

        user = ensure_oidc_user("operator@example.com", ["sixtyops-users"])
        assert user["role"] == "operator"


# ---------------------------------------------------------------------------
# Database CRUD
# ---------------------------------------------------------------------------

class TestUserCRUD:
    def test_create_and_get(self, mock_db):
        user_id = db.create_user("testuser", None, "viewer", "local")
        user = db.get_user_by_id(user_id)
        assert user["username"] == "testuser"
        assert user["role"] == "viewer"

    def test_list_users_excludes_password(self, mock_db):
        users = db.list_users()
        for u in users:
            assert "password_hash" not in u

    def test_update_role(self, mock_db):
        user_id = db.create_user("roletest", None, "viewer", "local")
        db.update_user(user_id, role="operator")
        user = db.get_user_by_id(user_id)
        assert user["role"] == "operator"

    def test_invalid_role_rejected(self, mock_db):
        with pytest.raises(ValueError):
            db.create_user("bad", None, "superadmin", "local")

    def test_count_admins(self, mock_db):
        count = db.count_admin_users()
        assert count >= 1  # At least the seeded admin

    def test_delete_user(self, mock_db):
        user_id = db.create_user("todelete", None, "viewer", "local")
        assert db.delete_user(user_id) is True
        assert db.get_user_by_id(user_id) is None

    def test_case_insensitive_lookup(self, mock_db):
        db.create_user("CamelCase", None, "viewer", "local")
        assert db.get_user("camelcase") is not None
        assert db.get_user("CAMELCASE") is not None
