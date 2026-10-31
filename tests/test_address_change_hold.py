"""Keep unqualified address transitions behind the shared firmware gate."""

from datetime import datetime, timedelta, timezone
import json
import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from updater import app as app_module, database as db, rollout_gate as gate
from updater.vendors.base import VendorDeviceInfo, VendorUpdateResult, VendorSmokeTestResult
from updater.vendors.tachyon.client import DeviceInfo, TachyonClient

IP = "192.0.2.10"
OTHER_IP = "192.0.2.11"
SOURCE = "1.12.3.54970"
TARGET = "1.15.0.55142"
FW = "tna-30x-1.15.0-r55142-test.bin"
ARTIFACT = b"synthetic qualified firmware"
SCOPE = {
    "source_sha256": "a" * 64,
    "target_sha256": hashlib.sha256(ARTIFACT).hexdigest(),
    "management_vlan": 100,
    "management_ip_mode": "dhcp",
    "recovery_case": "firmware_flash",
}
OBSERVED = {"firmware_sha256": SCOPE["source_sha256"],
            "management_vlan": 100, "management_ip_mode": "dhcp"}
TRANSITION = gate.AddressTransition("tna-30x", "ap", SOURCE, TARGET, **SCOPE)


def qualify():
    for recovery_case in ("pre_update_reboot", "firmware_flash"):
        gate.ADDRESS_CHANGE_QUALIFICATIONS[replace(TRANSITION, recovery_case=recovery_case)] = "synthetic proof"


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
    allowed, reason = gate.address_change_decision(family, role, source, target, **SCOPE)
    assert not allowed
    assert "Address-change hold" in reason


def test_qualified_exact_build_proceeds(tables):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic bench proof"
    assert gate.address_change_decision("tna-30x", "ap", "1.12.3.r54970", TARGET, **SCOPE) == (True, None)
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = ""
    assert not gate.address_change_decision("tna-30x", "ap", SOURCE, TARGET, **SCOPE)[0]


def write_plan(**overrides):
    import os
    from pathlib import Path
    plan = {
        "name": "lab-plan", "approval": "synthetic approval",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "devices": [{"ip": IP, "family": "tna-30x", "role": "ap", "source": SOURCE, "target": TARGET,
                     **SCOPE, "recovery_case": recovery_case}
                    for recovery_case in ("pre_update_reboot", "firmware_flash")],
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
        "tna-30x", "ap", source, TARGET, ip=ip, bench_exception=name, scheduled=scheduled, **SCOPE,
    )[0] is allowed


@pytest.mark.parametrize("overrides", [
    {"approval": ""}, {"approval": " "}, {"expires_at": "2000-01-01T00:00:00+00:00"},
    {"expires_at": "2099-01-01T00:00:00"}, {"expires_at": "unknown"},
    {"devices": []}, {"devices": [{}]}, {"devices": "all"},
])
def test_invalid_or_expired_plan_holds(tables, overrides):
    write_plan(**overrides)
    assert not gate.address_change_decision(
        "tna-30x", "ap", SOURCE, TARGET, ip=IP, bench_exception="lab-plan", **SCOPE,
    )[0]


@pytest.mark.parametrize("contents", ["broken JSON", "[]", "{}", "x" * 65537])
def test_unreadable_plan_holds(tables, contents):
    import os
    from pathlib import Path
    Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"]).write_text(contents)
    assert gate._address_bench_plan("lab-plan") is None


