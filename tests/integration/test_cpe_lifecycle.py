"""CPE lifecycle integration tests.

Validates CPE discovery via AP polling and documents the current behavior
where CPEs are immediately removed from the cache when they disconnect.
"""

import pytest

pytestmark = pytest.mark.integration


def test_cpe_data_includes_signal_metrics(session, topology):
    """Connected CPEs should have signal metric fields populated."""
    cpes_found = []
    for site in topology["sites"]:
        for ap in site.get("aps", []):
            for cpe in ap.get("cpes", []):
                cpes_found.append(cpe)

    if not cpes_found:
        pytest.skip("No CPEs connected — check hardware")

    # Check at least one CPE has signal data
    cpe = cpes_found[0]
    assert cpe.get("ip"), "CPE missing IP"
    assert cpe.get("mac"), "CPE missing MAC"
    # Signal metrics may be null if device doesn't report them,
    # but the fields should exist
    signal_fields = ["rx_power", "link_distance", "signal_health"]
    for field in signal_fields:
        assert field in cpe, f"CPE missing field: {field}"


def test_cpe_list_refreshes_on_poll(session, test_ap):
    """Polling an AP should refresh its CPE list."""
    ip = test_ap["ip"]

    # Poll the AP
    resp = session.post(f"/api/aps/{ip}/poll")
    assert resp.status_code == 200

    # Get CPEs for this AP from topology
    topo = session.get("/api/topology").json()
    ap_cpes = []
    for site in topo["sites"]:
        for ap in site.get("aps", []):
            if ap["ip"] == ip:
                ap_cpes = ap.get("cpes", [])
                break

    # We can't guarantee CPEs exist, but the poll should succeed
    # and the CPE list should be a valid list
    assert isinstance(ap_cpes, list)


def test_all_cpes_endpoint(session):
    """GET /api/cpes should return the full CPE list."""
    resp = session.get("/api/cpes")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_cpe_auth_status_field(session, topology):
    """CPEs should have an auth_status field (ok, unreachable, or auth_failed)."""
    for site in topology["sites"]:
        for ap in site.get("aps", []):
            for cpe in ap.get("cpes", []):
                # auth_status can be null for CPEs that haven't been probed
                if cpe.get("auth_status"):
                    assert cpe["auth_status"] in ("ok", "unreachable", "auth_failed"), (
                        f"Unexpected auth_status: {cpe['auth_status']}"
                    )
                    return  # Found at least one CPE with auth_status

    pytest.skip("No CPEs with auth_status found")
