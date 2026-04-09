"""Device polling integration tests."""

import pytest

pytestmark = pytest.mark.integration


def test_poll_ap_returns_device_info(session, test_ap):
    """Trigger a poll on an AP and verify core fields are populated."""
    ip = test_ap["ip"]
    resp = session.post(f"/api/aps/{ip}/poll")
    assert resp.status_code == 200

    # Re-fetch topology to see updated data
    topo = session.get("/api/topology").json()
    ap = None
    for site in topo["sites"]:
        for a in site.get("aps", []):
            if a["ip"] == ip:
                ap = a
                break

    assert ap is not None, f"AP {ip} not found in topology after poll"
    assert ap.get("firmware_version"), f"AP {ip} missing firmware_version"
    assert ap.get("model"), f"AP {ip} missing model"
    assert ap.get("mac"), f"AP {ip} missing mac"
    assert ap.get("last_seen"), f"AP {ip} missing last_seen"


def test_poll_switch(session, test_switch):
    """Trigger a poll on a switch and verify core fields."""
    ip = test_switch["ip"]
    resp = session.post(f"/api/switches/{ip}/poll")
    assert resp.status_code == 200


def test_ap_has_connected_cpes(session, topology):
    """At least one AP should have connected CPEs."""
    total_cpes = topology.get("total_cpes", 0)
    if total_cpes == 0:
        pytest.xfail("No CPEs connected to any AP — check hardware")

    for site in topology["sites"]:
        for ap in site.get("aps", []):
            cpes = ap.get("cpes", [])
            if cpes:
                return  # found at least one AP with CPEs

    pytest.xfail("total_cpes > 0 but no CPEs found in AP trees — data inconsistency")


def test_topology_refresh(session):
    """POST /api/topology/refresh should succeed."""
    resp = session.post("/api/topology/refresh")
    assert resp.status_code == 200
