import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from updater import app as app_module
from updater import database as db
from updater import rollout_gate


class _DeviceClient:
    calls = {}
    fail_at = None
    connect_calls = {}

    def __init__(self, ip, username, password):
        self.ip = ip
        self.updated = False
        self.calls.setdefault(ip, []).append("client")

    async def connect(self):
        self.calls[self.ip].append("connect")
        self.connect_calls[self.ip] = self.connect_calls.get(self.ip, 0) + 1
        if (
            self.ip == "192.0.2.1"
            and self.fail_at == "reconnect_error"
            and self.connect_calls[self.ip] == 2
        ):
            raise RuntimeError("reconnect failed")
        return True

    async def get_config(self):
        self.calls[self.ip].append("snapshot")
        if self.ip == "192.0.2.1" and self.fail_at == "snapshot":
            raise RuntimeError("snapshot unavailable")
        return {"system": {"name": "test"}}

    async def get_device_info(self):
        return SimpleNamespace(
            model="TNA-301", serial="SYNTHETIC-SERIAL",
            current_version="2.0.0.1" if self.updated else "1.0.0.100",
            extra={},
        )

    def get_hardware_id(self, model):
        return "test-hardware"

    def get_reboot_timeout(self, role):
        return 1

    def get_update_timeout(self, role):
        return 1

    async def reboot(self, timeout):
        self.calls[self.ip].append("pre_reboot")
        return not (self.ip == "192.0.2.1" and self.fail_at == "pre_reboot")

    async def update_firmware(self, *args, **kwargs):
        self.calls[self.ip].append("flash")
        if self.ip == "192.0.2.1" and self.fail_at == "post_flash_timeout":
            raise asyncio.TimeoutError
        if self.ip == "192.0.2.1" and self.fail_at in ("flash", "recovery"):
            error = "Upload failed" if self.fail_at == "flash" else "Device did not come back online"
            return app_module.UpdateResult(ip=self.ip, success=False, error=error)
        self.updated = True
        return app_module.UpdateResult(ip=self.ip, success=True, old_version="1.0.0.100", new_version="2.0.0.1")

    async def run_smoke_tests(self, **kwargs):
        self.calls[self.ip].append("smoke")
        if self.ip == "192.0.2.1" and self.fail_at == "smoke_exception":
            raise RuntimeError("smoke runner failed")
        passed = not (self.ip == "192.0.2.1" and self.fail_at == "smoke")
        checks = [
            {"check": "device_responsive", "passed": passed},
            {"check": "config_intact", "passed": passed},
            {"check": "cpe_connectivity", "passed": passed},
            {"check": "cpe_signal_levels", "passed": passed},
        ]
        return SimpleNamespace(passed=passed, warnings=[] if passed else ["traffic missing"], checks=checks)


