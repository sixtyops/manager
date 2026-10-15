"""Offline checks for private curl request transport."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from updater.vendors.tachyon import client as transport

SECRET = 'synthetic-"quote"\\slash\noutput = /tmp/injected\r\tvalue'


def config_values(proc):
    """Decode the quoted config values used in these synthetic requests."""
    config = proc.communicate.call_args.args[0].decode()
    values = {}
    for line in config.splitlines():
        name, separator, value = line.partition(" = ")
        values.setdefault(name, []).append(json.loads(value) if separator else None)
    return values


@pytest.fixture
def process():
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate.return_value = (b'{"ok":true}\n200', b'')
    return proc


def assert_private(mock_exec):
    for call in mock_exec.call_args_list:
        assert call.args == ("curl", "--disable", "--config", "-")
        assert call.kwargs == {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("verify", [False, True])
async def test_config_and_cookie_round_trip(process, verify):
    client = transport.TachyonClient("192.0.2.1", SECRET, SECRET, timeout=19)
    client._token = SECRET
    payload = {"radius_secret": SECRET, "password": SECRET}
    with patch.object(transport, "VERIFY_SSL", verify), patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    ) as execute:
        assert await client._curl("POST", "/cgi.lua/config", data=payload) == (200, '{"ok":true}')
    assert_private(execute)
    config = config_values(process)
    assert config["data"] == [json.dumps(payload)]
    assert config["header"] == [f"Cookie: token={SECRET}", "Content-Type: application/json"]
    assert config["max-time"] == ["19"]
    assert config["request"] == ["POST"]
    assert config["url"] == ["https://192.0.2.1/cgi.lua/config"]
    assert config["write-out"] == ["\n%{http_code}"]
    assert ("insecure" in config) is (not verify)
    assert "output" not in config


@pytest.mark.asyncio
async def test_login_payload_and_cookie_cleanup(process, caplog):
    client = transport.TachyonClient("192.0.2.1", SECRET, SECRET)
    cookie_paths = []

    async def communicate(config):
        cookie_line = next(line for line in config.decode().splitlines() if line.startswith("cookie-jar = "))
        cookie_path = Path(json.loads(cookie_line.partition(" = ")[2]))
        cookie_paths.append(cookie_path)
        cookie_path.write_text("192.0.2.1\tFALSE\t/\tTRUE\t0\ttoken\tsynthetic-token\n")
        return b'{"auth":true}', b''

    process.communicate.side_effect = communicate
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as execute:
        assert await client.login() is True
    assert_private(execute)
    assert json.loads(config_values(process)["data"][0]) == {"username": SECRET, "password": SECRET}
    assert client._token == "synthetic-token"
    assert all(not path.exists() for path in cookie_paths)
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode,response,expected", [(0, b'{"status":"ok"}', True), (0, b'{"error":"rejected"}', False), (7, b'', False)])
async def test_upload_preserves_multipart_and_retries(process, returncode, response, expected, caplog):
    client = transport.TachyonClient("192.0.2.1", "admin", SECRET)
    client._token = SECRET
    process.returncode = returncode
    process.communicate.return_value = (response, SECRET.encode())
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as execute, patch(
        "asyncio.sleep", AsyncMock()
    ) as sleep:
        assert await client.upload_firmware("/tmp/fw.bin", bandwidth_limit_kbps=500) is expected
    assert_private(execute)
    config = config_values(process)
    assert config["header"] == [f"Cookie: token={SECRET}"]
    assert config["form"] == ["fw=@/tmp/fw.bin", "force=false"]
    assert config["request"] == ["PUT"]
    assert config["max-time"] == ["300"]
    assert config["limit-rate"] == ["500k"]
    assert execute.call_count == (3 if returncode else 1)
    assert [call.args for call in sleep.call_args_list] == ([(30,), (60,)] if returncode else [])
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_curl_error_never_exposes_stderr(process):
    client = transport.TachyonClient("192.0.2.1", "admin", SECRET)
    process.returncode = 28
    process.communicate.return_value = (b'', ("Cookie: " + SECRET).encode())
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
        with pytest.raises(RuntimeError) as error:
            await client._curl("POST", "/cgi.lua/config", data={"password": SECRET})
    assert str(error.value) == "curl failed: request timed out (exit code 28)"
    assert SECRET not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,returncode,expected", [(b'', 7, "Device not reachable"), (("Invalid credentials " + SECRET).encode(), 0, "Invalid credentials")])
async def test_login_failure_never_logs_reflected_secrets(process, response, returncode, expected, caplog):
    process.returncode = returncode
    process.communicate.return_value = (response, SECRET.encode())
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
        assert await transport.TachyonClient("192.0.2.1", SECRET, SECRET).login() == expected
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["\x00", "\x01", "\x0c", "\x7f"])
async def test_unsupported_control_is_rejected_before_spawn(control):
    client = transport.TachyonClient("192.0.2.1", "admin", "synthetic")
    client._token = "synthetic" + control
    with patch("asyncio.create_subprocess_exec", AsyncMock()) as execute:
        with pytest.raises(ValueError, match="Unsupported control character"):
            await client._curl("GET", "/cgi.lua/config")
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_generic_multipart_uses_private_config(process):
    client = transport.TachyonClient("192.0.2.1", "admin", "synthetic")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as execute:
        await client._curl("PUT", "/cgi.lua/update", file_path="/tmp/fw.bin", form_data={"secret": SECRET})
    assert_private(execute)
    assert config_values(process)["form"] == ["fw=@/tmp/fw.bin", f"secret={SECRET}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("status,json_body", [(400, True), (500, False), (200, True)])
async def test_config_failure_does_not_return_reflected_secrets(status, json_body, dry_run):
    client = transport.TachyonClient("192.0.2.1", "admin", SECRET)
    client._token = SECRET
    payload = {"radius": {"secret": SECRET}}
    reflected = {"success": False, "error": SECRET, "config": payload, "cookie": SECRET}
    body = json.dumps(reflected) if json_body else f"Cookie: token={SECRET} {payload}"
    with patch.object(client, "_curl", AsyncMock(return_value=(status, body))) as request:
        result = await client.apply_config(payload, dry_run=dry_run)
    assert result == {
        "success": False,
        "status_code": status,
        "error": f"Configuration request failed (HTTP {status})",
    }
    # The poller persists this caller-facing diagnostic after a failed request.
    diagnostic = result.get("error", result.get("raw_response", "Apply failed"))
    assert diagnostic == f"Configuration request failed (HTTP {status})"
    assert SECRET not in json.dumps(result)
    request.assert_awaited_once_with(
        "POST", "/cgi.lua/config" + ("?dry_run=true" if dry_run else ""), data=payload
    )