@pytest.fixture
def runner(mock_db, monkeypatch, tmp_path):
    db.set_setting("scheduled_rollout_safety_ready", "true")
    for name in ("broadcast",):
        monkeypatch.setattr(app_module, name, AsyncMock())
    monkeypatch.setattr(app_module, "_scheduled_job_guard", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(app_module, "_shutting_down", False)
    monkeypatch.setattr(app_module, "is_feature_enabled", lambda feature: False)
    monkeypatch.setattr(app_module, "get_scheduler", lambda: None)
    for module, name in [(app_module.services, "get_timezone"),
                         (app_module.slack, "notify_job_completed"),
                         (app_module.snmp, "notify_job_completed"),
                         (app_module.webhooks, "notify_job_completed"),
                         (app_module.email_notifier, "notify_job_completed")]:
        monkeypatch.setattr(module, name, AsyncMock(return_value="UTC"))
    from updater.vendors.tachyon import TachyonDriver
    client = TachyonDriver(IP, "synthetic", "synthetic")
    client.connect = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(return_value=VendorDeviceInfo(
        ip=IP, model="TNA-301", serial="SYNTHETIC-SERIAL",
        current_version=SOURCE, extra=dict(OBSERVED),
    ))
    client.reboot = AsyncMock(return_value=True)
    client.update_firmware = AsyncMock(return_value=VendorUpdateResult(
        ip=IP, success=True, old_version=SOURCE, new_version=TARGET, model="TNA-301",
    ))
    client.run_smoke_tests = AsyncMock(return_value=VendorSmokeTestResult(checks=[
        {"check": "device_responsive", "passed": True},
        {"check": "config_intact", "passed": True},
        {"check": "cpe_connectivity", "passed": True},
        {"check": "cpe_signal_levels", "passed": True},
    ]))
    async def device_info(*args, **kwargs):
        source = client.get_device_info.return_value
        current = TARGET if client.update_firmware.await_count else source.current_version
        return VendorDeviceInfo(
            ip=IP, model="TNA-301", serial="SYNTHETIC-SERIAL",
            current_version=current, extra=dict(source.extra),
        )
    client.get_device_info.side_effect = device_info
    monkeypatch.setattr(app_module, "get_driver", lambda vendor: lambda *args: client)
    monkeypatch.setattr(app_module, "_get_firmware_type_for_model",
                        lambda model: "tna-30x" if model == "TNA-301" else None)
    db.upsert_access_point(IP, "synthetic", "synthetic", model="TNA-301", firmware_version=SOURCE)
    for name in ("get_access_point", "get_cpe_by_ip", "get_switch"):
        read = getattr(db, name)
        def observed_record(ip, read=read):
            record = read(ip)
            return {**record, **OBSERVED} if record else None
        monkeypatch.setattr(db, name, observed_record)
    (tmp_path / FW).write_bytes(ARTIFACT)
    monkeypatch.setattr(app_module, "FIRMWARE_DIR", tmp_path)
    return client


def make_job(scheduled=False, reboot=True, role="ap", bank_mode="one"):
    return app_module.UpdateJob(
        job_id="address-hold-test", started_at=datetime.now(), is_scheduled=scheduled,
        pre_update_reboot=reboot, bank_mode=bank_mode,
        enforce_window_cutoff=False,
        device_roles={IP: role}, device_firmware_map={IP: str(app_module.FIRMWARE_DIR / FW)},
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
    runner.connect.assert_awaited()
    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"
    assert "Address-change hold" in job.devices[IP].error


@pytest.mark.asyncio
async def test_qualification_hold_stops_window_without_failure_stop(tables, runner):
    db.upsert_access_point(OTHER_IP, "synthetic", "synthetic", model="TNA-301", firmware_version=SOURCE)
    first_job = make_job()
    first_job.job_id = "qualification-hold-first-job"
    first_job.window_key = ("firmware", "2026-10-27")
    first_job.devices[OTHER_IP] = app_module.DeviceStatus(ip=OTHER_IP, role="ap")
    first_job.device_roles[OTHER_IP] = "ap"
    first_job.device_firmware_map[OTHER_IP] = first_job.device_firmware_map[IP]
    first_job.credentials[OTHER_IP] = ("synthetic", "synthetic")

    await app_module.run_update_job(first_job, 2)

    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()
    assert first_job.devices[IP].status == "deferred"
    assert first_job.devices[OTHER_IP].status == "cancelled"
    assert db.get_firmware_window_hold(first_job.window_key)["device_ip"] == IP
    assert db.get_firmware_window_stop(first_job.window_key) is None

    same_window_job = make_job()
    same_window_job.job_id = "qualification-hold-same-window-job"
    same_window_job.window_key = first_job.window_key
    await app_module.run_update_job(same_window_job, 1)
    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()

    qualify()
    next_window_job = make_job()
    next_window_job.job_id = "qualification-hold-next-window-job"
    next_window_job.window_key = ("firmware", "2026-10-28")
    await app_module.run_update_job(next_window_job, 1)
    runner.reboot.assert_awaited_once()
    runner.update_firmware.assert_awaited_once()
    assert next_window_job.devices[IP].status == "success"

    with db.get_db() as conn:
        assert conn.execute("SELECT 1 FROM firmware_window_stops").fetchone() is None
        rows = conn.execute(
            "SELECT status FROM device_update_history WHERE job_id IN (?, ?, ?)",
            (first_job.job_id, same_window_job.job_id, next_window_job.job_id),
        ).fetchall()
    assert all(row["status"] != "failed" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [True, False])
async def test_qualified_job_reboots_and_flashes(tables, runner, scheduled):
    qualify()
    job = make_job(scheduled)
    await app_module.run_update_job(job, 1)
    runner.reboot.assert_awaited_once()
    runner.update_firmware.assert_awaited_once()
    assert job.devices[IP].status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("live_change,expected_writes", [(None, ["upload", "trigger"]),
                                                          ("source", []),
                                                          ("management_vlan", [])])
async def test_driver_qualification_callback_normalizes_tachyon_info(
    tables, runner, live_change, expected_writes,
):
    qualify()
    writes = []
    version = "1.12.3.54970" if live_change != "source" else "1.12.3.54971"
    vlan = 100 if live_change != "management_vlan" else 200

    async def update_with_live_info(*args, qualification_check, **kwargs):
        live_info = DeviceInfo(
            ip=IP, model="TNA-301", serial="SYNTHETIC-SERIAL", current_version=version,
            management_interface="eth0.100", management_vlan_id=vlan,
            firmware_sha256=SCOPE["source_sha256"],
            management_ip_mode=SCOPE["management_ip_mode"],
        )
        admitted, reason = await qualification_check(live_info)
        if not admitted:
            return VendorUpdateResult(ip=IP, success=False, error=reason)
        writes.extend(("upload", "trigger"))
        return VendorUpdateResult(
            ip=IP, success=True, old_version=SOURCE, new_version=TARGET,
            model="TNA-301",
        )

    runner.update_firmware.side_effect = update_with_live_info
    job = make_job(reboot=False)

    await app_module.run_update_job(job, 1)

    assert writes == expected_writes
    if live_change is None:
        assert job.devices[IP].status == "success"
    else:
        assert job.devices[IP].status == "deferred"
        assert "Address-change hold" in job.devices[IP].error


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source_sha256", "management_ip_mode"])
async def test_pre_reboot_rechecks_fresh_device_info(tables, runner, monkeypatch, change):
    qualify()
    changed = dict(OBSERVED)
    if change == "source_sha256":
        changed["firmware_sha256"] = "b" * 64
    else:
        changed["management_ip_mode"] = "static"
    initial = VendorDeviceInfo(ip=IP, model="TNA-301", serial="SYNTHETIC-SERIAL", current_version=SOURCE, extra=dict(OBSERVED))
    boundary = VendorDeviceInfo(ip=IP, model="TNA-301", serial="SYNTHETIC-SERIAL", current_version=SOURCE, extra=changed)
    runner.get_device_info = AsyncMock(side_effect=[initial, boundary])
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 28, tzinfo=tz)
    monkeypatch.setattr(app_module, "datetime", FixedDateTime)
    job = make_job()
    job.window_key = ("firmware", "2026-10-28")

    await app_module.run_update_job(job, 1)

    runner.reboot.assert_not_awaited()
    assert job.devices[IP].status == "deferred"
    assert "Address-change hold" in job.devices[IP].error
    assert db.get_firmware_window_stop(job.window_key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["firmware_sha256", "management_ip_mode"])
async def test_missing_live_qualification_field_holds_before_reboot(tables, runner, field):
    qualify()
    initial = VendorDeviceInfo(ip=IP, model="TNA-301", serial="SYNTHETIC-SERIAL", current_version=SOURCE, extra=dict(OBSERVED))
    missing = dict(OBSERVED)
    missing.pop(field)
    boundary = VendorDeviceInfo(ip=IP, model="TNA-301", serial="SYNTHETIC-SERIAL", current_version=SOURCE, extra=missing)
    runner.get_device_info = AsyncMock(side_effect=[initial, boundary])

    job = make_job()
    await app_module.run_update_job(job, 1)

    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"
    assert "Address-change hold" in job.devices[IP].error


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["upload", "upload_retry", "trigger"])
@pytest.mark.parametrize("field", ["firmware_sha256", "management_ip_mode"])
async def test_tachyon_rechecks_live_fields_at_each_driver_write_boundary(
    tables, monkeypatch, tmp_path, boundary, field,
):
    import asyncio
    import updater.vendors.tachyon.client as tachyon_client_module

    client = TachyonClient(IP, "synthetic", "synthetic")
    client.login = AsyncMock(return_value=True)
    expected = {
        "firmware_sha256": SCOPE["source_sha256"],
        "management_ip_mode": SCOPE["management_ip_mode"],
    }

    def live_info(changed=False):
        values = dict(expected)
        if changed:
            values[field] = "static" if field == "management_ip_mode" else "b" * 64
        return DeviceInfo(
            ip=IP, model="TNA-301", current_version=SOURCE,
            firmware_sha256=values["firmware_sha256"],
            management_ip_mode=values["management_ip_mode"],
        )

    reads = 0

    async def read_info(*args, **kwargs):
        nonlocal reads
        reads += 1
        changed_read = {
            "upload": 2,
            "upload_retry": 3,
            "trigger": 3,
        }[boundary]
        return live_info(changed=reads == changed_read)

    client.get_device_info = AsyncMock(side_effect=read_info)
    qualification_checks = []

    async def qualification_check(info, error=""):
        qualification_checks.append((info, error))
        if error:
            return False, error
        if (info.firmware_sha256 != expected["firmware_sha256"]
                or info.management_ip_mode != expected["management_ip_mode"]):
            return False, "Deferred: live qualification changed"
        return True, ""

    curl_calls = 0

    async def fake_curl(args):
        nonlocal curl_calls
        curl_calls += 1
        if boundary == "upload_retry" and curl_calls == 1:
            return 7, b""
        return 0, b"{}"

    monkeypatch.setattr(tachyon_client_module, "_run_curl", fake_curl)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    client.trigger_update = AsyncMock(return_value=True)
    client.wait_for_reboot = AsyncMock(return_value=True)
    monkeypatch.setattr(client, "get_device_info", AsyncMock(side_effect=read_info))
    fw_path = tmp_path / FW
    fw_path.write_bytes(ARTIFACT)

    result = await client.update_firmware(str(fw_path), qualification_check=qualification_check)

    assert not result.success
    assert "live qualification changed" in result.error
    client.trigger_update.assert_not_awaited()
    if boundary == "trigger":
        assert curl_calls == 1
    elif boundary == "upload":
        assert curl_calls == 0
    else:
        assert curl_calls == 1
    assert reads == {"upload": 2, "upload_retry": 3, "trigger": 3}[boundary]


