"""Tests for updater.ssl_manager."""

from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from updater import ssl_manager


class TestGetSSLStatus:
    def test_disabled_returns_defaults(self, mock_db):
        status = ssl_manager.get_ssl_status()
        assert status["enabled"] is False
        assert status["needs_renewal"] is False
        assert status["using_letsencrypt"] is False
        assert status["days_until_expiry"] is None

    def test_enabled_without_cert(self, mock_db):
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_enabled", "true"),
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_domain", "example.com"),
        )
        mock_db.commit()
        status = ssl_manager.get_ssl_status()
        assert status["enabled"] is True
        assert status["domain"] == "example.com"
        assert status["cert_exists"] is False

    def test_needs_renewal_true_under_30_days(self, mock_db):
        expires = (datetime.now() + timedelta(days=15)).isoformat()
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_enabled", "true"),
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_domain", "example.com"),
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_cert_expires", expires),
        )
        mock_db.commit()
        status = ssl_manager.get_ssl_status()
        assert status["needs_renewal"] is True
        assert status["days_until_expiry"] == pytest.approx(15, abs=1)

    def test_needs_renewal_false_over_30_days(self, mock_db):
        expires = (datetime.now() + timedelta(days=60)).isoformat()
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_enabled", "true"),
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_domain", "example.com"),
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_cert_expires", expires),
        )
        mock_db.commit()
        status = ssl_manager.get_ssl_status()
        assert status["needs_renewal"] is False
        assert status["days_until_expiry"] == pytest.approx(60, abs=1)

    def test_invalid_expiry_date_handled(self, mock_db):
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_enabled", "true"),
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_domain", "example.com"),
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_cert_expires", "not-a-date"),
        )
        mock_db.commit()
        status = ssl_manager.get_ssl_status()
        assert status["days_until_expiry"] is None
        assert status["needs_renewal"] is False


class TestRenewCertificate:
    @pytest.mark.asyncio
    async def test_no_domain_returns_failure(self, mock_db):
        success, msg = await ssl_manager.renew_certificate()
        assert success is False
        assert "No domain" in msg

    @pytest.mark.asyncio
    async def test_successful_renewal(self, mock_db):
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_domain", "example.com"),
        )
        mock_db.commit()

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"Cert renewed", b""))

        async def fake_wait_for(coro, **kw):
            return await coro

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch("asyncio.wait_for", side_effect=fake_wait_for), \
             patch.object(ssl_manager, "_get_cert_expiry", return_value=datetime.now() + timedelta(days=90)), \
             patch.object(ssl_manager, "_reload_nginx"):
            success, msg = await ssl_manager.renew_certificate()
        assert success is True
        assert "renewed" in msg.lower()

    @pytest.mark.asyncio
    async def test_renewal_timeout(self, mock_db):
        import asyncio
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_domain", "example.com"),
        )
        mock_db.commit()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            success, msg = await ssl_manager.renew_certificate()
        assert success is False
        assert "timed out" in msg.lower()

    @pytest.mark.asyncio
    async def test_renewal_certbot_failure(self, mock_db):
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("ssl_domain", "example.com"),
        )
        mock_db.commit()

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"error output", b"detail"))

        async def fake_wait_for(coro, **kw):
            return await coro

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch("asyncio.wait_for", side_effect=fake_wait_for):
            success, msg = await ssl_manager.renew_certificate()
        assert success is False
        assert "failed" in msg.lower()
