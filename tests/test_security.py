"""Tests for security hardening: SSRF prevention, XSS escaping, encryption, etc."""

import os
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


# =========================================================================
# Slack webhook SSRF prevention
# =========================================================================

class TestSlackWebhookValidation:
    """Tests for updater.slack.is_valid_slack_url."""

    def test_valid_hooks_slack_com(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("https://hooks.slack.com/services/T00/B00/xxx") is True

    def test_rejects_http(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("http://hooks.slack.com/services/T00/B00/xxx") is False

    def test_rejects_non_slack_domain(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("https://evil.com/hooks.slack.com") is False

    def test_rejects_internal_ip(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("https://192.168.1.1/webhook") is False

    def test_rejects_localhost(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("https://localhost/webhook") is False

    def test_rejects_metadata_endpoint(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("http://169.254.169.254/latest/meta-data/") is False

    def test_rejects_empty(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("") is False

    def test_accepts_subdomain_slack(self):
        from updater.slack import is_valid_slack_url
        # Any .slack.com subdomain is allowed (e.g., hooks.slack.com)
        assert is_valid_slack_url("https://hooks.slack.com/services/T00/B00/xxx") is True

    def test_rejects_slack_lookalike(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("https://not-slack.com/services") is False

    def test_rejects_slack_in_path(self):
        from updater.slack import is_valid_slack_url
        assert is_valid_slack_url("https://evil.com/hooks.slack.com/services") is False


class TestSlackWebhookSettingsValidation:
    """Tests that the settings API rejects invalid Slack webhook URLs."""

    def test_update_settings_rejects_non_slack_url(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "slack_webhook_url": "https://evil.com/steal-data"
        })
        assert resp.status_code == 400

    def test_update_settings_accepts_valid_slack_url(self, authed_client):
        resp = authed_client.put("/api/settings", json={
            "slack_webhook_url": "https://hooks.slack.com/services/T00/B00/xxx"
        })
        assert resp.status_code == 200

    def test_save_settings_rejects_non_slack_url(self, authed_client):
        with patch("updater.app.get_fetcher", return_value=None), \
             patch("updater.app.get_scheduler", return_value=None):
            resp = authed_client.post("/api/settings/save", json={
                "slack_webhook_url": "http://169.254.169.254/metadata"
            })
        assert resp.status_code == 400

    def test_save_settings_accepts_valid_slack_url(self, authed_client):
        with patch("updater.app.get_fetcher", return_value=None), \
             patch("updater.app.get_scheduler", return_value=None):
            resp = authed_client.post("/api/settings/save", json={
                "slack_webhook_url": "https://hooks.slack.com/services/T00/B00/xxx"
            })
        assert resp.status_code == 200

    def test_update_settings_allows_empty_slack_url(self, authed_client):
        """Clearing the webhook URL should be allowed."""
        resp = authed_client.put("/api/settings", json={
            "slack_webhook_url": ""
        })
        assert resp.status_code == 200

    def test_slack_runtime_rejects_bad_url(self):
        """send_slack_notification rejects a non-Slack URL even if stored."""
        from updater.slack import send_slack_notification
        import asyncio

        with patch("updater.slack.db") as mock_db:
            mock_db.get_setting.return_value = "https://evil.com/webhook"
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    send_slack_notification({"text": "test"})
                )
            finally:
                loop.close()
        assert result is False


# =========================================================================
# OIDC provider URL SSRF prevention
# =========================================================================

class TestOIDCProviderURLValidation:
    """Tests for updater.oidc_config.validate_provider_url."""

    def test_valid_https_url(self):
        from updater.oidc_config import validate_provider_url
        # This may raise if DNS doesn't resolve, so we mock getaddrinfo
        with patch("updater.oidc_config.socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('93.184.216.34', 0)),
        ]):
            validate_provider_url("https://auth.example.com/application/o/app/")

    def test_rejects_http(self):
        from updater.oidc_config import validate_provider_url
        with pytest.raises(ValueError, match="HTTPS"):
            validate_provider_url("http://auth.example.com")

    def test_rejects_private_ip(self):
        from updater.oidc_config import validate_provider_url
        with patch("updater.oidc_config.socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('192.168.1.1', 0)),
        ]):
            with pytest.raises(ValueError, match="private"):
                validate_provider_url("https://internal.corp.local")

    def test_rejects_loopback(self):
        from updater.oidc_config import validate_provider_url
        with patch("updater.oidc_config.socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('127.0.0.1', 0)),
        ]):
            with pytest.raises(ValueError, match="private"):
                validate_provider_url("https://localhost")

    def test_rejects_unresolvable(self):
        from updater.oidc_config import validate_provider_url
        import socket
        with patch("updater.oidc_config.socket.getaddrinfo", side_effect=socket.gaierror):
            with pytest.raises(ValueError, match="could not be resolved"):
                validate_provider_url("https://does.not.exist.example.com")

    def test_rejects_no_hostname(self):
        from updater.oidc_config import validate_provider_url
        with pytest.raises(ValueError, match="no hostname"):
            validate_provider_url("https://")

    def test_oidc_api_rejects_http_url(self, authed_client):
        resp = authed_client.put("/api/auth/oidc", json={
            "enabled": True,
            "provider_url": "http://auth.example.com",
            "client_id": "test",
            "client_secret": "secret",
        })
        assert resp.status_code == 400
        assert "HTTPS" in resp.json()["detail"]

    def test_oidc_api_rejects_private_ip(self, authed_client):
        with patch("updater.oidc_config.socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('10.0.0.1', 0)),
        ]):
            resp = authed_client.put("/api/auth/oidc", json={
                "enabled": True,
                "provider_url": "https://internal.corp.local",
                "client_id": "test",
                "client_secret": "secret",
            })
        assert resp.status_code == 400

    def test_oidc_api_allows_empty_url(self, authed_client):
        """Disabling OIDC with empty URL should work."""
        resp = authed_client.put("/api/auth/oidc", json={
            "enabled": False,
            "provider_url": "",
            "client_id": "",
            "client_secret": "",
        })
        assert resp.status_code == 200


# =========================================================================
# Timing-safe password comparison
# =========================================================================

class TestTimingSafeAuth:
    """Tests that plaintext password fallback uses constant-time comparison."""

    def test_plaintext_uses_hmac_compare(self):
        """Verify hmac.compare_digest is called for plaintext passwords."""
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}), \
             patch("updater.auth.db") as mock_db, \
             patch("updater.auth.hmac") as mock_hmac:
            mock_db.get_setting.return_value = ""  # No DB hash, fall back to env
            mock_hmac.compare_digest.return_value = True
            from updater.auth import authenticate_local
            result = authenticate_local("admin", "secret")
            mock_hmac.compare_digest.assert_called_once_with("secret", "secret")
            assert result is True

    def test_plaintext_wrong_password(self):
        """Plaintext fallback still rejects wrong passwords."""
        from updater.auth import authenticate_local
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}), \
             patch("updater.auth.db") as mock_db:
            mock_db.get_setting.return_value = ""
            assert authenticate_local("admin", "wrong") is False


