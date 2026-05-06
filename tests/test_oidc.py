"""Tests for OIDC/SSO authentication configuration and flow."""

import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from updater.oidc_config import OIDCConfig, get_oidc_config, set_oidc_config, is_oidc_enabled
from updater.auth import authenticate_oidc_user, ensure_oidc_user


class TestOIDCConfig:
    def test_defaults_disabled(self, mock_db):
        config = get_oidc_config()
        assert config.enabled is False
        assert config.provider_url == ""
        assert config.client_id == ""

    def test_env_var_fallback(self, mock_db):
        with patch.dict(os.environ, {
            "OIDC_PROVIDER_URL": "https://authentik.example.com/application/o/sixtyops/",
            "OIDC_CLIENT_ID": "test-client-id",
            "OIDC_CLIENT_SECRET": "test-secret",
            "OIDC_REDIRECT_URI": "https://sixtyops.example.com/auth/oidc/callback",
            "OIDC_ALLOWED_GROUP": "sixtyops-admins",
        }):
            config = get_oidc_config()
            assert config.enabled is False
            assert config.provider_url == "https://authentik.example.com/application/o/sixtyops/"
            assert config.client_id == "test-client-id"
            assert config.allowed_group == "sixtyops-admins"

    def test_db_overrides_env(self, mock_db):
        with patch.dict(os.environ, {
            "OIDC_PROVIDER_URL": "https://env.example.com/",
            "OIDC_CLIENT_ID": "env-client",
        }):
            set_oidc_config(OIDCConfig(
                enabled=True,
                provider_url="https://db.example.com/",
                client_id="db-client",
                client_secret="db-secret",
                redirect_uri="https://sixtyops.example.com/auth/oidc/callback",
                allowed_group="admins",
            ))
            config = get_oidc_config()
            assert config.provider_url == "https://db.example.com/"
            assert config.client_id == "db-client"

    def test_is_oidc_enabled_requires_fields(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="",
            client_secret="",
        ))
        assert is_oidc_enabled() is False

    def test_is_oidc_enabled_complete(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="my-client",
            client_secret="my-secret",
        ))
        assert is_oidc_enabled() is True

    def test_set_oidc_config_clears_cached_logout_endpoint_when_provider_changes(self, mock_db):
        from updater import database as db

        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://old.example.com/",
            client_id="client-a",
            client_secret="secret-a",
        ))
        db.set_setting("oidc_end_session_endpoint", "https://old.example.com/logout")

        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://new.example.com/",
            client_id="client-b",
            client_secret="secret-b",
        ))

        assert db.get_setting("oidc_end_session_endpoint") is None


class TestAuthenticateOIDCUser:
    def test_allowed_group(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="sixtyops-admins",
        ))
        result = authenticate_oidc_user("admin@example.com", ["users", "sixtyops-admins"])
        assert result == "admin@example.com"

    def test_denied_wrong_group(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="sixtyops-admins",
        ))
        result = authenticate_oidc_user("user@example.com", ["users", "other-group"])
        assert result is None

    def test_denied_no_groups(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="sixtyops-admins",
        ))
        result = authenticate_oidc_user("user@example.com", [])
        assert result is None

    def test_denied_when_disabled(self, mock_db):
        set_oidc_config(OIDCConfig(enabled=False, allowed_group="sixtyops-admins"))
        result = authenticate_oidc_user("admin@example.com", ["sixtyops-admins"])
        assert result is None

    def test_denied_no_allowed_group_configured(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="",
        ))
        result = authenticate_oidc_user("admin@example.com", ["sixtyops-admins"])
        assert result is None

    def test_group_claim_string_is_matched_exactly(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="sixtyops-admins",
        ))
        result = authenticate_oidc_user("admin@example.com", "not-sixtyops-admins")
        assert result is None

    def test_group_claim_string_exact_match_is_allowed(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="sixtyops-admins",
        ))
        result = authenticate_oidc_user("admin@example.com", "sixtyops-admins")
        assert result == "admin@example.com"


