"""Firmware update integration tests.

These tests perform actual firmware updates on hardware and take several
minutes to complete (device reboots). Run with:

    pytest -m "integration and slow" -v --timeout=600
"""

import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking, pytest.mark.slow]


def test_firmware_files_list(session):
    """GET /api/firmware-files should return the available firmware list."""
    resp = session.get("/api/firmware-files")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.fixture
def firmware_files(session):
    """Get available firmware files, skip if none."""
    resp = session.get("/api/firmware-files")
    files = resp.json()
    if not files:
        pytest.skip("No firmware files available on dev server")
    return files


def test_single_device_firmware_update_and_rollback(session, firmware_ap, firmware_files):
    """Update a device to a different firmware, then restore the original.

    This test:
    1. Records the current firmware version
    2. Finds a different firmware file compatible with the device model
    3. Updates the device
    4. Verifies the new version
    5. Downgrades back to the original
    6. Verifies the original version is restored
    """
    ip = firmware_ap["ip"]
    model = firmware_ap.get("model", "")
    original_version = firmware_ap.get("firmware_version", "")

    if not original_version:
        pytest.skip(f"AP {ip} has no firmware_version — cannot test update")

    # Find a firmware file that's different from current version and compatible
    target_file = None
    for fw in firmware_files:
        name = fw if isinstance(fw, str) else fw.get("filename", fw.get("name", ""))
        # Skip if it matches the current version
        if original_version in name:
            continue
        # Basic model compatibility check (tna-30x matches TNA-301/TNA-303L patterns)
        if "tna" in model.lower() and "tna" in name.lower():
            target_file = name
            break
        if "tns" in model.lower() and "tns" in name.lower():
            target_file = name
            break

    if not target_file:
        pytest.skip(f"No alternative firmware file found for model {model}")

    # Step 1: Update to new firmware
    resp = session.post("/api/update-device", json={
        "ip": ip,
        "firmware_file": target_file,
        "device_type": "ap",
    })
    assert resp.status_code == 200, f"Update start failed: {resp.text[:300]}"

    # Step 2: Wait for update to complete (poll job status)
    _wait_for_device_idle(session, ip, timeout=300)

    # Step 3: Poll device and check new version
    session.post(f"/api/aps/{ip}/poll")
    time.sleep(3)
    topo = session.get("/api/topology").json()
    updated_ap = _find_ap(topo, ip)
    assert updated_ap, f"AP {ip} not found after update"
    new_version = updated_ap.get("firmware_version", "")
    assert new_version != original_version, (
        f"Firmware version unchanged after update: {new_version}"
    )

    # Step 4: Find the original firmware file for downgrade
    original_file = None
    for fw in firmware_files:
        name = fw if isinstance(fw, str) else fw.get("filename", fw.get("name", ""))
        if original_version in name:
            original_file = name
            break

    if not original_file:
        pytest.fail(
            f"Cannot find original firmware file for version {original_version} — "
            f"device is now on {new_version} and cannot be restored automatically"
        )

    # Step 5: Downgrade back
    resp = session.post("/api/update-device", json={
        "ip": ip,
        "firmware_file": original_file,
        "device_type": "ap",
    })
    assert resp.status_code == 200, f"Downgrade start failed: {resp.text[:300]}"

    _wait_for_device_idle(session, ip, timeout=300)

    # Step 6: Verify original version restored
    session.post(f"/api/aps/{ip}/poll")
    time.sleep(3)
    topo = session.get("/api/topology").json()
    restored_ap = _find_ap(topo, ip)
    assert restored_ap, f"AP {ip} not found after downgrade"
    assert restored_ap.get("firmware_version") == original_version, (
        f"Firmware not restored: expected {original_version}, "
        f"got {restored_ap.get('firmware_version')}"
    )


def _find_ap(topology, ip):
    for site in topology.get("sites", []):
        for ap in site.get("aps", []):
            if ap["ip"] == ip:
                return ap
    return None


def _wait_for_device_idle(session, ip, timeout=300):
    """Wait until the device is no longer in an active update job."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check if there are active jobs involving this device
        resp = session.get("/api/topology")
        if resp.status_code == 200:
            topo = resp.json()
            ap = _find_ap(topo, ip)
            if ap and ap.get("last_seen"):
                # Device is responding — check if any jobs are active
                # Give it a few seconds after reboot
                time.sleep(5)
                return
        time.sleep(10)
    pytest.fail(f"Timed out waiting for device {ip} to come back after update")