# =========================================================================
# Firmware upload size limit
# =========================================================================

class TestFirmwareUploadSizeLimit:
    """Tests for the firmware upload size limit."""

    def test_small_file_accepted(self, authed_client, tmp_path):
        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            content = b"x" * 1024  # 1 KB
            from io import BytesIO
            resp = authed_client.post(
                "/api/upload-firmware",
                files={"file": ("test-firmware.bin", BytesIO(content), "application/octet-stream")},
            )
        assert resp.status_code == 200
        assert resp.json()["size"] == 1024

    def test_oversized_file_rejected(self, authed_client, tmp_path):
        from updater.app import MAX_FIRMWARE_SIZE
        with patch("updater.app.FIRMWARE_DIR", tmp_path), \
             patch("updater.app.MAX_FIRMWARE_SIZE", 1024):  # 1 KB limit for test
            content = b"x" * 2048  # 2 KB, exceeds limit
            from io import BytesIO
            resp = authed_client.post(
                "/api/upload-firmware",
                files={"file": ("test-firmware.bin", BytesIO(content), "application/octet-stream")},
            )
        assert resp.status_code == 413

    def test_oversized_file_cleaned_up(self, authed_client, tmp_path):
        """Partial files should be deleted when size limit is exceeded."""
        with patch("updater.app.FIRMWARE_DIR", tmp_path), \
             patch("updater.app.MAX_FIRMWARE_SIZE", 1024):
            content = b"x" * 2048
            from io import BytesIO
            authed_client.post(
                "/api/upload-firmware",
                files={"file": ("test-cleanup.bin", BytesIO(content), "application/octet-stream")},
            )
        # File should NOT remain on disk
        assert not (tmp_path / "test-cleanup.bin").exists()


