"""Tests for email notification module."""

from unittest.mock import patch, MagicMock

import pytest


class TestEmailNotifier:
    """Tests for the email notifier module."""

    def test_get_config_defaults(self, mock_db):
        from updater import email_notifier as em
        config = em._get_config()
        assert config["enabled"] is False
        assert config["smtp_host"] == ""
        assert config["smtp_port"] == 587
        assert config["smtp_tls"] is True
        assert config["from_address"] == ""
        assert config["to_addresses"] == ""

    def test_get_config_from_settings(self, mock_db):
        from updater import database as db, email_notifier as em
        db.set_settings({
            "email_enabled": "true",
            "email_smtp_host": "smtp.example.com",
            "email_smtp_port": "465",
            "email_smtp_username": "user@example.com",
            "email_smtp_password": "secret",
            "email_smtp_tls": "false",
            "email_from_address": "noreply@example.com",
            "email_to_addresses": "admin@example.com,ops@example.com",
        })
        config = em._get_config()
        assert config["enabled"] is True
        assert config["smtp_host"] == "smtp.example.com"
        assert config["smtp_port"] == 465
        assert config["smtp_tls"] is False
        assert config["from_address"] == "noreply@example.com"

    def test_parse_recipients(self):
        from updater.email_notifier import _parse_recipients
        assert _parse_recipients("a@b.com,c@d.com") == ["a@b.com", "c@d.com"]
        assert _parse_recipients("a@b.com") == ["a@b.com"]
        assert _parse_recipients("") == []
        assert _parse_recipients("  a@b.com ,  c@d.com  ") == ["a@b.com", "c@d.com"]

    def test_send_email_not_enabled(self, mock_db):
        from updater import email_notifier as em
        config = em._get_config()
        success, msg = em._send_email(config, "Test", "<p>Test</p>", "Test")
        assert success is False
        assert "not enabled" in msg

    def test_send_email_no_host(self, mock_db):
        from updater import email_notifier as em
        config = {
            "enabled": True, "smtp_host": "", "smtp_port": 587,
            "smtp_username": "", "smtp_password": "", "smtp_tls": True,
            "from_address": "a@b.com", "to_addresses": "c@d.com",
        }
        success, msg = em._send_email(config, "Test", "<p>Test</p>", "Test")
        assert success is False
        assert "host" in msg.lower()

    def test_send_email_no_from(self, mock_db):
        from updater import email_notifier as em
        config = {
            "enabled": True, "smtp_host": "smtp.test.com", "smtp_port": 587,
            "smtp_username": "", "smtp_password": "", "smtp_tls": True,
            "from_address": "", "to_addresses": "c@d.com",
        }
        success, msg = em._send_email(config, "Test", "<p>Test</p>", "Test")
        assert success is False
        assert "from" in msg.lower()

    def test_send_email_no_recipients(self, mock_db):
        from updater import email_notifier as em
        config = {
            "enabled": True, "smtp_host": "smtp.test.com", "smtp_port": 587,
            "smtp_username": "", "smtp_password": "", "smtp_tls": True,
            "from_address": "a@b.com", "to_addresses": "",
        }
        success, msg = em._send_email(config, "Test", "<p>Test</p>", "Test")
        assert success is False
        assert "recipient" in msg.lower()

    @patch("updater.email_notifier.smtplib.SMTP")
    def test_send_email_success_with_tls(self, mock_smtp_cls, mock_db):
        from updater import email_notifier as em
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        config = {
            "enabled": True, "smtp_host": "smtp.test.com", "smtp_port": 587,
            "smtp_username": "user", "smtp_password": "pass", "smtp_tls": True,
            "from_address": "a@b.com", "to_addresses": "c@d.com",
        }
        success, msg = em._send_email(config, "Test Subject", "<p>Body</p>", "Body")
        assert success is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user", "pass")
        mock_server.sendmail.assert_called_once()
        mock_server.quit.assert_called_once()

    @patch("updater.email_notifier.smtplib.SMTP")
    def test_send_email_success_without_tls(self, mock_smtp_cls, mock_db):
        from updater import email_notifier as em
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        config = {
            "enabled": True, "smtp_host": "smtp.test.com", "smtp_port": 25,
            "smtp_username": "", "smtp_password": "", "smtp_tls": False,
            "from_address": "a@b.com", "to_addresses": "c@d.com",
        }
        success, msg = em._send_email(config, "Test", "<p>Body</p>", "Body")
        assert success is True
        mock_server.starttls.assert_not_called()
        mock_server.login.assert_not_called()

    @patch("updater.email_notifier.smtplib.SMTP")
    def test_send_email_auth_failure(self, mock_smtp_cls, mock_db):
        import smtplib
        from updater import email_notifier as em
        mock_server = MagicMock()
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth failed")
        mock_smtp_cls.return_value = mock_server

        config = {
            "enabled": True, "smtp_host": "smtp.test.com", "smtp_port": 587,
            "smtp_username": "bad", "smtp_password": "creds", "smtp_tls": True,
            "from_address": "a@b.com", "to_addresses": "c@d.com",
        }
        success, msg = em._send_email(config, "Test", "<p>Body</p>", "Body")
        assert success is False
        assert "authentication" in msg.lower()

    def test_get_status(self, mock_db):
        from updater import email_notifier as em
        status = em.get_status()
        assert status["enabled"] is False
        assert "has_credentials" in status
        assert status["has_credentials"] is False

    def test_send_test_email_not_configured(self, mock_db):
        from updater import email_notifier as em
        success, msg = em.send_test_email()
        assert success is False

    @pytest.mark.asyncio
    async def test_notify_job_completed_disabled(self, mock_db):
        from updater import email_notifier as em
        # Should not raise when disabled
        await em.notify_job_completed(
            job_id="test123", success_count=5, failed_count=0,
            skipped_count=0, cancelled_count=0, duration_seconds=120,
            firmware_name="test.bin",
        )

    @pytest.mark.asyncio
    @patch("updater.email_notifier._send_email")
    async def test_notify_job_completed_sends(self, mock_send, mock_db):
        from updater import database as db, email_notifier as em
        db.set_settings({"email_enabled": "true"})
        mock_send.return_value = (True, "sent")

        await em.notify_job_completed(
            job_id="abc123", success_count=10, failed_count=2,
            skipped_count=1, cancelled_count=0, duration_seconds=300,
            firmware_name="fw-1.0.bin", is_scheduled=True,
        )
        mock_send.assert_called_once()
        args = mock_send.call_args
        subject = args[0][1]
        assert "abc123" in subject
        assert "10/" in subject

    def test_email_status_api(self, authed_client):
        resp = authed_client.get("/api/email/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "smtp_host" in data

    def test_email_test_api(self, authed_client):
        resp = authed_client.post("/api/email/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False  # Not configured