@pytest.mark.asyncio
@pytest.mark.parametrize("reboot", [True, False])
async def test_fresh_unqualified_build_holds_at_write_boundary(tables, runner, reboot):
    qualify()
    runner.get_device_info.return_value.current_version = "1.12.3.54971"
    job = make_job(reboot=reboot)
    await app_module.run_update_job(job, 1)
    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"


@pytest.mark.asyncio
async def test_qualification_rechecked_after_reboot(tables, runner):
    qualify()
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
    job.device_firmware_map[OTHER_IP] = str(app_module.FIRMWARE_DIR / FW)
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
    db.register_firmware(FW, source="manual", sha256="a" * 64)
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
    qualify()
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
    phase_devices = db.get_rollout_devices(rollout["id"], phase="pct10")
    assert phase_devices and all(d["status"] == "deferred" for d in phase_devices)
    await harness.tick()
    assert start.call_count == 1


SCOPE_CHANGES = [
    ("source_sha256", "b" * 64), ("source_sha256", ""), ("source_sha256", None),
    ("source_sha256", "not-a-hash"), ("target_sha256", "b" * 64),
    ("target_sha256", ""), ("target_sha256", None),
    ("management_vlan", 101), ("management_vlan", None),
    ("management_vlan", True), ("management_vlan", -1), ("management_vlan", 4095),
    ("management_ip_mode", "static"), ("management_ip_mode", ""),
    ("management_ip_mode", "unknown"), ("management_ip_mode", None),
    ("recovery_case", "pre_update_reboot"), ("recovery_case", ""),
]