@pytest.fixture
def utc_window_clock(monkeypatch):
    """Keep UTC window tests inside their configured 00:00-23:00 window."""
    instant = datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(app_module, "datetime", Clock)
    return instant


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, expected_gate",
    [
        ("snapshot", "snapshot"),
        ("pre_reboot", "pre_reboot"),
        ("reconnect_error", "pre_reboot"),
        ("flash", "connecting"),
        ("post_flash_timeout", "connecting"),
        ("recovery", "connecting"),
        ("smoke", "smoke"),
        ("smoke_exception", "smoke"),
    ],
)
async def test_first_device_failure_stops_all_second_device_writes(
    mock_db, monkeypatch, failure, expected_gate
):
    _DeviceClient.calls = {}
    _DeviceClient.connect_calls = {}
    _DeviceClient.fail_at = failure
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: _DeviceClient)
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: True)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(db, "save_device_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "save_device_duration", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())
    db.set_setting("smoke_test_strict", "true")

    job = app_module.UpdateJob(
        job_id=f"test-{failure}",
        device_firmware_map={"192.0.2.1": "/tmp/tna-30x-2.0.0-r1.bin", "192.0.2.2": "/tmp/tna-30x-2.0.0-r1.bin"},
        credentials={"192.0.2.1": ("u", "p"), "192.0.2.2": ("u", "p")},
        device_roles={"192.0.2.1": "ap", "192.0.2.2": "ap"},
        devices={
            "192.0.2.1": app_module.DeviceStatus(ip="192.0.2.1", role="ap", model="TNA-301"),
            "192.0.2.2": app_module.DeviceStatus(ip="192.0.2.2", role="ap", model="TNA-301"),
        },
        bank_mode="one",
        pre_update_reboot=True,
        window_key=("firmware", "2026-10-27"),
        started_at=datetime.now(),
    )

    await app_module.run_update_job(job, concurrency=8)

    assert job.devices["192.0.2.1"].status == "failed", (job.cancel_reason, job.devices["192.0.2.1"].error)
    assert job.devices["192.0.2.2"].status == "cancelled"
    second_device_calls = _DeviceClient.calls.get("192.0.2.2", [])
    assert not {"pre_reboot", "flash"}.intersection(second_device_calls)
    stop = db.get_firmware_window_stop(job.window_key)
    assert stop["device_ip"] == "192.0.2.1"
    assert stop["gate"] == expected_gate
    assert stop["reason"]


@pytest.mark.asyncio
async def test_unexpected_device_task_error_stops_window_before_next_device(tmp_path, monkeypatch):
    db_path = tmp_path / "unexpected-device-error.sqlite"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    _DeviceClient.calls = {}
    _DeviceClient.connect_calls = {}
    _DeviceClient.fail_at = None
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: _DeviceClient)
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: True)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(db, "save_device_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "save_device_duration", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())

    original_update = app_module._update_single_device
    attempted = []

    async def raise_for_first_device(job, ip, pass_number=1):
        attempted.append(ip)
        if ip == "192.0.2.1":
            raise RuntimeError("unexpected device task failure")
        await original_update(job, ip, pass_number=pass_number)

    monkeypatch.setattr(app_module, "_update_single_device", raise_for_first_device)
    window_key = ("firmware", "2026-10-27")
    job = app_module.UpdateJob(
        job_id="test-unexpected-device-error",
        device_firmware_map={"192.0.2.1": "/tmp/tna-30x-2.0.0-r1.bin", "192.0.2.2": "/tmp/tna-30x-2.0.0-r1.bin"},
        credentials={"192.0.2.1": ("u", "p"), "192.0.2.2": ("u", "p")},
        device_roles={"192.0.2.1": "ap", "192.0.2.2": "ap"},
        devices={
            "192.0.2.1": app_module.DeviceStatus(ip="192.0.2.1", role="ap", model="TNA-301"),
            "192.0.2.2": app_module.DeviceStatus(ip="192.0.2.2", role="ap", model="TNA-301"),
        },
        bank_mode="one",
        pre_update_reboot=True,
        window_key=window_key,
        started_at=datetime.now(),
    )

    await app_module.run_update_job(job, concurrency=8)

    assert job.devices["192.0.2.1"].status == "failed"
    assert job.devices["192.0.2.2"].status == "cancelled"
    assert attempted == ["192.0.2.1"]
    second_device_calls = _DeviceClient.calls.get("192.0.2.2", [])
    assert not {"pre_reboot", "flash"}.intersection(second_device_calls)
    with sqlite3.connect(db_path) as reopened:
        row = reopened.execute(
            "SELECT device_ip, gate, reason FROM firmware_window_stops"
        ).fetchone()
    assert row == ("192.0.2.1", "update", "Internal device task error: unexpected device task failure")


def test_stopped_window_blocks_device_action_and_resolution_does_not_reopen_it(mock_db, monkeypatch):
    key = ("firmware", "2026-10-27")
    db.stop_firmware_window(key, "old-job", "192.0.2.1", "flash", "Upload failed")
    assert db.get_firmware_window_stop(key)["reason"] == "Upload failed"
    with pytest.raises(app_module.HTTPException):
        app_module._check_firmware_window_admission(key)

    job = app_module.UpdateJob(
        job_id="new-job", devices={"192.0.2.2": app_module.DeviceStatus(ip="192.0.2.2")},
        window_key=key,
    )
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    asyncio.run(app_module._update_single_device(job, "192.0.2.2"))
    assert job.devices["192.0.2.2"].status == "cancelled"

    stop = db.get_firmware_window_stop(key)
    db.resolve_firmware_window_stop(stop["id"], "admin", "Bench device passed the recovery check")
    with pytest.raises(app_module.HTTPException):
        app_module._check_firmware_window_admission(key)
    app_module._check_firmware_window_admission(("firmware", "2026-10-28"))


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff_checks, expected_reboot", [((20, 0), False), ((20, 20, 0), True)])
async def test_queued_device_action_stays_pending_when_window_closes(mock_db, monkeypatch, cutoff_checks, expected_reboot):
    db.set_setting("scheduled_rollout_safety_ready", "true")
    _DeviceClient.calls = {}
    _DeviceClient.fail_at = None
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: _DeviceClient)
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: True)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(db, "save_device_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())
    cutoff_checks_iter = iter(cutoff_checks)
    monkeypatch.setattr(app_module, "_minutes_until_window_end", lambda *args: next(cutoff_checks_iter))
    monkeypatch.setattr(rollout_gate, "firmware_window_key", lambda *args: ("firmware", "2026-10-27"))
    job = app_module.UpdateJob(
        job_id="cutoff", device_firmware_map={"192.0.2.1": "/tmp/tna-30x-2.0.0-r1.bin"},
        credentials={"192.0.2.1": ("u", "p")}, device_roles={"192.0.2.1": "ap"},
        devices={"192.0.2.1": app_module.DeviceStatus(ip="192.0.2.1", role="ap", model="TNA-301")},
        is_scheduled=True, start_hour=0, end_hour=23,
        schedule_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        schedule_timezone="UTC", window_key=("firmware", "2026-10-27"),
    )

    await app_module._update_single_device(job, "192.0.2.1")

    assert job.devices["192.0.2.1"].status == "pending"
    assert "retry next window" in job.devices["192.0.2.1"].progress_message
    assert ("pre_reboot" in _DeviceClient.calls["192.0.2.1"]) is expected_reboot
    assert "flash" not in _DeviceClient.calls["192.0.2.1"]


def test_restart_routes_keep_failed_window_closed_and_record_resolution(tmp_path, monkeypatch):
    db_path = tmp_path / "restart.sqlite"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    failed_key = ("firmware", "2026-10-27")
    next_key = ("firmware", "2026-10-28")
    db.stop_firmware_window(failed_key, "old-job", "192.0.2.1", "flash", "Upload failed")

    # Database helpers open fresh connections, as they do after a process restart.
    with db.get_db() as reopened:
        assert reopened.execute("SELECT window_key FROM firmware_window_stops").fetchone()[0] == '["firmware","2026-10-27"]'

    rollout_id = db.create_rollout("firmware.bin")
    db.pause_rollout(rollout_id, "test pause")
    expires = (datetime.now() + timedelta(hours=1)).isoformat()
    with db.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)", ("admin", "admin", "local"))
        conn.execute(
            "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
            ("restart-session", "admin", "127.0.0.1", expires),
        )
    client = TestClient(app_module.app, headers={"Origin": "http://testserver"})
    client.cookies.set("session_id", "restart-session")
    monkeypatch.setattr(rollout_gate, "firmware_window_key", lambda settings, now=None: failed_key)

    resume = client.post(f"/api/rollout/{rollout_id}/resume")
    manual = client.post("/api/start-update", data={
        "firmware_file": "missing.bin", "device_type": "ap", "ip_list": "192.0.2.2",
    })
    assert resume.status_code == 409
    assert manual.status_code == 409
    with pytest.raises(RuntimeError, match="Upload failed"):
        asyncio.run(app_module._start_scheduled_update(
            ap_ips=["192.0.2.2"], firmware_file="missing.bin", start_hour=1,
            end_hour=2, schedule_days=["tue"], schedule_timezone="UTC", window_key=failed_key,
        ))

    stop_id = db.get_firmware_window_stop(failed_key)["id"]
    monkeypatch.setattr(rollout_gate, "firmware_window_key", lambda settings, now=None: next_key)
    resolved = client.post(f"/api/firmware-window-stops/{stop_id}/resolve", data={
        "resolution": "Bench recovery verified",
    })
    assert resolved.status_code == 200
    record = db.get_firmware_window_stop(failed_key)
    assert record["resolved_by"] == "admin"
    assert record["resolution"] == "Bench recovery verified"
    assert record["resolved_at"]
    with pytest.raises(app_module.HTTPException):
        app_module._check_firmware_window_admission(failed_key)


