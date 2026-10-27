"""Keep unqualified address transitions behind the shared firmware gate."""

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest

from updater import app as app_module, database as db, rollout_gate as gate
from updater.vendors.base import VendorDeviceInfo, VendorUpdateResult, VendorSmokeTestResult

IP = "192.0.2.10"
OTHER_IP = "192.0.2.11"
SOURCE = "1.12.3.54970"
TARGET = "1.15.0.55142"
FW = "tna-30x-1.15.0-r55142-test.bin"
TRANSITION = gate.AddressTransition("tna-30x", "ap", SOURCE, TARGET)


@pytest.fixture
def tables(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "ADDRESS_CHANGE_QUALIFICATIONS", {})
    monkeypatch.setenv("SIXTYOPS_ADDRESS_BENCH_PLAN_FILE", str(tmp_path / "bench.json"))


def test_shipped_tables_start_empty():
    assert gate.ADDRESS_CHANGE_QUALIFICATIONS == {}
    assert gate._address_bench_plan("unregistered-plan") is None


@pytest.mark.parametrize("family,role,source,target", [
    ("tna-303l", "ap", SOURCE, TARGET), ("tna-30x", "cpe", SOURCE, TARGET),
    ("tna-30x", "ap", "1.12.3.54971", TARGET),
    ("tna-30x", "ap", SOURCE, "1.15.0.55143"),
    (None, "ap", SOURCE, TARGET), ("tna-30x", "unknown", SOURCE, TARGET),
    ("tna-30x", "ap", "1.12.3", TARGET), ("tna-30x", "ap", SOURCE, "1.15.0"),
    ("tna-30x", "ap", SOURCE + ".extra", TARGET),
])
def test_qualification_matches_every_key(tables, family, role, source, target):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic bench proof"
    allowed, reason = gate.address_change_decision(family, role, source, target)
    assert not allowed
    assert "Address-change hold" in reason


def test_qualified_exact_build_proceeds(tables):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic bench proof"
    assert gate.address_change_decision("tna-30x", "ap", "1.12.3.r54970", TARGET) == (True, None)
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = ""
    assert not gate.address_change_decision("tna-30x", "ap", SOURCE, TARGET)[0]


def write_plan(**overrides):
    import os
    from pathlib import Path
    plan = {
        "name": "lab-plan", "approval": "synthetic approval",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "devices": [{"ip": IP, "family": "tna-30x", "role": "ap", "source": SOURCE, "target": TARGET}],
    }
    plan.update(overrides)
    Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"]).write_text(json.dumps(plan))


@pytest.mark.parametrize("ip,name,scheduled,source,allowed", [
    (IP, "lab-plan", False, SOURCE, True),
    (OTHER_IP, "lab-plan", False, SOURCE, False),
    (IP, "", False, SOURCE, False), (IP, "other-plan", False, SOURCE, False),
    (IP, "lab-plan", True, SOURCE, False),
    (IP, "lab-plan", False, "1.12.3.54971", False),
])
def test_named_bench_exception_scope(tables, ip, name, scheduled, source, allowed):
    write_plan()
    assert gate.address_change_decision(
        "tna-30x", "ap", source, TARGET, ip=ip, bench_exception=name, scheduled=scheduled,
    )[0] is allowed


@pytest.mark.parametrize("overrides", [
    {"approval": ""}, {"approval": " "}, {"expires_at": "2000-01-01T00:00:00+00:00"},
    {"expires_at": "2099-01-01T00:00:00"}, {"expires_at": "unknown"},
    {"devices": []}, {"devices": [{}]}, {"devices": "all"},
])
def test_invalid_or_expired_plan_holds(tables, overrides):
    write_plan(**overrides)
    assert not gate.address_change_decision(
        "tna-30x", "ap", SOURCE, TARGET, ip=IP, bench_exception="lab-plan",
    )[0]


@pytest.mark.parametrize("contents", ["broken JSON", "[]", "{}", "x" * 65537])
def test_unreadable_plan_holds(tables, contents):
    import os
    from pathlib import Path
    Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"]).write_text(contents)
    assert gate._address_bench_plan("lab-plan") is None


