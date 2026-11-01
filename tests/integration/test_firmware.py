"""Verify lab firmware writes and restore the recorded firmware state."""

from datetime import datetime, timezone
import ipaddress
import os
import re
import time

import pytest

from updater.firmware_policy import compare_versions, detect_platform, extract_version_from_filename
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
    """Require a fresh observation and interpret timestamps without an offset as UTC."""
    __tracebackhide__ = True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _request(session, "post", f"/api/aps/{ip}/poll")
            ap = _find_ap(_request(session, "get", "/api/topology"), ip)
            seen = datetime.fromisoformat(ap.get("last_seen", "")) if ap else None
            if seen and seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen and after < seen.timestamp() <= time.time() + 5:
                return ap
        except (FirmwareCheckError, TypeError, ValueError):
            pass
        time.sleep(2)
    raise FirmwareCheckError("Fresh device observation timed out")


def _select_images(ap, files):
    """Require a compatible newer target image and the compatible original image."""
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
    # Send only an upgrade as the target write. Restoration is the only downgrade.
    target = next((item for item in compatible if compare_versions(item[1], original) > 0), None)
    if not rollback:
        pytest.skip("No compatible image is available for the original firmware version")
    if not target:
        pytest.skip("No compatible newer firmware image is available for this device")
    return target, rollback


def _start_update(session, ip, image, allow_current=False):
    __tracebackhide__ = True
    field = {"tna-303l": "firmware_file_303l", "tns-100": "firmware_file_tns100"}.get(
        detect_platform(image), "firmware_file")
    data = {"ip": ip, "firmware_file": image, "bank_mode": "both", field: image}
    address_bench_plan = os.environ.get("SIXTYOPS_TEST_ADDRESS_BENCH_PLAN", "").strip()
    if address_bench_plan:
        data["address_bench_exception"] = address_bench_plan
    result = _request(session, "post", "/api/update-device", data=data)
    if allow_current and result.get("status") == "already_current":
        return None
    _require(result.get("job_id"), "Firmware update job was not started")
    return result["job_id"]


_JOB_DEVICE_FIELDS = (
    "status", "error", "failed_stage", "current_stage", "old_version", "new_version",
    "active_bank", "bank1_version", "bank2_version",
)


def _safe_text(value, ip=None, preserve_version=False):
    """Keep diagnostic text useful without exposing device addresses or secrets."""
    if value is None:
        return "None"
    value = str(value)
    if ip:
        value = value.replace(str(ip), "<device>")
    # Redact addresses before preserving dotted firmware versions. Some firmware
    # versions can look like IPv4 addresses, so only preserve what remains.
    value = re.sub(
        r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/(?:\d{1,2})|:\d{1,5})?(?![\w.])",
        _redact_ipv4,
        value,
    )
    versions = []
    def keep_version(match):
        versions.append(match.group(0))
        return f"\x00VERSION{len(versions) - 1}\x00"
    if preserve_version:
        value = re.sub(r"\b[0-9]+(?:\.[0-9]+){2,}\b", keep_version, value)
    else:
        value = re.sub(r"(?i)\b(?:old_version|new_version|firmware_version|bank[12]_version)\s*=\s*[0-9]+(?:\.[0-9]+){2,}", keep_version, value)
    value = re.sub(
        r"(?i)(?<![\w.])(?:[0-9a-f]{2}(?:[:-])){5}[0-9a-f]{2}(?![\w.])|"
        r"(?<![\w.])(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}(?![\w.])",
        "<mac>",
        value,
    )
    value = re.sub(r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![\w:])", _redact_ipv6, value, flags=re.IGNORECASE)
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>", value)
    value = re.sub(
        r"(?i)\b(password|passwd|token|secret|credential|authorization|api[_-]?key)"
        r"[\"']?\s*[:=]\s*[\"']?[^\s,;\"'}]+[\"']?",
        r"\1=<redacted>", value,
    )
    value = re.sub(r"https?://[^\s,;]+", "<url>", value)
    for index, version in enumerate(versions):
        value = value.replace(f"\x00VERSION{index}\x00", version)
    return value[:240]


def _redact_ipv4(match):
    address = match.group(0).split("/", 1)[0].split(":", 1)[0]
    try:
        ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return match.group(0)
    return "<address>"


def _redact_ipv6(match):
    try:
        ipaddress.IPv6Address(match.group(0))
    except ipaddress.AddressValueError:
        return match.group(0)
    return "<address>"


def _job_failure_message(step, job, ip):
    """Build a private-safe summary from the fields needed to diagnose a job."""
    device = (job.get("devices") or {}).get(ip, {})
    job_fields = (
        ("status", job.get("status")),
        ("cancelled", job.get("cancelled")),
        ("cancel_reason", job.get("cancel_reason")),
    )
    device_fields = ((key, device.get(key)) for key in _JOB_DEVICE_FIELDS)
    job_summary = ", ".join(f"{key}={_safe_text(value, ip)}" for key, value in job_fields)
    version_fields = {"old_version", "new_version", "bank1_version", "bank2_version"}
    device_summary = ", ".join(
        f"{key}={_safe_text(value, ip, preserve_version=key in version_fields)}"
        for key, value in device_fields
    )
    return f"{step} failed; job {job_summary}; device <device>: {device_summary}"


