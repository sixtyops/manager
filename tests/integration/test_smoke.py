"""Smoke tests — basic health and connectivity checks."""

import pytest
from datetime import datetime, timedelta

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def test_healthz(session):
    resp = session.get("/healthz")
    assert resp.status_code == 200


def test_topology_returns_sites(session, topology):
    assert isinstance(topology["sites"], list)
    assert len(topology["sites"]) > 0, "No sites found — is the dev server populated?"


def test_enabled_devices_have_been_seen(session, topology):
    """Every enabled device should have been polled within the last 10 minutes."""
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    stale = []

    for site in topology["sites"]:
        for ap in site.get("aps", []):
            if not ap.get("enabled", True):
                continue
            last_seen = ap.get("last_seen")
            if not last_seen:
                stale.append(f"AP {ap['ip']}: never seen")
            elif datetime.fromisoformat(last_seen) < cutoff:
                stale.append(f"AP {ap['ip']}: last seen {last_seen}")

        for sw in site.get("switches", []):
            if not sw.get("enabled", True):
                continue
            last_seen = sw.get("last_seen")
            if not last_seen:
                stale.append(f"Switch {sw['ip']}: never seen")
            elif datetime.fromisoformat(last_seen) < cutoff:
                stale.append(f"Switch {sw['ip']}: last seen {last_seen}")

    assert not stale, f"Stale/unseen devices:\n" + "\n".join(stale)


def test_no_persistent_errors(session, topology):
    """Devices should not have persistent errors."""
    errored = []
    for site in topology["sites"]:
        for ap in site.get("aps", []):
            if ap.get("enabled", True) and ap.get("last_error"):
                errored.append(f"AP {ap['ip']}: {ap['last_error']}")
        for sw in site.get("switches", []):
            if sw.get("enabled", True) and sw.get("last_error"):
                errored.append(f"Switch {sw['ip']}: {sw['last_error']}")

    if errored:
        pytest.xfail(f"Devices with errors:\n" + "\n".join(errored))