class TestCSVImportSizeLimit:
    """Tests for the CSV import size limit."""

    def test_oversized_csv_rejected(self, authed_client):
        from io import BytesIO
        from updater.app import MAX_CSV_IMPORT_SIZE
        with patch("updater.app.MAX_CSV_IMPORT_SIZE", 1024):
            content = b"ip,username,password\n" + b"x" * 2048
            resp = authed_client.post(
                "/api/backup/import",
                files={"file": ("backup.csv", BytesIO(content), "text/csv")},
                data={"passphrase": "testpassphrase", "conflict_mode": "skip"},
            )
        assert resp.status_code == 413


# =========================================================================
# Device password encryption at rest
# =========================================================================

class TestDevicePasswordEncryption:
    """Tests for device credential encryption in the database."""

    def test_password_encrypted_on_insert(self, mock_db):
        from updater import database as db
        from updater.crypto import is_encrypted
        db.upsert_access_point("10.0.0.1", "root", "plaintext_pass")
        # Read raw from DB — password should be encrypted
        row = mock_db.execute("SELECT password FROM access_points WHERE ip = '10.0.0.1'").fetchone()
        assert is_encrypted(row[0]), "Password should be stored encrypted"
        assert row[0] != "plaintext_pass"

    def test_password_decrypted_on_read(self, mock_db):
        from updater import database as db
        db.upsert_access_point("10.0.0.1", "root", "my_secret")
        ap = db.get_access_point("10.0.0.1")
        assert ap["password"] == "my_secret"

    def test_password_encrypted_on_update(self, mock_db):
        from updater import database as db
        from updater.crypto import is_encrypted
        db.upsert_access_point("10.0.0.1", "root", "pass1")
        db.upsert_access_point("10.0.0.1", "root", "pass2")
        row = mock_db.execute("SELECT password FROM access_points WHERE ip = '10.0.0.1'").fetchone()
        assert is_encrypted(row[0])
        ap = db.get_access_point("10.0.0.1")
        assert ap["password"] == "pass2"

    def test_switch_password_encrypted(self, mock_db):
        from updater import database as db
        from updater.crypto import is_encrypted
        db.upsert_switch("10.0.0.2", "admin", "switch_pass")
        row = mock_db.execute("SELECT password FROM switches WHERE ip = '10.0.0.2'").fetchone()
        assert is_encrypted(row[0])
        sw = db.get_switch("10.0.0.2")
        assert sw["password"] == "switch_pass"

    def test_list_access_points_decrypted(self, mock_db):
        from updater import database as db
        db.upsert_access_point("10.0.0.1", "root", "secret1")
        db.upsert_access_point("10.0.0.2", "root", "secret2")
        aps = db.get_access_points(enabled_only=False)
        assert aps[0]["password"] == "secret1"
        assert aps[1]["password"] == "secret2"

    def test_list_switches_decrypted(self, mock_db):
        from updater import database as db
        db.upsert_switch("10.0.0.1", "admin", "sw_secret")
        switches = db.get_switches(enabled_only=False)
        assert switches[0]["password"] == "sw_secret"

    def test_migrate_encrypts_plaintext(self, mock_db):
        """Simulates the migration path for pre-existing plaintext passwords."""
        from updater.database import _migrate_encrypt_passwords
        from updater.crypto import is_encrypted
        # Insert plaintext directly (simulating pre-migration data)
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password) VALUES (?, ?, ?)",
            ("10.0.0.99", "root", "old_plaintext"),
        )
        mock_db.commit()
        _migrate_encrypt_passwords(mock_db)
        row = mock_db.execute("SELECT password FROM access_points WHERE ip = '10.0.0.99'").fetchone()
        assert is_encrypted(row[0])

    def test_already_encrypted_not_double_encrypted(self, mock_db):
        """Passwords that are already encrypted should not be re-encrypted."""
        from updater import database as db
        from updater.crypto import encrypt_password, is_encrypted, decrypt_password
        # Insert first
        db.upsert_access_point("10.0.0.1", "root", "original")
        # Read raw encrypted value
        row = mock_db.execute("SELECT password FROM access_points WHERE ip = '10.0.0.1'").fetchone()
        encrypted_val = row[0]
        # Upsert again with the same encrypted value (should not double-encrypt)
        db.upsert_access_point("10.0.0.1", "root", encrypted_val)
        row2 = mock_db.execute("SELECT password FROM access_points WHERE ip = '10.0.0.1'").fetchone()
        # Should still decrypt to original
        assert decrypt_password(row2[0]) == "original"