def _run_step(name, operation, ip):
    """Name an operation failure and retain its sanitized exception as the cause."""
    try:
        return operation()
    except Exception as error:
        safe_error = _safe_text(error, ip)
        raise FirmwareCheckError(f"{name} failed: {safe_error}") from error


def _wait_for_job(session, job_id, ip, timeout=600, require_success=True, step="Firmware job"):
    """Wait for this job's terminal state and verify this device's result."""
    __tracebackhide__ = True
    deadline = time.monotonic() + timeout
    last_job = {}
    while time.monotonic() < deadline:
        job = _request(session, "get", f"/api/job/{job_id}")
        last_job = job
        status = job.get("status")
        terminal = status in ("failed", "cancelled") or (
            status == "completed" and job.get("completed_at")
        )
        if terminal:
            if require_success:
                device = job.get("devices", {}).get(ip, {})
                if (status != "completed" or job.get("cancelled")
                        or device.get("status") != "success"):
                    if (device.get("failed_stage") == "address_change_hold"
                            and not os.environ.get("SIXTYOPS_TEST_ADDRESS_BENCH_PLAN", "").strip()):
                        raise FirmwareCheckError(
                            f"{_job_failure_message(step, job, ip)}; set "
                            "SIXTYOPS_TEST_ADDRESS_BENCH_PLAN to the approved lab plan name"
                        )
                    raise FirmwareCheckError(_job_failure_message(step, job, ip))
            return
        time.sleep(2)
    raise FirmwareCheckError(f"{step} completion timed out; "
                             f"{_job_failure_message(step, last_job, ip)}")


def _wait_for_restored_state(session, ip, original, timeout=180):
    """Poll fresh AP state until all original versions and bank numbers return."""
    __tracebackhide__ = True
    fields = ("firmware_version", "active_bank", "bank1_version", "bank2_version")
    deadline = time.monotonic() + timeout
    actual = None
    last_error = None
    while time.monotonic() < deadline:
        remaining = max(1, deadline - time.monotonic())
        try:
            actual = _fresh_ap(session, ip, time.time(), timeout=min(30, remaining))
            if all(actual.get(key) == original.get(key) for key in fields):
                return actual
        except FirmwareCheckError as error:
            last_error = error
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    expected = ", ".join(f"{key}={original.get(key)}" for key in fields)
    observed = ", ".join(f"{key}={actual.get(key) if actual else 'unavailable'}" for key in fields)
    detail = f"Restore state check failed after {timeout} seconds; expected {expected}; actual {observed}"
    if actual is None and last_error:
        detail += f"; last observation: {_safe_text(last_error, ip)}"
    raise FirmwareCheckError(detail)


def _exercise_firmware(session, ip, files):
    __tracebackhide__ = True
    original = _fresh_ap(session, ip, time.time())
    target, rollback = _select_images(original, files)
    job_id = None
    upgrade_error = None
    try:
        job_id = _run_step("Upgrade submission", lambda: _start_update(session, ip, target[0]), ip)
        _wait_for_job(session, job_id, ip, step="Upgrade job")
        upgraded = _run_step("Upgrade state poll", lambda: _fresh_ap(session, ip, time.time()), ip)
        if upgraded.get("firmware_version") != target[1]:
            raise FirmwareCheckError(
                f"Target version check failed: expected {target[1]}, "
                f"actual {upgraded.get('firmware_version')}"
            )
    except Exception as error:
        upgrade_error = error

    restore_error = None
    try:
        # Wait for the writer to stop before starting the recovery write.
        _require(job_id, "Restore precondition failed: upgrade writer status is unknown")
        _wait_for_job(session, job_id, ip, require_success=False, step="Upgrade job settle")
        _run_step("Pre-restore state poll", lambda: _fresh_ap(session, ip, time.time()), ip)
        restore_job = _run_step(
            "Restore submission",
            lambda: _start_update(session, ip, rollback[0], allow_current=True), ip,
        )
        if restore_job:
            _wait_for_job(session, restore_job, ip, step="Restore job")
        _wait_for_restored_state(session, ip, original)
    except Exception as error:
        restore_error = error

    if upgrade_error and restore_error:
        raise FirmwareCheckError(
            f"Upgrade phase failed: {_safe_text(upgrade_error, ip)}; "
            f"restore phase failed: {_safe_text(restore_error, ip)}; "
            "operator recovery may be required"
        ) from upgrade_error
    if upgrade_error:
        raise upgrade_error
    if restore_error:
        raise FirmwareCheckError(
            f"Restore phase failed; operator recovery may be required: "
            f"{_safe_text(restore_error, ip)}"
        ) from restore_error
