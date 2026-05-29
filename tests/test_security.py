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

    def test_allows_private_ip_with_kwarg(self):
        from updater.oidc_config import validate_provider_url
        with patch("updater.oidc_config.socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('192.168.1.1', 0)),
        ]):
            validate_provider_url("https://internal.corp.local", allow_private=True)

    def test_allows_loopback_with_kwarg(self):
        from updater.oidc_config import validate_provider_url
        with patch("updater.oidc_config.socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('127.0.0.1', 0)),
        ]):
            validate_provider_url("https://localhost", allow_private=True)

    def test_allows_private_ip_with_env_var(self):
        from updater.oidc_config import validate_provider_url
        with patch.dict(os.environ, {"OIDC_ALLOW_PRIVATE_IPS": "true"}):
            with patch("updater.oidc_config.socket.getaddrinfo", return_value=[
                (2, 1, 6, '', ('10.0.0.1', 0)),
            ]):
                validate_provider_url("https://internal.corp.local")

    def test_still_rejects_http_with_private_override(self):
        from updater.oidc_config import validate_provider_url
        with pytest.raises(ValueError, match="HTTPS"):
            validate_provider_url("http://internal.corp.local", allow_private=True)

    def test_still_rejects_unresolvable_with_private_override(self):
        from updater.oidc_config import validate_provider_url
        import socket
        with patch("updater.oidc_config.socket.getaddrinfo", side_effect=socket.gaierror):
            with pytest.raises(ValueError, match="could not be resolved"):
                validate_provider_url("https://does.not.exist.example.com", allow_private=True)

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
            mock_db.get_user.return_value = None  # No user in DB
            mock_db.count_admin_users.return_value = 0  # Trigger env fallback
            mock_db.get_setting.return_value = ""  # No DB hash, fall back to env
            mock_hmac.compare_digest.return_value = True
            from updater.auth import authenticate_local
            result = authenticate_local("admin", "secret")
            mock_hmac.compare_digest.assert_called_once_with("secret", "secret")
            assert result is not None

    def test_plaintext_wrong_password(self):
        """Plaintext fallback still rejects wrong passwords."""
        from updater.auth import authenticate_local
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "secret"}), \
             patch("updater.auth.db") as mock_db:
            mock_db.get_user.return_value = None
            mock_db.count_admin_users.return_value = 0
            mock_db.get_setting.return_value = ""
            assert authenticate_local("admin", "wrong") is None


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
# Device config encryption at rest (#35)
# =========================================================================

