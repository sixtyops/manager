"""Tests for audit logging and API token authentication."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


class TestAuditLog:
    """Tests for audit log database operations and API."""

    def test_log_audit_creates_entry(self, mock_db):
        from updater import database as db
        db.log_audit("admin", "auth.login", None, None, None, "127.0.0.1")

        rows = mock_db.execute("SELECT * FROM audit_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["username"] == "admin"
        assert rows[0]["action"] == "auth.login"
        assert rows[0]["ip_address"] == "127.0.0.1"

    def test_log_audit_with_target(self, mock_db):
        from updater import database as db
        db.log_audit("admin", "device.add", "ap", "192.168.1.1", "model=TNA-300", "10.0.0.1")

        rows = mock_db.execute("SELECT * FROM audit_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["target_type"] == "ap"
        assert rows[0]["target_id"] == "192.168.1.1"
        assert rows[0]["details"] == "model=TNA-300"

    def test_get_audit_log_returns_entries(self, mock_db):
        from updater import database as db
        db.log_audit("admin", "auth.login", None, None, None, "127.0.0.1")
        db.log_audit("admin", "device.add", "ap", "192.168.1.1", None, None)

        entries = db.get_audit_log()
        assert len(entries) == 2

    def test_get_audit_log_filter_by_username(self, mock_db):
        from updater import database as db
        db.log_audit("admin", "auth.login", None, None, None, None)
        db.log_audit("operator1", "device.add", "ap", "1.2.3.4", None, None)

        entries = db.get_audit_log(username="admin")
        assert len(entries) == 1
        assert entries[0]["username"] == "admin"

    def test_get_audit_log_filter_by_action(self, mock_db):
        from updater import database as db
        db.log_audit("admin", "auth.login", None, None, None, None)
        db.log_audit("admin", "device.add", "ap", "1.2.3.4", None, None)

        entries = db.get_audit_log(action="device.add")
        assert len(entries) == 1
        assert entries[0]["action"] == "device.add"

    def test_get_audit_log_pagination(self, mock_db):
        from updater import database as db
        for i in range(10):
            db.log_audit("admin", f"action.{i}", None, None, None, None)

        page1 = db.get_audit_log(limit=3, offset=0)
        page2 = db.get_audit_log(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0]["action"] != page2[0]["action"]

    def test_get_audit_log_count(self, mock_db):
        from updater import database as db
        db.log_audit("admin", "auth.login", None, None, None, None)
        db.log_audit("admin", "auth.login", None, None, None, None)
        db.log_audit("operator1", "device.add", None, None, None, None)

        assert db.get_audit_log_count() == 3
        assert db.get_audit_log_count(username="admin") == 2
        assert db.get_audit_log_count(action="device.add") == 1

    def test_cleanup_old_audit_log(self, mock_db):
        from updater import database as db
        # Insert an old entry
        old_date = (datetime.now() - timedelta(days=100)).isoformat()
        mock_db.execute(
            "INSERT INTO audit_log (username, action, created_at) VALUES (?, ?, ?)",
            ("admin", "old.action", old_date),
        )
        db.log_audit("admin", "new.action", None, None, None, None)
        mock_db.commit()

        db.cleanup_old_audit_log(max_age_days=90)
        entries = db.get_audit_log()
        assert len(entries) == 1
        assert entries[0]["action"] == "new.action"

    def test_get_audit_log_limit_capped(self, mock_db):
        from updater import database as db
        entries = db.get_audit_log(limit=9999)
        # Should not error; internally capped to 500
        assert isinstance(entries, list)

    def test_audit_log_api_requires_admin(self, authed_client):
        resp = authed_client.get("/api/audit-log")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total" in data

    def test_audit_log_api_denied_for_viewer(self, viewer_client):
        resp = viewer_client.get("/api/audit-log")
        assert resp.status_code == 403

    def test_login_creates_audit_entry(self, client, mock_db):
        """Login should create an audit log entry."""
        resp = client.post("/login", data={"username": "admin", "password": "testpass123"})
        rows = mock_db.execute(
            "SELECT * FROM audit_log WHERE action = 'auth.login'"
        ).fetchall()
        assert len(rows) >= 1


class TestApiTokens:
    """Tests for API token CRUD and Bearer authentication."""

    def test_generate_api_token_format(self):
        from updater.auth import generate_api_token
        token, token_hash, token_prefix = generate_api_token()
        assert token.startswith("tach_")
        assert len(token) > 20
        assert token_prefix.endswith("...")
        assert len(token_hash) == 64  # SHA-256 hex

    def test_hash_api_token_deterministic(self):
        from updater.auth import hash_api_token
        h1 = hash_api_token("test_token_123")
        h2 = hash_api_token("test_token_123")
        assert h1 == h2

    def test_create_and_list_tokens(self, mock_db):
        from updater import database as db
        from updater.auth import generate_api_token, hash_api_token

        token, token_hash, token_prefix = generate_api_token()
        token_id = db.create_api_token("My Token", token_hash, token_prefix, user_id=1)
        assert token_id > 0

        tokens = db.list_api_tokens(user_id=1)
        assert len(tokens) == 1
        assert tokens[0]["name"] == "My Token"
        assert tokens[0]["token_prefix"] == token_prefix
        # Token hash should NOT be in the list response
        assert "token_hash" not in tokens[0]

    def test_get_token_by_hash(self, mock_db):
        from updater import database as db
        from updater.auth import hash_api_token

        token_hash = hash_api_token("test_token")
        db.create_api_token("Test", token_hash, "test_...", user_id=1)

        found = db.get_api_token_by_hash(token_hash)
        assert found is not None
        assert found["name"] == "Test"

    def test_get_token_by_hash_not_found(self, mock_db):
        from updater import database as db
        assert db.get_api_token_by_hash("nonexistent") is None

    def test_delete_token(self, mock_db):
        from updater import database as db
        from updater.auth import hash_api_token

        token_id = db.create_api_token("Del", hash_api_token("t1"), "t1...", user_id=1)
        assert db.delete_api_token(token_id)
        assert db.get_api_token_by_hash(hash_api_token("t1")) is None

    def test_delete_token_scoped_to_user(self, mock_db):
        from updater import database as db
        from updater.auth import hash_api_token

        token_id = db.create_api_token("Own", hash_api_token("t2"), "t2...", user_id=1)
        # Wrong user can't delete
        assert not db.delete_api_token(token_id, user_id=999)
        # Right user can
        assert db.delete_api_token(token_id, user_id=1)

    def test_cleanup_expired_tokens(self, mock_db):
        from updater import database as db
        from updater.auth import hash_api_token

        past = (datetime.now() - timedelta(days=1)).isoformat()
        future = (datetime.now() + timedelta(days=30)).isoformat()
        db.create_api_token("Expired", hash_api_token("exp"), "exp...", user_id=1, expires_at=past)
        db.create_api_token("Valid", hash_api_token("val"), "val...", user_id=1, expires_at=future)

        db.cleanup_expired_api_tokens()
        tokens = db.list_api_tokens()
        assert len(tokens) == 1
        assert tokens[0]["name"] == "Valid"

    def test_bearer_auth_works(self, client, mock_db):
        """Bearer token should authenticate API requests."""
        from updater import database as db
        from updater.auth import generate_api_token

        token, token_hash, token_prefix = generate_api_token()
        db.create_api_token("Bearer Test", token_hash, token_prefix, user_id=1)

        resp = client.get("/api/aps", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_bearer_auth_invalid_token(self, client, mock_db):
        """Invalid bearer token should return 401."""
        resp = client.get("/api/aps", headers={"Authorization": "Bearer invalid_token_here"})
        assert resp.status_code == 401

    def test_bearer_auth_expired_token(self, client, mock_db):
        """Expired bearer token should return 401."""
        from updater import database as db
        from updater.auth import generate_api_token

        token, token_hash, token_prefix = generate_api_token()
        past = (datetime.now() - timedelta(days=1)).isoformat()
        db.create_api_token("Expired", token_hash, token_prefix, user_id=1, expires_at=past)

        resp = client.get("/api/aps", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_bearer_auth_disabled_user(self, client, mock_db):
        """Token for a disabled user should return 401."""
        from updater import database as db
        from updater.auth import generate_api_token

        # Create a user and disable them
        user_id = db.create_user("tokenuser", None, "operator", "local")
        db.update_user(user_id, enabled=False)

        token, token_hash, token_prefix = generate_api_token()
        db.create_api_token("Disabled", token_hash, token_prefix, user_id=user_id)

        resp = client.get("/api/aps", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_token_crud_api(self, authed_client, mock_db):
        """Test create and delete token via API."""
        # Create
        resp = authed_client.post("/api/tokens", json={"name": "CI Token", "scopes": "read"})
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["token"].startswith("tach_")
        assert data["name"] == "CI Token"
        token_id = data["id"]

        # List
        resp = authed_client.get("/api/tokens")
        assert resp.status_code == 200
        tokens = resp.json()["tokens"]
        assert any(t["id"] == token_id for t in tokens)

        # Delete
        resp = authed_client.delete(f"/api/tokens/{token_id}")
        assert resp.status_code == 200

        # Verify gone
        resp = authed_client.get("/api/tokens")
        tokens = resp.json()["tokens"]
        assert not any(t["id"] == token_id for t in tokens)

    def test_token_create_validation(self, authed_client):
        """Token creation should validate inputs."""
        # Missing name
        resp = authed_client.post("/api/tokens", json={"name": ""})
        assert resp.status_code == 400

        # Name too long
        resp = authed_client.post("/api/tokens", json={"name": "x" * 101})
        assert resp.status_code == 400

        # Invalid scopes
        resp = authed_client.post("/api/tokens", json={"name": "test", "scopes": "admin"})
        assert resp.status_code == 400

        # Invalid expires_days
        resp = authed_client.post("/api/tokens", json={"name": "test", "expires_days": 999})
        assert resp.status_code == 400

    def test_token_creates_audit_entry(self, authed_client, mock_db):
        """Token CRUD should create audit log entries."""
        resp = authed_client.post("/api/tokens", json={"name": "Audited"})
        assert resp.status_code == 200

        rows = mock_db.execute(
            "SELECT * FROM audit_log WHERE action = 'token.create'"
        ).fetchall()
        assert len(rows) == 1

    def test_token_delete_not_found(self, authed_client):
        resp = authed_client.delete("/api/tokens/99999")
        assert resp.status_code == 404

    def test_update_last_used(self, mock_db):
        from updater import database as db
        from updater.auth import hash_api_token

        token_id = db.create_api_token("Used", hash_api_token("t3"), "t3...", user_id=1)
        db.update_api_token_last_used(token_id)

        token = db.get_api_token_by_hash(hash_api_token("t3"))
        assert token["last_used_at"] is not None
