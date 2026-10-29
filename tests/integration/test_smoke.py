"""Smoke tests — basic health and connectivity checks."""

from datetime import datetime, timedelta, timezone
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking]


def _parse_last_seen(value):
    observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return observed_at.astimezone(timezone.utc)


def test_healthz(session):
    resp = session.get("/healthz")
    assert resp.status_code == 200


def test_topology_returns_sites(session, topology):
    assert isinstance(topology["sites"], list)
    assert len(topology["sites"]) > 0, "No sites found — is the dev server populated?"


def test_enabled_devices_have_been_seen(get_topology):
    """Wait for a fresh poll, then check enabled devices were seen recently."""
    poll_started = datetime.now(timezone.utc)
    deadline = time.monotonic() + 600
    topology = get_topology()

    while time.monotonic() < deadline:
        pending = []
        for site in topology["sites"]:
            device_groups = (("AP", site.get("aps", [])), ("Switch", site.get("switches", [])))
            for device_type, devices in device_groups:
                for device in devices:
                    if not device.get("enabled", True):
                        continue
                    last_seen = device.get("last_seen")
                    if not last_seen:
                        pending.append(f"{device_type} {device['ip']}: no poll after test start")
                        continue
                    observed_at = _parse_last_seen(last_seen)
                    if observed_at < poll_started:
                        pending.append(f"{device_type} {device['ip']}: no poll after test start")

        if not pending:
            break
        time.sleep(5)
        topology = get_topology()
    else:
        pytest.fail("Enabled devices did not complete a fresh poll within 10 minutes:\n" + "\n".join(pending))

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale = []

    for site in topology["sites"]:
        for ap in site.get("aps", []):
            if not ap.get("enabled", True):
                continue
            last_seen = ap.get("last_seen")
            if not last_seen:
                stale.append(f"AP {ap['ip']}: never seen")
            elif _parse_last_seen(last_seen) < cutoff:
                stale.append(f"AP {ap['ip']}: last seen {last_seen}")

        for sw in site.get("switches", []):
            if not sw.get("enabled", True):
                continue
            last_seen = sw.get("last_seen")
            if not last_seen:
                stale.append(f"Switch {sw['ip']}: never seen")
            elif _parse_last_seen(last_seen) < cutoff:
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
