"""A5a parser and CPE-pause tests using sanitized Tachyon response shapes."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from updater.poller import NetworkPoller
from updater.vendors.tachyon.client import TachyonClient


FIXTURES = Path(__file__).parent / "fixtures" / "tachyon"


@pytest.mark.asyncio
async def test_ap_peer_counters_parse_from_captured_peer_shape():
    response = json.loads((FIXTURES / "ap_peer_list.json").read_text())
    peer = response["wireless"]["peers"][0]
    peer.update({"ipv4": "192.0.2.10", "rxBytes": 12345, "txBytes": "67890"})
    client = TachyonClient("192.0.2.1", "user", "password")
    client._curl = AsyncMock(return_value=(200, json.dumps(response)))

    cpes = await client.get_connected_cpes()

    assert (cpes[0].rx_bytes, cpes[0].tx_bytes) == (12345, 67890)


@pytest.mark.asyncio
async def test_cpe_eth0_state_parses_counters_and_only_reported_link_fields():
    capture = json.loads((FIXTURES / "cpe_interfaces.json").read_text())
    response = capture["reads"][0]["response"]
    response["interfaces"]["eth0"].update({
        "status": "up", "speed_mbps": 1000, "rx_bytes": 456, "tx_bytes": 789,
    })
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(return_value=(200, json.dumps(response)))

    state = await client.get_eth0_recovery_state()

    assert state == {
        "link_up": True,
        "link_rate_mbps": 1000.0,
        "reported": {"status": "up", "speed_mbps": 1000},
        "rx_bytes": 456,
        "tx_bytes": 789,
    }


@pytest.mark.asyncio
async def test_captured_eth0_without_link_fields_does_not_infer_carrier_from_disabled():
    capture = json.loads((FIXTURES / "cpe_interfaces.json").read_text())
    response = capture["reads"][0]["response"]
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(return_value=(200, json.dumps(response)))

    state = await client.get_eth0_recovery_state()

    assert state["link_up"] is None
    assert state["link_rate_mbps"] is None
    assert state["rx_bytes"] is None
    assert state["tx_bytes"] is None


@pytest.mark.parametrize("value", [True, -1, 1.5, "1.5", None])
def test_counters_reject_non_integer_or_negative_values(value):
    assert TachyonClient._counter(value) is None


@pytest.mark.parametrize(("value", "expected"), [("100 Mbps", 100), ("1 Gbps", 1000), ("unknown", None)])
def test_link_rate_parser_normalizes_supported_units(value, expected):
    assert TachyonClient._rate_mbps(value) == expected


@pytest.mark.asyncio
async def test_missing_eth0_fails_closed_for_recovery_state_read():
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(return_value=(200, '{"interfaces": {}}'))

    with pytest.raises(RuntimeError, match="Missing eth0"):
        await client.get_eth0_recovery_state()


@pytest.mark.asyncio
async def test_manager_cpe_config_requests_pause_for_recovery_window():
    poller = NetworkPoller()
    fetch = AsyncMock()
    poller._fetch_and_store_config_unlocked = fetch
    poller._cache_cpe_client("192.0.2.10", object())

    async with poller.pause_cpe_requests("192.0.2.10"):
        await poller._fetch_and_store_config("192.0.2.10", "user", "password")
        assert "192.0.2.10" not in poller._cpe_clients
        fetch.assert_not_awaited()

    await poller._fetch_and_store_config("192.0.2.10", "user", "password")
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_pause_waits_for_inflight_cpe_config_request():
    poller = NetworkPoller()
    started = asyncio.Event()
    release = asyncio.Event()
    pause_entered = asyncio.Event()
    pause_release = asyncio.Event()

    async def fetch(*_args):
        started.set()
        await release.wait()

    poller._fetch_and_store_config_unlocked = fetch
    request = asyncio.create_task(
        poller._fetch_and_store_config("192.0.2.10", "user", "password")
    )
    await started.wait()

    async def hold_pause():
        async with poller.pause_cpe_requests("192.0.2.10"):
            pause_entered.set()
            await pause_release.wait()

    paused = asyncio.create_task(hold_pause())
    await asyncio.sleep(0)
    assert not pause_entered.is_set()
    release.set()
    await request
    await pause_entered.wait()
    assert "192.0.2.10" in poller._paused_cpe_ips
    pause_release.set()
    await paused
