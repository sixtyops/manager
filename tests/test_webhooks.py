"""Tests for generic webhook notification module."""

import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest.mock import patch, AsyncMock, MagicMock

import pytest


@pytest.fixture
def webhook_db(memory_db):
    """Memory DB with webhook settings enabled."""
    memory_db.execute("UPDATE settings SET value = 'true' WHERE key = 'webhook_enabled'")
    memory_db.execute("UPDATE settings SET value = 'https://example.com/webhook' WHERE key = 'webhook_url'")
    memory_db.commit()
    return memory_db


class TestWebhookValidation:
    """Test webhook URL validation."""

    def test_valid_https_url(self):
        from updater.webhooks import is_valid_webhook_url
        assert is_valid_webhook_url("https://example.com/webhook")

    def test_valid_http_url(self):
        from updater.webhooks import is_valid_webhook_url
        assert is_valid_webhook_url("http://internal.local:8080/hook")

    def test_invalid_url(self):
        from updater.webhooks import is_valid_webhook_url
        assert not is_valid_webhook_url("not-a-url")
        assert not is_valid_webhook_url("")
        assert not is_valid_webhook_url("ftp://example.com")


class TestHMACSigning:
    """Test HMAC-SHA256 payload signing."""

    def test_sign_payload(self):
        from updater.webhooks import _sign_payload

        payload = b'{"test": true}'
        secret = "my-secret"
        signature = _sign_payload(payload, secret)

        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert signature == expected

    def test_different_secrets_produce_different_signatures(self):
        from updater.webhooks import _sign_payload

        payload = b'{"test": true}'
        sig1 = _sign_payload(payload, "secret1")
        sig2 = _sign_payload(payload, "secret2")
        assert sig1 != sig2


class TestWebhookSend:
    """Test webhook sending logic."""

    @pytest.mark.asyncio
    async def test_disabled_webhook_returns_false(self, memory_db, mock_db):
        from updater.webhooks import send_webhook
        # webhook_enabled is "false" by default
        result = await send_webhook("test", {"data": "value"})
        assert result is False

    @pytest.mark.asyncio
    async def test_enabled_webhook_sends_post(self, webhook_db, mock_db):
        from updater.webhooks import send_webhook

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await send_webhook("job_completed", {"job_id": "123"})
            assert result is True

            # Verify POST was called
            mock_client.request.assert_called_once()
            call_args = mock_client.request.call_args
            assert call_args[0][0] == "POST"
            assert call_args[0][1] == "https://example.com/webhook"

    @pytest.mark.asyncio
    async def test_event_filtering(self, webhook_db, mock_db):
        from updater.webhooks import send_webhook

        # Set webhook_events to only allow "job_completed"
        webhook_db.execute("UPDATE settings SET value = 'job_completed' WHERE key = 'webhook_events'")
        webhook_db.commit()

        # "device_offline" not in allowed events
        result = await send_webhook("device_offline", {"ip": "10.0.0.1"})
        assert result is False

    @pytest.mark.asyncio
    async def test_hmac_signature_included(self, webhook_db, mock_db):
        from updater.webhooks import send_webhook

        webhook_db.execute("UPDATE settings SET value = 'my-secret' WHERE key = 'webhook_secret'")
        webhook_db.commit()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await send_webhook("job_completed", {"job_id": "abc"})

            call_kwargs = mock_client.request.call_args
            headers = call_kwargs[1]["headers"] if "headers" in call_kwargs[1] else call_kwargs.kwargs.get("headers", {})
            assert "X-Webhook-Signature" in headers
            assert headers["X-Webhook-Signature"].startswith("sha256=")

    @pytest.mark.asyncio
    async def test_invalid_url_returns_false(self, webhook_db, mock_db):
        from updater.webhooks import send_webhook

        webhook_db.execute("UPDATE settings SET value = 'not-a-url' WHERE key = 'webhook_url'")
        webhook_db.commit()

        result = await send_webhook("test", {})
        assert result is False

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self, webhook_db, mock_db):
        from updater.webhooks import send_webhook

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await send_webhook("test", {})
            assert result is False