@pytest.fixture
def runner(mock_db, monkeypatch):
    for name in ("broadcast",):
        monkeypatch.setattr(app_module, name, AsyncMock())
    monkeypatch.setattr(app_module, "_scheduled_job_guard", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda feature: False)
    monkeypatch.setattr(app_module, "get_scheduler", lambda: None)
    for module, name in [(app_module.services, "get_timezone"),
                         (app_module.telemetry, "send_telemetry_background"),
                         (app_module.slack, "notify_job_completed"),
                         (app_module.snmp, "notify_job_completed"),
                         (app_module.webhooks, "notify_job_completed"),
                         (app_module.email_notifier, "notify_job_completed")]:
        monkeypatch.setattr(module, name, AsyncMock(return_value="UTC"))
    from updater.vendors.tachyon import TachyonDriver
    client = TachyonDriver(IP, "synthetic", "synthetic")
    client.connect = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(return_value=VendorDeviceInfo(
        ip=IP, model="TNA-301", current_version=SOURCE,
    ))
    client.reboot = AsyncMock(return_value=True)
    client.update_firmware = AsyncMock(return_value=VendorUpdateResult(
        ip=IP, success=True, old_version=SOURCE, new_version=TARGET, model="TNA-301",
    ))
    client.run_smoke_tests = AsyncMock(return_value=VendorSmokeTestResult())
    monkeypatch.setattr(app_module, "get_driver", lambda vendor: lambda *args: client)
    monkeypatch.setattr(app_module, "_get_firmware_type_for_model",
                        lambda model: "tna-30x" if model == "TNA-301" else None)
    db.upsert_access_point(IP, "synthetic", "synthetic", model="TNA-301", firmware_version=SOURCE)
    return client


def make_job(scheduled=False, reboot=True, role="ap", bank_mode="one"):
    return app_module.UpdateJob(
        job_id="address-hold-test", started_at=datetime.now(), is_scheduled=scheduled,
        pre_update_reboot=reboot, bank_mode=bank_mode,
        device_roles={IP: role}, device_firmware_map={IP: FW},
        credentials={IP: ("synthetic", "synthetic")},
        devices={IP: app_module.DeviceStatus(ip=IP, role=role)},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [True, False])
@pytest.mark.parametrize("reboot", [True, False])
@pytest.mark.parametrize("role", ["ap", "cpe", "switch"])
async def test_unqualified_job_holds_before_writes(tables, runner, scheduled, reboot, role):
    if role == "cpe":
        db.upsert_cpe(OTHER_IP, {"ip": IP, "model": "TNA-301", "firmware_version": SOURCE})
    elif role == "switch":
        db.upsert_switch(IP, "synthetic", "synthetic", model="TNA-301", firmware_version=SOURCE)
    job = make_job(scheduled, reboot, role, bank_mode="both")
    await app_module.run_update_job(job, 2)
    runner.connect.assert_not_awaited()
    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"
    assert "Address-change hold" in job.devices[IP].error


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [True, False])
async def test_qualified_job_reboots_and_flashes(tables, runner, scheduled):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic proof"
    job = make_job(scheduled)
    await app_module.run_update_job(job, 1)
    runner.reboot.assert_awaited_once()
    runner.update_firmware.assert_awaited_once()
    assert job.devices[IP].status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("reboot", [True, False])
async def test_fresh_unqualified_build_holds_at_write_boundary(tables, runner, reboot):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic proof"
    runner.get_device_info.return_value.current_version = "1.12.3.54971"
    job = make_job(reboot=reboot)
    await app_module.run_update_job(job, 1)
    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"


@pytest.mark.asyncio
async def test_qualification_rechecked_after_reboot(tables, runner):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic proof"
    async def revoke(**kwargs):
        gate.ADDRESS_CHANGE_QUALIFICATIONS.clear()
        return True
    runner.reboot.side_effect = revoke
    await app_module.run_update_job(make_job(), 1)
    runner.reboot.assert_awaited_once()
    runner.update_firmware.assert_not_awaited()