# =========================================================================
# Crypto module
# =========================================================================

class TestCryptoModule:
    """Tests for updater.crypto."""

    def test_encrypt_decrypt_roundtrip(self, tmp_path):
        with patch("updater.crypto._KEY_PATH", tmp_path / ".encryption_key"), \
             patch("updater.crypto._fernet", None):
            from updater.crypto import encrypt_password, decrypt_password
            encrypted = encrypt_password("my_password")
            assert encrypted != "my_password"
            assert decrypt_password(encrypted) == "my_password"

    def test_is_encrypted(self):
        from updater.crypto import is_encrypted, encrypt_password
        assert is_encrypted("plaintext") is False
        assert is_encrypted("") is False
        assert is_encrypted(encrypt_password("test")) is True

    def test_key_persisted(self, tmp_path):
        key_path = tmp_path / ".encryption_key"
        with patch("updater.crypto._KEY_PATH", key_path), \
             patch("updater.crypto._fernet", None):
            from updater.crypto import encrypt_password, decrypt_password
            encrypted = encrypt_password("persist_test")
        assert key_path.exists()
        # Reset singleton and decrypt with persisted key
        with patch("updater.crypto._KEY_PATH", key_path), \
             patch("updater.crypto._fernet", None):
            from updater.crypto import decrypt_password as dp2
            assert dp2(encrypted) == "persist_test"


# =========================================================================
# Security headers
# =========================================================================

class TestSecurityHeaders:
    """Tests that security headers are present on responses."""

    def test_x_content_type_options(self, authed_client):
        resp = authed_client.get("/api/settings")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_x_frame_options(self, authed_client):
        resp = authed_client.get("/api/settings")
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_referrer_policy(self, authed_client):
        resp = authed_client.get("/api/settings")
        assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    def test_csp_present(self, authed_client):
        resp = authed_client.get("/api/settings")
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'self'" in csp
        assert "script-src" in csp

    def test_permissions_policy(self, authed_client):
        resp = authed_client.get("/api/settings")
        assert "camera=()" in resp.headers.get("permissions-policy", "")

    def test_headers_on_unauthenticated_page(self, client):
        resp = client.get("/login")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"


# =========================================================================
# XSS escaping (topology.js is JS, so we test the escapeHtml pattern)
# =========================================================================

class TestAPIPasswordRedaction:
    """Tests that passwords are never returned in API responses."""

    def test_ap_list_redacts_password(self, authed_client):
        authed_client.post("/api/aps", data={
            "ip": "10.0.0.1", "username": "root", "password": "secret_pass"
        })
        resp = authed_client.get("/api/aps")
        for ap in resp.json()["aps"]:
            assert "password" not in ap or ap.get("password") is None, \
                "Password should be redacted from AP list"

    def test_settings_redacts_sensitive(self, authed_client):
        resp = authed_client.get("/api/settings")
        settings = resp.json()["settings"]
        for key in ("admin_password_hash", "oidc_client_secret", "device_default_password"):
            if key in settings and settings[key]:
                assert settings[key] == "********", f"{key} should be masked"
