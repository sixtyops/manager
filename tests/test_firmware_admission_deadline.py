import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from updater import app as app_module
from updater import database as db


SHA = "a" * 64
WINDOW = (1, "2026-11-03")
TARGET = "tna-30x-1.12.3-r55002.bin"


def _timed_job(
    ip="192.0.2.1", firmware_path=TARGET, sha256=SHA, cutoff="2026-11-03T03:30:00+00:00"
):
    db.register_firmware(TARGET, sha256=sha256)
    family = app_module._get_firmware_type_for_model("TNA-301")
    db.record_firmware_window_admission(
        family, sha256, WINDOW, cutoff
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


@pytest.mark.parametrize(
    ("cutoff", "reason"),
    [
        ("2000-01-01T00:00:00+00:00", "cutoff_reached"),
        ("invalid", "timing_unavailable"),
    ],
)
def test_first_write_gate_records_cutoff_deferral(mock_db, monkeypatch, cutoff, reason):
    job = _timed_job(cutoff=cutoff)
    ip = "192.0.2.1"
    monkeypatch.setattr(
        app_module.rollout_gate, "current_scheduled_admission_decision",
        lambda: (True, ""),
    )

    allowed, message = app_module._firmware_write_gate(job, ip)

    assert not allowed
    assert job.devices[ip].status == "pending"
    assert job.devices[ip].deferral_reason == reason
    assert job.devices[ip].deferral_message == message
    assert job._deferred_admission_ips == {ip}


def test_later_same_day_window_defers_every_device_after_cutoff(mock_db, monkeypatch):
    first, second = "192.0.2.1", "192.0.2.2"
    job = _timed_job(first)
    job.devices[second] = app_module.DeviceStatus(ip=second, model="TNA-301")
    job.device_firmware_map[second] = TARGET
    job.device_order.append(second)
    job.device_roles[second] = "ap"
    job.window_key = WINDOW
    monkeypatch.setattr(
        app_module.rollout_gate, "current_scheduled_admission_decision",
        lambda: (True, ""),
    )

    for ip in job.device_order:
        assert not app_module._firmware_write_gate(job, ip)[0]

    assert job._deferred_admission_ips == {first, second}
    assert all(job.devices[ip].status == "pending" for ip in job.device_order)
    assert all(job.devices[ip].deferral_reason == "cutoff_reached" for ip in job.device_order)


@pytest.mark.parametrize("timing", ["missing", "mismatched"])
def test_write_gate_records_unavailable_timing(mock_db, monkeypatch, timing):
    job = _timed_job()
    ip = "192.0.2.1"
    with db.get_db() as conn:
        if timing == "mismatched":
            conn.execute(
                "UPDATE firmware_window_timing SET last_window_key = ?",
                ('[1,"2026-11-04"]',),
            )
        else:
            conn.execute("DELETE FROM firmware_window_timing")
    monkeypatch.setattr(
        app_module.rollout_gate, "current_scheduled_admission_decision",
        lambda: (True, ""),
    )

    allowed, _ = app_module._firmware_write_gate(job, ip)

    assert not allowed
    assert job.devices[ip].status == "pending"
    assert job.devices[ip].deferral_reason == "timing_unavailable"


@pytest.mark.parametrize(
    ("schedule_timezone", "schedule_days", "reason"),
    [
        ("UTC", ["tue"], "window_closed"),
        ("", ["tue"], "timing_unavailable"),
        ("Invalid/Timezone", ["tue"], "timing_unavailable"),
        ("UTC", ["invalid"], "timing_unavailable"),
    ],
)
def test_write_gate_records_window_deferral(
    mock_db, monkeypatch, schedule_timezone, schedule_days, reason
):
    ip = "192.0.2.1"
    job = app_module.UpdateJob(
        job_id="window-closed", device_order=[ip], devices={ip: app_module.DeviceStatus(ip=ip)},
        is_scheduled=True, enforce_window_cutoff=True, window_key=(1, "2026-11-03"),
        start_hour=1, end_hour=5, schedule_timezone=schedule_timezone, schedule_days=schedule_days,
    )
    monkeypatch.setattr(
        app_module.rollout_gate, "current_scheduled_admission_decision",
        lambda: (True, ""),
    )
    monkeypatch.setattr(app_module, "_firmware_deadline_admits", lambda *_a: True)
    allowed, _ = app_module._firmware_write_gate(job, ip)

    assert not allowed
    assert job.devices[ip].status == "pending"
    assert job.devices[ip].deferral_reason == reason
    assert job._deferred_admission_ips == {ip}


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

        async def get_config(self):
            return {"system": {"name": "deadline-test"}}

        async def get_device_info(self):
            prepared.append("device info ready")
            return SimpleNamespace(
                serial="serial-1", current_version="1.0", model="TNA-301"
            )

        def get_hardware_id(self, _model):
            return "deadline-test"

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
@pytest.mark.parametrize(
    ("cutoff", "reason"),
    [
        ("2000-01-01T00:00:00+00:00", "cutoff_reached"),
        ("invalid", "timing_unavailable"),
        ("2099-01-01T00:00:00+00:00", "window_closed"),
    ],
)
async def test_serial_queue_defers_remaining_devices(mock_db, monkeypatch, cutoff, reason):
    first, second = "192.0.2.1", "192.0.2.2"
    job = _timed_job(first, cutoff=cutoff)
    job.device_order.append(second)
    job.device_roles[second] = "ap"
    job.device_firmware_map[second] = TARGET
    job.devices[second] = app_module.DeviceStatus(ip=second, model="TNA-301")
    job.started_at = datetime.now()
    job.enforce_window_cutoff = True
    job.schedule_timezone = "UTC"
    job.schedule_days = ["tue"]
    job.start_hour = 1
    job.end_hour = 5

    attempts = []

    async def update_device(update_job, ip, pass_number=1):
        allowed, message = app_module._firmware_write_gate(update_job, ip)
        attempts.append((ip, message))
        assert not allowed

    broadcast = AsyncMock()
    scheduler = SimpleNamespace(on_job_completed=Mock())
    monkeypatch.setattr(app_module, "_update_single_device", update_device)
    monkeypatch.setattr(app_module, "_scheduled_job_guard", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(app_module, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(app_module, "broadcast", broadcast)
    monkeypatch.setattr(
        app_module.rollout_gate, "current_scheduled_admission_decision", lambda: (True, ""),
    )
    monkeypatch.setattr(app_module.rollout_gate, "firmware_window_key", lambda *_args: None)

    await app_module.run_update_job(job, concurrency=1)

    [(attempted_ip, message)] = attempts
    assert attempted_ip == first
    assert message
    history = db.get_job_history(limit=1)[0]["devices"]
    result = next(
        call.args[0] for call in broadcast.await_args_list if call.args[0]["type"] == "job_completed"
    )
    for devices in (history, result["devices"]):
        for ip in (first, second):
            assert devices[ip]["status"] == "pending"
            assert devices[ip]["deferral_reason"] == reason
            assert devices[ip]["deferral_message"] == message

    scheduler.on_job_completed.assert_called_once()
    for completion in (result, scheduler.on_job_completed.call_args.kwargs):
        assert completion["deferred_count"] == 2
        assert completion["deferral_reasons"] == {first: reason, second: reason}
