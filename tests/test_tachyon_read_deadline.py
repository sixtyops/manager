"""Offline checks for recovery read deadlines and curl cleanup."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest

from updater.vendors.tachyon import client as transport


@pytest.mark.asyncio
async def test_twenty_second_reads_share_one_deadline_without_aggregate_probe_limit(monkeypatch):
    loop = asyncio.get_running_loop()
    started = loop.time()
    elapsed = 0
    timeouts = []
    durations = iter([20, 20, 5])
    client = transport.TachyonClient("192.0.2.10", "synthetic", "synthetic", timeout=7)
    client.read_deadline = started + 50
    monkeypatch.setattr(loop, "time", lambda: started + elapsed)

    async def request(args):
        nonlocal elapsed
        timeouts.append(float(args[args.index("-m") + 1]))
        elapsed += next(durations)
        if args[-1].endswith("type=system"):
            return 0, b'{"system":{"serial":"ap-serial","model":"TNA-301"}}\n200'
        return 0, b'{}\n200'

    monkeypatch.setattr(transport, "_run_curl", request)
    info = await client.get_device_info()
    assert info.serial == "ap-serial"
    assert info.model == "TNA-301"
    assert elapsed == 45
    assert timeouts == [30, 30, 10]
    assert client.read_deadline == started + 50
    assert client.timeout == 7


@pytest.mark.asyncio
async def test_expiry_between_subrequests_blocks_the_next_request(monkeypatch):
    loop = asyncio.get_running_loop()
    started = loop.time()
    elapsed = 0
    client = transport.TachyonClient("192.0.2.10", "synthetic", "synthetic")
    client.read_deadline = started + 25
    monkeypatch.setattr(loop, "time", lambda: started + elapsed)

    async def request(args):
        nonlocal elapsed
        elapsed += 25
        return 0, b'{"system":{"serial":"ap-serial","model":"TNA-301"}}\n200'

    request_mock = AsyncMock(side_effect=request)
    monkeypatch.setattr(transport, "_run_curl", request_mock)
    with pytest.raises(TimeoutError, match="deadline expired"):
        await client.get_device_info()
    assert request_mock.await_count == 1


@pytest.mark.asyncio
async def test_management_subrequests_also_share_the_read_deadline(monkeypatch):
    loop = asyncio.get_running_loop()
    started = loop.time()
    elapsed = 0
    timeouts = []
    client = transport.TachyonClient("192.0.2.10", "synthetic", "synthetic")
    client.read_deadline = started + 50
    monkeypatch.setattr(loop, "time", lambda: started + elapsed)

    async def request(args):
        nonlocal elapsed
        timeouts.append(float(args[args.index("-m") + 1]))
        elapsed += 10
        return 0, b'{}\n200'

    monkeypatch.setattr(transport, "_run_curl", request)
    await client.get_device_info(include_management=True)
    assert timeouts == pytest.approx([30, 30, 30, 20, 10])
    assert client.read_deadline == started + 50


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["login", "read"])
async def test_login_and_reads_use_positive_fractional_remaining_time(monkeypatch, operation):
    loop = asyncio.get_running_loop()
    started = loop.time()
    monkeypatch.setattr(loop, "time", lambda: started)
    client = transport.TachyonClient("192.0.2.10", "synthetic", "synthetic")
    client.read_deadline = started + 0.125
    request = AsyncMock(return_value=(0, b'{}\n200'))
    monkeypatch.setattr(transport, "_run_curl", request)
    if operation == "login":
        await client.login()
    else:
        await client._curl("GET", "/cgi.lua/status?type=system")
    args = request.await_args.args[0]
    assert float(args[args.index("-m") + 1]) == 0.125


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [0, -1, float("nan"), float("inf")])
@pytest.mark.parametrize("operation", ["login", "read"])
async def test_expired_or_invalid_deadline_never_starts_curl(monkeypatch, remaining, operation):
    client = transport.TachyonClient("192.0.2.10", "synthetic", "synthetic")
    client.read_deadline = asyncio.get_running_loop().time() + remaining
    request = AsyncMock()
    monkeypatch.setattr(transport, "_run_curl", request)
    with pytest.raises(TimeoutError):
        if operation == "login":
            await client.login()
        else:
            await client._curl("GET", "/cgi.lua/status?type=system")
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["login", "read"])
async def test_default_timeout_and_cleared_deadline_keep_existing_behavior(monkeypatch, operation):
    client = transport.TachyonClient("192.0.2.10", "synthetic", "synthetic", timeout=19)
    assert client.read_deadline is None
    client.read_deadline = asyncio.get_running_loop().time() - 1
    client.read_deadline = None
    request = AsyncMock(return_value=(0, b'{}\n200'))
    monkeypatch.setattr(transport, "_run_curl", request)
    if operation == "login":
        await client.login()
    else:
        await client._curl("GET", "/cgi.lua/status?type=system")
    args = request.await_args.args[0]
    assert args[args.index("-m") + 1] == "19"


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", [False, True])
async def test_cancellation_or_expiry_kills_and_reaps_real_transport_child(monkeypatch, expiry):
    spawn = asyncio.create_subprocess_exec
    created = asyncio.Event()
    processes = []

    async def child(*args, **kwargs):
        assert args == ("curl", "--disable", "--config", "-")
        process = await spawn(
            sys.executable, "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(60)",
            **kwargs,
        )
        processes.append(process)
        created.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", child)
    client = transport.TachyonClient("192.0.2.10", "synthetic", "synthetic")
    if expiry:
        client.read_deadline = asyncio.get_running_loop().time() + 0.2
    task = asyncio.create_task(client._curl("GET", "/cgi.lua/status?type=system"))
    await asyncio.wait_for(created.wait(), 1)
    if not expiry:
        task.cancel()
    with pytest.raises(TimeoutError if expiry else asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert processes[0].returncode is not None
    assert processes[0].returncode < 0
    assert await processes[0].wait() == processes[0].returncode


@pytest.mark.asyncio
async def test_cancellation_reaps_a_child_that_already_exited(monkeypatch):
    process = AsyncMock()
    process.returncode = 0
    entered = asyncio.Event()

    async def communicate(config=None):
        if config is not None:
            entered.set()
            await asyncio.Event().wait()
        return b"", b""

    process.communicate.side_effect = communicate
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(transport._run_curl(["-s", "https://192.0.2.10"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_not_called()
    assert process.communicate.await_count == 2


@pytest.mark.asyncio
async def test_deadline_timeout_is_sent_in_private_curl_config(monkeypatch):
    client = transport.TachyonClient("192.0.2.10", "synthetic", "synthetic")
    client.read_deadline = asyncio.get_running_loop().time() + 20
    process = AsyncMock(returncode=0)
    process.communicate.return_value = (b'{}\n200', b'')
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    await client._curl("GET", "/cgi.lua/status?type=system")
    config = process.communicate.await_args.args[0].decode()
    timeout_line = next(line for line in config.splitlines() if line.startswith("max-time = "))
    assert 19 < float(json.loads(timeout_line.partition(" = ")[2])) <= 20