@pytest.mark.parametrize("bench", [False, True])
@pytest.mark.parametrize("field,value", SCOPE_CHANGES)
def test_changed_or_missing_scope_stays_held(tables, bench, field, value):
    if bench:
        write_plan()
    else:
        gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic proof"
    scope = {**SCOPE, field: value}
    # The plan includes both cases; use an unknown case for this mismatch check.
    if bench and field == "recovery_case" and value == "pre_update_reboot":
        scope[field] = "unknown"
    assert not gate.address_change_decision(
        "tna-30x", "ap", SOURCE, TARGET, ip=IP,
        bench_exception="lab-plan" if bench else "", **scope,
    )[0]


@pytest.mark.parametrize("field", list(SCOPE))
def test_legacy_plan_without_scope_stays_held(tables, field):
    write_plan()
    import os
    from pathlib import Path
    path = Path(os.environ["SIXTYOPS_ADDRESS_BENCH_PLAN_FILE"])
    plan = json.loads(path.read_text())
    for device in plan["devices"]:
        device.pop(field)
    path.write_text(json.dumps(plan))
    assert gate._address_bench_plan("lab-plan") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [True, False])
@pytest.mark.parametrize("reboot", [True, False])
@pytest.mark.parametrize("field", ["source_sha256", "target_sha256", "management_vlan", "management_ip_mode"])
async def test_live_scope_change_holds_before_each_write(tables, runner, scheduled, reboot, field):
    qualify()
    job = make_job(scheduled, reboot)
    if field == "target_sha256":
        async def replace_bytes():
            from pathlib import Path
            Path(job.device_firmware_map[IP]).write_bytes(b"different artifact, same build")
            return True
        runner.connect.side_effect = replace_bytes
    else:
        key = "firmware_sha256" if field == "source_sha256" else field
        runner.get_device_info.return_value.extra[key] = {
            "source_sha256": "b" * 64, "management_vlan": 101,
            "management_ip_mode": "static",
        }[field]
    await app_module.run_update_job(job, 1)
    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [True, False])
