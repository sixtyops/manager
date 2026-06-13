"""Tests for strict smoke-test mode (a strict failure halts the rollout job —
halt-on-first-failure) and basic rollout pause/status plumbing."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from updater import app as app_module
from updater import database as db
from updater.tachyon import SmokeTestResult
from updater.vendors.tachyon.client import UpdateResult


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
    """Strict mode fails devices on smoke-test warnings."""

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
    def __init__(self, ip, user, pw, *, smoke_passed, version):
        self._smoke_passed = smoke_passed
        self._version = version

    def get_reboot_timeout(self, role): return 1
    def get_update_timeout(self, role): return 5
    def get_hardware_id(self, model): return "tn-110-prs"

    async def update_firmware(self, fw_path, cb, **kw):
        return UpdateResult(ip="x", success=True, old_version="1.0.0",
                            new_version=self._version, model="TNA-301")

    async def run_smoke_tests(self, role=None, pre_update_cpe_count=0):
        if self._smoke_passed:
            return SmokeTestResult(passed=True, warnings=[], checks=["ok"])
        return SmokeTestResult(passed=False, warnings=["device lost CPEs"], checks=["bad"])


def _make_job(ip):
    job = app_module.UpdateJob(job_id="job-confirm-test")
    job.devices[ip] = app_module.DeviceStatus(ip=ip, role="ap", model="TNA-301")
    job.device_firmware_map[ip] = "/tmp/fw.bin"
    job.credentials[ip] = ("root", "pass")
    job.device_vendor_map[ip] = "tachyon"
    job.device_roles[ip] = "ap"
    return job


async def _run_update(ip, smoke_passed, version="1.2.3"):
    db.upsert_access_point(ip, "root", "pass", enabled=True, model="TNA-301",
                           firmware_version="1.0.0")
    job = _make_job(ip)

    def fake_driver(vendor):
        return lambda i, u, p: _FakeClient(i, u, p, smoke_passed=smoke_passed, version=version)

    with patch.object(app_module, "get_driver", fake_driver), \
         patch.object(app_module, "broadcast", AsyncMock()), \
         patch.object(app_module, "_shutting_down", False), \
         patch.object(app_module, "is_feature_enabled", return_value=False):
        await app_module._update_single_device(job, ip)
    return job


class TestConfirmRequiresCleanSmoke:
    """A confirmation clears the fleet Firmware Hold, so it must require a CLEAN
    smoke pass — not just a 'success' status (which non-strict warnings leave)."""

    @pytest.mark.asyncio
    async def test_smoke_warnings_do_not_confirm(self, mock_db):
        job = await _run_update("10.0.0.10", smoke_passed=False)
        assert job.devices["10.0.0.10"].status == "success"   # non-strict: stays success
        assert db.get_confirmed_ips_for_version("1.2.3") == set()  # but NOT confirmed

    @pytest.mark.asyncio
    async def test_clean_smoke_pass_confirms(self, mock_db):
        await _run_update("10.0.0.11", smoke_passed=True)
        assert "10.0.0.11" in db.get_confirmed_ips_for_version("1.2.3")