@pytest.mark.parametrize(
    "case,stored_key,current_key,expected_status",
    [
        ("same window", '["firmware","2026-10-27"]', ("firmware", "2026-10-27"), 409),
        ("earlier window", '["firmware","2026-10-28"]', ("firmware", "2026-10-27"), 409),
        ("later window", '["firmware","2026-10-26"]', ("firmware", "2026-10-27"), 200),
        ("legacy key", '"2026-10-26"', ("firmware", "2026-10-27"), 409),
        ("malformed key", '["firmware","not-a-date"]', ("firmware", "2026-10-27"), 409),
        # A window that starts on Oct 26 and ends on Oct 27 keeps the Oct 26
        # identity after midnight, so it is still the stopped window.
        ("same window after midnight", '["firmware","2026-10-26"]', ("firmware", "2026-10-26"), 409),
    ],
)
def test_configured_stop_resolution_requires_a_later_parsed_window(
    monkeypatch, case, stored_key, current_key, expected_status,
):
    stop_id = 12
    resolved = {"id": stop_id, "resolved_at": "now"}
    monkeypatch.setattr(db, "get_unresolved_firmware_window_stops", lambda: [{"id": stop_id, "window_key": stored_key}])
    monkeypatch.setattr(db, "get_all_settings", lambda: {})
    monkeypatch.setattr(rollout_gate, "firmware_window_key", lambda _settings: current_key)
    monkeypatch.setattr(db, "resolve_firmware_window_stop", lambda *args: resolved)
    monkeypatch.setattr(db, "log_audit", lambda *args: None)

    if expected_status == 409:
        with pytest.raises(app_module.HTTPException) as error:
            asyncio.run(app_module.resolve_firmware_window_stop_api(
                stop_id, "Recovery verified", {"username": "admin"},
            ))
        assert error.value.status_code == expected_status, case
    else:
        result = asyncio.run(app_module.resolve_firmware_window_stop_api(
            stop_id, "Recovery verified", {"username": "admin"},
        ))
        assert result == resolved, case