class TestOIDCRoutes:
    def test_oidc_login_redirect_when_disabled(self, client):
        """When OIDC is disabled, /auth/oidc/login redirects to /login."""
        resp = client.get("/auth/oidc/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_oidc_callback_no_state(self, client):
        """Callback without state redirects to login when SSO disabled."""
        resp = client.get("/auth/oidc/callback", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_oidc_callback_invalid_state(self, client):
        """Callback with unknown state redirects to login when SSO disabled."""
        resp = client.get("/auth/oidc/callback?code=abc&state=invalid", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_oidc_callback_error_param(self, client):
        """Callback with error parameter redirects to login when SSO disabled."""
        resp = client.get("/auth/oidc/callback?error=access_denied", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_login_page_no_sso_button_when_disabled(self, client):
        """SSO button should not appear when OIDC is disabled."""
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "Sign in with SSO" not in resp.text

    def test_oidc_callback_blocked_when_disabled(self, client):
        """Callback redirects to /login when SSO is disabled."""
        resp = client.get("/auth/oidc/callback?code=abc&state=valid", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_oidc_config_api_requires_auth(self, client):
        """OIDC config API requires authentication."""
        resp = client.get("/api/auth/oidc")
        assert resp.status_code == 401

    def test_oidc_config_api_returns_config(self, authed_client):
        """OIDC config API returns current config."""
        resp = authed_client.get("/api/auth/oidc")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "configured" in data


class TestOIDCLogout:
    """Tests for RP-initiated OIDC logout."""

    def _create_oidc_session(self, mock_db, client, with_id_token=False):
        """Helper: create an OIDC user and session, return session_id."""
        from updater import database as db
        # create_user is idempotent-safe: skip if user already exists
        if not db.get_user("oidcuser@example.com"):
            db.create_user("oidcuser@example.com", None, "viewer", "oidc")
        session_id = "oidc-session-1234"
        expires = (datetime.now() + timedelta(hours=24)).isoformat()
        mock_db.execute(
            "INSERT OR REPLACE INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, "oidcuser@example.com", "127.0.0.1", expires),
        )
        mock_db.commit()
        if with_id_token:
            db.set_setting(f"oidc_id_token_{session_id}", "eyJ.fake.id_token")
        client.cookies.set("session_id", session_id)
        return session_id

    def test_oidc_logout_redirects_to_end_session(self, client, mock_db):
        """OIDC user logout redirects to provider's end_session_endpoint."""
        self._create_oidc_session(mock_db, client)
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/application/o/sixtyops/",
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri="https://sixtyops.example.com/auth/oidc/callback",
            allowed_group="admins",
        ))
        with patch("updater.app._get_oidc_end_session_url", new_callable=AsyncMock,
                    return_value="https://auth.example.com/end-session"):
            resp = client.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "auth.example.com/end-session" in location
        assert "post_logout_redirect_uri=https%3A%2F%2Fsixtyops.example.com%2Flogin" in location
        assert "client_id=test-client" in location
        assert "state=" in location

    def test_oidc_logout_includes_id_token_hint(self, client, mock_db):
        """Logout sends id_token_hint when id_token was stored at login."""
        self._create_oidc_session(mock_db, client, with_id_token=True)
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/application/o/sixtyops/",
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri="https://sixtyops.example.com/auth/oidc/callback",
            allowed_group="admins",
        ))
        with patch("updater.app._get_oidc_end_session_url", new_callable=AsyncMock,
                    return_value="https://auth.example.com/end-session"):
            resp = client.post("/logout", follow_redirects=False)
        location = resp.headers["location"]
        assert "id_token_hint=eyJ.fake.id_token" in location

    def test_oidc_logout_without_id_token_omits_hint(self, client, mock_db):
        """Logout omits id_token_hint when no id_token was stored."""
        self._create_oidc_session(mock_db, client, with_id_token=False)
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/application/o/sixtyops/",
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri="https://sixtyops.example.com/auth/oidc/callback",
            allowed_group="admins",
        ))
        with patch("updater.app._get_oidc_end_session_url", new_callable=AsyncMock,
                    return_value="https://auth.example.com/end-session"):
            resp = client.post("/logout", follow_redirects=False)
        location = resp.headers["location"]
        assert "id_token_hint" not in location

    def test_oidc_logout_cleans_up_id_token_setting(self, client, mock_db):
        """Logout deletes the stored id_token from settings."""
        from updater import database as db
        session_id = self._create_oidc_session(mock_db, client, with_id_token=True)
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/application/o/sixtyops/",
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri="https://sixtyops.example.com/auth/oidc/callback",
            allowed_group="admins",
        ))
        with patch("updater.app._get_oidc_end_session_url", new_callable=AsyncMock,
                    return_value="https://auth.example.com/end-session"):
            client.post("/logout", follow_redirects=False)
        assert db.get_setting(f"oidc_id_token_{session_id}") is None

    def test_oidc_logout_falls_back_on_discovery_failure(self, client, mock_db):
        """If OIDC discovery fails, logout falls back to /login."""
        self._create_oidc_session(mock_db, client)
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/application/o/sixtyops/",
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri="https://sixtyops.example.com/auth/oidc/callback",
            allowed_group="admins",
        ))
        with patch("updater.app._get_oidc_end_session_url", new_callable=AsyncMock,
                    return_value=None):
            resp = client.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_local_user_logout_unchanged(self, authed_client):
        """Local auth user logout goes to /login (no OIDC redirect)."""
        resp = authed_client.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_oidc_logout_always_deletes_session(self, client, mock_db):
        """Session is always deleted from DB even when OIDC redirect happens."""
        from updater import database as db
        session_id = self._create_oidc_session(mock_db, client)
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/application/o/sixtyops/",
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri="https://sixtyops.example.com/auth/oidc/callback",
            allowed_group="admins",
        ))
        with patch("updater.app._get_oidc_end_session_url", new_callable=AsyncMock,
                    return_value="https://auth.example.com/end-session"):
            client.post("/logout", follow_redirects=False)
        assert db.get_session(session_id) is None


