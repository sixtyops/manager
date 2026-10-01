"""Tests for updater.auth and login/logout flows."""

import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
import bcrypt as _bcrypt

from updater.auth import authenticate_local, authenticate


def _no_db_user():
    """Patch context that removes the DB user so env-var fallback is tested."""
    return (
        patch("updater.auth.db.get_user", return_value=None),
        patch("updater.auth.db.count_admin_users", return_value=0),
        patch("updater.auth.db.get_setting", return_value=""),
    )


class TestLocalAuth:
    def test_valid_creds(self):
        m1, m2, m3 = _no_db_user()
        with m1, m2, m3, patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            result = authenticate_local("admin", "secret")
            assert result is not None
            assert result["username"] == "admin"
            assert result["role"] == "admin"

    def test_wrong_password(self):
        m1, m2, m3 = _no_db_user()
        with m1, m2, m3, patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            assert authenticate_local("admin", "wrong") is None

    def test_wrong_username(self):
        m1, m2, m3 = _no_db_user()
        with m1, m2, m3, patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            assert authenticate_local("notadmin", "secret") is None

    def test_no_env_vars(self):
        m1, m2, m3 = _no_db_user()
        env = os.environ.copy()
        env.pop("ADMIN_USERNAME", None)
        env.pop("ADMIN_PASSWORD", None)
        with m1, m2, m3, patch.dict(os.environ, env, clear=True):
            assert authenticate_local("admin", "secret") is None

    def test_bcrypt_password(self):
        m1, m2, m3 = _no_db_user()
        hashed = _bcrypt.hashpw(b"mysecret", _bcrypt.gensalt()).decode()
        with m1, m2, m3, patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": hashed}):
            assert authenticate_local("admin", "mysecret") is not None
            assert authenticate_local("admin", "wrong") is None


class TestAuthenticate:
    def test_local_success(self):
        m1, m2, m3 = _no_db_user()
        with m1, m2, m3, patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "pass123"}):
            result = authenticate("admin", "pass123")
            assert result is not None
            assert result["username"] == "admin"

    def test_failure(self):
        m1, m2, m3 = _no_db_user()
        with m1, m2, m3, patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "pass123"}):
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


@pytest.fixture
def login_limits(monkeypatch):
    """Isolate counters and control lock time without sleeping."""
    import updater.app as app_mod

    monkeypatch.setattr(app_mod, "_auth_rate_attempts", {})
    monkeypatch.setattr(app_mod, "_login_failures", {})
    monkeypatch.setattr(app_mod, "_login_locks", {})
    clock = MagicMock(return_value=1000.0)
    monkeypatch.setattr(app_mod, "monotonic", clock)
    return clock


def _post_login(client, username="admin", password="wrong"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


class TestUsernameLock:
    @pytest.mark.parametrize("password", ["wrong", "testpass123"])
    def test_eleventh_attempt_locked_and_expires(self, client, login_limits, password):
        from updater import database as db

        for _ in range(10):
            assert _post_login(client).status_code == 401
        response = _post_login(client, password=password)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert "Try again in 60 seconds" in response.text
        assert "session_id" not in response.cookies

        login_limits.return_value = 1059.1
        for _ in range(25):
            response = _post_login(client, password="testpass123")
            assert response.status_code == 429
            assert response.headers["Retry-After"] == "1"
        entries = db.get_audit_log(action="auth.lockout")
        assert len(entries) == 1
        assert entries[0]["username"] == "admin"
        assert entries[0]["ip_address"] == "testclient"
        assert entries[0]["details"] == (
            "10 failed logins in 60 seconds. Locked for 60 seconds.")
        assert db.get_user("admin")["enabled"] == 1

        login_limits.return_value = 1060.0
        response = _post_login(client, password="testpass123")
        assert response.status_code == 303
        assert "session_id" in response.cookies

    def test_failures_follow_username_across_ips(self, client, login_limits, monkeypatch):
        import updater.app as app_mod

        for i in range(10):
            monkeypatch.setattr(app_mod, "_client_ip", lambda request, i=i: f"192.0.2.{i}")
            assert _post_login(client).status_code == 401
        monkeypatch.setattr(app_mod, "_client_ip", lambda request: "192.0.2.100")
        assert _post_login(client, password="testpass123").status_code == 429
        assert _post_login(client, username="someone-else").status_code == 401

    def test_lock_ignores_username_case(self, client, login_limits):
        from updater import database as db

        hashed = _bcrypt.hashpw(b"unit-test-only", _bcrypt.gensalt(rounds=4)).decode()
        db.create_user("operator", hashed, "admin", "local")
        for name in ["operator", "OPERATOR", "Operator", "oPeRaToR", "operator"] * 2:
            assert _post_login(client, username=name).status_code == 401
        for name in ["OPERATOR", "Operator", "operator"]:
            response = _post_login(client, username=name, password="unit-test-only")
            assert response.status_code == 429
            assert "session_id" not in response.cookies

    def test_mixed_case_success_resets_counter(self, client, login_limits):
        import updater.app as app_mod
        from updater import database as db

        hashed = _bcrypt.hashpw(b"unit-test-only", _bcrypt.gensalt(rounds=4)).decode()
        db.create_user("operator", hashed, "admin", "local")
        for _ in range(9):
            assert _post_login(client, username="operator").status_code == 401
        response = _post_login(client, username="OPERATOR", password="unit-test-only")
        assert response.status_code == 303
        assert app_mod._login_failures == {}

    def test_ip_limit_still_blocks_different_usernames(self, client, login_limits):
        import updater.app as app_mod

        for i in range(app_mod.LOGIN_RATE_LIMIT):
            assert _post_login(client, username=f"unknown-{i}").status_code == 401
        response = _post_login(client, password="testpass123")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == str(app_mod.AUTH_RATE_WINDOW)
        assert "Too many sign-in attempts" in response.text

    def test_success_resets_both_counters(self, client, login_limits):
        import updater.app as app_mod

        for _ in range(3):
            for _ in range(9):
                assert _post_login(client).status_code == 401
            assert _post_login(client, password="testpass123").status_code == 303
            assert app_mod._login_failures == {}
            assert app_mod._auth_rate_attempts == {}

    def test_failures_outside_window_expire(self, client, login_limits):
        for _ in range(9):
            assert _post_login(client).status_code == 401
        login_limits.return_value = 1060.0
        assert _post_login(client).status_code == 401
        assert _post_login(client, password="testpass123").status_code == 303

    def test_live_lane_login_pattern(self, client, login_limits):
        from updater import database as db

        password = "unit-test-only"
        hashed = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=4)).decode()
        db.create_user("live-lane", hashed, "admin", "local")
        # The live lane logs in once per session and follows the redirect.
        for _ in range(25):
            client.cookies.clear()
            response = client.post("/login", data={"username": "live-lane", "password": password})
            assert response.status_code == 200
            assert "session_id" in client.cookies
        assert db.get_audit_log(action="auth.lockout") == []

    def test_idle_state_is_removed(self, client, login_limits):
        import updater.app as app_mod

        assert _post_login(client, username="idle").status_code == 401
        for _ in range(10):
            assert _post_login(client).status_code == 401
        login_limits.return_value = 1060.0
        assert _post_login(client, password="testpass123").status_code == 303
        assert app_mod._login_failures == {}
        assert app_mod._login_locks == {}
