"""Check trusted device-token registration with synthetic subprocess output."""

import gc
import io
import json
import logging
import sys
import weakref
from collections import OrderedDict
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
    monkeypatch.setattr(scrub, "_secrets", set())
    monkeypatch.setattr(scrub, "_pattern", None)
    monkeypatch.setattr(scrub, "_generated_api_tokens", OrderedDict())


@pytest.fixture
def token_owner():
    return TachyonClient("192.0.2.1", "synthetic-user", "synthetic-password")


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

    def observe(owner, value):
        assert owner is client
        assert client._token is None
        registered.append(value)
        register(owner, value)

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
def test_device_token_literal_and_escaped_output(output, form, token_owner):
    value = 'synthetic-quote"backslash\\device-token'
    scrub.register_device_token(token_owner, value)
    driver.logger.warning("Synthetic diagnostic: %s", form(value))
    stream, _ = output
    assert "[REDACTED]" in stream.getvalue()
    assert form(value) not in stream.getvalue()


@pytest.mark.asyncio
async def test_active_token_survives_more_than_100_trusted_logins(login_stub, output):
    clients = []
    for index in range(105):
        login_stub(cookie(f"synthetic-live-device-token-{index:04d}"))
        client = TachyonClient("192.0.2.1", "synthetic-user", "synthetic-password")
        assert await client.login() is True
        clients.append(client)
    assert len(scrub._device_tokens) == len(clients)
    driver.logger.warning("first=%s last=%s", clients[0]._token, clients[-1]._token)
    stream, _ = output
    assert "first=[REDACTED] last=[REDACTED]" in stream.getvalue()
    assert clients[0]._token not in stream.getvalue()
    assert scrub._secrets == set()


@pytest.mark.asyncio
async def test_new_login_replaces_registration_without_token_history(login_stub, token_owner, output):
    for index in range(105):
        login_stub(cookie(f"synthetic-replacement-token-{index:04d}"))
        assert await token_owner.login() is True
        assert len(scrub._device_tokens) == 1
    assert list(scrub._device_tokens.values()) == [token_owner._token]
    assert "synthetic-replacement-token-0000" not in scrub._pattern.pattern
    driver.logger.warning("current=%s", token_owner._token)
    stream, _ = output
    assert "current=[REDACTED]" in stream.getvalue()


def test_client_collection_retires_registration_and_pattern(output):
    client = TachyonClient("192.0.2.1", "synthetic-user", "synthetic-password")
    ref = weakref.ref(client)
    scrub.register_device_token(client, TOKEN)
    assert len(scrub._device_tokens) == 1
    del client
    gc.collect()
    assert ref() is None
    assert scrub._device_tokens == {}
    assert scrub._pattern is None


def test_shared_token_remains_until_last_client_is_collected(output, token_owner):
    other = TachyonClient("192.0.2.2", "synthetic-user", "synthetic-password")
    scrub.register_device_token(token_owner, TOKEN)
    scrub.register_device_token(other, TOKEN)
    del other
    gc.collect()
    assert len(scrub._device_tokens) == 1
    driver.logger.warning("shared=%s", TOKEN)
    stream, _ = output
    assert "shared=[REDACTED]" in stream.getvalue()


def test_client_collection_preserves_other_registered_literals(output):
    scrub.register_secret("synthetic-ordinary-secret")
    client = TachyonClient("192.0.2.1", "synthetic-user", "synthetic-password")
    scrub.register_device_token(client, TOKEN)
    del client
    gc.collect()
    assert scrub._device_tokens == {}
    driver.logger.warning("ordinary=%s", "synthetic-ordinary-secret")
    stream, _ = output
    assert "ordinary=[REDACTED]" in stream.getvalue()


def test_empty_registration_preserves_existing_token(token_owner):
    scrub.register_device_token(token_owner, TOKEN)
    scrub.register_device_token(token_owner, None)
    scrub.register_device_token(token_owner, "")
    assert list(scrub._device_tokens.values()) == [TOKEN]


@pytest.mark.asyncio
async def test_failed_relogin_preserves_existing_registration(login_stub, token_owner, output):
    login_stub(cookie())
    assert await token_owner.login() is True
    login_stub(cookie("synthetic-denied-token"), b'{"auth": false}')
    assert await token_owner.login() == "Invalid credentials"
    assert token_owner._token == TOKEN
    assert list(scrub._device_tokens.values()) == [TOKEN]
    driver.logger.warning("retained=%s", token_owner._token)
    stream, _ = output
    assert "retained=[REDACTED]" in stream.getvalue()


def test_device_token_formatter_error_is_redacted(output, monkeypatch, token_owner):
    scrub.register_device_token(token_owner, TOKEN)

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


def test_url_redaction_precedes_device_literals(output, token_owner):
    scrub.register_device_token(token_owner, "synthetic-user")
    driver.logger.warning("https://synthetic-user:synthetic-password@device.example.test/")
    stream, _ = output
    assert "https://[REDACTED]@device.example.test/" in stream.getvalue()
    assert "synthetic-password" not in stream.getvalue()


def test_device_token_redaction_precedes_registered_prefix(output, token_owner):
    scrub.register_device_token(token_owner, TOKEN)
    scrub.register_secret("synthetic-device")
    driver.logger.warning("Synthetic diagnostic: %s", TOKEN)
    stream, _ = output
    assert "Synthetic diagnostic: [REDACTED]\n" in stream.getvalue()
    assert "session-token" not in stream.getvalue()


@pytest.mark.parametrize("device_first", [True, False])
@pytest.mark.parametrize("form", [str, repr, json.dumps])
def test_longer_ordinary_secret_and_shorter_device_token_share_one_pass(
        output, token_owner, device_first, form):
    token = 'synthetic-quote"backslash\\device-token'
    secret = token + "-synthetic-private-suffix"
    registrations = [
        lambda: scrub.register_device_token(token_owner, token),
        lambda: scrub.register_secret(secret),
    ]
    for register in registrations if device_first else reversed(registrations):
        register()
    driver.logger.warning("diagnostic=%s", form(secret))
    stream, _ = output
    assert "diagnostic=" + form("[REDACTED]") in stream.getvalue()
    assert "synthetic-private-suffix" not in stream.getvalue()


@pytest.mark.parametrize("device_first", [True, False])
@pytest.mark.parametrize("device_longer", [True, False])
@pytest.mark.parametrize("form", [str, repr, json.dumps])
def test_api_and_device_literals_share_one_pass(
        output, token_owner, monkeypatch, device_first, device_longer, form):
    monkeypatch.setattr(scrub, "_generated_api_tokens", OrderedDict())
    shorter = 'synthetic-quote"backslash\\token'
    longer = shorter + "-synthetic-private-suffix"
    device_token, api_token = (longer, shorter) if device_longer else (shorter, longer)
    registrations = [
        lambda: scrub.register_device_token(token_owner, device_token),
        lambda: scrub.register_generated_api_token(api_token),
    ]
    for register in registrations if device_first else reversed(registrations):
        register()
    driver.logger.warning("diagnostic=%s", form(longer))
    stream, _ = output
    assert "diagnostic=" + form("[REDACTED]") in stream.getvalue()
    assert "synthetic-private-suffix" not in stream.getvalue()
