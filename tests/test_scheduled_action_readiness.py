"""Stop queued scheduled actions after readiness revocation; finish recovery."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from updater import app as app_module
from updater import database as db
from updater.vendors.tachyon import TachyonDriver
from updater.vendors.tachyon.client import SmokeTestResult, UpdateResult
from tests.test_strict_smoke_canary import _make_job


@pytest.fixture
def runtime_mocks(mock_db, monkeypatch):
    db.set_setting("scheduled_rollout_safety_ready", "true")
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module, "get_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda *a: False)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(app_module.services, "validate_time_sources", AsyncMock(
        return_value=(True, datetime(2026, 6, 2, 3, 15))
    ))
    for module in (app_module.slack, app_module.snmp, app_module.webhooks, app_module.email_notifier):
        monkeypatch.setattr(module, "notify_job_completed", AsyncMock())
    monkeypatch.setattr(app_module.telemetry, "send_telemetry_background", AsyncMock())
    monkeypatch.setattr(app_module.syslog_forwarder, "send_event", MagicMock())


def scheduled_job():
    job = _make_job("192.0.2.1")
    job.is_scheduled = True
    job.enforce_window_cutoff = False
    job.start_hour, job.end_hour = 3, 4
    job.schedule_days = ["tue"]
    job.schedule_timezone = "America/Chicago"
    job.started_at = datetime.now()
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reboot", "flash"])
async def test_queued_action_stops_while_submitted_action_recovers(runtime_mocks, monkeypatch, action):
    job = scheduled_job()
    job.device_firmware_map["192.0.2.1"] = "/tmp/tna-30x-1.2.3-r200.bin"
    second = _make_job("192.0.2.2")
    for attribute in ("devices", "credentials", "device_roles", "device_vendor_map", "device_firmware_map"):
        getattr(job, attribute).update(getattr(second, attribute))
    job.pre_update_reboot = action == "reboot"
    started, release, recovered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writes = []

    class Client:
        def __init__(self, ip, *args):
            self.ip = ip
            self.updated = False

        def get_reboot_timeout(self, role): return 5
        def get_update_timeout(self, role): return 5
        async def connect(self): return True
        async def get_device_info(self):
            return SimpleNamespace(
                ip=self.ip, model="TNA-301", serial=f"SERIAL-{self.ip}",
                current_version="1.2.3.200" if self.updated else "1.0.0.100",
                extra={},
            )

        async def recover(self, *, block=True):
            if block:
                writes.append(self.ip)
                started.set()
                await release.wait()
                recovered.set()

        async def reboot(self, **kwargs):
            await self.recover(block=action == "reboot")
            return True

        async def update_firmware(self, *args, **kwargs):
            await self.recover(block=True)
            self.updated = True
            return UpdateResult(ip=self.ip, success=True, old_version="1.0.0.100", new_version="1.2.3.200")

        async def run_smoke_tests(self, **kwargs):
            return SmokeTestResult(checks=[
                {"check": "device_responsive", "passed": True},
                {"check": "config_intact", "passed": True},
                {"check": "cpe_connectivity", "passed": True},
                {"check": "cpe_signal_levels", "passed": True},
            ])

    monkeypatch.setattr(app_module, "get_driver", lambda *a: Client)
    task = asyncio.create_task(app_module.run_update_job(job, concurrency=1))
    try:
        await asyncio.wait_for(started.wait(), 2)
        db.set_setting("scheduled_rollout_safety_ready", "false")
    finally:
        release.set()
        await asyncio.wait_for(task, 5)

    assert writes == ["192.0.2.1"]
    assert recovered.is_set()
    assert job.cancelled
    assert job.devices["192.0.2.2"].status == "cancelled"
    if action == "flash":
        assert job.devices["192.0.2.1"].status == "success"


@pytest.mark.asyncio
async def test_revocation_during_reboot_login_prevents_reboot(runtime_mocks, monkeypatch):
    job = scheduled_job()
    job.pre_update_reboot = True
    client = MagicMock()
    async def connect():
        db.set_setting("scheduled_rollout_safety_ready", "false")
        return True
    client.connect = AsyncMock(side_effect=connect)
    client.reboot = AsyncMock(return_value=True)
    client.update_firmware = AsyncMock()
    monkeypatch.setattr(app_module, "get_driver", lambda *a: lambda *a: client)
    await app_module.run_update_job(job, concurrency=1)
    client.reboot.assert_not_awaited()
    client.update_firmware.assert_not_awaited()
    assert job.cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["info", "upload", "install"])
async def test_driver_rechecks_before_upload_and_install(runtime_mocks, monkeypatch, boundary):
    job = scheduled_job()
    driver = TachyonDriver("192.0.2.1", "user", "pass")
    client = driver._client
    info = SimpleNamespace(current_version="1.2.3", model="TNA-301", bank1_version="1.2.3",
                           bank2_version="1.0.0", active_bank=1)
    async def get_info():
        if boundary == "info":
            db.set_setting("scheduled_rollout_safety_ready", "false")
        return info
    async def upload(*a, **kw):
        if boundary == "upload":
            db.set_setting("scheduled_rollout_safety_ready", "false")
        return True
    async def install():
        db.set_setting("scheduled_rollout_safety_ready", "false")
        return True
    monkeypatch.setattr(client, "login", AsyncMock(return_value=True))
    monkeypatch.setattr(client, "get_device_info", AsyncMock(side_effect=get_info))
    monkeypatch.setattr(client, "upload_firmware", AsyncMock(side_effect=upload))
    monkeypatch.setattr(client, "trigger_update", AsyncMock(side_effect=install))
    monkeypatch.setattr(client, "wait_for_reboot", AsyncMock(return_value=True))
    result = await driver.update_firmware(
        "/tmp/tna-30x-1.2.3.bin", admission_check=lambda: app_module._scheduled_action_decision(job)
    )
    assert client.upload_firmware.await_count == (0 if boundary == "info" else 1)
    assert client.trigger_update.await_count == (1 if boundary == "install" else 0)
    assert client.wait_for_reboot.await_count == (1 if boundary == "install" else 0)
    assert result.success is (boundary == "install")


@pytest.mark.asyncio
async def test_runtime_guard_rechecks_after_time_validation(runtime_mocks, monkeypatch):
    async def validate(*args):
        db.set_setting("scheduled_rollout_safety_ready", "false")
        return True, datetime(2026, 6, 2, 3, 15)
    monkeypatch.setattr(app_module.services, "validate_time_sources", validate)
    allowed, reason = await app_module._scheduled_job_guard(scheduled_job())
    assert not allowed
    assert "safety qualification" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [True, False])
@pytest.mark.parametrize("revocation", ["result", "backoff"])
@pytest.mark.parametrize("revoke_after", [1, 2])
async def test_upload_retries_recheck_current_readiness(
    runtime_mocks, monkeypatch, scheduled, revocation, revoke_after
):
    job = scheduled_job()
    job.is_scheduled = scheduled
    driver = TachyonDriver("192.0.2.1", "user", "pass")
    client = driver._client
    info = SimpleNamespace(current_version="1.2.3", model="TNA-301", bank1_version="1.2.3",
                           bank2_version="1.0.0", active_bank=1)
    attempts = []

    async def run_curl(args):
        attempts.append(args)
        attempt = len(attempts)
        if revocation == "result" and attempt == revoke_after:
            db.set_setting("scheduled_rollout_safety_ready", "false")
        return (0 if attempt == 3 else 1), b"{}"

    async def backoff(delay):
        if revocation == "backoff" and len(attempts) == revoke_after:
            db.set_setting("scheduled_rollout_safety_ready", "false")

    monkeypatch.setattr(client, "login", AsyncMock(return_value=True))
    monkeypatch.setattr(client, "get_device_info", AsyncMock(return_value=info))
    monkeypatch.setattr(client, "trigger_update", AsyncMock(return_value=True))
    monkeypatch.setattr(client, "wait_for_reboot", AsyncMock(return_value=True))
    monkeypatch.setattr("updater.vendors.tachyon.client._run_curl", run_curl)
    monkeypatch.setattr("updater.vendors.tachyon.client.asyncio.sleep", backoff)
    options = {"admission_check": lambda: app_module._scheduled_action_decision(job)} if scheduled else {}

    result = await driver.update_firmware("/tmp/tna-30x-1.2.3.bin", **options)

    assert db.get_setting("scheduled_rollout_safety_ready") == "false"
    assert len(attempts) == (revoke_after if scheduled else 3)
    assert result.success is (not scheduled)
    assert job.cancelled is scheduled
    assert client.trigger_update.await_count == (0 if scheduled else 1)


@pytest.mark.asyncio
async def test_upload_checks_readiness_before_first_attempt(runtime_mocks, monkeypatch):
    job = scheduled_job()
    driver = TachyonDriver("192.0.2.1", "user", "pass")
    run_curl = AsyncMock()
    monkeypatch.setattr("updater.vendors.tachyon.client._run_curl", run_curl)
    db.set_setting("scheduled_rollout_safety_ready", "false")
    result = await driver._client.upload_firmware(
        "/tmp/tna-30x-1.2.3.bin", admission_check=lambda: app_module._scheduled_action_decision(job)
    )
    assert result is False
    assert job.cancelled
    run_curl.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_admission_denial_after_progress_stays_pending(runtime_mocks, monkeypatch):
    job = scheduled_job()
    job.window_key = ("firmware", "2026-06-02T03:00:00Z")
    job.pre_update_reboot = False
    job.device_firmware_map["192.0.2.1"] = "/tmp/tna-30x-1.2.3-r200.bin"
    driver_clients = []
    progress_messages = []
    run_curl = AsyncMock()
    monkeypatch.setattr("updater.vendors.tachyon.client._run_curl", run_curl)

    def make_driver(*args):
        driver = TachyonDriver(*args)
        client = driver._client
        driver_clients.append(client)
        monkeypatch.setattr(client, "login", AsyncMock(return_value=True))
        monkeypatch.setattr(client, "get_device_info", AsyncMock(return_value=SimpleNamespace(
                ip="192.0.2.1", model="TNA-301", serial="SERIAL-1", current_version="1.2.3.100",
            bank1_version="1.2.3", bank2_version="1.0.0", active_bank=1,
            mac=None, macs={}, management_interface=None, management_vlan_id=100,
            management_mac=None, firmware_sha256="a" * 64, management_ip_mode="dhcp",
        )))
        monkeypatch.setattr(client, "reboot", AsyncMock(return_value=True))
        original_update = client.update_firmware

        async def update_with_progress(path, progress, *args, **kwargs):
            def revoke_after_upload_message(ip, message):
                progress_messages.append(message)
                progress(ip, message)
                if message == "Uploading firmware...":
                    db.set_setting("scheduled_rollout_safety_ready", "false")

            return await original_update(path, revoke_after_upload_message, *args, **kwargs)

        monkeypatch.setattr(client, "update_firmware", update_with_progress)
        return driver

    monkeypatch.setattr(app_module, "get_driver", lambda *_: make_driver)
    await app_module._update_single_device(job, "192.0.2.1")

    assert "Uploading firmware..." in progress_messages, (job.devices["192.0.2.1"].status, job.devices["192.0.2.1"].error)
    assert len(driver_clients) == 1
    assert job.devices["192.0.2.1"].status == "pending"
    assert job.devices["192.0.2.1"].error is None
    run_curl.assert_not_awaited()
    assert db.get_firmware_window_stop(job.window_key) is None