@pytest.mark.asyncio
async def test_manual_local_day_stop_survives_reopen_and_blocks_manual_routes(
    tmp_path, monkeypatch, utc_window_clock,
):
    db_path = tmp_path / "manual-local-day.sqlite"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    db.set_settings({
        "schedule_days": "",
        "schedule_start_hour": "0",
        "schedule_end_hour": "1",
        "schedule_timezone": "UTC",
    })
    _DeviceClient.calls = {}
    _DeviceClient.fail_at = "flash"
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: _DeviceClient)
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: True)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(db, "save_device_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "save_device_duration", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())
    assert rollout_gate.firmware_window_key(db.get_all_settings()) is None
    failed_key = app_module._manual_firmware_window_key()
    assert failed_key[0] == "manual"

    job = app_module.UpdateJob(
        job_id="manual-local-day-failure",
        device_firmware_map={"192.0.2.1": "/tmp/tna-30x-2.0.0-r1.bin"},
        credentials={"192.0.2.1": ("u", "p")},
        device_roles={"192.0.2.1": "ap"},
        devices={"192.0.2.1": app_module.DeviceStatus(ip="192.0.2.1", role="ap", model="TNA-301")},
        window_key=failed_key,
        started_at=datetime.now(),
    )
    await app_module.run_update_job(job, concurrency=1)
    assert job.devices["192.0.2.1"].status == "failed"
    _DeviceClient.fail_at = None

    # Read the stop through a new database connection, as after a process restart.
    with db.get_db() as reopened:
        assert reopened.execute("SELECT window_key FROM firmware_window_stops").fetchone()[0] == '["manual","2026-10-27"]'

    expires = (datetime.now() + timedelta(hours=1)).isoformat()
    with db.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)", ("admin", "admin", "local"))
        conn.execute(
            "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
            ("manual-local-day-session", "admin", "127.0.0.1", expires),
        )
    client = TestClient(app_module.app, headers={"Origin": "http://testserver"})
    client.cookies.set("session_id", "manual-local-day-session")

    start_update = client.post("/api/start-update", data={
        "firmware_file": "missing.bin", "device_type": "ap", "ip_list": "192.0.2.2",
    })
    update_device = client.post("/api/update-device", data={
        "ip": "192.0.2.2", "firmware_file": "missing.bin",
    })
    assert start_update.status_code == 409
    assert update_device.status_code == 409

    stop_id = db.get_firmware_window_stop(failed_key)["id"]
    same_day_resolution = client.post(f"/api/firmware-window-stops/{stop_id}/resolve", data={
        "resolution": "Same-day retry must stay blocked",
    })
    assert same_day_resolution.status_code == 409

    next_day = datetime.fromisoformat(failed_key[1]) + timedelta(days=1)

    class NextDayDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = next_day.replace(hour=12, minute=0, second=0, microsecond=0)
            return value.replace(tzinfo=tz) if tz else value

    monkeypatch.setattr(app_module, "datetime", NextDayDateTime)
    resolved = client.post(f"/api/firmware-window-stops/{stop_id}/resolve", data={
        "resolution": "Bench recovery verified",
    })
    assert resolved.status_code == 200
    record = db.get_firmware_window_stop(failed_key)
    assert record["resolved_by"] == "admin"
    assert record["resolved_at"]
    assert app_module._manual_firmware_window_key() != failed_key
    app_module._check_firmware_window_admission(app_module._manual_firmware_window_key())
    assert client.post("/api/start-update", data={
        "firmware_file": "missing.bin", "device_type": "ap", "ip_list": "192.0.2.2",
    }).status_code == 400


