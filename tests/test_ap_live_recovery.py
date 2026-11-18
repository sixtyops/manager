"""Offline proof for live AP relocation and recovery boundaries."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from updater import app, database as db
from updater.vendors.tachyon.client import TachyonClient
from tests.test_ap_recovery_intent import prepared, capture, IP, RADIO, Client

NEW = "192.0.2.11"


@pytest.fixture
def live(prepared, monkeypatch):
    job, client, device_id = prepared
    db.upsert_access_point(IP, "test-user", "test-password")
    with db.get_db() as conn:
        conn.execute("UPDATE devices SET uid='ap-serial', uid_source='serial' WHERE ip=?", (IP,))
    candidate = {"ip": NEW}

    async def locate(**options):
        assert options["deadline"] <= job.recovery_locators[IP]["ap_recovery"]["deadline"]
        await options["checkpoint"](options["state"])
        return candidate["ip"]

    class Probe(Client):
        login = Client.connect

        async def get_device_info(self, include_management=False):
            return await super().get_device_info()

    monkeypatch.setattr(app, "TachyonClient", Probe)
    monkeypatch.setattr(app, "locate_ap", locate)
    monkeypatch.setattr(app, "get_poller", lambda: None)
    return job, client, candidate


async def callback(job, client):
    recovery = app._make_recovery_callback(job, IP, client, "ap-serial", "TNA-301", RADIO)
    saved = job.recovery_locators[IP]["ap_recovery"]
    return await recovery(saved["deadline"] + 50, saved["reboot_started"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["reboot", "install"])
@pytest.mark.parametrize("moved", [True, False])
async def test_callback_moves_once_and_projects_current_address(live, monkeypatch, stage, moved):
    job, client, candidate = live
    candidate["ip"] = NEW if moved else IP
    await capture(job, client, stage=stage)
    app._arm_ap_reboot_deadline(job, IP, client, stage)
    deadline = job.recovery_locators[IP]["ap_recovery"]["deadline"]
    move = Mock(wraps=db.relocate_ap_address)
    monkeypatch.setattr(db, "relocate_ap_address", move)
    assert await callback(job, client)
    assert move.call_count == int(moved)
    assert client.ip == candidate["ip"]
    assert job.recovery_locators[IP]["ap_recovery"]["ip"] == candidate["ip"]
    assert client._client.read_deadline <= deadline + .01
    assert job.recovery_locators[IP]["ap_recovery"]["deadline"] == deadline
    row = db.get_active_jobs()[0]
    payload = json.loads(row["device_ips_json"])
    assert payload["devices"] == payload["order"] == [candidate["ip"]]
    assert row["in_flight_device_ip"] == candidate["ip"]
    assert list(job.devices) == [IP]


@pytest.mark.asyncio
@pytest.mark.parametrize(("moved", "reads", "error", "elapsed"), [
    (False, [12], None, 0),
    (True, [12], None, 0),
    (True, [None, 12], None, 1),
    (True, ["transport error", 12], None, 1),
    (True, [None, None, None], "management VLAN is unavailable after update", 2.5),
    (True, [99], "management VLAN changed from 12 to 99", 0),
])
async def test_callback_requires_management_reads(live, monkeypatch, moved, reads, error, elapsed):
    job, client, candidate = live
    candidate["ip"] = NEW if moved else IP
    await capture(job, client)
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    saved = job.recovery_locators[IP]["ap_recovery"]
    start = app.monotonic()
    deadline = saved["deadline"] = start + 2.5
    now = start
    endpoints = []
    request_deadlines = []
    zones = "/cgi.lua/status?type=zones"
    config = "/cgi.lua/config"
    remaining_reads = iter(reads)

    async def curl(probe, method, endpoint):
        assert method == "GET"
        assert now < deadline
        assert probe.ip == candidate["ip"]
        assert client.ip == IP
        assert db.get_device(IP)["uid"] == "ap-serial"
        endpoints.append(endpoint)
        request_deadlines.append(probe.read_deadline)
        if endpoint == "/cgi.lua/status?type=system":
            return 200, json.dumps({"model": "TNA-301", "serial": "ap-serial"})
        if endpoint == zones:
            vlan = next(remaining_reads)
            if vlan == "transport error":
                raise RuntimeError("curl failed (exit code 7)")
            return 200, json.dumps({"zones": {"wan": {
                "management_interface": "eth0.12", "mgmt_vlan_id": vlan,
                "management_ip_mode": "dhcp",
            }}})
        if endpoint == config:
            # A confirmed VLAN change must stop recovery even if IP mode is missing.
            if reads == [99]:
                return 503, ""
            return 200, json.dumps({"network": {"zones": {"wan": {
                "mode": "dhcp", "management": {"use_zone_ip": True},
            }}}})
        assert endpoint in {"/cgi.lua/bootbank", "/cgi.lua/status?type=interfaces"}
        return 200, "{}"

    async def sleep(delay):
        nonlocal now
        assert 0 < delay <= 1
        assert now + delay <= deadline
        now += delay

    monkeypatch.setattr(app, "TachyonClient", TachyonClient)
    monkeypatch.setattr(TachyonClient, "login", AsyncMock(return_value=True))
    monkeypatch.setattr(TachyonClient, "_curl", curl)
    monkeypatch.setattr(app, "monotonic", lambda: now)
    monkeypatch.setattr(app.asyncio, "sleep", sleep)

    if error:
        with pytest.raises(RuntimeError, match=error):
            await callback(job, client)
        assert client.ip == IP
        assert db.get_device(IP)["uid"] == "ap-serial"
        assert db.get_device(NEW) is None
    else:
        assert await callback(job, client)
        assert client.ip == candidate["ip"]
        assert db.get_device(candidate["ip"])["uid"] == "ap-serial"
        assert (db.get_device(IP) is None) == moved
    assert [endpoint for endpoint in endpoints if endpoint in {zones, config}] == [
        endpoint for read in reads
        for endpoint in ([zones] if read == "transport error" else [zones, config])
    ]
    assert len(set(request_deadlines)) == 1
    assert now - start == pytest.approx(elapsed)
    assert saved["deadline"] == deadline


@pytest.mark.asyncio
async def test_management_read_deadline_reports_unavailable(live, monkeypatch):
    job, client, _ = live
    await capture(job, client)
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    saved = job.recovery_locators[IP]["ap_recovery"]
    saved["deadline"] = app.monotonic() + 0.1
    move = Mock()
    read_started = False

    class Probe(Client):
        login = Client.connect

        async def get_device_info(self, include_management=False):
            nonlocal read_started
            assert include_management
            read_started = True
            await asyncio.get_running_loop().create_future()

    monkeypatch.setattr(app, "TachyonClient", Probe)
    monkeypatch.setattr(db, "relocate_ap_address", move)

    with pytest.raises(RuntimeError, match="management VLAN is unavailable after update"):
        await callback(job, client)

    assert read_started
    move.assert_not_called()
    assert client.ip == IP
    assert db.get_device(NEW) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["ethernet_mac", "radio_mac", "serial", "unknown", "owner", "static", "rollback", "false", "auth", "wrong_serial", "wrong_model", "incomplete", "vlan", "missing_inventory", "inventory_serial"])
async def test_failed_gate_never_retargets_driver(live, monkeypatch, conflict):
    job, client, _ = live
    await capture(job, client)
    saved = job.recovery_locators[IP]["ap_recovery"]
    if conflict in {"ethernet_mac", "radio_mac", "serial", "unknown"}:
        monkeypatch.setattr(app, "_has_open_recovery_identity_conflict", lambda serial, mac: (
            conflict in {"serial", "unknown"} or mac == saved[conflict]))
    elif conflict == "owner":
        db.upsert_device(NEW, "cpe", "test-user", "test-password")
    elif conflict in {"missing_inventory", "inventory_serial"}:
        with db.get_db() as conn:
            if conflict == "missing_inventory":
                conn.execute("DELETE FROM devices WHERE ip=?", (IP,))
            else:
                conn.execute("UPDATE devices SET uid='other' WHERE ip=?", (IP,))
    elif conflict == "static":
        saved["management"]["ip_mode"] = "static"
    elif conflict == "rollback":
        with db.get_db() as conn:
            conn.execute("CREATE TRIGGER reject_move BEFORE UPDATE OF ip ON access_points BEGIN SELECT RAISE(ABORT, 'rollback'); END")
    elif conflict in {"auth", "wrong_serial", "wrong_model", "incomplete", "vlan"}:
        class Probe(Client):
            async def login(self):
                return conflict != "auth"

            async def get_device_info(self, include_management=False):
                info = await super().get_device_info()
                if conflict == "wrong_serial":
                    info.serial = "OTHER-SERIAL"
                if conflict in {"wrong_model", "incomplete"}:
                    info.model = "TNA-302" if conflict == "wrong_model" else None
                if conflict == "vlan":
                    info.extra["management_vlan_id"] = 99
                return info

        monkeypatch.setattr(app, "TachyonClient", Probe)
    else:
        monkeypatch.setattr(db, "relocate_ap_address", lambda *args: False)
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    with pytest.raises(Exception):
        await callback(job, client)
    assert client.ip == IP
    if conflict == "missing_inventory":
        assert db.get_device(IP) is None
    else:
        assert db.get_device(IP)["ip"] == IP


@pytest.mark.asyncio
async def test_move_then_checkpoint_failure_keeps_committed_inventory(live, monkeypatch):
    job, client, _ = live
    await capture(job, client)
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    original = db.save_active_job

    def save(*args):
        if job.recovery_locators[IP]["ap_recovery"]["state"] == "committed":
            return False
        return original(*args)

    monkeypatch.setattr(db, "save_active_job", save)
    with pytest.raises(RuntimeError, match="checkpoint"):
        await callback(job, client)
    assert db.get_device(NEW)["uid"] == "ap-serial"
    assert db.get_device(IP) is None
    assert client.ip == IP
    assert db.get_active_jobs()[0]["in_flight_device_ip"] == NEW


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "peers", "smoke", "expiry"])
async def test_both_write_paths_use_confirmed_address_and_stop_on_failure(live, monkeypatch, failure):
    job, client, candidate = live
    writes = []

    async def reboot(**options):
        writes.append(("reboot", client.ip))
        return await options["recovery_callback"](app.monotonic() + 650, app.monotonic())

    async def update(_path, _progress, *, serial_check, recovery_callback, **options):
        assert await serial_check("upload")
        writes.append(("upload", client.ip))
        assert client._client.read_deadline is None
        assert await serial_check("install")
        writes.append(("install", client.ip))
        candidate["ip"] = NEW
        assert await recovery_callback(app.monotonic() + 650, app.monotonic())
        client.updated = True
        if failure == "expiry":
            job.recovery_locators[IP]["ap_recovery"]["deadline"] = app.monotonic() - 1
        return app.UpdateResult(ip=client.ip, success=True, new_version="1.15.1.1")

    client.reboot, client.update_firmware = reboot, update
    if failure == "peers":
        checks = 0

        async def check(*args):
            nonlocal checks
            checks += 1
            return checks == 1, {"reason": "missing peers"}

        monkeypatch.setattr(app, "_check_ap_recovery", check)
    if failure == "smoke":
        client.run_smoke_tests = AsyncMock(return_value=SimpleNamespace(passed=False, warnings=["RSSI"], checks=[]))
    next_ip = "192.0.2.99"
    job.started_at = datetime.now()
    job.bank_mode = "one"
    job.device_order = [IP, next_ip]
    job.device_roles = {IP: "ap", next_ip: "switch"}
    job.devices[next_ip] = app.DeviceStatus(ip=next_ip, role="switch")
    job.schedule_timezone = "UTC"
    real_update = app._update_single_device
    next_starts = []

    async def run_device(current_job, address, pass_number=1):
        if address == IP:
            return await real_update(current_job, address, pass_number)
        assert current_job.devices[IP].status == "success"
        assert current_job.recovery_locators[IP]["ap_recovery"]["state"] == "complete"
        assert db.get_active_jobs()[0]["in_flight_device_ip"] is None
        next_starts.append(address)
        current_job.devices[address].status = "success"

    monkeypatch.setattr(app, "_update_single_device", run_device)
    monkeypatch.setattr(app, "_scheduled_job_guard", AsyncMock(return_value=(True, "")))
    await app.run_update_job(job, concurrency=1)
    assert next_starts == ([] if failure else [next_ip])
    assert writes == [("reboot", IP), ("upload", NEW), ("install", NEW)]
    assert db.get_device(NEW)["uid"] == "ap-serial"
    if failure:
        assert job.devices[IP].status == "failed"
        assert job.cancelled
        assert "Address changed after update" not in job.devices[IP].progress_message
    else:
        assert job.devices[IP].status == "success", job.devices[IP].error
        assert not job.cancelled
        assert job.devices[IP].progress_message == "Address changed after update; identity verified"
        assert client._client.read_deadline is None


@pytest.mark.asyncio
async def test_callback_uses_shared_locator_and_separate_probe_client(live, monkeypatch):
    from updater.ap_locator import locate_ap
    job, client, candidate = live
    candidate["ip"] = IP
    await capture(job, client)
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    saved = job.recovery_locators[IP]["ap_recovery"]
    saved["reboot_started"] -= 100
    saved["next_probe_at"] = app.monotonic() - 1
    monkeypatch.setattr(app, "locate_ap", locate_ap)
    assert await callback(job, client)
    assert saved["rounds"] == 1
    assert saved["addresses"] == [IP]


@pytest.mark.asyncio
async def test_expired_or_unarmed_callback_never_probes(live, monkeypatch):
    job, client, _ = live
    locate = AsyncMock()
    monkeypatch.setattr(app, "locate_ap", locate)
    await capture(job, client)
    recovery = app._make_recovery_callback(job, IP, client, "ap-serial", "TNA-301", RADIO)
    with pytest.raises(RuntimeError, match="not armed"):
        await recovery(app.monotonic() + 600, app.monotonic())
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    with pytest.raises(RuntimeError, match="Device not found"):
        await recovery(app.monotonic() - 1, app.monotonic())
    locate.assert_not_called()


@pytest.mark.asyncio
async def test_shared_locator_finds_moved_ap_from_saved_cpe(live, monkeypatch):
    from updater.ap_locator import locate_ap
    job, client, _ = live
    await capture(job, client)
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    saved = job.recovery_locators[IP]["ap_recovery"]
    source = db.get_access_point(IP)
    cpe_ip = "192.0.2.20"
    saved["cpes"] = [{"ip": cpe_ip, "serial": "cpe-serial", "model": "TNA-302",
                      "credential_ref": f"ap:{source['id']}"}]
    saved["reboot_started"] -= 100
    saved["next_probe_at"] = app.monotonic() - 1
    probes = []

    class Probe(Client):
        last_connected_aps_valid = True

        async def login(self):
            probes.append(self.ip)
            assert client.ip == IP
            return self.ip != IP

        async def get_device_info(self, include_management=False):
            if self.ip == cpe_ip:
                return SimpleNamespace(serial="cpe-serial", model="TNA-302")
            return await super().get_device_info()

        async def get_connected_aps(self):
            return [{"ip": NEW, "mac": RADIO}]

    monkeypatch.setattr(app, "TachyonClient", Probe)
    monkeypatch.setattr(app, "locate_ap", locate_ap)
    assert await callback(job, client)
    assert probes == [cpe_ip, IP, NEW, NEW]
    assert client.ip == NEW
    assert saved["rounds"] == 1
    assert saved["addresses"] == [IP, NEW]
