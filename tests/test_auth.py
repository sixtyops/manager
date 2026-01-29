"""Tests for updater.auth and login/logout flows."""

import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
import bcrypt as _bcrypt

from updater.auth import authenticate_local, authenticate_radius, authenticate


class TestLocalAuth:
    def test_valid_creds(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            assert authenticate_local("admin", "secret") is True

    def test_wrong_password(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            assert authenticate_local("admin", "wrong") is False

    def test_wrong_username(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}):
            assert authenticate_local("notadmin", "secret") is False

    def test_no_env_vars(self):
        env = os.environ.copy()
        env.pop("ADMIN_USERNAME", None)
        env.pop("ADMIN_PASSWORD", None)
        with patch.dict(os.environ, env, clear=True):
            assert authenticate_local("admin", "secret") is False

    def test_bcrypt_password(self):
        hashed = _bcrypt.hashpw(b"mysecret", _bcrypt.gensalt()).decode()
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": hashed}):
            assert authenticate_local("admin", "mysecret") is True
            assert authenticate_local("admin", "wrong") is False


class TestRadiusAuth:
    def test_not_configured(self):
        env = os.environ.copy()
        env.pop("RADIUS_SERVER", None)
        env.pop("RADIUS_SECRET", None)
        with patch.dict(os.environ, env, clear=True):
            assert authenticate_radius("user", "pass") is False

    def test_access_accept(self):
        from pyrad import packet as pyrad_packet

        mock_reply = MagicMock()
        mock_reply.code = pyrad_packet.AccessAccept

        mock_req = MagicMock()
        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value
        mock_client_instance.CreateAuthPacket.return_value = mock_req
        mock_client_instance.SendPacket.return_value = mock_reply

        mock_dict_cls = MagicMock()

        with patch.dict(os.environ, {"RADIUS_SERVER": "127.0.0.1", "RADIUS_SECRET": "testing123"}), \
             patch.dict("sys.modules", {}), \
             patch("pyrad.client.Client", mock_client_cls), \
             patch("pyrad.dictionary.Dictionary", mock_dict_cls):
            assert authenticate_radius("user", "pass") is True

    def test_access_reject(self):
        from pyrad import packet as pyrad_packet

        mock_reply = MagicMock()
        mock_reply.code = pyrad_packet.AccessReject

        mock_req = MagicMock()
        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value
        mock_client_instance.CreateAuthPacket.return_value = mock_req
        mock_client_instance.SendPacket.return_value = mock_reply

        mock_dict_cls = MagicMock()

        with patch.dict(os.environ, {"RADIUS_SERVER": "127.0.0.1", "RADIUS_SECRET": "testing123"}), \
             patch("pyrad.client.Client", mock_client_cls), \
             patch("pyrad.dictionary.Dictionary", mock_dict_cls):
            assert authenticate_radius("user", "wrongpass") is False

    def test_unreachable(self):
        with patch.dict(os.environ, {"RADIUS_SERVER": "127.0.0.1", "RADIUS_SECRET": "testing123"}):
            result = authenticate_radius("user", "pass")
            assert result is False


class TestAuthenticate:
    def test_local_success(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "pass123"}):
            result = authenticate("admin", "pass123")
            assert result == "admin"

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