def test_resolving_manual_stop_does_not_clear_configured_window_stop(mock_db):
    db.set_settings({
        "schedule_days": "",
        "schedule_start_hour": "0",
        "schedule_end_hour": "1",
        "schedule_timezone": "UTC",
    })
    today = datetime.now().astimezone().date()
    manual_key = ("manual", (today - timedelta(days=1)).isoformat())
    configured_key = ("firmware", (today - timedelta(days=2)).isoformat())
    manual_stop = db.stop_firmware_window(manual_key, "manual-job", "192.0.2.2", "flash", "Upload failed")
    configured_stop = db.stop_firmware_window(
        configured_key, "scheduled-job", "192.0.2.1", "flash", "Upload failed",
    )
    expires = (datetime.now() + timedelta(hours=1)).isoformat()
    with db.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)", ("admin", "admin", "local"))
        conn.execute(
            "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
            ("unrelated-configured-stop-session", "admin", "127.0.0.1", expires),
        )
    client = TestClient(app_module.app, headers={"Origin": "http://testserver"})
    client.cookies.set("session_id", "unrelated-configured-stop-session")

    response = client.post(f"/api/firmware-window-stops/{manual_stop['id']}/resolve", data={
        "resolution": "Manual device recovery was verified",
    })

    assert response.status_code == 200
    assert db.get_firmware_window_stop(manual_key)["resolved_at"]
    assert db.get_firmware_window_stop(configured_key)["resolved_at"] is None
    assert db.get_firmware_window_stop(configured_key)["id"] == configured_stop["id"]


