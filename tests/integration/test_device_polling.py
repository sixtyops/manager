"""Device polling integration tests."""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def _find_ap(topology: dict, ip: str) -> dict | None:
    for site in topology.get("sites", []):
        for ap in site.get("aps", []):
            if ap.get("ip") == ip:
                return ap
        for switch in site.get("switches", []):
            for ap in switch.get("aps", []) or []:
                if ap.get("ip") == ip:
                    return ap
    return None


def test_poll_ap_returns_device_info(session, test_ap, request_with_retry):
    """Trigger a poll on an AP and verify core fields are populated."""
    ip = test_ap["ip"]
    resp = request_with_retry("POST", f"/api/aps/{ip}/poll", timeout=90.0)
    assert resp.status_code == 200, resp.text[:300]

    # Re-fetch topology to see updated data
    topo = session.get("/api/topology").json()
    ap = _find_ap(topo, ip)

    assert ap is not None, f"AP {ip} not found in topology after poll"
    assert ap.get("firmware_version"), f"AP {ip} missing firmware_version"
    assert ap.get("model"), f"AP {ip} missing model"
    assert ap.get("mac"), f"AP {ip} missing mac"
    assert ap.get("last_seen"), f"AP {ip} missing last_seen"


def test_poll_switch(session, test_switch, request_with_retry):
    """Trigger a poll on a switch and verify core fields."""
    ip = test_switch["ip"]
    resp = request_with_retry("POST", f"/api/switches/{ip}/poll", timeout=90.0)
    assert resp.status_code == 200, resp.text[:300]


def test_ap_has_connected_cpes(test_ap):
    """The dedicated AP should have connected CPEs."""
    assert test_ap.get("cpes"), "Dedicated test AP must have connected CPEs"


def test_topology_refresh(session, request_with_retry):
    """POST /api/topology/refresh should succeed."""
    resp = request_with_retry("POST", "/api/topology/refresh", timeout=90.0)
    assert resp.status_code == 200, resp.text[:300]
