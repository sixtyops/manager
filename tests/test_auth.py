"""Tests for updater.auth and login/logout flows."""

import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
import bcrypt as _bcrypt

from updater.auth import authenticate_local, authenticate


class TestLocalAuth:
    def test_valid_creds(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            result = authenticate_local("admin", "secret")
            assert result is not None
            assert result["username"] == "admin"
            assert result["role"] == "admin"

    def test_wrong_password(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            assert authenticate_local("admin", "wrong") is None

    def test_wrong_username(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            assert authenticate_local("notadmin", "secret") is None

    def test_no_env_vars(self):
        env = os.environ.copy()
        env.pop("ADMIN_USERNAME", None)
        env.pop("ADMIN_PASSWORD", None)
        with patch.dict(os.environ, env, clear=True):
            assert authenticate_local("admin", "secret") is None

    def test_bcrypt_password(self):
        hashed = _bcrypt.hashpw(b"mysecret", _bcrypt.gensalt()).decode()
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": hashed}):
            assert authenticate_local("admin", "mysecret") is not None
            assert authenticate_local("admin", "wrong") is None


class TestAuthenticate:
    def test_local_success(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "pass123"}):
            result = authenticate("admin", "pass123")
            assert result is not None
            assert result["username"] == "admin"

    def test_failure(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "pass123"}):
            result = authenticate("admin", "wrong")
            assert result is None


class TestLoginFlow:
    def test_get_login_page(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "Sign in" in resp.text

    def test_post_valid_creds(self, client):
        resp = client.post("/login", data={"username": "admin", "password": "testpass123"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert "session_id" in resp.cookies

    def test_post_invalid_creds(self, client):
        resp = client.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False)
        assert resp.status_code == 401
        assert "Invalid" in resp.text

    def test_post_rate_limited_after_repeated_failures(self, client):
        import updater.app as app_mod

        old_limit = app_mod.LOGIN_RATE_LIMIT
        old_window = app_mod.AUTH_RATE_WINDOW
        app_mod.LOGIN_RATE_LIMIT = 2
        app_mod.AUTH_RATE_WINDOW = 60
        app_mod._auth_rate_attempts.clear()
        try:
            client.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False)
            client.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False)
            resp = client.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False)
            assert resp.status_code == 429
            assert "Too many sign-in attempts" in resp.text
        finally:
            app_mod.LOGIN_RATE_LIMIT = old_limit
            app_mod.AUTH_RATE_WINDOW = old_window
            app_mod._auth_rate_attempts.clear()

    def test_protected_page_redirect(self, client):
        resp = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 303

    def test_protected_api_401(self, client):
        resp = client.get("/api/sites")
        assert resp.status_code == 401

    def test_authed_page_access(self, authed_client):
        resp = authed_client.get("/", headers={"accept": "text/html"})
        assert resp.status_code == 200

    def test_authed_api_access(self, authed_client):
        resp = authed_client.get("/api/sites")
        assert resp.status_code == 200

    def test_logout(self, authed_client):
        resp = authed_client.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