def test_manual_fallback_admission_is_blocked_by_configured_window_stop(mock_db, monkeypatch):
    configured_key = ("firmware", "2026-10-27")
    local_day_key = ("manual", "2026-10-27")
    db.stop_firmware_window(configured_key, "scheduled-job", "192.0.2.1", "flash", "Upload failed")
    monkeypatch.setattr(rollout_gate, "firmware_window_key", lambda *args: None)
    monkeypatch.setattr(app_module, "_manual_firmware_window_key", lambda now=None: local_day_key)

    manual_key = rollout_gate.firmware_window_key(db.get_all_settings()) or app_module._manual_firmware_window_key()
    assert manual_key == local_day_key
    assert manual_key != configured_key

    expires = (datetime.now() + timedelta(hours=1)).isoformat()
    with db.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)", ("admin", "admin", "local"))
        conn.execute(
            "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
            ("configured-stop-session", "admin", "127.0.0.1", expires),
        )
    client = TestClient(app_module.app, headers={"Origin": "http://testserver"})
    client.cookies.set("session_id", "configured-stop-session")

    response = client.post("/api/start-update", data={
        "firmware_file": "missing.bin", "device_type": "ap", "ip_list": "192.0.2.2",
    })

    assert response.status_code == 409
    assert "failure resolution" in response.text
    assert db.get_firmware_window_stop(configured_key)["reason"] == "Upload failed"


@pytest.mark.asyncio
async def test_manual_fallback_job_does_not_bypass_scheduled_one_wave_gate(
    mock_db, monkeypatch, utc_window_clock,
):
    db.set_setting("scheduled_rollout_safety_ready", "true")
    settings = {
        "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
        "schedule_start_hour": 0,
        "schedule_end_hour": 23,
        "schedule_timezone": "UTC",
    }
    now = utc_window_clock
    configured_key = rollout_gate.firmware_window_key(settings, now)
    assert configured_key is not None

    _DeviceClient.calls = {}
    _DeviceClient.fail_at = None
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: _DeviceClient)
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: True)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(db, "save_device_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "save_device_duration", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())

    with monkeypatch.context() as manual_path:
        manual_path.setattr(rollout_gate, "firmware_window_key", lambda *args: None)
        manual_key = rollout_gate.firmware_window_key(db.get_all_settings()) or app_module._manual_firmware_window_key(now)
        assert manual_key[0] == "manual"
        assert manual_key != configured_key
        manual_job = app_module.UpdateJob(
            job_id="manual-fallback-before-rollout",
            device_firmware_map={"192.0.2.1": "/tmp/tna-30x-2.0.0-r1.bin"},
            credentials={"192.0.2.1": ("u", "p")},
            device_roles={"192.0.2.1": "ap"},
            devices={"192.0.2.1": app_module.DeviceStatus(ip="192.0.2.1", role="ap", model="TNA-301")},
            window_key=manual_key,
            started_at=datetime.now(),
        )
        await app_module.run_update_job(manual_job, concurrency=1)

    assert manual_job.devices["192.0.2.1"].status == "success"

    firmware = "tna-30x-1.2.3-r123.bin"
    db.set_settings({
        "schedule_enabled": "true",
        "timezone": "UTC",
        "schedule_days": settings["schedule_days"],
        "schedule_start_hour": "0",
        "schedule_end_hour": "23",
        "selected_firmware_30x": firmware,
        "weather_check_enabled": "false",
        "firmware_canary_hold_days": "0",
    })
    rollout_id = db.create_rollout(firmware)
    db.complete_rollout_phase(rollout_id)
    db.set_rollout_phase_window(rollout_id, configured_key)

    from updater.scheduler import AutoUpdateScheduler

    start_update = AsyncMock()
    scheduler = AutoUpdateScheduler(AsyncMock(), start_update)
    monkeypatch.setattr(app_module.services, "validate_time_sources", AsyncMock(return_value=(True, now)))
    monkeypatch.setattr(scheduler, "_firmware_changed", lambda *args: False)
    monkeypatch.setattr(scheduler, "_held_families", lambda *args: (set(), {}))

    await scheduler._check_and_run()

    assert db.get_active_rollout()["phase"] == "pct50"
    assert db.get_active_rollout()["last_phase_window"] == '["firmware","' + configured_key[1] + '"]'
    start_update.assert_not_awaited()
    assert scheduler._state == "waiting"


def test_window_key_stays_constant_across_midnight():
    schedule = {
        "schedule_days": "mon",
        "schedule_start_hour": 23,
        "schedule_end_hour": 2,
        "schedule_timezone": "UTC",
    }
    before_midnight = rollout_gate.firmware_window_key(schedule, datetime.fromisoformat("2026-10-26T23:30:00+00:00"))
    after_midnight = rollout_gate.firmware_window_key(schedule, datetime.fromisoformat("2026-10-27T01:30:00+00:00"))
    assert before_midnight == after_midnight == ("firmware", "2026-10-26")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["pre_reboot", "flash"])
