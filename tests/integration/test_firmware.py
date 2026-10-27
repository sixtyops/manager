"""Verify lab firmware writes and restore the recorded firmware state."""

from datetime import datetime
import os
import time

import pytest

from updater.firmware_policy import detect_platform, extract_version_from_filename
from updater.vendors.tachyon.client import TachyonClient

pytestmark = [pytest.mark.integration, pytest.mark.dev_blocking, pytest.mark.slow]


class FirmwareCheckError(Exception):
    """Report a firmware check with a fixed, private-safe message."""


def _require(condition, message):
    __tracebackhide__ = True
    if not condition:
        raise FirmwareCheckError(message)


def _request(session, method, path, **kwargs):
    __tracebackhide__ = True
    try:
        response = getattr(session, method)(path, **kwargs)
        _require(response.status_code == 200, "Firmware API request failed")
        return response.json()
    except FirmwareCheckError:
        raise
    except Exception:
        raise FirmwareCheckError("Firmware API request failed") from None


@pytest.fixture
def firmware_files(session):
    """Read the server's firmware inventory before device writes."""
    __tracebackhide__ = True
    try:
        data = _request(session, "get", "/api/firmware-files")
        files = data.get("files")
        _require(isinstance(files, list), "Firmware inventory is invalid")
        return files
    except Exception:
        pytest.fail("Firmware inventory check failed", pytrace=False)


def test_firmware_files_list(firmware_files):
    """Read the firmware inventory through the live API."""
    __tracebackhide__ = True
    _require(isinstance(firmware_files, list), "Firmware inventory is invalid")


# Let bounded step waits finish recovery before pytest exits.
@pytest.mark.timeout(0)
def test_single_device_firmware_update_and_rollback(session, firmware_files):
    """Verify each firmware step and attempt restoration after every write."""
    __tracebackhide__ = True
    ip = os.environ.get("SIXTYOPS_TEST_FIRMWARE_AP_IP", "").strip()
    if not ip:
        pytest.skip("SIXTYOPS_TEST_FIRMWARE_AP_IP is required")
    try:
        _exercise_firmware(session, ip, firmware_files)
    except Exception as error:
        message = str(error) if isinstance(error, FirmwareCheckError) else "Firmware test failed"
        pytest.fail(message, pytrace=False)


def _find_ap(topology, ip):
    __tracebackhide__ = True
    for site in topology.get("sites", []):
        aps = list(site.get("aps", []))
        for switch in site.get("switches", []):
            aps.extend(switch.get("aps", []) or [])
        for ap in aps:
            if ap.get("ip") == ip:
                return ap
    return None


def _fresh_ap(session, ip, after, timeout=300):
    """Require a successful poll and an observation newer than this step."""
    __tracebackhide__ = True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _request(session, "post", f"/api/aps/{ip}/poll")
            ap = _find_ap(_request(session, "get", "/api/topology"), ip)
            seen = datetime.fromisoformat(ap.get("last_seen", "")) if ap else None
            if seen and after < seen.timestamp() <= time.time() + 5:
                return ap
        except (FirmwareCheckError, TypeError, ValueError):
            pass
        time.sleep(2)
    raise FirmwareCheckError("Fresh device observation timed out")


def _select_images(ap, files):
    """Require compatible, deployable images for the target and original build."""
    __tracebackhide__ = True
    model = ap.get("model", "")
    patterns = TachyonClient._patterns_for_model(model)
    _require(patterns, "Device model is unsupported")
    original = ap.get("firmware_version")
    _require(original, "Original firmware version is missing")
    _require(ap.get("active_bank") in (1, 2), "Original active bank is missing")
    # Two-pass writes can reproduce a baseline with matching bank versions.
    _require(ap.get("bank1_version") == ap.get("bank2_version") == original,
             "Prepare matching original bank versions before this test")
    compatible = []
    for item in files:
        name = item.get("name", "")
        if (item.get("deployable") is True and not item.get("incomplete")
                and detect_platform(name) in patterns):
            version = extract_version_from_filename(name)
            if version:
                compatible.append((name, version))
    rollback = next((item for item in compatible if item[1] == original), None)
    target = next((item for item in compatible if item[1] != original), None)
    _require(rollback, "Compatible original firmware image is required")
    _require(target, "Compatible target firmware image is required")
    return target, rollback


def _start_update(session, ip, image, allow_current=False):
    __tracebackhide__ = True
    field = {"tna-303l": "firmware_file_303l", "tns-100": "firmware_file_tns100"}.get(
        detect_platform(image), "firmware_file")
    data = {"ip": ip, "firmware_file": image, "bank_mode": "both", field: image}
    result = _request(session, "post", "/api/update-device", data=data)
    if allow_current and result.get("status") == "already_current":
        return None
    _require(result.get("job_id"), "Firmware update job was not started")
    return result["job_id"]


def _wait_for_job(session, job_id, ip, timeout=600, require_success=True):
    """Wait for this job's terminal state and verify this device's result."""
    __tracebackhide__ = True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = _request(session, "get", f"/api/job/{job_id}")
        if job.get("status") == "completed" and job.get("completed_at"):
            if require_success:
                device = job.get("devices", {}).get(ip, {})
                _require(not job.get("cancelled") and device.get("status") == "success",
                         "Firmware job failed or did not write the device")
            return
        time.sleep(2)
    raise FirmwareCheckError("Firmware job completion timed out")


def _exercise_firmware(session, ip, files):
    __tracebackhide__ = True
    original = _fresh_ap(session, ip, time.time())
    target, rollback = _select_images(original, files)
    job_id = None
    try:
        job_id = _start_update(session, ip, target[0])
        _wait_for_job(session, job_id, ip)
        upgraded = _fresh_ap(session, ip, time.time())
        _require(upgraded.get("firmware_version") == target[1],
                 "Device did not reach the expected target version")
    finally:
        try:
            # Wait for the writer to stop before starting the recovery write.
            _require(job_id, "Confirm the upgrade writer stopped before recovery")
            _wait_for_job(session, job_id, ip, require_success=False)
            _fresh_ap(session, ip, time.time())
            restore_job = _start_update(session, ip, rollback[0], allow_current=True)
            if restore_job:
                _wait_for_job(session, restore_job, ip)
            restored = _fresh_ap(session, ip, time.time())
            _require(all(restored.get(key) == original.get(key) for key in
                         ("firmware_version", "active_bank", "bank1_version", "bank2_version")),
                     "Original firmware and bank state were not restored")
        except Exception:
            raise FirmwareCheckError("Firmware restoration failed; operator recovery is required") from None