@pytest.mark.asyncio
async def test_bench_plan_runs_only_named_device(tables, runner):
    write_plan()
    db.upsert_access_point(OTHER_IP, "synthetic", "synthetic", model="TNA-301", firmware_version=SOURCE)
    job = make_job()
    job.address_bench_exception = "lab-plan"
    job.devices[OTHER_IP] = app_module.DeviceStatus(ip=OTHER_IP, role="ap")
    job.device_roles[OTHER_IP] = "ap"
    job.device_firmware_map[OTHER_IP] = FW
    job.credentials[OTHER_IP] = ("synthetic", "synthetic")
    await app_module.run_update_job(job, 1)
    runner.reboot.assert_awaited_once()
    runner.update_firmware.assert_awaited_once()
    assert job.devices[IP].status == "success"
    assert job.devices[OTHER_IP].status == "deferred"
    with db.get_db() as conn:
        assert conn.execute("SELECT 1 FROM schedule_log WHERE event='address_bench_exception'").fetchone()


@pytest.mark.asyncio
async def test_fleet_status_exposes_hold_reason(tables, mock_db, monkeypatch, tmp_path):
    (tmp_path / FW).write_bytes(b"synthetic firmware")
    monkeypatch.setattr(app_module, "FIRMWARE_DIR", tmp_path)
    monkeypatch.setattr(app_module, "get_scheduler", lambda: None)
    db.set_setting("selected_firmware_30x", FW)
    db.upsert_access_point(IP, "synthetic", "synthetic", model="TNA-301", firmware_version=SOURCE)
    result = await app_module.get_fleet_status(session={})
    device = result["devices"][0]
    assert device["update_state"] == "on_hold"
    assert device["hold"]["code"] == "address_change_hold"
    assert SOURCE in device["hold"]["reason"] and TARGET in device["hold"]["reason"]
    assert device["next_attempt_iso"] is None
    assert result["summary"]["on_hold"] == 1


@pytest.mark.asyncio
async def test_live_read_error_holds_before_flash(tables, runner):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic proof"
    runner.get_device_info.side_effect = RuntimeError("synthetic read failure")
    job = make_job(reboot=False)
    await app_module.run_update_job(job, 1)
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"


@pytest.mark.parametrize("endpoint", ["/api/start-update", "/api/update-device"])
def test_manual_route_carries_explicit_plan(tables, mock_db, authed_client, monkeypatch, tmp_path, endpoint):
    from unittest.mock import MagicMock
    (tmp_path / FW).write_bytes(b"synthetic firmware")
    monkeypatch.setattr(app_module, "FIRMWARE_DIR", tmp_path)
    db.upsert_access_point(IP, "synthetic", "synthetic", model="TNA-301", firmware_version=SOURCE)
    spawn = MagicMock()
    monkeypatch.setattr(app_module, "_spawn_update_job", spawn)
    response = authed_client.post(endpoint, data={
        "firmware_file": FW, "ip": IP, "ip_list": IP, "device_type": "mixed",
        "bank_mode": "one", "address_bench_exception": "lab-plan",
    })
    assert response.status_code == 200
    assert spawn.call_args.args[0].address_bench_exception == "lab-plan"


@pytest.mark.asyncio
async def test_scheduled_hold_keeps_wave_pending(tables, mock_db, monkeypatch):
    from tests.test_rollout_invariants import _settings, _make_scheduler, _Harness, _seed_aps
    _seed_aps(10)
    db.set_settings(_settings(hold_days=0))
    scheduler, start = _make_scheduler()
    harness = _Harness(scheduler, monkeypatch)
    await harness.tick()
    rollout = db.get_active_rollout()
    ips = start.call_args.kwargs["ap_ips"]
    scheduler.on_job_completed(
        rollout["last_job_id"], 0, 0, device_statuses={ip: "deferred" for ip in ips},
    )
    assert db.get_active_rollout()["phase"] == "pct10"
    assert all(d["status"] == "deferred" for d in db.get_rollout_devices(rollout["id"]))
    await harness.tick()
    assert start.call_count == 1