@pytest.mark.parametrize("closure", ["readiness", "window"])
@pytest.mark.parametrize("scheduled", [True, False])
async def test_current_admission_after_connect_blocks_scheduled_writes(
    mock_db, monkeypatch, boundary, closure, scheduled,
):
    db.set_setting("scheduled_rollout_safety_ready", "true")
    state = {"closed": False}
    _DeviceClient.calls = {}
    _DeviceClient.connect_calls = {}
    _DeviceClient.fail_at = None

    class ClosingClient(_DeviceClient):
        async def connect(self):
            result = await super().connect()
            state["closed"] = True
            if closure == "readiness":
                db.set_setting("scheduled_rollout_safety_ready", "false")
            return result

    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: ClosingClient)
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: True)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(db, "save_device_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "save_device_duration", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(app_module, "_minutes_until_window_end", lambda *args: 60)
    key = ("firmware", "2026-10-27")
    monkeypatch.setattr(
        rollout_gate, "firmware_window_key",
        lambda *args: None if closure == "window" and state["closed"] else key,
    )
    job = app_module.UpdateJob(
        job_id="admission-closes-during-connect",
        device_firmware_map={"192.0.2.1": "/tmp/tna-30x-2.0.0-r1.bin"},
        credentials={"192.0.2.1": ("u", "p")}, device_roles={"192.0.2.1": "ap"},
        devices={"192.0.2.1": app_module.DeviceStatus(ip="192.0.2.1", role="ap", model="TNA-301")},
        is_scheduled=scheduled, pre_update_reboot=boundary == "pre_reboot",
        start_hour=0, end_hour=23, schedule_days=["tue"],
        schedule_timezone="UTC", window_key=key,
    )

    await app_module._update_single_device(job, "192.0.2.1")

    calls = _DeviceClient.calls["192.0.2.1"]
    assert "connect" in calls
    assert (boundary in calls) is not scheduled
    assert ("flash" in calls) is not scheduled
    if scheduled:
        assert job.devices["192.0.2.1"].status == ("cancelled" if closure == "readiness" else "pending")
        assert job.cancelled is (closure == "readiness")
    else:
        assert job.devices["192.0.2.1"].status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("closure", ["cutoff", "readiness", "window_stop"])
async def test_driver_login_rechecks_write_gate_before_upload_and_trigger(
    mock_db, monkeypatch, closure,
):
    db.set_setting("scheduled_rollout_safety_ready", "true")
    key = ("firmware", "2026-10-27")
    state = {"minutes_until_end": 60}

    class SlowLoginClient(_DeviceClient):
        async def update_firmware(self, *args, admission_check=None, **kwargs):
            self.calls[self.ip].append("driver_login")
            await asyncio.sleep(0)
            if closure == "cutoff":
                state["minutes_until_end"] = 0
            elif closure == "readiness":
                db.set_setting("scheduled_rollout_safety_ready", "false")
            else:
                app_module._stop_firmware_window(
                    job, "192.0.2.2", "upload", "Another device failed",
                )

            admitted, reason = admission_check()
            if not admitted:
                return app_module.UpdateResult(ip=self.ip, success=False, error=reason)

            self.calls[self.ip].append("upload")
            admitted, reason = admission_check()
            if not admitted:
                return app_module.UpdateResult(ip=self.ip, success=False, error=reason)

            self.calls[self.ip].append("trigger")
            return app_module.UpdateResult(ip=self.ip, success=True, old_version="1", new_version="2")

    _DeviceClient.calls = {}
    _DeviceClient.connect_calls = {}
    _DeviceClient.fail_at = None
    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: SlowLoginClient)
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: False)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module, "_minutes_until_window_end", lambda *args: state["minutes_until_end"])
    monkeypatch.setattr(rollout_gate, "firmware_window_key", lambda *args: key)
    job = app_module.UpdateJob(
        job_id=f"driver-login-{closure}",
        device_firmware_map={"192.0.2.1": "/tmp/tna-30x-2.0.0-r1.bin"},
        credentials={"192.0.2.1": ("u", "p")},
        device_roles={"192.0.2.1": "ap"},
        devices={"192.0.2.1": app_module.DeviceStatus(ip="192.0.2.1", role="ap", model="TNA-301")},
        is_scheduled=True, start_hour=0, end_hour=23, schedule_days=["tue"],
        schedule_timezone="UTC", window_key=key, pre_update_reboot=False,
    )

    await app_module._update_single_device(job, "192.0.2.1")

    calls = _DeviceClient.calls["192.0.2.1"]
    assert "driver_login" in calls
    assert "upload" not in calls
    assert "trigger" not in calls
    if closure in ("cutoff", "readiness"):
        assert job.devices["192.0.2.1"].status == "pending"
        assert job.devices["192.0.2.1"].error is None
        assert db.get_firmware_window_stop(key) is None
    else:
        assert job.devices["192.0.2.1"].status == "cancelled"
        stop = db.get_firmware_window_stop(key)
        assert stop["device_ip"] == "192.0.2.2"


