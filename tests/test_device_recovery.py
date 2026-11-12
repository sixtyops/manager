"""Offline checks for serial-confirmed Tachyon reboot recovery."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from updater import app
from updater.models import CPEInfo
from updater.poller import NetworkPoller
from updater.vendors.tachyon import client as tachyon


OLD_IP = "192.0.2.20"
NEW_IP = "192.0.2.21"
OLD_AP = "192.0.2.1"
NEW_AP = "192.0.2.2"
RADIO_MAC = "02:00:00:00:00:20"
SERIAL = "TACHYON-EXPECTED"


class RecoveryClient:
    def __init__(self, serials):
        self.ip = OLD_IP
        self.serials = serials
        self._token = "old-token"

    async def connect(self):
        return self.ip in self.serials

    async def get_device_info(self):
        return SimpleNamespace(serial=self.serials.get(self.ip), model="TNA-301")


def recovery_job(role="cpe"):
    job = app.UpdateJob(job_id="recovery-test")
    job.devices[OLD_IP] = app.DeviceStatus(
        ip=OLD_IP, role=role, parent_ap=OLD_AP if role == "cpe" else None
    )
    job.recovery_locators[OLD_IP] = {
        "original_ip": OLD_IP,
        "original_parent_ap": OLD_AP if role == "cpe" else None,
        "ip": OLD_IP,
        "parent_ap": OLD_AP if role == "cpe" else None,
        "radio_mac": RADIO_MAC,
        "serial": SERIAL,
        "model": "TNA-301",
    }
    return job


def peer(ip, mac=RADIO_MAC):
    return CPEInfo(ip=ip, mac=mac)


async def run_recovery(monkeypatch, serials, snapshots, role="cpe"):
    job = recovery_job(role)
    client = RecoveryClient(serials)
    now = app.monotonic()
    removed = []
    poller = SimpleNamespace(
        get_latest_peer_snapshots=lambda: snapshots,
        _remove_cached_cpe_client=removed.append,
    )
    monkeypatch.setattr(app, "get_poller", lambda: poller)
    monkeypatch.setattr(app.db, "get_access_points", lambda enabled_only=True: [
        {"ip": OLD_AP}, {"ip": NEW_AP},
    ])
    monkeypatch.setattr(app.db, "save_active_job", lambda *args: None)
    relocate = MagicMock(return_value=True)
    monkeypatch.setattr(app.db, "relocate_cpe_cache", relocate)
    callback = app._make_recovery_callback(
        job, OLD_IP, client, SERIAL, "TNA-301", RADIO_MAC,
    )
    success = await callback(now + 5, now)
    return success, job, client, relocate, removed


@pytest.mark.asyncio
async def test_same_ip_is_confirmed_by_serial_without_relocation(monkeypatch):
    success, job, client, relocate, removed = await run_recovery(
        monkeypatch, {OLD_IP: SERIAL}, {},
    )
    assert success
    assert client.ip == OLD_IP
    assert job.recovery_locators[OLD_IP]["serial"] == SERIAL
    relocate.assert_not_called()
    assert OLD_IP in removed


@pytest.mark.parametrize("reported_mac", ["02:00:00:00:00:21", "02:00:00:00:00:20"])
@pytest.mark.asyncio
async def test_ap_recovery_uses_saved_cpe_peer_and_commits_verified_move(monkeypatch, reported_mac):
    class APClient:
        def __init__(self):
            self.ip = OLD_IP
            self._client = SimpleNamespace(ip=OLD_IP, _base_url=f"https://{OLD_IP}", _token=None)

        async def connect(self):
            return self.ip == NEW_IP

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            return [{"ip": NEW_IP, "mac": reported_mac}]

    job = recovery_job(role="ap")
    job.device_firmware_map[OLD_IP] = "/tmp/tna-30x-1.15.1.bin"
    job.credentials = {OLD_IP: ("user", "password"), NEW_IP: ("user", "password")}
    job.recovery_locators[OLD_IP].update({
        "ap_identity": {
            "device_id": 1,
            "ethernet_mac": "02:00:00:00:00:21",
            "radio_mac": "02:00:00:00:00:20",
        },
        "management_baseline": {"vlan": 12, "ip_mode": "dhcp", "address": OLD_IP},
        "cpe_locators": [{"ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X"}],
    })
    monkeypatch.setattr(app, "get_poller", lambda: None)
    monkeypatch.setattr(app.db, "save_active_job", lambda *args: None)
    monkeypatch.setattr(app, "_has_open_recovery_identity_conflict", lambda *_args: False)
    relocate = MagicMock(return_value=True)
    monkeypatch.setattr(app.db, "relocate_ap_address", relocate)
    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda *_args, **_kwargs: CPEClient())
    client = APClient()
    callback = app._make_recovery_callback(job, OLD_IP, client, SERIAL, "TNA-301", RADIO_MAC)

    assert await callback(app.monotonic() + 20, app.monotonic())
    assert client.ip == NEW_IP
    assert job.recovery_locators[OLD_IP]["ip"] == NEW_IP
    assert job.recovery_locators[OLD_IP]["address_changed"] is True
    relocate.assert_called_once_with(1, SERIAL, OLD_IP, NEW_IP)


@pytest.mark.asyncio
async def test_ap_baseline_reads_radio_mac_from_wrapped_tachyon_client(monkeypatch):
    job = recovery_job(role="ap")
    job.credentials = {}

    class APClient:
        last_connected_cpes_valid = True

        def __init__(self):
            self._client = SimpleNamespace(
                get_ap_radio_mac=AsyncMock(return_value="02:00:00:00:00:20")
            )

        async def get_connected_cpes(self):
            return [peer(NEW_IP)]

        async def get_device_info(self):
            return SimpleNamespace(
                ip=OLD_IP, serial=SERIAL, model="TNA-301", mac="02:00:00:00:00:21",
                management_vlan_id=12, management_ip_mode="dhcp",
                management_address=OLD_IP,
            )

    monkeypatch.setattr(app.db, "get_device", lambda ip: {"id": 1} if ip == OLD_IP else None)
    monkeypatch.setattr(app.db, "save_active_job", lambda *args: None)

    baseline = await app._capture_ap_recovery_baseline(job, OLD_IP, APClient())

    assert baseline == {"peers": {RADIO_MAC: {"ip": NEW_IP, "eth0_link_was_up": None}}}
    assert job.recovery_locators[OLD_IP]["ap_identity"] == {
        "device_id": 1,
        "ethernet_mac": "02:00:00:00:00:21",
        "radio_mac": "02:00:00:00:00:20",
    }
    assert job.recovery_locators[OLD_IP]["cpe_locators"] == []


@pytest.mark.asyncio
async def test_open_identity_conflict_stops_recovery(monkeypatch):
    connection = SimpleNamespace(execute=lambda query: SimpleNamespace(
        fetchall=lambda: [("mac", RADIO_MAC.lower().replace(":", "-"))],
    ))
    monkeypatch.setattr(app.db, "get_db", lambda: nullcontext(connection))
    snapshot = {
        NEW_AP: {"observed_at": app.monotonic() + 1, "peers": [peer(NEW_IP)]},
    }
    success, _, client, relocate, _ = await run_recovery(
        monkeypatch, {NEW_IP: SERIAL}, snapshot,
    )
    assert not success
    assert client.ip == NEW_IP
    relocate.assert_not_called()


@pytest.mark.parametrize("parent", [OLD_AP, NEW_AP])
@pytest.mark.asyncio
async def test_cpe_move_uses_peer_radio_mac_and_commits_current_locator(monkeypatch, parent):
    snapshot = {
        parent: {"observed_at": app.monotonic() + 1, "peers": [peer(NEW_IP)]},
    }
    success, job, client, relocate, _ = await run_recovery(
        monkeypatch, {NEW_IP: SERIAL}, snapshot,
    )
    assert success
    assert client.ip == NEW_IP
    assert job.devices[OLD_IP].parent_ap == parent
    relocate.assert_called_once()
    assert job.recovery_locators[OLD_IP]["ip"] == NEW_IP


@pytest.mark.asyncio
async def test_reused_old_ip_is_rejected_and_moved_serial_is_found(monkeypatch):
    snapshot = {
        NEW_AP: {"observed_at": app.monotonic() + 1, "peers": [peer(NEW_IP)]},
    }
    success, _, client, _, _ = await run_recovery(
        monkeypatch, {OLD_IP: "OTHER-SERIAL", NEW_IP: SERIAL}, snapshot,
    )
    assert success
    assert client.ip == NEW_IP


@pytest.mark.parametrize("candidate_serial", ["OTHER-SERIAL", None])
@pytest.mark.asyncio
async def test_wrong_or_missing_serial_is_not_recovered(monkeypatch, candidate_serial):
    snapshot = {
        NEW_AP: {"observed_at": app.monotonic(), "peers": [peer(NEW_IP)]},
    }
    success, _, _, relocate, _ = await run_recovery(
        monkeypatch, {OLD_IP: "OTHER-SERIAL", NEW_IP: candidate_serial}, snapshot,
    )
    assert not success
    relocate.assert_not_called()


@pytest.mark.parametrize("mac,observed", [
    ("02:00:00:00:00:99", None),
    (RADIO_MAC, -10),
    (RADIO_MAC, None),
])
@pytest.mark.asyncio
async def test_missing_changed_stale_or_failed_peer_report_does_not_recover(
    monkeypatch, mac, observed,
):
    snapshots = {}
    if observed is not None:
        snapshots[NEW_AP] = {
            "observed_at": app.monotonic() + observed,
            "peers": [peer(NEW_IP, mac)],
        }
    success, _, _, _, _ = await run_recovery(
        monkeypatch, {}, snapshots,
    )
    assert not success


@pytest.mark.asyncio
async def test_ambiguous_serial_confirmed_candidates_are_rejected(monkeypatch):
    now = app.monotonic()
    snapshots = {
        OLD_AP: {"observed_at": now + 1, "peers": [peer(NEW_IP)]},
        NEW_AP: {"observed_at": now + 1, "peers": [peer("192.0.2.22")]},
    }
    success, _, _, relocate, _ = await run_recovery(
        monkeypatch,
        {NEW_IP: SERIAL, "192.0.2.22": SERIAL},
        snapshots,
    )
    assert not success
    relocate.assert_not_called()


@pytest.mark.parametrize("role", ["ap", "switch"])
@pytest.mark.asyncio
async def test_ap_and_switch_retry_known_address_without_peer_discovery(monkeypatch, role):
    snapshot = {
        NEW_AP: {"observed_at": app.monotonic() + 1, "peers": [peer(NEW_IP)]},
    }
    success, _, client, _, _ = await run_recovery(
        monkeypatch, {}, snapshot, role=role,
    )
    assert not success
    assert client.ip == OLD_IP


def test_poller_keeps_only_latest_successful_peer_snapshot():
    poller = NetworkPoller()
    client = SimpleNamespace(last_connected_cpes_valid=True)
    poller._record_peer_snapshot(OLD_AP, client, [peer(OLD_IP)])
    first = poller.get_latest_peer_snapshots()[OLD_AP]
    client.last_connected_cpes_valid = False
    poller._record_peer_snapshot(OLD_AP, client, [])
    latest = poller.get_latest_peer_snapshots()[OLD_AP]
    assert latest == first
    latest["peers"].clear()
    assert poller.get_latest_peer_snapshots()[OLD_AP]["peers"]


@pytest.mark.asyncio
async def test_crash_recovery_reads_saved_locator_and_serial_without_replaying_firmware(monkeypatch):
    payload = {
        "devices": [OLD_IP],
        "recovery": {OLD_IP: {"serial": SERIAL, "ip": NEW_IP, "parent_ap": OLD_AP}},
    }
    monkeypatch.setattr(app.db, "get_active_jobs", lambda: [{
        "job_id": "crashed", "device_ips_json": json.dumps(payload),
        "in_flight_device_ip": OLD_IP,
    }])
    monkeypatch.setattr(app.db, "get_access_point", lambda ip: {
        "username": "synthetic", "password": "synthetic",
    })
    checked = []

    class ReadOnlyClient:
        def __init__(self, ip, username, password):
            checked.append((ip, username, password))

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL)

    monkeypatch.setattr(app, "get_driver", lambda vendor: ReadOnlyClient)
    history = []
    monkeypatch.setattr(app.db, "save_device_update_history", lambda **row: history.append(row))
    monkeypatch.setattr(app.db, "clear_active_job", lambda job_id: None)
    await app._recover_crashed_device_jobs()
    assert checked == [(NEW_IP, "synthetic", "synthetic")]
    assert len(history) == 1
    assert history[0]["ip"] == OLD_IP
    assert history[0]["failed_stage"] == "crash_recovery"
    assert "read-only recovery confirmed serial" in history[0]["error"]


@pytest.mark.asyncio
async def test_upload_attempt_stops_when_serial_check_fails(monkeypatch):
    client = tachyon.TachyonClient(OLD_IP, "synthetic", "synthetic")
    request = AsyncMock(return_value=(0, b'{"status":"ok"}'))
    monkeypatch.setattr(tachyon, "_run_curl", request)
    uploaded = await client.upload_firmware(
        "/tmp/tna-30x-1.15.0-r55142.bin",
        serial_check=AsyncMock(return_value=False),
    )
    assert not uploaded
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_retry_checks_serial_again_before_next_attempt(monkeypatch):
    client = tachyon.TachyonClient(OLD_IP, "synthetic", "synthetic")
    request = AsyncMock(return_value=(7, b""))
    serial_check = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(tachyon, "_run_curl", request)
    monkeypatch.setattr(tachyon.asyncio, "sleep", AsyncMock())
    uploaded = await client.upload_firmware(
        "/tmp/tna-30x-1.15.0-r55142.bin", serial_check=serial_check,
    )
    assert not uploaded
    assert serial_check.await_count == 2
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_install_stops_when_serial_changes_after_upload(monkeypatch):
    client = tachyon.TachyonClient(OLD_IP, "synthetic", "synthetic")
    client.login = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(return_value=tachyon.DeviceInfo(
        ip=OLD_IP, model="TNA-301", serial=SERIAL,
        current_version="1.12.3.54970",
    ))
    serial_check = AsyncMock(side_effect=[True, False])
    request = AsyncMock(return_value=(0, b'{"status":"ok"}'))
    monkeypatch.setattr(tachyon, "_run_curl", request)
    client.trigger_update = AsyncMock(return_value=True)
    result = await client.update_firmware(
        "/tmp/tna-30x-1.15.0-r55142.bin", serial_check=serial_check,
    )
    assert not result.success
    assert "serial" in result.error.lower()
    client.trigger_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_reboot_recovery_callback_uses_one_absolute_deadline(monkeypatch):
    client = tachyon.TachyonClient(OLD_IP, "synthetic", "synthetic")
    monkeypatch.setattr(tachyon.asyncio, "sleep", AsyncMock())

    class FailedPing:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def subprocess(*args, **kwargs):
        return FailedPing()

    monkeypatch.setattr(tachyon.asyncio, "create_subprocess_exec", subprocess)
    deadlines = []

    async def recover(deadline, started_at):
        deadlines.append((deadline, started_at))
        return len(deadlines) == 2

    assert await client.wait_for_reboot(timeout=10, recovery_callback=recover)
    assert len(deadlines) == 2
    assert deadlines[0][0] == deadlines[1][0]
    assert deadlines[0][1] == deadlines[1][1]


@pytest.mark.asyncio
async def test_recovery_callback_exception_fails_even_when_target_version_is_read_back():
    client = tachyon.TachyonClient(OLD_IP, "synthetic", "synthetic")
    target_version = "1.15.0.55142"
    client.login = AsyncMock(return_value=True)
    client.get_device_info = AsyncMock(side_effect=[
        tachyon.DeviceInfo(
            ip=OLD_IP, model="TNA-301", current_version="1.14.0.50000",
        ),
        tachyon.DeviceInfo(
            ip=OLD_IP, model="TNA-301", current_version=target_version,
        ),
    ])
    client.upload_firmware = AsyncMock(return_value=True)
    client.trigger_update = AsyncMock(return_value=True)

    async def wait_for_reboot(*, timeout, recovery_callback):
        return await recovery_callback(200, 100)

    client.wait_for_reboot = AsyncMock(side_effect=wait_for_reboot)

    async def recovery_callback(_deadline, _started_at):
        raise RuntimeError("recovery smoke check failed")

    result = await client.update_firmware(
        "/tmp/tna-30x-1.15.0-r55142-test.bin",
        recovery_callback=recovery_callback,
    )

    assert result.success is False
    assert result.new_version == target_version
    assert result.error == "recovery smoke check failed"
