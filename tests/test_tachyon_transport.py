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
@pytest.mark.parametrize("verify", [False, True])
async def test_login_payload_and_cookie_cleanup(process, caplog, verify):
    client = transport.TachyonClient("192.0.2.1", SECRET, SECRET)
    cookie_paths = []

    async def communicate(config):
        cookie_line = next(line for line in config.decode().splitlines() if line.startswith("cookie-jar = "))
        cookie_path = Path(json.loads(cookie_line.partition(" = ")[2]))
        cookie_paths.append(cookie_path)
        cookie_path.write_text("192.0.2.1\tFALSE\t/\tTRUE\t0\ttoken\tsynthetic-token\n")
        return b'{"auth":true}', b''

    process.communicate.side_effect = communicate
    with patch.object(transport, "VERIFY_SSL", verify), patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    ) as execute:
        assert await client.login() is True
    assert_private(execute)
    assert json.loads(config_values(process)["data"][0]) == {"username": SECRET, "password": SECRET}
    assert ("insecure" in config_values(process)) is (not verify)
    assert client._token == "synthetic-token"
    assert all(not path.exists() for path in cookie_paths)
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name,kwargs,requests", [
    ("session_valid", {}, [("GET", "/cgi.lua/status?type=system")]),
    ("get_device_info", {}, [
        ("GET", "/cgi.lua/status?type=system"),
        ("GET", "/cgi.lua/bootbank"),
        ("GET", "/cgi.lua/status?type=interfaces"),
    ]),
    ("get_ap_info", {}, [
        ("GET", "/cgi.lua/status?type=system"),
        ("GET", "/cgi.lua/status?type=zones"),
        ("GET", "/cgi.lua/status?type=interfaces"),
    ]),
    ("get_connected_cpes", {}, [("GET", "/cgi.lua/status?type=wireless,zones")]),
    ("get_bridge_table", {}, [("GET", "/cgi.lua/bridge_table")]),
    ("fetch_config", {}, [("GET", "/cgi.lua/config")]),
    ("get_config", {}, [("GET", "/cgi.lua/config")]),
    ("apply_config", {"config": {"password": SECRET}}, [("POST", "/cgi.lua/config")]),
    ("apply_config", {"config": {"password": SECRET}, "dry_run": True}, [
        ("POST", "/cgi.lua/config?dry_run=true"),
    ]),
    ("trigger_update", {}, [("POST", "/cgi.lua/update")]),
    ("reboot", {}, [("POST", "/reboot")]),
])
@pytest.mark.parametrize("verify", [False, True])
async def test_authenticated_request_paths_keep_secrets_in_stdin(
    process, method_name, kwargs, requests, verify,
):
    client = transport.TachyonClient("192.0.2.1", SECRET, SECRET)
    client._token = SECRET
    with patch.object(transport, "VERIFY_SSL", verify), patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    ) as execute, patch.object(client, "wait_for_reboot", AsyncMock(return_value=True)):
        await getattr(client, method_name)(**kwargs)
    assert execute.await_count == len(requests)
    assert_private(execute)
    assert process.communicate.await_count == len(requests)
    for call, (method, endpoint) in zip(process.communicate.call_args_list, requests):
        config = call.args[0].decode()
        assert f'url = "https://192.0.2.1{endpoint}"' in config
        assert f'request = "{method}"' in config
        assert "header = " + transport._curl_config_value(f"Cookie: token={SECRET}") in config
        assert ("insecure\n" in config) is (not verify)
        if "config" in kwargs:
            assert "data = " + transport._curl_config_value(json.dumps(kwargs["config"])) in config


@pytest.mark.asyncio
@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("ping_available", [False, True])
async def test_reboot_probes_keep_stored_credentials_out_of_arguments(ping_available, verify):
    client = transport.TachyonClient("192.0.2.1", SECRET, SECRET)
    client._token = SECRET
    ping = AsyncMock(returncode=0)
    ping.communicate.return_value = (b"", b"")
    probe = AsyncMock(returncode=0)
    probe.communicate.return_value = (b"200", b"")

    async def spawn(*args, **kwargs):
        if args[0] == "ping":
            if not ping_available:
                raise FileNotFoundError("synthetic missing ping")
            return ping
        return probe

    with patch.object(transport, "VERIFY_SSL", verify), patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)) as execute, patch(
        "asyncio.sleep", AsyncMock()
    ):
        assert await client.wait_for_reboot() is True
    curl_calls = [call for call in execute.call_args_list if call.args[0] == "curl"]
    assert len(curl_calls) == (1 if ping_available else 2)
    assert all(("-k" in call.args) is (not verify) for call in curl_calls)
    for call in execute.call_args_list:
        assert all(SECRET not in argument for argument in call.args)
        assert "-H" not in call.args
        assert "-d" not in call.args


@pytest.mark.asyncio
@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("returncode,response,expected", [(0, b'{"status":"ok"}', True), (0, b'{"error":"rejected"}', False), (7, b'', False)])
async def test_upload_preserves_multipart_and_retries(process, returncode, response, expected, caplog, verify):
    client = transport.TachyonClient("192.0.2.1", "admin", SECRET)
    client._token = SECRET
    process.returncode = returncode
    process.communicate.return_value = (response, SECRET.encode())
    with patch.object(transport, "VERIFY_SSL", verify), patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as execute, patch(
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
    assert ("insecure" in config) is (not verify)
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


@pytest.mark.asyncio
async def test_config_http_200_error_does_not_return_reflected_secret():
    client = transport.TachyonClient("192.0.2.1", "admin", SECRET)
    client._token = SECRET
    payload = {"radius": {"secret": SECRET}}
    reflected = {"error": f"Rejected value: {SECRET}", "config": payload}
    with patch.object(client, "_curl", AsyncMock(return_value=(200, json.dumps(reflected)))):
        result = await client.apply_config(payload)
    assert result == {
        "success": False,
        "status_code": 200,
        "error": "Configuration request failed (HTTP 200)",
    }
    assert SECRET not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_error", [True, False])
async def test_trigger_rejection_never_logs_reflected_cookie(with_error, caplog):
    client = transport.TachyonClient("192.0.2.1", "admin", SECRET)
    client._token = SECRET
    reflected = {"statusCode": 403, "cookie": f"Cookie: token={SECRET}"}
    if with_error:
        reflected["error"] = f"Cookie: token={SECRET}"
    with patch.object(client, "_curl", AsyncMock(return_value=(200, json.dumps(reflected)))):
        assert await client.trigger_update() is False
    assert caplog.messages == ["Update trigger rejected by 192.0.2.1 (HTTP 200)"]
    assert SECRET not in caplog.text
    assert "Cookie:" not in caplog.text