class TestWebhookNotifications:
    """Test high-level notification functions."""

    @pytest.mark.asyncio
    async def test_notify_job_completed_disabled(self, memory_db, mock_db):
        from updater.webhooks import notify_job_completed

        with patch("updater.webhooks.send_webhook") as mock_send:
            await notify_job_completed(
                job_id="test-1",
                success_count=5,
                failed_count=0,
                skipped_count=0,
                cancelled_count=0,
                duration_seconds=60.0,
                devices={},
                firmware_name="test.bin",
            )
            # Webhook disabled, should not create task
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_test_webhook_not_enabled(self, memory_db, mock_db):
        from updater.webhooks import send_test_webhook

        success, message = await send_test_webhook()
        assert success is False
        assert "not enabled" in message

    @pytest.mark.asyncio
    async def test_send_test_webhook_no_url(self, webhook_db, mock_db):
        from updater.webhooks import send_test_webhook

        webhook_db.execute("UPDATE settings SET value = '' WHERE key = 'webhook_url'")
        webhook_db.commit()

        success, message = await send_test_webhook()
        assert success is False
        assert "No webhook URL" in message


class TestFeatureGating:
    """Test webhook feature gating."""

    def test_webhooks_feature_exists(self):
        from updater.license import Feature
        assert Feature.WEBHOOKS == "webhooks"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, None])
@pytest.mark.parametrize("userinfo,credentials", [
    ("synthetic-user:synthetic-password", b"synthetic-user:synthetic-password"),
    ("synthetic%40user:synthetic%3Apassword", b"synthetic@user:synthetic:password"),
    ("synthetic'user:synthetic(pass)", b"synthetic'user:synthetic(pass)"),
    ('synthetic-user:synthetic"password', b'synthetic-user:synthetic"password'),
    ("synthetic-user:synthetic password", b"synthetic-user:synthetic password"),
    ("synthetic-user:synthetic{password", b"synthetic-user:synthetic{password"),
    ("synthetic-user:synthetic}password", b"synthetic-user:synthetic}password"),
    ("synthetic-user:synthetic<password", b"synthetic-user:synthetic<password"),
    ("synthetic-user:synthetic>password", b"synthetic-user:synthetic>password"),
])
async def test_actual_webhook_userinfo_logs_preserve_request(monkeypatch, status, userinfo, credentials):
    import base64
    import io
    import logging
    import socket
    import sys
    from datetime import datetime
    import httpx
    from updater import app, webhooks, logging_filter as scrub

    monkeypatch.setattr(scrub, "_secrets", set())
    monkeypatch.setattr(scrub, "_pattern", None)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    app.configure_logging()
    url = "https://" + userinfo + "@receiver.example.test:8443/hook?mode=test#result"
    settings = {"webhook_enabled": "true", "webhook_url": url, "webhook_method": "PUT",
                "webhook_events": "test", "webhook_headers": '{"X-Synthetic": "unchanged"}'}
    monkeypatch.setattr(webhooks.db, "get_setting", lambda k, d="": settings.get(k, d))
    # Exercise actual URL validation without making a DNS query.
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.1", 443))])
    requests = []

    def respond(request):
        requests.append(request)
        if status is None:
            raise httpx.ConnectError("Synthetic failure: " + url, request=request)
        return httpx.Response(status, text="Synthetic response")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(respond), **kwargs))
    try:
        assert await webhooks.send_webhook("test", {"synthetic": True}) is (status == 200)
        assert len(requests) == 1
        request = requests[0]
        assert request.url == httpx.URL(url)
        assert request.method == "PUT"
        assert request.headers["authorization"] == "Basic " + base64.b64encode(credentials).decode()
        assert request.headers["X-Synthetic"] == "unchanged"
        body = json.loads(request.content)
        assert body["event"] == "test" and body["data"] == {"synthetic": True}
        assert datetime.fromisoformat(body["timestamp"])
        output = stream.getvalue()
        redacted = "https://[REDACTED]@receiver.example.test:8443/hook?mode=test#result"
        assert redacted in output
        assert userinfo not in output
        if status is None:
            assert "Failed to send webhook: Synthetic failure: " in output
        else:
            assert f'HTTP Request: PUT {redacted} "HTTP/1.1 {status}' in output
        assert scrub._secrets == set() and scrub._pattern is None
    finally:
        root.handlers[0].close()
