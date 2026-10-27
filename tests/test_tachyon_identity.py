"""Fixture-based Tachyon identity parsing checks."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from updater.vendors.tachyon.client import TachyonClient


FIXTURES = Path(__file__).parent / "fixtures" / "tachyon"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def fixture_body(name):
    return json.dumps(load_fixture(name))


@pytest.mark.asyncio
@pytest.mark.parametrize("role,model,count,distinct", [
    ("ap", "TNA-301", 63, 52),
    ("switch", "TNS-100", 9, 7),
])
async def test_device_info_parses_system_all_macs_and_management_alias(role, model, count, distinct):
    responses = {
        "/cgi.lua/status?type=system": fixture_body(f"{role}_system.json"),
        "/cgi.lua/bootbank": "{}",
        "/cgi.lua/status?type=interfaces": fixture_body(f"{role}_interfaces.json"),
        "/cgi.lua/status?type=zones": fixture_body(f"{role}_zones.json"),
    }
    client = TachyonClient("placeholder", "user", "password")
    client._curl = AsyncMock(side_effect=lambda _method, endpoint: (200, responses[endpoint]))

    info = await client.get_device_info()

    assert info.model == model
    assert info.serial is None  # Sanitized serial placeholders are not identity claims.
    assert len(info.macs) == count
    assert len(set(info.macs.values())) == distinct
    assert info.management_interface == "br-wan.12"
    assert info.management_vlan_id == 12
    assert info.management_mac == info.macs[info.management_interface]
    assert info.macs["eth0"] == info.management_mac
    assert info.mac == info.macs["eth0"]

    system = load_fixture(f"{role}_system.json")
    system["system"]["serial"] = f"SYNTHETIC-SERIAL-{role.upper()}"
    parsed_system = client._parse_system_status(json.dumps(system))
    assert client._parse_serial(parsed_system) == f"SYNTHETIC-SERIAL-{role.upper()}"


@pytest.mark.asyncio
async def test_ap_info_parses_serial_macs_and_management_fields_from_fixtures():
    responses = {
        "/cgi.lua/status?type=system": fixture_body("ap_system.json"),
        "/cgi.lua/status?type=zones": fixture_body("ap_zones.json"),
        "/cgi.lua/status?type=interfaces": fixture_body("ap_interfaces.json"),
    }
    client = TachyonClient("placeholder", "user", "password")
    client._curl = AsyncMock(side_effect=lambda _method, endpoint: (200, responses[endpoint]))

    info = await client.get_ap_info()

    assert info["model"] == "TNA-301"
    assert info["serial"] is None
    assert len(info["macs"]) == 63
    assert info["management_interface"] == "br-wan.12"
    assert info["management_vlan_id"] == 12
    assert info["management_mac"] == info["macs"]["br-wan.12"]
    assert info["mac"] == info["macs"]["eth0"]


@pytest.mark.asyncio
async def test_peer_list_fixture_keeps_cpe_radio_mac_as_candidate_alias():
    client = TachyonClient("placeholder", "user", "password")
    client._curl = AsyncMock(return_value=(200, fixture_body("ap_peer_list.json")))

    cpes = await client.get_connected_cpes()

    assert len(cpes) == 1
    assert cpes[0].model == "TNA-303X"
    assert cpes[0].mac == "<MAC_CPE_PEER_001>"


@pytest.mark.parametrize("serial", [None, "", "  ", "<SERIAL>", "unknown", [], {}])
def test_invalid_serial_claims_are_rejected(serial):
    assert TachyonClient._parse_serial({"serial": serial}) is None