class TestDeviceConfigEncryption:
    """Tests for device_configs.config_json encryption at rest."""

    def test_config_encrypted_on_save(self, mock_db):
        from updater import database as db
        from updater.crypto import is_encrypted
        cleartext = '{"radius_secret":"hunter2","wpa_psk":"correct horse battery"}'
        db.save_device_config("10.0.0.1", cleartext, "abc123", model="tn-110-prs")
        row = mock_db.execute(
            "SELECT config_json FROM device_configs WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert is_encrypted(row[0]), "config_json must be Fernet-encrypted at rest"
        assert row[0] != cleartext
        assert "hunter2" not in row[0]
        assert "correct horse" not in row[0]

    def test_config_decrypted_on_get_latest(self, mock_db):
        from updater import database as db
        cleartext = '{"foo":"bar"}'
        db.save_device_config("10.0.0.1", cleartext, "h1", model="tn-110-prs")
        latest = db.get_latest_device_config("10.0.0.1")
        assert latest["config_json"] == cleartext

    def test_config_decrypted_on_get_by_id(self, mock_db):
        from updater import database as db
        cleartext = '{"snmp_community":"public"}'
        db.save_device_config("10.0.0.1", cleartext, "h2", model="tn-110-prs")
        row = mock_db.execute(
            "SELECT id FROM device_configs WHERE ip = '10.0.0.1'"
        ).fetchone()
        snap = db.get_device_config_by_id(row[0])
        assert snap["config_json"] == cleartext

    def test_config_decrypted_on_get_all_latest(self, mock_db):
        from updater import database as db
        cleartext_a = '{"name":"A"}'
        cleartext_b = '{"name":"B"}'
        # Seed managed devices so the JOIN in get_all_latest_configs returns
        # both rows (otherwise we'd still see them, since the query is left-
        # joined, but this matches realistic shape).
        db.upsert_access_point("10.0.0.1", "root", "pw")
        db.upsert_access_point("10.0.0.2", "root", "pw")
        db.save_device_config("10.0.0.1", cleartext_a, "ha", model="tn-110-prs")
        db.save_device_config("10.0.0.2", cleartext_b, "hb", model="tn-110-prs")
        latest = db.get_all_latest_configs()
        assert latest["10.0.0.1"]["config_json"] == cleartext_a
        assert latest["10.0.0.2"]["config_json"] == cleartext_b

    def test_hash_is_plaintext(self, mock_db):
        """config_hash stays plaintext so change-detection queries stay cheap."""
        from updater import database as db
        db.save_device_config("10.0.0.1", '{"x":1}', "deadbeef", model="m")
        row = mock_db.execute(
            "SELECT config_hash FROM device_configs WHERE ip = '10.0.0.1'"
        ).fetchone()
        assert row[0] == "deadbeef"
        assert db.get_latest_config_hash("10.0.0.1") == "deadbeef"

    def test_imported_config_encrypted(self, mock_db):
        """insert_imported_device_config (used by backup restore) encrypts on insert."""
        from updater import database as db
        from updater.crypto import is_encrypted
        cleartext = '{"radius_secret":"importsecret"}'
        db.insert_imported_device_config(
            ip="10.0.0.7",
            config_json=cleartext,
            config_hash="h7",
            fetched_at="2026-05-11T10:00:00",
            model="tn-110-prs",
        )
        row = mock_db.execute(
            "SELECT config_json FROM device_configs WHERE ip = '10.0.0.7'"
        ).fetchone()
        assert is_encrypted(row[0])
        assert "importsecret" not in row[0]
        snap = db.get_latest_device_config("10.0.0.7")
        assert snap["config_json"] == cleartext

    def test_imported_config_preserves_metadata(self, mock_db):
        """Backup-restore must preserve fetched_at and recycle-bin fields."""
        from updater import database as db
        db.insert_imported_device_config(
            ip="10.0.0.8",
            config_json='{"x":1}',
            config_hash="h8",
            fetched_at="2026-01-01T00:00:00",
            model="tn-110-prs",
            deleted_at="2026-01-02T00:00:00",
            device_label="AP-Lobby",
        )
        row = mock_db.execute(
            """SELECT fetched_at, deleted_at, device_label
                 FROM device_configs WHERE ip = '10.0.0.8'"""
        ).fetchone()
        assert row[0] == "2026-01-01T00:00:00"
        assert row[1] == "2026-01-02T00:00:00"
        assert row[2] == "AP-Lobby"

    def test_migrate_encrypts_plaintext_rows(self, mock_db):
        """Pre-#35 plaintext rows get encrypted in-place on next init."""
        from updater.database import _migrate_encrypt_device_configs
        from updater.crypto import is_encrypted
        # Insert plaintext directly, simulating a pre-migration row
        mock_db.execute(
            """INSERT INTO device_configs (ip, config_json, config_hash, model, fetched_at)
                 VALUES (?, ?, ?, ?, ?)""",
            ("10.0.0.99", '{"plain":"json"}', "h99", "m", "2026-01-01T00:00:00"),
        )
        mock_db.commit()
        _migrate_encrypt_device_configs(mock_db)
        row = mock_db.execute(
            "SELECT config_json FROM device_configs WHERE ip = '10.0.0.99'"
        ).fetchone()
        assert is_encrypted(row[0])

    def test_migrate_idempotent(self, mock_db):
        """Running the migration twice does not double-encrypt."""
        from updater import database as db
        from updater.database import _migrate_encrypt_device_configs
        cleartext = '{"x":1}'
        db.save_device_config("10.0.0.1", cleartext, "h1")  # already encrypted
        _migrate_encrypt_device_configs(mock_db)
        _migrate_encrypt_device_configs(mock_db)
        # Decrypt should still succeed
        snap = db.get_latest_device_config("10.0.0.1")
        assert snap["config_json"] == cleartext

    def test_legacy_plaintext_row_decrypts_on_read(self, mock_db):
        """Reads tolerate legacy plaintext rows (in case migration hasn't
        run yet on a hot read path)."""
        from updater import database as db
        mock_db.execute(
            """INSERT INTO device_configs (ip, config_json, config_hash, model, fetched_at)
                 VALUES (?, ?, ?, ?, ?)""",
            ("10.0.0.50", '{"legacy":"plaintext"}', "h50", "m", "2026-01-01T00:00:00"),
        )
        mock_db.commit()
        snap = db.get_latest_device_config("10.0.0.50")
        assert snap["config_json"] == '{"legacy":"plaintext"}'


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


# =========================================================================
# CSRF middleware (Origin / Referer enforcement)
# =========================================================================

class TestCSRFMiddleware:
    """Cookie-authenticated state-changing requests must come with a same-
    origin Origin/Referer header. SameSite=Lax alone does not block top-
    level form POSTs from another origin."""

    def test_get_is_unaffected(self, authed_client):
        """Safe methods bypass CSRF entirely."""
        resp = authed_client.get("/api/aps", headers={"Origin": "https://evil.example"})
        assert resp.status_code != 403

    def test_same_origin_post_allowed(self, authed_client):
        """The conftest fixture sets Origin: http://testserver (matches base_url)."""
        resp = authed_client.post("/api/aps", data={
            "ip": "10.0.0.50", "username": "root", "password": "x" * 8
        })
        assert resp.status_code != 403, (
            f"Same-origin POST got CSRF-blocked: {resp.status_code} {resp.text}"
        )

    def test_cross_origin_post_blocked(self, authed_client):
        """Cross-origin POST with a cookie must be rejected."""
        resp = authed_client.post(
            "/api/aps",
            data={"ip": "10.0.0.51", "username": "root", "password": "x" * 8},
            headers={"Origin": "https://attacker.example"},
        )
        assert resp.status_code == 403
        assert "csrf" in resp.text.lower()

    def test_missing_origin_and_referer_blocked(self, authed_client):
        """A cookie-auth POST with neither Origin nor Referer is rejected —
        we have no way to verify the request originated from our own page."""
        resp = authed_client.post(
            "/api/aps",
            data={"ip": "10.0.0.52", "username": "root", "password": "x" * 8},
            headers={"Origin": "", "Referer": ""},
        )
        assert resp.status_code == 403

    def test_referer_fallback_allows_same_origin(self, authed_client):
        """If Origin is missing but Referer matches, allow."""
        resp = authed_client.post(
            "/api/aps",
            data={"ip": "10.0.0.53", "username": "root", "password": "x" * 8},
            headers={"Origin": "", "Referer": "http://testserver/login"},
        )
        assert resp.status_code != 403

    def test_bearer_token_skips_csrf(self, client, mock_db):
        """API-token auth has no cookie credential, so CSRF doesn't apply.
        We just verify the middleware doesn't reject before auth runs."""
        # No real token registered — we just check the response is NOT a
        # CSRF 403 ("missing origin/referer..."). Auth will 401 instead.
        resp = client.post(
            "/api/aps",
            data={"ip": "10.0.0.54", "username": "root", "password": "x" * 8},
            headers={"Authorization": "Bearer tach_notreal"},
        )
        assert resp.status_code == 401
        assert "csrf" not in resp.text.lower()

    def test_anonymous_post_passes_csrf(self, client, mock_db):
        """No session cookie -> nothing to forge against -> CSRF skips."""
        resp = client.post(
            "/login",
            data={"username": "nobody", "password": "wrong"},
            headers={"Origin": ""},
        )
        # 401 from auth, not 403 from CSRF.
        assert resp.status_code != 403


# =========================================================================
# Trusted proxy gating for X-Forwarded-* headers
# =========================================================================

class TestTrustedProxyGate:
    """X-Forwarded-For is only honored when the immediate peer is in the
    trusted-proxy network list (SIXTYOPS_TRUSTED_PROXIES). Untrusted peers
    can otherwise spoof their source IP to bypass per-IP rate limits."""

    def test_loopback_peer_is_trusted_by_default(self):
        from updater.app import _is_trusted_proxy
        assert _is_trusted_proxy("127.0.0.1") is True

    def test_docker_bridge_peer_is_trusted_by_default(self):
        from updater.app import _is_trusted_proxy
        assert _is_trusted_proxy("172.20.0.5") is True

    def test_public_peer_is_not_trusted(self):
        from updater.app import _is_trusted_proxy
        assert _is_trusted_proxy("8.8.8.8") is False

    def test_garbage_peer_is_not_trusted(self):
        from updater.app import _is_trusted_proxy
        assert _is_trusted_proxy("not-an-ip") is False
        assert _is_trusted_proxy("") is False

    def test_env_override_replaces_defaults(self):
        """SIXTYOPS_TRUSTED_PROXIES env var replaces the default network list."""
        from updater.app import _parse_trusted_proxies
        with patch.dict(os.environ, {"SIXTYOPS_TRUSTED_PROXIES": "10.0.0.0/24"}, clear=False):
            nets = _parse_trusted_proxies()
        assert len(nets) == 1
        assert str(nets[0]) == "10.0.0.0/24"

    def test_invalid_cidr_in_env_is_ignored(self):
        """Bad entries in the env var should not crash startup."""
        from updater.app import _parse_trusted_proxies
        with patch.dict(os.environ, {"SIXTYOPS_TRUSTED_PROXIES": "10.0.0.0/24,not-a-cidr"}, clear=False):
            nets = _parse_trusted_proxies()
        assert len(nets) == 1


# =========================================================================
# OIDC state cookie binding (replay protection)
# =========================================================================

class TestOIDCStateCookieBinding:
    """Even if the OIDC state value leaks (referrer, log, link copied from
    a chat), a different browser must not be able to complete the flow.
    The /auth/oidc/login response sets an `oidc_state` cookie that the
    callback validates against the query `state`."""

    def _enable_oidc(self):
        from updater.oidc_config import set_oidc_config, OIDCConfig
        set_oidc_config(OIDCConfig(
            enabled=True,
            provider_url="https://auth.example.com/application/o/sixtyops/",
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri="https://sixtyops.example.com/auth/oidc/callback",
        ))

    def test_callback_rejects_state_without_matching_cookie(self, client, mock_db):
        """A query state with no companion cookie is treated as invalid."""
        from updater import database as db
        self._enable_oidc()
        # Pre-seed a DB state row so the failure must come from cookie
        # mismatch, not from "unknown state".
        import json as _json
        db.set_setting("oidc_state_leaked-state-value", _json.dumps({
            "nonce": "n", "code_verifier": "v",
            "created_at": "2025-01-01T00:00:00",
        }))
        resp = client.get(
            "/auth/oidc/callback?code=abc&state=leaked-state-value",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "invalid_state" in resp.headers["location"]

    def test_callback_rejects_cookie_state_mismatch(self, client, mock_db):
        """Cookie state must match query state exactly."""
        from updater import database as db
        self._enable_oidc()
        import json as _json
        db.set_setting("oidc_state_real-state", _json.dumps({
            "nonce": "n", "code_verifier": "v",
            "created_at": "2025-01-01T00:00:00",
        }))
        client.cookies.set("oidc_state", "different-state")
        resp = client.get(
            "/auth/oidc/callback?code=abc&state=real-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "invalid_state" in resp.headers["location"]


# =========================================================================
# Settings encryption — OIDC id_token / client_secret prefix matching
# =========================================================================

class TestOIDCSettingsEncryption:
    """The settings table holds raw OIDC id_tokens (claims include email
    + group membership) keyed by session id, plus the OIDC client_secret.
    Both must be Fernet-wrapped at rest so a DB read alone doesn't leak
    them."""

    def test_id_token_setting_is_encrypted_in_db(self, mock_db):
        from updater import database as db
        from updater.crypto import is_encrypted
        db.set_setting("oidc_id_token_session-xyz", "eyJ.fake.token.value")
        row = mock_db.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("oidc_id_token_session-xyz",),
        ).fetchone()
        assert row is not None
        assert is_encrypted(row[0]), (
            f"Expected Fernet ciphertext, got: {row[0]!r}"
        )

    def test_id_token_round_trip(self, mock_db):
        """Reads must transparently decrypt — callers never see ciphertext."""
        from updater import database as db
        db.set_setting("oidc_id_token_session-abc", "eyJ.another.token")
        assert db.get_setting("oidc_id_token_session-abc") == "eyJ.another.token"

    def test_oidc_client_secret_is_encrypted_in_db(self, mock_db):
        from updater import database as db
        from updater.crypto import is_encrypted
        db.set_setting("oidc_client_secret", "super-secret-client-creds")
        row = mock_db.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("oidc_client_secret",),
        ).fetchone()
        assert is_encrypted(row[0]), (
            f"Expected Fernet ciphertext, got: {row[0]!r}"
        )


# =========================================================================
# CSRF: Host header carrying a non-default port
# =========================================================================

class TestCSRFAcceptsHostWithPort:
    """Operators commonly publish nginx on a non-standard host port
    (e.g. 8443:443). The browser writes Host: example:8443 and Origin:
    https://example:8443; nginx must forward the full Host header
    ($http_host, not $host) so the CSRF middleware sees matching values.

    These tests pin both halves: the middleware accepts matching
    host:port pairs (#1), and the bundled nginx config uses $http_host
    so the port survives the proxy hop (#2)."""

    def test_middleware_accepts_matching_port_in_origin_and_host(self, authed_client):
        """When the request comes in with Host: testserver:8443, the
        middleware must accept Origin: http://testserver:8443."""
        resp = authed_client.post(
            "/api/aps",
            data={"ip": "10.0.0.60", "username": "root", "password": "x" * 8},
            headers={
                "Host": "testserver:8443",
                "Origin": "http://testserver:8443",
            },
        )
        assert resp.status_code != 403, (
            f"Same-origin POST with non-default port got CSRF-blocked: "
            f"{resp.status_code} {resp.text}"
        )

    def test_bundled_nginx_forwards_full_host_with_port(self):
        """nginx/conf.d/default.conf must use $http_host, not $host —
        $host strips the port and breaks the CSRF Origin check on any
        custom-port publish."""
        from pathlib import Path
        conf = Path(__file__).resolve().parent.parent / "nginx/conf.d/default.conf"
        text = conf.read_text()
        assert "proxy_set_header Host $http_host;" in text, (
            "nginx/conf.d/default.conf must forward Host as $http_host so the "
            "browser-sent port survives the proxy hop"
        )
        assert "proxy_set_header Host $host;" not in text, (
            "nginx/conf.d/default.conf still has 'Host $host' — that strips "
            "the port from the Host header and breaks CSRF on custom ports"
        )

    def test_generated_letsencrypt_config_forwards_full_host_with_port(self):
        """The Let's Encrypt nginx config generated by ssl_manager must
        forward $http_host for the same reason."""
        from updater.ssl_manager import _generate_letsencrypt_nginx_config
        rendered = _generate_letsencrypt_nginx_config("example.com")
        assert "proxy_set_header Host $http_host;" in rendered
        assert "proxy_set_header Host $host;" not in rendered


# =========================================================================
# Fresh-install DB file mode
# =========================================================================

class TestFreshDBFileMode:
    """init_db() must chmod the DB to 0600 even on first run, when the
    file doesn't exist until SQLite creates it inside the get_db() block.
    An earlier version of this fix only chmod'd existing DBs."""

    def test_fresh_init_sets_db_mode_0600(self, tmp_path, monkeypatch):
        from updater import database as db
        target = tmp_path / "freshdata" / "sixtyops.db"
        monkeypatch.setattr(db, "DB_PATH", target)
        assert not target.exists()
        db.init_db()
        assert target.exists(), "init_db should have created the DB file"
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0600 on fresh DB, got {oct(mode)}"
