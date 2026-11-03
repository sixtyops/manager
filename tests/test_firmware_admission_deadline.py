import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater import app as app_module
from updater import database as db


SHA = "a" * 64
WINDOW = (1, "2026-11-03")
TARGET = "tna-30x-1.12.3-r55002.bin"


def _timed_job(ip="192.0.2.1", firmware_path=TARGET, sha256=SHA):
    db.register_firmware(TARGET, sha256=sha256)
    family = app_module._get_firmware_type_for_model("TNA-301")
    db.record_firmware_window_admission(
        family, sha256, WINDOW, "2026-11-03T03:30:00+00:00"
    )
    return app_module.UpdateJob(
        job_id="deadline",
        device_firmware_map={ip: firmware_path},
        credentials={ip: ("user", "password")},
        device_order=[ip],
        device_roles={ip: "ap"},
        devices={ip: app_module.DeviceStatus(ip=ip, model="TNA-301")},
        is_scheduled=True,
        window_key=WINDOW,
    )


def test_persisted_cutoff_denies_at_equality(mock_db):
    job = _timed_job()

    assert app_module._firmware_deadline_admits(
        job, "192.0.2.1", datetime(2026, 11, 3, 3, 29, 59, tzinfo=timezone.utc)
    )
    assert not app_module._firmware_deadline_admits(
        job, "192.0.2.1", datetime(2026, 11, 3, 3, 30, tzinfo=timezone.utc)
    )


@pytest.mark.asyncio
async def test_slow_preparation_rechecks_cutoff_before_first_action(mock_db, monkeypatch, tmp_path):
    firmware = tmp_path / TARGET
    firmware.write_bytes(b"test firmware")
    sha256 = hashlib.sha256(firmware.read_bytes()).hexdigest()
    job = _timed_job(firmware_path=str(firmware), sha256=sha256)
    ip = "192.0.2.1"
    prepared = []
    writes = []

    class Client:
        def __init__(self, *_args):
            self.ip = ip

        async def connect(self):
            return True

        async def get_device_info(self):
            prepared.append("device info ready")
            return SimpleNamespace(
                serial="serial-1", current_version="1.0", model="TNA-301"
            )

        def get_reboot_timeout(self, _role):
            return 1

        def get_update_timeout(self, _role):
            return 1

        async def update_firmware(self, *_args, **_kwargs):
            writes.append("started")
            raise AssertionError("firmware write must not start")

    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: Client)
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: False)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(app_module, "_capture_ap_recovery_baseline", lambda *_args: _resolved(None))
    monkeypatch.setattr(
        app_module.rollout_gate,
        "current_scheduled_admission_decision",
        lambda: (True, ""),
    )

    def deadline_after_preparation(_job, _ip):
        assert prepared
        return False

    monkeypatch.setattr(app_module, "_firmware_deadline_admits", deadline_after_preparation)
    await app_module._update_single_device(job, ip)

    assert prepared
    assert writes == []
    assert job.devices[ip].status == "pending", job.devices[ip].error


async def _resolved(value):
    return value


def test_started_device_keeps_admission_after_cutoff(mock_db, monkeypatch):
    job = _timed_job()
    ip = "192.0.2.1"
    job.devices[ip]._firmware_sequence_started = True
    monkeypatch.setattr(
        app_module.rollout_gate,
        "current_scheduled_admission_decision",
        lambda: (True, ""),
    )
    monkeypatch.setattr(
        app_module, "_firmware_deadline_admits",
        lambda *_args, **_kwargs: False,
    )

    assert app_module._firmware_write_gate(job, ip) == (True, "")


@pytest.mark.asyncio
async def test_serial_queue_leaves_next_device_pending_after_cutoff(mock_db, monkeypatch):
    first, second = "192.0.2.1", "192.0.2.2"
    job = app_module.UpdateJob(
        job_id="queue-deadline",
        device_order=[first, second],
        device_roles={first: "ap", second: "ap"},
        devices={first: app_module.DeviceStatus(ip=first), second: app_module.DeviceStatus(ip=second)},
        is_scheduled=True,
        schedule_timezone="UTC",
        started_at=datetime.now(),
    )
    touched = []

    async def deferred_first(_job, ip, pass_number=1):
        touched.append(ip)
        _job.devices[ip].status = "pending"
        _job._deferred_admission_ips = {ip}

    async def allowed_guard(_job):
        return True, ""

    async def no_broadcast(_message):
        return None

    monkeypatch.setattr(app_module, "_update_single_device", deferred_first)
    monkeypatch.setattr(app_module, "_scheduled_job_guard", allowed_guard)
    monkeypatch.setattr(app_module, "broadcast", no_broadcast)

    await app_module.run_update_job(job, concurrency=1)

    assert touched == [first]
    assert job.devices[second].status == "pending"