@pytest.mark.asyncio
async def test_driver_login_device_failure_still_stops_window(mock_db, monkeypatch, utc_window_clock):
    key = ("firmware", "2026-10-27")
    db.set_setting("scheduled_rollout_safety_ready", "true")

    class FailedUpdateClient(_DeviceClient):
        async def update_firmware(self, _path, progress, **kwargs):
            progress(self.ip, "Uploading firmware...")
            return app_module.UpdateResult(ip=self.ip, success=False, error="Upload failed")

    monkeypatch.setattr(app_module, "get_driver", lambda _vendor: FailedUpdateClient)
    monkeypatch.setattr(app_module, "_address_change_guard", AsyncMock(return_value=True))
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda _feature: False)
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(rollout_gate, "firmware_window_key", lambda *args: key)
    job = app_module.UpdateJob(
        job_id="driver-login-real-failure",
        device_firmware_map={"192.0.2.1": "/tmp/tna-30x-2.0.0-r1.bin"},
        credentials={"192.0.2.1": ("u", "p")},
        device_roles={"192.0.2.1": "ap"},
        devices={"192.0.2.1": app_module.DeviceStatus(ip="192.0.2.1", role="ap")},
        is_scheduled=True, start_hour=0, end_hour=23, schedule_days=["tue"],
        schedule_timezone="UTC", window_key=key, pre_update_reboot=False,
    )

    await app_module._update_single_device(job, "192.0.2.1")

    assert job.devices["192.0.2.1"].status == "failed"
    stop = db.get_firmware_window_stop(key)
    assert stop["device_ip"] == "192.0.2.1"
    assert stop["reason"] == "Upload failed"
