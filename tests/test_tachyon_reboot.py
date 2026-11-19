"""Offline proof checks for Tachyon reboot requests."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from updater.vendors.tachyon.client import DeviceInfo, TachyonClient


def system(uptime):
    return 200, json.dumps({"system": {"uptime": uptime}})


@pytest_asyncio.fixture
async def clock(monkeypatch):
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: now[0])

    async def sleep(delay):
        now[0] += delay

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return now


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 201, 204, 302, 400, 401, 500])
async def test_reboot_rejects_every_status_except_200(status):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client._curl = AsyncMock(side_effect=[system(1000), (status, "")])
    client.wait_for_reboot = AsyncMock()
    assert not await client.reboot()
    client._curl.assert_awaited_with("POST", "/cgi.lua/reboot")
    client.wait_for_reboot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", [
    [system(1001), system(1002), system(1003)],
    [system(None), system(None), system(None)],
    [(401, ""), (401, ""), (401, "")],
])
async def test_reboot_without_drop_or_reset_fails_at_deadline(clock, observations):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client._curl = AsyncMock(side_effect=[system(1000), (200, ""), *observations])
    client.login = AsyncMock(return_value=False)
    started = clock[0]
    assert not await client.reboot(timeout=7)
    assert clock[0] == started + 7
    assert client.last_reboot_deadline == started + 7
    assert client.read_deadline is None


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", [
    [RuntimeError("connection refused"), system(2)],
    [system(2)],
])
async def test_reboot_accepts_drop_then_return_or_uptime_reset(clock, observations):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client._curl = AsyncMock(side_effect=[system(1000), (200, ""), *observations])
    assert await client.reboot(timeout=7)
    assert not hasattr(client, "_reboot_observation")


@pytest.mark.asyncio
async def test_callback_success_does_not_replace_reboot_proof(clock):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client._curl = AsyncMock(side_effect=[system(1000), (200, ""), *[system(1001)] * 4])
    callback = AsyncMock(return_value=True)
    assert not await client.reboot(timeout=7, recovery_callback=callback)
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_reboot_request_and_baseline_consume_wait_budget(clock):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    started = clock[0]
    deadlines = []

    async def request(method, endpoint):
        deadlines.append(client.read_deadline)
        if method == "POST":
            clock[0] += 2
            return 200, ""
        clock[0] += 1
        return system(1000)

    client._curl = request
    assert not await client.reboot(timeout=7)
    assert set(deadlines) == {started + 7}
    assert clock[0] == started + 7


@pytest.mark.asyncio
async def test_expired_deadline_does_not_start_curl():
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client.read_deadline = asyncio.get_running_loop().time() - 1
    with pytest.raises(TimeoutError):
        await client._curl("POST", "/cgi.lua/reboot")


@pytest.mark.asyncio
async def test_reboot_transport_error_fails_without_wait():
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client._curl = AsyncMock(side_effect=[system(1000), RuntimeError("connection closed")])
    client.wait_for_reboot = AsyncMock()
    assert not await client.reboot()
    client.wait_for_reboot.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_retarget_requires_uptime_read_at_new_address(clock):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client._curl = AsyncMock(side_effect=[system(1000), (200, ""), system(2), system(3)])

    async def recover(deadline, started):
        assert deadline == started + 7
        client.ip = "192.0.2.2"
        client._base_url = "https://192.0.2.2"
        return True

    assert await client.reboot(timeout=7, recovery_callback=recover)
    assert client._curl.await_count == 4


@pytest.mark.asyncio
async def test_existing_read_deadline_caps_reboot(clock):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client.read_deadline = clock[0] + 4
    deadline = client.read_deadline
    client._curl = AsyncMock(side_effect=[system(1000), (200, ""), system(1001), system(1002)])
    assert not await client.reboot(timeout=7)
    assert clock[0] == deadline
    assert client.last_reboot_deadline == deadline
    assert client.read_deadline == deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [(401, ""), (302, ""), (200, '{"statusCode":401}')])
async def test_reboot_reauthenticates_after_drop(clock, expired):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    client._curl = AsyncMock(side_effect=[
        system(1000), (200, ""), RuntimeError("connection refused"),
        expired, system(2),
    ])
    client.login = AsyncMock(return_value=True)
    assert await client.reboot(timeout=7)
    client.login.assert_awaited_once()


@pytest.mark.parametrize("uptime", [None, True, "2", -1, float("nan"), float("inf")])
def test_invalid_uptime_cannot_prove_reset(uptime):
    assert TachyonClient._parse_uptime(json.dumps({"system": {"uptime": uptime}})) is None


@pytest.mark.asyncio
async def test_install_reboot_accepts_uptime_reset_without_transport_drop(clock):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    started = clock[0]
    client.login = AsyncMock(return_value=True)
    client.upload_firmware = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(side_effect=[
        DeviceInfo(ip=client.ip, model="TNA-301", current_version="1.12.3.54970"),
        DeviceInfo(ip=client.ip, model="TNA-301", current_version="1.15.0.55142"),
    ])
    responses = iter([system(1000), (200, ""), system(2)])
    requests = []

    async def request(method, endpoint, **kwargs):
        requests.append((method, endpoint))
        assert client.read_deadline == started + 7
        clock[0] += 1
        return next(responses)

    client._curl = request
    result = await client.update_firmware("/tmp/tna-30x-1.15.0-r55142.bin", reboot_timeout=7)

    assert result.success, result.error
    assert result.new_version == "1.15.0.55142"
    assert requests == [
        ("GET", "/cgi.lua/status?type=system"),
        ("POST", "/cgi.lua/update"),
        ("GET", "/cgi.lua/status?type=system"),
    ]
    assert client.read_deadline is None
    assert not hasattr(client, "_reboot_observation")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pre-update", "install"])
async def test_reboot_recovery_finds_device_only_at_new_address(clock, stage):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    started = clock[0]
    observations = []
    installed = False
    dropped = False
    recovered = False

    async def request(method, endpoint):
        nonlocal dropped
        observations.append((client.ip, method, endpoint))
        assert client.read_deadline == started + 7
        if method == "POST":
            return 200, ""
        if stage == "pre-update" and len(observations) == 1:
            return system(1000)
        if stage == "install" and not installed:
            raise RuntimeError("uptime baseline unavailable")
        if not dropped:
            # The old API can still answer before the actual reboot starts.
            if len(observations) == (3 if stage == "pre-update" else 2):
                return system(1001)
            dropped = True
            raise RuntimeError("old address is unreachable")
        assert recovered, "recovery must run immediately after the drop"
        assert client.ip == "192.0.2.2"
        return system(2)

    async def recover(deadline, reboot_started):
        nonlocal recovered
        assert dropped
        assert deadline == started + 7
        assert reboot_started == started
        client.ip = "192.0.2.2"
        client._base_url = "https://192.0.2.2"
        recovered = True
        return True

    async def login():
        assert client.ip == ("192.0.2.2" if recovered else "192.0.2.1")
        assert not dropped or recovered, "never require login at the unreachable old address"
        return True

    async def info():
        return DeviceInfo(ip=client.ip, model="TNA-301", current_version=(
            "1.15.0.55142" if installed else "1.12.3.54970"
        ))

    async def install():
        nonlocal installed
        installed = True
        return True

    client._curl = AsyncMock(side_effect=request)
    client.login = AsyncMock(side_effect=login)
    callback = AsyncMock(side_effect=recover)
    if stage == "pre-update":
        assert await client.reboot(timeout=7, recovery_callback=callback)
    else:
        client.get_device_info = AsyncMock(side_effect=info)
        client.upload_firmware = AsyncMock(return_value=True)
        client.trigger_update = AsyncMock(side_effect=install)
        result = await client.update_firmware(
            "/tmp/tna-30x-1.15.0-r55142.bin", reboot_timeout=7,
            recovery_callback=callback,
        )
        assert result.success, result.error
        assert result.new_version == "1.15.0.55142"
    callback.assert_awaited_once()
    assert client.ip == "192.0.2.2"
    assert client.read_deadline is None
    assert client.last_reboot_deadline == started + 7