class TestEnsureOIDCUserRolePersistence:
    """Regression tests: manually set OIDC user roles must persist across logins
    when no admin_group is configured. When admin_group IS configured, the IdP
    is the source of truth and roles re-sync on every login.
    """

    def _set_config(self, admin_group=""):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="users",
            admin_group=admin_group,
        ))

    def test_creates_user_with_oidc_default_role_when_no_admin_group(self, mock_db):
        """First login creates user with oidc_default_role when admin_group unset."""
        from updater import database as db
        self._set_config(admin_group="")
        db.set_setting("oidc_default_role", "viewer")

        user = ensure_oidc_user("new@example.com", ["users"])
        assert user["role"] == "viewer"
        assert user["auth_method"] == "oidc"

    def test_manual_role_override_persists_when_no_admin_group(self, mock_db):
        """Regression: when admin_group is unset, an admin's manual role change
        must NOT be reverted on the user's next login.
        """
        from updater import database as db
        self._set_config(admin_group="")
        db.set_setting("oidc_default_role", "viewer")

        user = ensure_oidc_user("isaac@example.com", ["users"])
        assert user["role"] == "viewer"

        db.update_user(user["id"], role="admin")
        assert db.get_user("isaac@example.com")["role"] == "admin"

        user = ensure_oidc_user("isaac@example.com", ["users"])
        assert user["role"] == "admin", (
            "Manual role override must persist when admin_group is not configured"
        )

    def test_manual_role_override_persists_when_groups_change(self, mock_db):
        """Even if the IdP returns different groups on each login, no admin_group
        means the stored role wins.
        """
        from updater import database as db
        self._set_config(admin_group="")
        db.set_setting("oidc_default_role", "viewer")

        ensure_oidc_user("isaac@example.com", ["users"])
        user = db.get_user("isaac@example.com")
        db.update_user(user["id"], role="operator")

        ensure_oidc_user("isaac@example.com", ["different-group", "another"])
        assert db.get_user("isaac@example.com")["role"] == "operator"

    def test_manual_role_override_persists_when_groups_missing(self, mock_db):
        """Login with no groups claim still preserves the manual role."""
        from updater import database as db
        self._set_config(admin_group="")
        db.set_setting("oidc_default_role", "viewer")

        ensure_oidc_user("isaac@example.com", [])
        user = db.get_user("isaac@example.com")
        db.update_user(user["id"], role="admin")

        ensure_oidc_user("isaac@example.com", None)
        assert db.get_user("isaac@example.com")["role"] == "admin"

    def test_admin_group_promotes_member_on_every_login(self, mock_db):
        """When admin_group is set and user is in it, role is forced to admin."""
        from updater import database as db
        self._set_config(admin_group="sixtyops-admins")

        user_id = db.create_user("alice@example.com", None, "viewer", "oidc")

        ensure_oidc_user("alice@example.com", ["users", "sixtyops-admins"])
        assert db.get_user_by_id(user_id)["role"] == "admin"

    def test_admin_group_demotes_non_member_on_every_login(self, mock_db):
        """When admin_group is set and user is NOT in it, role is forced to viewer
        even if an admin manually promoted them in the UI. IdP wins.
        """
        from updater import database as db
        self._set_config(admin_group="sixtyops-admins")

        user_id = db.create_user("bob@example.com", None, "admin", "oidc")

        ensure_oidc_user("bob@example.com", ["users"])
        assert db.get_user_by_id(user_id)["role"] == "viewer"

    def test_local_users_unaffected_by_oidc_login(self, mock_db):
        """ensure_oidc_user must not touch a row whose auth_method is 'local'."""
        from updater import database as db
        self._set_config(admin_group="sixtyops-admins")

        user_id = db.create_user("admin2@example.com", "$bcrypt$hash", "admin", "local")

        ensure_oidc_user("admin2@example.com", ["users"])
        assert db.get_user_by_id(user_id)["role"] == "admin"
        assert db.get_user_by_id(user_id)["auth_method"] == "local"


class TestUsersListEndpoint:
    """Regression tests for the GET /api/users response shape used by the
    Local Users panel to know whether to disable the role dropdown.
    """

    def test_response_includes_oidc_admin_group_flag_false_by_default(self, authed_client):
        resp = authed_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        assert data.get("oidc_admin_group_configured") is False

    def test_response_flag_true_when_admin_group_configured(self, authed_client, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="users",
            admin_group="sixtyops-admins",
        ))
        resp = authed_client.get("/api/users")
        assert resp.status_code == 200
        assert resp.json()["oidc_admin_group_configured"] is True
