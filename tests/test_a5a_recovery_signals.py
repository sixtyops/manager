"""A5a parser tests using sanitized Tachyon response shapes."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

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
        "read_state": "ok",
        "reason": None,
        "link_up": True,
        "link_rate_mbps": 1000.0,
        "reported": {"status": "up", "speed_mbps": 1000},
        "rx_bytes": 456,
        "tx_bytes": 789,
    }


@pytest.mark.asyncio
async def test_captured_eth0_without_link_fields_or_counters_is_unknown():
    capture = json.loads((FIXTURES / "cpe_interfaces.json").read_text())
    response = capture["reads"][0]["response"]
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(return_value=(200, json.dumps(response)))

    state = await client.get_eth0_recovery_state()

    assert state["link_up"] is None
    assert state["read_state"] == "unknown"
    assert state["reason"] == "missing_rx_bytes,missing_tx_bytes"
    assert state["link_rate_mbps"] is None
    assert state["rx_bytes"] is None
    assert state["tx_bytes"] is None


@pytest.mark.asyncio
async def test_malformed_reported_eth0_link_field_makes_read_unknown():
    response = {"interfaces": {"eth0": {
        "status": "maybe", "rx_bytes": 456, "tx_bytes": 789,
    }}}
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(return_value=(200, json.dumps(response)))

    state = await client.get_eth0_recovery_state()

    assert state["read_state"] == "unknown"
    assert state["reason"] == "invalid_link_state"
    assert state["link_up"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("counter_key", "counter_value", "reason", "expected_rx", "expected_tx"), [
    ("rx_bytes", "invalid", "invalid_rx_bytes", None, 789),
    ("tx_bytes", -1, "invalid_tx_bytes", 456, None),
])
async def test_invalid_eth0_counter_returns_unknown_and_preserves_other_fields(
    counter_key, counter_value, reason, expected_rx, expected_tx
):
    response = {"interfaces": {"eth0": {
        "status": "up", "speed_mbps": 1000, "rx_bytes": 456, "tx_bytes": 789,
    }}}
    response["interfaces"]["eth0"][counter_key] = counter_value
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(return_value=(200, json.dumps(response)))

    state = await client.get_eth0_recovery_state()

    assert state["read_state"] == "unknown"
    assert state["reason"] == reason
    assert state["link_up"] is True
    assert state["link_rate_mbps"] == 1000.0
    assert (state["rx_bytes"], state["tx_bytes"]) == (expected_rx, expected_tx)


@pytest.mark.parametrize("value", [True, -1, 1.5, "1.5", None])
def test_counters_reject_non_integer_or_negative_values(value):
    assert TachyonClient._counter(value) is None


@pytest.mark.parametrize(("value", "expected"), [("100 Mbps", 100), ("1 Gbps", 1000), ("unknown", None)])
def test_link_rate_parser_normalizes_supported_units(value, expected):
    assert TachyonClient._rate_mbps(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "reason"), [
    ("not json", "invalid_json"),
    ('{"status": "ok"}', "missing_interfaces"),
    ('{"interfaces": {}}', "missing_eth0"),
    ('{"interfaces": null}', "invalid_interfaces"),
])
async def test_unavailable_eth0_state_returns_unknown_with_reason(body, reason):
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(return_value=(200, body))

    state = await client.get_eth0_recovery_state()

    assert state["read_state"] == "unknown"
    assert state["reason"] == reason
    assert state["link_up"] is None
    assert state["rx_bytes"] is None
    assert state["tx_bytes"] is None


@pytest.mark.asyncio
async def test_non_200_eth0_state_read_returns_unknown_with_status_reason():
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(return_value=(503, "unavailable"))

    state = await client.get_eth0_recovery_state()

    assert state["read_state"] == "unknown"
    assert state["reason"] == "http_status_503"
    assert state["link_up"] is None
    assert state["rx_bytes"] is None


@pytest.mark.asyncio
async def test_transport_exception_returns_unknown_with_reason():
    client = TachyonClient("192.0.2.10", "user", "password")
    client._curl = AsyncMock(side_effect=TimeoutError)

    state = await client.get_eth0_recovery_state()

    assert state["read_state"] == "unknown"
    assert state["reason"] == "request_failed"
    assert state["link_up"] is None
