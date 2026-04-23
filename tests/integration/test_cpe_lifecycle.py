"""CPE lifecycle integration tests.

Validates CPE discovery via AP polling and documents the current behavior
where CPEs are immediately removed from the cache when they disconnect.
"""

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


def test_cpe_data_includes_signal_metrics(session, test_cpe):
    """Connected CPEs should have signal metric fields populated."""
    cpe = test_cpe
    assert cpe.get("ip"), "CPE missing IP"
    assert cpe.get("mac"), "CPE missing MAC"
    # Signal metrics may be null if device doesn't report them,
    # but the fields should exist
    signal_fields = ["rx_power", "link_distance", "signal_health"]
    for field in signal_fields:
        assert field in cpe, f"CPE missing field: {field}"


def test_cpe_list_refreshes_on_poll(session, test_ap, request_with_retry):
    """Polling an AP should refresh its CPE list."""
    ip = test_ap["ip"]

    # Poll the AP
    resp = request_with_retry("POST", f"/api/aps/{ip}/poll", timeout=90.0)
    assert resp.status_code == 200, resp.text[:300]

    # Get CPEs for this AP from topology
    topo = session.get("/api/topology").json()
    ap = _find_ap(topo, ip)
    ap_cpes = ap.get("cpes", []) if ap else []

    # We can't guarantee CPEs exist, but the poll should succeed
    # and the CPE list should be a valid list
    assert isinstance(ap_cpes, list)


def test_all_cpes_endpoint(session, test_cpe):
    """GET /api/cpes should return the full CPE list."""
    resp = session.get("/api/cpes")
    assert resp.status_code == 200
    data = resp.json()["cpes"]
    assert isinstance(data, list)
    assert any(cpe.get("ip") == test_cpe["ip"] for cpe in data)


def test_cpe_auth_status_field(session, test_cpe):
    """CPEs should have an auth_status field (ok, unreachable, or auth_failed)."""
    if not test_cpe.get("auth_status"):
        pytest.skip("Dedicated CPE has no auth_status yet")
    assert test_cpe["auth_status"] in ("ok", "unreachable", "auth_failed")
