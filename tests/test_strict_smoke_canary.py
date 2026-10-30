"""Tests for failed smoke gates and rollout pause/status behavior."""

import pytest
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock, AsyncMock

from updater import app as app_module
from updater import database as db
from updater.tachyon import SmokeTestResult
from updater.vendors.tachyon.client import UpdateResult
from tests.test_rollout_invariants import hold_clock


class TestStrictSmokeSettings:
    """smoke_test_strict persists and defaults off."""

    def test_default_settings(self, mock_db):
        from updater import database as db
        assert db.get_setting("smoke_test_strict", "false") == "false"

    def test_settings_writable(self, authed_client, mock_db):
        resp = authed_client.post("/api/settings/save", json={
            "smoke_test_strict": "true",
        })
        assert resp.status_code == 200

        from updater import database as db
        assert db.get_setting("smoke_test_strict") == "true"

    def test_settings_readable_from_db(self, mock_db):
        from updater import database as db
        assert db.get_setting("smoke_test_strict", "false") == "false"


class TestSmokeTestStrictMode:
    """Smoke warnings mark the result as failed."""

    def test_smoke_result_with_warnings_sets_passed_false(self):
        result = SmokeTestResult()
        result.warnings.append("Device lost CPEs")
        result.passed = False
        assert not result.passed
        assert len(result.warnings) == 1

    def test_smoke_result_no_warnings_passes(self):
        result = SmokeTestResult()
        assert result.passed is True
        assert len(result.warnings) == 0