@pytest.mark.parametrize("field", ["firmware_sha256", "management_vlan", "management_ip_mode"])
async def test_missing_inventory_scope_uses_live_evidence(tables, runner, monkeypatch, scheduled, field):
    qualify()
    read = db.get_access_point
    def missing(ip):
        record = read(ip)
        record.pop(field, None)
        return record
    monkeypatch.setattr(db, "get_access_point", missing)
    job = make_job(scheduled)
    await app_module.run_update_job(job, 1)
    runner.connect.assert_awaited()
    if job.pre_update_reboot:
        runner.reboot.assert_awaited_once()
    runner.update_firmware.assert_awaited_once()
    assert job.devices[IP].status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [True, False])
async def test_flash_proof_alone_holds_pre_reboot_and_later_passes(tables, runner, scheduled):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[TRANSITION] = "synthetic flash-only proof"
    job = make_job(scheduled, bank_mode="both")
    await app_module.run_update_job(job, 1)
    runner.reboot.assert_not_awaited()
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"


@pytest.mark.asyncio
async def test_pre_reboot_proof_alone_holds_flash(tables, runner):
    gate.ADDRESS_CHANGE_QUALIFICATIONS[replace(TRANSITION, recovery_case="pre_update_reboot")] = "synthetic reboot-only proof"
    job = make_job()
    await app_module.run_update_job(job, 1)
    runner.reboot.assert_awaited_once()
    runner.update_firmware.assert_not_awaited()
    assert job.devices[IP].status == "deferred"
