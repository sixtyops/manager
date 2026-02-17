"""Tests for OIDC/SSO authentication configuration and flow."""

import os
from unittest.mock import patch, MagicMock

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
            "OIDC_PROVIDER_URL": "https://authentik.example.com/application/o/tachyon/",
            "OIDC_CLIENT_ID": "test-client-id",
            "OIDC_CLIENT_SECRET": "test-secret",
            "OIDC_REDIRECT_URI": "https://tachyon.example.com/auth/oidc/callback",
            "OIDC_ALLOWED_GROUP": "tachyon-admins",
        }):
            config = get_oidc_config()
            assert config.enabled is False
            assert config.provider_url == "https://authentik.example.com/application/o/tachyon/"
            assert config.client_id == "test-client-id"
            assert config.allowed_group == "tachyon-admins"

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
                redirect_uri="https://tachyon.example.com/auth/oidc/callback",
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
            allowed_group="tachyon-admins",
        ))
        result = authenticate_oidc_user("admin@example.com", ["users", "tachyon-admins"])
        assert result == "admin@example.com"

    def test_denied_wrong_group(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="tachyon-admins",
        ))
        result = authenticate_oidc_user("user@example.com", ["users", "other-group"])
        assert result is None

    def test_denied_no_groups(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="tachyon-admins",
        ))
        result = authenticate_oidc_user("user@example.com", [])
        assert result is None

    def test_denied_when_disabled(self, mock_db):
        set_oidc_config(OIDCConfig(enabled=False, allowed_group="tachyon-admins"))
        result = authenticate_oidc_user("admin@example.com", ["tachyon-admins"])
        assert result is None

    def test_denied_no_allowed_group_configured(self, mock_db):
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/",
            client_id="c",
            client_secret="s",
            allowed_group="",
        ))
        result = authenticate_oidc_user("admin@example.com", ["tachyon-admins"])
        assert result is None


class TestOIDCRoutes:
    def test_oidc_login_redirect_when_disabled(self, client):
        """When OIDC is disabled, /auth/oidc/login redirects to /login."""
        resp = client.get("/auth/oidc/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_oidc_callback_no_state(self, client):
        """Callback without state redirects to login with error."""
        resp = client.get("/auth/oidc/callback", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=oidc_denied" in resp.headers["location"]

    def test_oidc_callback_invalid_state(self, client):
        """Callback with unknown state redirects to login with error."""
        resp = client.get("/auth/oidc/callback?code=abc&state=invalid", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=invalid_state" in resp.headers["location"]

    def test_oidc_callback_error_param(self, client):
        """Callback with error parameter from provider."""
        resp = client.get("/auth/oidc/callback?error=access_denied", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=oidc_denied" in resp.headers["location"]

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
