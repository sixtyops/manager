"""Tests for OIDC/SSO authentication configuration and flow."""

import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from updater.oidc_config import OIDCConfig, get_oidc_config, set_oidc_config, is_oidc_enabled
from updater.auth import authenticate_oidc_user


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
