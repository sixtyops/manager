"""Fixture-based Tachyon identity parsing checks."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from updater.vendors.tachyon.client import TachyonClient
from updater.vendors.tachyon import TachyonDriver


FIXTURES = Path(__file__).parent / "fixtures" / "tachyon"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def fixture_body(name):
    return json.dumps(load_fixture(name))


@pytest.mark.parametrize("value,expected", [
    ("dhcp", "dhcp"), ("static", "static"), (" DHCP ", "dhcp"),
    (None, None), ("", None), ("automatic", None), (4, None),
])
def test_management_ip_mode_config_is_normalized(value, expected):
    config = {"network": {"zones": {"wan": {"management": {"proto": value}}}}}
    assert TachyonClient._parse_config_management_ip_mode(config) == (True, True, expected)


def test_management_ip_mode_config_missing_key_is_unknown():
    assert TachyonClient._parse_config_management_ip_mode({"network": {}}) == (True, False, None)


@pytest.mark.parametrize("config,present", [
    (None, False), ([], False), ({"network": None}, False),
    ({"network": {"zones": []}}, False),
])
def test_management_ip_mode_malformed_config_is_unknown(config, present):
    valid, _mode_present, mode = TachyonClient._parse_config_management_ip_mode(config)
    assert valid is present
    assert mode is None


@pytest.mark.parametrize("body", ["{", "[]", "{}", '{"zones": []}'])
def test_management_ip_mode_malformed_status_is_invalid(body):
    assert TachyonClient._parse_management_ip_mode(body) == (False, False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_mode,config_mode,expected", [
    ("dhcp", "dhcp", "dhcp"), ("__missing__", "static", "static"),
    ("dhcp", "static", None), ("automatic", "dhcp", None),
    ("dhcp", "automatic", None), ("dhcp", "__malformed__", None),
    ("__bad_status_json__", "static", None),
    ("dhcp", "__bad_config_json__", None),
])
async def test_management_mode_reads_config_and_reconciles_status(status_mode, config_mode, expected):
    client = TachyonClient("192.0.2.1", "synthetic", "synthetic")
    async def curl(method, endpoint):
        if endpoint.endswith("type=system"):
            return 200, "{}"
        if endpoint == "/cgi.lua/bootbank":
            return 200, "{}"
        if endpoint.endswith("type=interfaces"):
            return 200, "{}"
        if endpoint.endswith("type=zones"):
            if status_mode == "__bad_status_json__":
                return 200, "{"
            zone = {"management_interface": "eth0", "mgmt_vlan_id": 100}
            if status_mode != "__missing__":
                zone["management_ip_mode"] = status_mode
            return 200, json.dumps({"zones": {"wan": zone}})
        if endpoint == "/cgi.lua/config":
            if config_mode == "__bad_config_json__":
                return 200, "{"
            if config_mode == "__malformed__":
                return 200, json.dumps({"network": {"zones": []}})
            return 200, json.dumps({"network": {"zones": {"wan": {
                "management": {"proto": config_mode},
            }}}})
        raise AssertionError(endpoint)
    client._curl = AsyncMock(side_effect=curl)
    info = await client.get_device_info(include_management=True)
    assert info.management_ip_mode == expected
    assert info.management_vlan_id == (None if status_mode == "__bad_status_json__" else 100)
    assert client._curl.await_args_list[-1].args == ("GET", "/cgi.lua/config")


def syntheticize_interface_macs(response):
    """Replace sanitized fixture placeholders with deterministic test MACs."""
    response = json.loads(json.dumps(response))
    interfaces = response.get("interfaces", {})
    replacements = {}
    for row in interfaces.values():
        placeholder = row.get("mac_address") if isinstance(row, dict) else None
        if isinstance(placeholder, str) and placeholder.startswith("<MAC_"):
            if placeholder not in replacements:
                replacements[placeholder] = f"02:00:00:00:00:{len(replacements) + 1:02X}"
            row["mac_address"] = replacements[placeholder]
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("role,model,count,distinct", [
    ("ap", "TNA-301", 63, 52),
    ("switch", "TNS-100", 9, 7),
])
async def test_device_info_parses_system_all_macs_and_management_alias(role, model, count, distinct):
    responses = {
        "/cgi.lua/status?type=system": fixture_body(f"{role}_system.json"),
        "/cgi.lua/bootbank": "{}",
        "/cgi.lua/status?type=interfaces": json.dumps(syntheticize_interface_macs(
            load_fixture(f"{role}_interfaces.json"))),
        "/cgi.lua/status?type=zones": fixture_body(f"{role}_zones.json"),
    }
    client = TachyonClient("placeholder", "user", "password")
    client._curl = AsyncMock(side_effect=lambda _method, endpoint: (200, responses[endpoint]))

    info = await client.get_device_info(include_management=True)

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
async def test_normal_driver_info_includes_switch_management_alias():
    responses = {
        "/cgi.lua/status?type=system": fixture_body("switch_system.json"),
        "/cgi.lua/bootbank": "{}",
        "/cgi.lua/status?type=interfaces": json.dumps(syntheticize_interface_macs(
            load_fixture("switch_interfaces.json"))),
        "/cgi.lua/status?type=zones": fixture_body("switch_zones.json"),
    }
    driver = TachyonDriver("placeholder", "user", "password")
    driver._client._curl = AsyncMock(side_effect=lambda _method, endpoint: (200, responses[endpoint]))

    info = await driver.get_device_info()

    assert info.extra["management_interface"] == "br-wan.12"
    assert info.extra["management_vlan_id"] == 12
    assert info.extra["management_mac"] == info.extra["macs"]["br-wan.12"]


@pytest.mark.asyncio
async def test_ap_info_parses_serial_macs_and_management_fields_from_fixtures():
    responses = {
        "/cgi.lua/status?type=system": fixture_body("ap_system.json"),
        "/cgi.lua/status?type=zones": fixture_body("ap_zones.json"),
        "/cgi.lua/status?type=interfaces": json.dumps(syntheticize_interface_macs(
            load_fixture("ap_interfaces.json"))),
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
async def test_cpe_fixture_parses_serial_every_mac_and_bootbank():
    interfaces = syntheticize_interface_macs(
        load_fixture("cpe_interfaces.json")["reads"][0]["response"])
    responses = {
        "/cgi.lua/status?type=system": json.dumps(load_fixture("cpe_system.json")["reads"][0]["response"]),
        "/cgi.lua/bootbank": json.dumps(load_fixture("cpe_bootbank.json")["reads"][0]["response"]),
        "/cgi.lua/status?type=interfaces": json.dumps(interfaces),
        "/cgi.lua/status?type=zones": "{}",
    }
    client = TachyonClient("placeholder", "user", "password")
    client._curl = AsyncMock(side_effect=lambda _method, endpoint: (200, responses[endpoint]))

    info = await client.get_device_info()

    expected_macs = interfaces["interfaces"]
    assert info.model == "TNA-303X"
    assert info.serial is None
    assert info.macs == {name: row["mac_address"] for name, row in expected_macs.items()}
    assert info.mac == info.macs.get("eth0")
    assert info.active_bank == 1


@pytest.mark.asyncio
async def test_peer_list_fixture_rejects_invalid_cpe_radio_mac(caplog):
    client = TachyonClient("placeholder", "user", "password")
    client._curl = AsyncMock(return_value=(200, fixture_body("ap_peer_list.json")))

    cpes = await client.get_connected_cpes()

    assert len(cpes) == 1
    assert cpes[0].model == "TNA-303X"
    assert cpes[0].mac is None
    assert "Ignoring invalid MAC for peer" in caplog.text


@pytest.mark.asyncio
async def test_peer_list_validity_distinguishes_empty_from_unreadable():
    client = TachyonClient("placeholder", "user", "password")
    client._curl = AsyncMock(side_effect=[
        (503, ""), (200, json.dumps({"wireless": {"peers": []}})),
    ])

    assert await client.get_connected_cpes() == []
    assert client.last_connected_cpes_valid is False
    assert await client.get_connected_cpes() == []
    assert client.last_connected_cpes_valid is True


@pytest.mark.parametrize("serial", [None, "", "  ", "<SERIAL>", "unknown", [], {}])
def test_invalid_serial_claims_are_rejected(serial):
    assert TachyonClient._parse_serial({"serial": serial}) is None


@pytest.mark.parametrize("mac", [
    "not-a-mac",
    "AA:BB:CC:DD:EE",
    "AA:BB:CC:DD:EE:GG",
    "<MAC_ETH0>",
    "<REDACTED>",
    "",
    None,
    123,
])
def test_invalid_interface_macs_are_not_identity_aliases(mac):
    body = json.dumps({"interfaces": {"eth0": {"mac_address": mac}}})

    assert TachyonClient._parse_interface_macs(body) == {}


@pytest.mark.parametrize(("mac", "expected"), [
    ("aabbccddeeff", "AA:BB:CC:DD:EE:FF"),
    ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
    ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
])
def test_valid_interface_macs_are_normalized(mac, expected):
    body = json.dumps({"interfaces": {"eth0": {"mac_address": mac}}})

    assert TachyonClient._parse_interface_macs(body) == {"eth0": expected}


@pytest.mark.asyncio
async def test_missing_identity_fields_fail_safe():
    responses = {
        "/cgi.lua/status?type=system": '{"system": {"model": "TNA-301"}}',
        "/cgi.lua/bootbank": "{}",
        "/cgi.lua/status?type=interfaces": '{"status": "ok"}',
        "/cgi.lua/status?type=zones": '{"status": "ok"}',
    }
    client = TachyonClient("placeholder", "user", "password")
    client._curl = AsyncMock(side_effect=lambda _method, endpoint: (200, responses[endpoint]))

    info = await client.get_device_info(include_management=True)

    assert info.serial is None
    assert info.macs == {}
    assert info.mac is None
    assert info.management_interface is None
    assert info.management_vlan_id is None
    assert info.management_mac is None


def test_missing_interface_and_zone_fields_fail_safe():
    assert TachyonClient._parse_interface_macs("{}") == {}
    assert TachyonClient._parse_management_zone("{}") == (None, None)
