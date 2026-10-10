"""Tests for the shared fleet factory in tests/fixtures/fleet.py."""

from updater import database as db
from tests.fixtures.fleet import FAMILY_FILES, make_artifacts, make_fleet


def test_default_fleet_counts(mock_db):
    fleet = make_fleet()
    assert len(fleet["aps"]) == 10
    assert len(fleet["switches"]) == 2
    assert len(fleet["cpes"]) == 40
    assert len(db.get_all_cpes()) == 40


def test_cpes_attach_to_their_ap(mock_db):
    fleet = make_fleet(sites=1, aps_per_site=2, cpes_per_ap=3, switches_per_site=0)
    for ap in fleet["aps"]:
        rows = db.get_cpes_for_ap(ap["ip"])
        assert len(rows) == 3
        assert {r["mac"] for r in rows}.isdisjoint({ap["mac"]})


def test_zero_cpes(mock_db):
    make_fleet(sites=1, aps_per_site=2, cpes_per_ap=0)
    assert db.get_all_cpes() == []


def test_macs_are_unique_and_stored(mock_db):
    fleet = make_fleet(macs_per_device=2)
    devices = fleet["aps"] + fleet["switches"]
    all_macs = [m for d in devices for m in fleet["macs"][d["ip"]]]
    all_macs += [c["mac"] for c in fleet["cpes"]]
    assert len(all_macs) == len(set(all_macs))
    for device in devices:
        macs = fleet["macs"][device["ip"]]
        assert len(macs) == 2
        assert db.get_device(device["ip"])["mac"] == device["mac"] == macs[0]


def test_every_ap_mac_links_to_site_switch(mock_db):
    fleet = make_fleet(macs_per_device=2)
    switch_by_site = {s["tower_site_id"]: s for s in fleet["switches"]}
    assert len(fleet["links"]) == 10 * 2
    for ap in fleet["aps"]:
        switch = switch_by_site[ap["tower_site_id"]]
        for mac in fleet["macs"][ap["ip"]]:
            port = db.get_ap_switch_port(mac)
            assert port["switch_ip"] == switch["ip"]
    ports = {l["ap_ip"]: l["port"] for l in fleet["links"]}
    assert len(set(ports.values())) == 5  # one port per AP in a site


def test_site_without_switch_has_no_links(mock_db):
    fleet = make_fleet(sites=1, aps_per_site=2, switches_per_site=0)
    assert fleet["links"] == []
    assert db.get_ap_switch_port(fleet["aps"][0]["mac"]) is None


def test_artifacts_registered_per_family(mock_db):
    fleet = make_fleet(families=("tna-30x", "tns-100"))
    assert set(fleet["artifacts"]) == {"tna-30x", "tns-100"}
    registered = {r["filename"] for r in db.get_firmware_registry()}
    assert set(fleet["artifacts"].values()) <= registered
    assert FAMILY_FILES["tna-303l"] not in registered
    for name in fleet["artifacts"].values():
        assert db.get_firmware_sha256(name)


def test_make_artifacts_is_idempotent(mock_db):
    first = make_artifacts()
    second = make_artifacts()
    assert first == second
    names = [r["filename"] for r in db.get_firmware_registry()]
    assert len(names) == len(set(names)) == 3


def test_ips_are_unique(mock_db):
    fleet = make_fleet()
    ips = [d["ip"] for d in fleet["aps"] + fleet["switches"]] + [c["ip"] for c in fleet["cpes"]]
    assert len(ips) == len(set(ips))
