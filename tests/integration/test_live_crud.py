"""Blocking live-dev coverage for reversible CRUD flows."""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def _pick_loopback_ip(offset: int) -> str:
    last = 10 + (offset % 200)
    return f"127.0.10.{last}"


def _find_device(rows: list[dict], ip: str) -> dict | None:
    for row in rows:
        if row.get("ip") == ip:
            return row
    return None


def test_tower_site_device_group_and_bulk_device_flows(session, unique_name):
    site_name = unique_name("live-site")
    group_name = unique_name("live-group")
    seed = int(uuid.uuid4().hex[:4], 16)
    ap_ip = _pick_loopback_ip(seed)
    switch_ip = _pick_loopback_ip(seed + 1)
    cleanup = {"site_id": None, "group_id": None}

    try:
        create_site = session.post("/api/sites", data={"name": site_name, "location": "Live integration site"})
        assert create_site.status_code == 200
        site_id = create_site.json()["id"]
        cleanup["site_id"] = site_id

        add_ap = session.post("/api/aps", data={
            "ip": ap_ip,
            "username": "root",
            "password": "not-a-real-device",
            "tower_site_id": site_id,
        })
        assert add_ap.status_code == 200

        add_switch = session.post("/api/switches", data={
            "ip": switch_ip,
            "username": "admin",
            "password": "not-a-real-device",
            "tower_site_id": site_id,
        })
        assert add_switch.status_code == 200

        disable_ap = session.post("/api/devices/bulk-disable", json={"device_type": "ap", "ips": [ap_ip]})
        disable_switch = session.post("/api/devices/bulk-disable", json={"device_type": "switch", "ips": [switch_ip]})
        assert disable_ap.status_code == 200
        assert disable_switch.status_code == 200

        aps = session.get("/api/aps").json()["aps"]
        switches = session.get("/api/switches").json()["switches"]
        assert _find_device(aps, ap_ip)["enabled"] in (False, 0)
        assert _find_device(switches, switch_ip)["enabled"] in (False, 0)

        enable_ap = session.post("/api/devices/bulk-enable", json={"device_type": "ap", "ips": [ap_ip]})
        enable_switch = session.post("/api/devices/bulk-enable", json={"device_type": "switch", "ips": [switch_ip]})
        assert enable_ap.status_code == 200
        assert enable_switch.status_code == 200

        move_ap = session.post("/api/devices/bulk-move", json={
            "device_type": "ap",
            "ips": [ap_ip],
            "site_id": site_id,
        })
        move_switch = session.post("/api/devices/bulk-move", json={
            "device_type": "switch",
            "ips": [switch_ip],
            "site_id": site_id,
        })
        assert move_ap.status_code == 200
        assert move_switch.status_code == 200

        create_group = session.post("/api/device-groups", json={
            "name": group_name,
            "description": "Live CRUD integration group",
            "filter_json": {"device_type": "ap", "site_ids": [site_id]},
        })
        assert create_group.status_code == 201
        group_id = create_group.json()["id"]
        cleanup["group_id"] = group_id

        resolve_group = session.get(f"/api/device-groups/{group_id}/resolve")
        assert resolve_group.status_code == 200
        resolved = resolve_group.json()
        assert ap_ip in resolved["device_ips"]
        assert resolved["count"] >= 1
    finally:
        if cleanup["group_id"]:
            session.delete(f"/api/device-groups/{cleanup['group_id']}")
        session.delete(f"/api/aps/{ap_ip}")
        session.delete(f"/api/switches/{switch_ip}")
        if cleanup["site_id"]:
            session.delete(f"/api/sites/{cleanup['site_id']}")