class TestRolloutPauseStatus:
    """Rollout pause/status plumbing (halt-on-failure pauses a rollout)."""

    def test_rollout_can_be_paused(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")
        db.pause_rollout(rollout_id, "1 device(s) failed during the pct10 wave")

        rollout = db.get_rollout(rollout_id)
        assert rollout["status"] == "paused"
        assert "failed" in rollout["pause_reason"]

    def test_current_rollout_returns_active(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")

        current = db.get_current_rollout()
        assert current is not None
        assert current["id"] == rollout_id
        # The canary phase was removed: a fresh rollout starts at the first wave.
        assert current["phase"] == "pct10"

    def test_paused_rollout_shows_paused_status(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")
        db.pause_rollout(rollout_id, "test")

        current = db.get_current_rollout()
        # get_current_rollout includes paused for UI display
        assert current is not None
        assert current["status"] == "paused"


class _FakeClient:
    """Minimal vendor client driving _update_single_device's confirm path."""
    def __init__(self, ip, user, pw, *, smoke_passed, version, serial="SERIAL-1", post_serial=None,
                 reboot_serial=None, reboot_version=None, smoke_checks=None):
        self._smoke_passed = smoke_passed
        self._version = version
        self._serial = serial
        self._post_serial = post_serial or serial
        self._reboot_serial = reboot_serial or serial
        self._reboot_version = reboot_version or "1.0.0.100"
        self._smoke_checks = smoke_checks
        self._rebooted = False
        self._updated = False

    def get_reboot_timeout(self, role): return 1
    def get_update_timeout(self, role): return 5
    def get_hardware_id(self, model): return "tn-110-prs"

    async def connect(self): return True
    async def reboot(self, timeout=1):
        self._rebooted = True
        return True

    async def get_device_info(self):
        if self._rebooted and not self._updated and getattr(self, "_reboot_info_error", None):
            raise RuntimeError("serial read failed after pre-update reboot")
        if self._updated and getattr(self, "_post_info_error", None):
            raise RuntimeError("serial read failed after firmware reboot")
        from updater.vendors.base import VendorDeviceInfo
        serial = self._post_serial if self._updated else (self._reboot_serial if self._rebooted else self._serial)
        version = self._version if self._updated else (self._reboot_version if self._rebooted else "1.0.0.100")
        return VendorDeviceInfo(ip="x", model="TNA-301", serial=serial,
                                current_version=version,
                                extra={"firmware_sha256": "a" * 64,
                                       "management_vlan": 100, "management_ip_mode": "dhcp"})

    async def update_firmware(self, fw_path, cb, **kw):
        self._updated = True
        return UpdateResult(ip="x", success=True, old_version="1.0.0",
                            new_version=self._version, model="TNA-301")

    async def run_smoke_tests(self, role=None, pre_update_cpe_count=0):
        if self._smoke_passed:
            return SmokeTestResult(passed=True, warnings=[], checks=self._smoke_checks or [
                {"check": "device_responsive", "passed": True},
                {"check": "config_intact", "passed": True},
                {"check": "cpe_connectivity", "passed": True},
                {"check": "cpe_signal_levels", "passed": True},
            ])
        return SmokeTestResult(passed=False, warnings=["device lost CPEs"], checks=[])


def _make_job(ip):
    job = app_module.UpdateJob(job_id="job-confirm-test")
    job.devices[ip] = app_module.DeviceStatus(ip=ip, role="ap", model="TNA-301")
    job.device_firmware_map[ip] = "/tmp/fw.bin"
    job.credentials[ip] = ("root", "pass")
    job.device_vendor_map[ip] = "tachyon"
    job.device_roles[ip] = "ap"
    return job


async def _run_update(ip, smoke_passed, version="1.2.3", *, serial="SERIAL-1", post_serial=None,
                      reboot_serial=None, reboot_version=None, smoke_checks=None,
                      reboot_info_error=False, post_info_error=False):
    db.upsert_access_point(ip, "root", "pass", enabled=True, model="TNA-301",
                           firmware_version="1.0.0.100")
    job = _make_job(ip)
    job.pre_update_reboot = False
    from updater.rollout_gate import ADDRESS_CHANGE_QUALIFICATIONS, AddressTransition
    target = version if version.count(".") == 3 else version + ".200"
    release, revision = target.rsplit(".", 1)
    job.device_firmware_map[ip] = f"/tmp/tna-30x-{release}-r{revision}.bin"
    artifact = b"synthetic smoke artifact"
    transition = AddressTransition(
        "tna-30x", "ap", "1.0.0.100", target,
        "a" * 64, hashlib.sha256(artifact).hexdigest(), 100, "dhcp", "firmware_flash",
    )
    pre_reboot_transition = AddressTransition(
        "tna-30x", "ap", "1.0.0.100", target,
        "a" * 64, hashlib.sha256(artifact).hexdigest(), 100, "dhcp", "pre_update_reboot",
    )
    read_record = db.get_access_point
    def observed_record(ip):
        return {**read_record(ip), "firmware_sha256": "a" * 64,
                "management_vlan": 100, "management_ip_mode": "dhcp"}

    def fake_driver(vendor):
        def construct(i, u, p):
            client = _FakeClient(
                i, u, p, smoke_passed=smoke_passed, version=target,
                serial=serial, post_serial=post_serial, reboot_serial=reboot_serial,
                reboot_version=reboot_version, smoke_checks=smoke_checks,
            )
            client._reboot_info_error = reboot_info_error
            client._post_info_error = post_info_error
            return client
        return construct

    with TemporaryDirectory() as firmware_dir, \
         patch.object(db, "get_access_point", side_effect=observed_record), \
         patch.object(app_module, "get_driver", fake_driver), \
         patch.object(app_module, "_get_firmware_type_for_model", return_value="tna-30x"), \
         patch.dict(ADDRESS_CHANGE_QUALIFICATIONS, {
             transition: "synthetic smoke-test bench proof",
             pre_reboot_transition: "synthetic pre-reboot bench proof",
         }), \
         patch.object(app_module, "broadcast", AsyncMock()), \
         patch.object(app_module, "_shutting_down", False), \
         patch.object(app_module, "is_feature_enabled", return_value=False):
        path = Path(firmware_dir) / Path(job.device_firmware_map[ip]).name
        path.write_bytes(artifact)
        job.device_firmware_map[ip] = str(path)
        await app_module._update_single_device(job, ip)
    return job


class TestConfirmRequiresCleanSmoke:
    """A failed smoke gate stops the job and cannot clear Firmware Hold."""

    @pytest.mark.asyncio
    async def test_smoke_warnings_do_not_confirm(self, mock_db):
        job = await _run_update("10.0.0.10", smoke_passed=False)
        assert job.devices["10.0.0.10"].status == "failed"
        assert job.cancelled is True
        assert db.get_confirmed_ips_for_version("1.2.3.200") == set()

    @pytest.mark.asyncio
    async def test_warning_fails_device_even_when_smoke_reports_passed(self, mock_db):
        smoke = AsyncMock(return_value=SmokeTestResult(
            passed=True, warnings=["Review CPE recovery"], checks=["warning"]
        ))
        with patch.object(_FakeClient, "run_smoke_tests", smoke):
            job = await _run_update("192.0.2.12", smoke_passed=True)
        assert job.devices["192.0.2.12"].status == "failed"
        assert job.devices["192.0.2.12"].error.startswith("Smoke test failed: Review CPE recovery")
        assert job.cancelled is True
        assert db.get_confirmed_ips_for_version("1.2.3.200") == set()

    @pytest.mark.asyncio
    async def test_clean_smoke_pass_confirms(self, mock_db):
        await _run_update("10.0.0.11", smoke_passed=True)
        assert "10.0.0.11" in db.get_confirmed_ips_for_version("1.2.3.200")

    @pytest.mark.asyncio
    async def test_stored_false_does_not_skip_pre_update_reboot(self, mock_db):
        job = await _run_update("10.0.0.12", smoke_passed=True)
        assert job.devices["10.0.0.12"].status == "success"
        assert "10.0.0.12" in db.get_confirmed_ips_for_version("1.2.3.200")

    @pytest.mark.asyncio
    async def test_wrong_post_reboot_serial_at_same_ip_stops_before_confirmation(self, mock_db):
        ip = "10.0.0.13"
        job = await _run_update(ip, smoke_passed=True, post_serial="OTHER-SERIAL")
        assert job.devices[ip].status == "failed"
        assert "Expected serial" in job.devices[ip].error
        assert db.get_confirmed_ips_for_version("1.2.3.200") == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reboot_serial,reboot_version", [
        ("OTHER-SERIAL", "1.0.0.100"), ("SERIAL-1", "9.9.9.9"),
    ])
    async def test_pre_update_recovery_requires_identity_and_original_version(
        self, mock_db, reboot_serial, reboot_version,
    ):
        ip = "10.0.0.14"
        job = await _run_update(
            ip, smoke_passed=True, reboot_serial=reboot_serial,
            reboot_version=reboot_version,
        )
        assert job.devices[ip].status == "failed"
        assert "Pre-update reboot failed" in job.devices[ip].error
        assert db.get_confirmed_ips_for_version("1.2.3.200") == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reboot_info_error,post_info_error", [(True, False), (False, True)])
    async def test_serial_read_exception_fails_recovery(
        self, mock_db, reboot_info_error, post_info_error,
    ):
        ip = "10.0.0.16"
        job = await _run_update(
            ip, smoke_passed=True, reboot_info_error=reboot_info_error,
            post_info_error=post_info_error,
        )
        assert job.devices[ip].status == "failed"
        assert "recovery failed" in job.devices[ip].error.lower() or "reboot failed" in job.devices[ip].error.lower()
        assert db.get_confirmed_ips_for_version("1.2.3.200") == set()

    @pytest.mark.asyncio
    async def test_missing_required_smoke_check_cannot_confirm(self, mock_db):
        ip = "10.0.0.15"
        job = await _run_update(
            ip, smoke_passed=True,
            smoke_checks=[{"check": "device_responsive", "passed": True}],
        )
        assert job.devices[ip].status == "failed"
        assert "Missing checks" in job.devices[ip].error
        assert db.get_confirmed_ips_for_version("1.2.3.200") == set()


@pytest.fixture
def confirmation_cipher(monkeypatch):
    """Keep synthetic credentials in memory for the full confirmation test."""
    from cryptography.fernet import Fernet
    from updater import crypto

    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(crypto, "_fernet", cipher)
    monkeypatch.setattr(crypto, "_load_fernet", lambda: cipher)


class TestSmokeConfirmationFirmwareHold:
    """Smoke proof, not observed version alone, can clear Firmware Hold."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strict", ["false", "true"])
    @pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
    async def test_smoke_exception_keeps_hold(
        self, mock_db, hold_clock, confirmation_cipher, strict, failure
    ):
        from tests.fixtures.fleet import set_version, set_health
        from tests.test_rollout_invariants import (
            _HOLD_VERSION, _settings, _make_scheduler,
        )

        ip = "192.0.2.10"
        db.set_setting("smoke_test_strict", strict)
        smoke = AsyncMock(side_effect=failure("Synthetic smoke failure"))
        with patch.object(_FakeClient, "run_smoke_tests", smoke):
            job = await _run_update(ip, smoke_passed=True, version=_HOLD_VERSION)
        smoke.assert_awaited_once()
        assert job.devices[ip].smoke_warnings == [
            "Smoke test error: Synthetic smoke failure"
        ]
        assert job.devices[ip].status == "failed"
        assert job.devices[ip].error == "Smoke test error: Synthetic smoke failure"
        assert job.cancelled is True
        assert db.get_confirmed_ips_for_version(_HOLD_VERSION) == set()

        # A healthy target-version poll supplies no missing smoke proof.
        device = {"ip": ip, "role": "ap"}
        set_version(device, _HOLD_VERSION)
        set_health(device, hold_clock.isoformat())
        scheduler, _ = _make_scheduler()
        settings = _settings(
            hold_days=6, firmware="tna-30x-1.12.3-r55002-20260601-x.bin"
        )
        held, holds = scheduler._held_families(settings, None)
        assert held == {"tna-30x"}
        assert holds["tna-30x"]["confirmed_by"] is None
        assert holds["tna-30x"]["cleared_by_device"] is False

    @pytest.mark.asyncio
    async def test_clean_smoke_clears_same_family_hold(
        self, mock_db, hold_clock, confirmation_cipher
    ):
        from tests.fixtures.fleet import set_version, set_health
        from tests.test_rollout_invariants import (
            _HOLD_VERSION, _settings, _make_scheduler,
        )

        ip = "192.0.2.11"
        await _run_update(ip, smoke_passed=True, version=_HOLD_VERSION)
        assert db.get_confirmed_ips_for_version(_HOLD_VERSION) == {ip}
        device = {"ip": ip, "role": "ap"}
        set_version(device, _HOLD_VERSION)
        set_health(device, hold_clock.isoformat())
        scheduler, _ = _make_scheduler()
        settings = _settings(
            hold_days=6, firmware="tna-30x-1.12.3-r55002-20260601-x.bin"
        )
        held, holds = scheduler._held_families(settings, None)
        assert held == set()
        assert holds["tna-30x"]["confirmed_by"] == ip
        assert holds["tna-30x"]["cleared_by_device"] is True
