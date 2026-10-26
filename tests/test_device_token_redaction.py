"""Check trusted device-token registration with synthetic subprocess output."""

import io
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from updater import logging_filter as scrub
from updater.tachyon import TachyonClient
from updater.vendors.tachyon import client as driver


TOKEN = "synthetic-device-session-token"


@pytest.fixture(autouse=True)
def isolated_device_registry(monkeypatch):
    monkeypatch.setattr(scrub, "_device_tokens", {})
    monkeypatch.setattr(scrub, "_device_token_pattern", None)
    monkeypatch.setattr(scrub, "_secrets", set())
    monkeypatch.setattr(scrub, "_pattern", None)


@pytest.fixture
def output(monkeypatch):
    from updater import app

    root = logging.getLogger()
    old_level = root.level
    stream = io.StringIO()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(sys, "stderr", stream)
    app.configure_logging()
    try:
        yield stream, root.handlers[0]
    finally:
        root.handlers[0].close()
        root.setLevel(old_level)


@pytest.fixture
def login_stub(monkeypatch):
    paths = []

    def install(cookie_text, response=b'{}', returncode=0):
        async def spawn(*args, **kwargs):
            path = Path(args[args.index("-c") + 1])
            paths.append(path)
            path.write_text(cookie_text)
            proc = AsyncMock()
            proc.returncode = returncode
            proc.communicate.return_value = (response, b"synthetic transport failure")
            return proc

        mock = AsyncMock(side_effect=spawn)
        monkeypatch.setattr(driver.asyncio, "create_subprocess_exec", mock)
        return mock

    yield install
    assert all(not path.exists() for path in paths)


def cookie(value=TOKEN, name="token"):
    return f"device.example.test\tFALSE\t/\tFALSE\t0\t{name}\t{value}\n"


@pytest.mark.asyncio
async def test_login_registers_before_assignment_and_downstream_use(monkeypatch, login_stub, output):
    spawn = login_stub(cookie())
    client = TachyonClient("192.0.2.1", "synthetic-user", "synthetic-password")
    registered = []
    register = scrub.register_device_token

    def observe(value):
        assert client._token is None
        registered.append(value)
        register(value)

    monkeypatch.setattr(driver, "register_device_token", observe)
    assert await client.login() is True
    assert registered == [TOKEN]
    assert client._token == TOKEN
    assert spawn.await_count == 1

    async def downstream(method, endpoint):
        assert registered == [TOKEN]
        assert client._token == TOKEN
        assert (method, endpoint) == ("GET", "/cgi.lua/status?type=system")
        return 200, '{"system": {"model": "synthetic-model"}}'

    monkeypatch.setattr(client, "_curl", downstream)
    assert await client.session_valid() == "ok"
    driver.logger.warning("Synthetic diagnostic: %s", client._token)
    stream, _ = output
    assert "Synthetic diagnostic: [REDACTED]" in stream.getvalue()
    assert TOKEN not in stream.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("jar,response,returncode,expected", [
    (cookie(name="other"), b'{}', 0, "Login failed: no token received"),
    ("", json.dumps({"token": TOKEN}).encode(), 0, "Login failed: no token received"),
    (cookie(), b'{"auth": false}', 0, "Invalid credentials"),
    (cookie(), b'{}', 1, "Device not reachable"),
])
async def test_untrusted_or_failed_login_values_are_not_registered(
        login_stub, jar, response, returncode, expected):
    login_stub(jar, response, returncode)
    client = TachyonClient("192.0.2.1", "synthetic-user", "synthetic-password")
    assert await client.login() == expected
    assert client._token is None
    assert scrub._device_tokens == {}
    assert scrub._secrets == set()


@pytest.mark.parametrize("form", [str, repr, json.dumps])
def test_device_token_literal_and_escaped_output(output, form):
    value = 'synthetic-quote"backslash\\device-token'
    scrub.register_device_token(value)
    driver.logger.warning("Synthetic diagnostic: %s", form(value))
    stream, _ = output
    assert "[REDACTED]" in stream.getvalue()
    assert form(value) not in stream.getvalue()


def test_device_token_registry_is_bounded_and_refreshes_recent_values(output):
    values = [f"synthetic-device-token-{index:04d}" for index in range(scrub._DEVICE_TOKEN_LIMIT)]
    for value in values:
        scrub.register_device_token(value)
    scrub.register_device_token(values[0])
    scrub.register_device_token("synthetic-new-device-token")
    scrub.register_device_token(None)
    scrub.register_device_token("")
    assert len(scrub._device_tokens) == scrub._DEVICE_TOKEN_LIMIT
    assert values[0] in scrub._device_tokens
    assert values[1] not in scrub._device_tokens
    assert scrub._secrets == set()
    driver.logger.warning("recent=%s new=%s", values[0], "synthetic-new-device-token")
    stream, _ = output
    assert "recent=[REDACTED] new=[REDACTED]" in stream.getvalue()


def test_device_token_formatter_error_is_redacted(output, monkeypatch):
    scrub.register_device_token(TOKEN)

    class FailedFormatter(logging.Formatter):
        def format(self, record):
            raise ValueError("Synthetic formatter failure: " + TOKEN)

    stream, handler = output
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    handler.setFormatter(FailedFormatter())
    scrub.install_sanitizer(handler)
    handler.handle(logging.LogRecord("synthetic", logging.WARNING, __file__, 1,
                                    "Synthetic diagnostic: %s", (TOKEN,), None))
    assert "--- Logging error ---" in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()
    assert TOKEN not in stream.getvalue()


def test_url_redaction_precedes_device_literals(output):
    scrub.register_device_token("synthetic-user")
    driver.logger.warning("https://synthetic-user:synthetic-password@device.example.test/")
    stream, _ = output
    assert "https://[REDACTED]@device.example.test/" in stream.getvalue()
    assert "synthetic-password" not in stream.getvalue()


def test_device_token_redaction_precedes_registered_prefix(output):
    scrub.register_device_token(TOKEN)
    scrub.register_secret("synthetic-device")
    driver.logger.warning("Synthetic diagnostic: %s", TOKEN)
    stream, _ = output
    assert "Synthetic diagnostic: [REDACTED]\n" in stream.getvalue()
    assert "session-token" not in stream.getvalue()
