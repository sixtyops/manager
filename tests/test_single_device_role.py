"""Manual endpoint jobs preserve inventory roles through reboot recovery."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from updater import app
from updater import database as db
from updater.vendors.tachyon import TachyonDriver
from updater.vendors.tachyon.client import TachyonClient, UpdateResult, SmokeTestResult

IP = "192.0.2.20"
PARENT = "192.0.2.10"
RADIO = "02:00:00:00:00:20"
FIRMWARE = "tna-30x-1.15.1-r1.bin"


@pytest.mark.asyncio
@pytest.mark.parametrize("role,missing_intent", [("cpe", False), ("ap", False), ("ap", True)])
async def test_endpoint_job_selects_role_and_recovery(mock_db, monkeypatch, tmp_path, role, missing_intent):
    db.set_setting("timezone", "UTC")
    db.upsert_access_point(PARENT, "parent-user", "parent-password")
    db.upsert_device(IP, role, "device-user", "device-password", model="TNA-301")
    with db.get_db() as conn:
        conn.execute("UPDATE devices SET uid='test-serial', uid_source='serial' WHERE ip=?", (IP,))
        if role == "cpe":
            conn.execute(
                "INSERT INTO cpe_cache(ap_ip,ip,mac,model) VALUES (?,?,?,?)",
                (PARENT, IP, RADIO, "TNA-301"),
            )
    (tmp_path / FIRMWARE).write_bytes(b"test firmware")
    monkeypatch.setattr(app, "FIRMWARE_DIR", tmp_path)
    monkeypatch.setattr(app, "broadcast", AsyncMock())
    monkeypatch.setattr(app, "get_poller", lambda: None)
    monkeypatch.setattr(app, "_shutting_down", False)
    monkeypatch.setattr(app, "update_jobs", {})
    monkeypatch.setattr(app, "_firmware_job_owner", None)
    spawn = Mock()
    monkeypatch.setattr(app, "_spawn_update_job", spawn)
    monkeypatch.setattr(app.slack, "notify_job_completed", AsyncMock())
    monkeypatch.setattr(app.snmp, "notify_job_completed", AsyncMock())
    updated = False
    reads = []
    callbacks = []
    config = (Path(__file__).parent / "fixtures/tachyon/management_zone_dhcp.json").read_text()

    async def transport(client, method, endpoint, **kwargs):
        assert method == "GET"
        reads.append(endpoint)
        if endpoint.endswith("type=system"):
            return 200, json.dumps({"system": {"serial": "test-serial", "model": "TNA-301",
                "version": {"firmux": "1.15.1.1" if updated else "1.12.3"}}})
        if endpoint.endswith("type=interfaces"):
            return 200, json.dumps({"interfaces": {"eth0": {"mac_address": "02:00:00:00:00:21"}}})
        if endpoint.endswith("type=zones"):
            return 200, json.dumps({"zones": {"wan": {
                "management_interface": "br-wan.12", "mgmt_vlan_id": 12}}})
        if endpoint == "/cgi.lua/config":
            return 200, config
        if "wireless" in endpoint:
            return 200, json.dumps({"wireless": {"peers": [], "radios": [{"mac": RADIO}]}})
        assert endpoint == "/cgi.lua/bootbank"
        return 200, "{}"

    async def recover(callback, stage):
        callbacks.append(stage)
        if role == "cpe":
            assert "ap_recovery" not in job.recovery_locators[IP]
        else:
            assert job.recovery_locators[IP]["ap_recovery"]["state"] == "write_pending"
        return await callback(app.monotonic() + 600, app.monotonic())

    async def reboot(client, *, timeout, recovery_callback):
        if missing_intent:
            del job.recovery_locators[IP]["ap_recovery"]
            with pytest.raises(RuntimeError, match="AP recovery intent is not armed"):
                await recovery_callback(app.monotonic() + 600, app.monotonic())
            raise RuntimeError("missing AP intent")
        return await recover(recovery_callback, "reboot")

    async def update(client, path, progress, *, serial_check, recovery_callback, **kwargs):
        nonlocal updated
        assert await serial_check("upload")
        assert await serial_check("install")
        updated = True
        assert await recover(recovery_callback, "install")
        return UpdateResult(ip=IP, success=True, new_version="1.15.1.1")

    monkeypatch.setattr(TachyonClient, "_curl", transport)
    monkeypatch.setattr(TachyonClient, "login", AsyncMock(return_value=True))
    monkeypatch.setattr(TachyonDriver, "reboot", reboot)
    monkeypatch.setattr(TachyonDriver, "update_firmware", update)
    monkeypatch.setattr(TachyonDriver, "run_smoke_tests", AsyncMock(return_value=SmokeTestResult(
        passed=True, checks=[{"check": name, "passed": True} for name in
            ("device_responsive", "config_intact", "cpe_connectivity", "cpe_signal_levels")],
    )))
    ap_recovery = AsyncMock(wraps=app._recover_ap)
    monkeypatch.setattr(app, "_recover_ap", ap_recovery)
    monkeypatch.setattr(app, "locate_ap", AsyncMock(return_value=IP))

    result = await app.update_single_device_endpoint(
        ip=IP, firmware_file=FIRMWARE, firmware_file_303l="", firmware_file_tns100="",
        bank_mode="one", session={"role": "operator"}, _pro=None,
    )
    job = spawn.call_args.args[0]
    assert result["device_count"] == 1
    assert job.device_roles == {IP: role}
    assert list(job.devices) == [IP]
    assert job.devices[IP].role == role
    if role == "cpe":
        assert job.devices[IP].parent_ap == PARENT
        assert job.credentials[IP] == ("parent-user", "parent-password")
    await app.run_update_job(job, concurrency=1)
    assert job.devices[IP].role == role
    if missing_intent:
        assert job.devices[IP].status == "failed"
        assert not updated
        assert "missing AP intent" in job.devices[IP].error
        ap_recovery.assert_awaited_once()
    else:
        assert job.devices[IP].status == "success", job.devices[IP].error
        assert callbacks == ["reboot", "install"]
        assert ap_recovery.await_count == (2 if role == "ap" else 0)
    assert "/cgi.lua/status?type=system" in reads
    assert "/cgi.lua/config" in reads
