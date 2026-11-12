"""Offline checks for serial-confirmed Tachyon reboot recovery."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
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
        "cpe_locator_count": 1,
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
    assert job.recovery_locators[OLD_IP]["cpe_locators"] == [{
        "device_id": None, "ip": NEW_IP, "serial": None, "model": None,
        "mac": RADIO_MAC, "credential_ref": OLD_IP, "parent_ap_ip": OLD_IP,
    }]


@pytest.mark.asyncio
async def test_ap_baseline_fails_closed_without_radio_mac_reader(monkeypatch):
    job = recovery_job(role="ap")

    class APClient:
        async def get_connected_cpes(self):
            return []

        async def get_device_info(self):
            return SimpleNamespace(
                ip=OLD_IP, serial=SERIAL, model="TNA-301", mac="02:00:00:00:00:21",
                management_vlan_id=12, management_ip_mode="dhcp",
                management_address=OLD_IP,
            )

    with pytest.raises(RuntimeError, match="radio MAC reader is unavailable"):
        await app._capture_ap_recovery_baseline(job, OLD_IP, APClient())


@pytest.mark.asyncio
async def test_ap_recovery_fails_closed_without_saved_cpe_locators(monkeypatch):
    job = recovery_job(role="ap")
    client = RecoveryClient({})
    callback = app._make_recovery_callback(job, OLD_IP, client, SERIAL, "TNA-301", RADIO_MAC)

    assert not await callback(app.monotonic() + 5, app.monotonic())
    assert callback.failure_reason["reason"] == "Device not found after update"


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


@pytest.mark.parametrize("failure", ["non_answering_cpe", "wrong_ap_identity", "multiple_matches", "timeout"])
@pytest.mark.asyncio
async def test_crashed_ap_recovery_discovers_read_only_and_fails_closed(monkeypatch, failure):
    cpe_ip = NEW_IP
    second_ap_ip = "192.0.2.22"
    timeout_seconds = 70 if failure in {"non_answering_cpe", "timeout"} else 30
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)).isoformat()
    clock = [100.0]
    attempts = []

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(app, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app.asyncio, "sleep", advance)
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_deadline_utc": deadline,
        "reboot_started_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "ap_identity": {
            "device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
            "radio_mac": "02:00:00:00:00:20",
        },
        "cpe_locators": [{
            "ip": cpe_ip, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 1, "parent_ap_ip": OLD_AP,
        }],
        "cpe_locator_count": 1,
    }
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: {
        "username": "cpe-user", "password": "cpe-pass",
    })

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            attempts.append(clock[0])
            if failure == "timeout":
                raise asyncio.TimeoutError
            return failure != "non_answering_cpe"

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            if failure == "multiple_matches":
                return [
                    {"ip": NEW_AP, "mac": "02:00:00:00:00:21"},
                    {"ip": second_ap_ip, "mac": "02:00:00:00:00:20"},
                ]
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:21"}]

    class APClient:
        def __init__(self, ip, *_args, **_kwargs):
            self.ip = ip

        async def connect(self):
            return failure not in {"non_answering_cpe", "timeout"}

        async def get_device_info(self):
            wrong = failure == "wrong_ap_identity"
            return SimpleNamespace(
                serial="OTHER-AP" if wrong else SERIAL,
                model="TNA-302" if wrong else "TNA-301",
            )

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda ip, *_args, **_kwargs:
                        CPEClient() if ip == cpe_ip else APClient(ip))
    monkeypatch.setattr(app, "_persist_recovery_locator", lambda *_args: pytest.fail("read-only path persisted state"))
    monkeypatch.setattr(app.db, "relocate_ap_address", lambda *_args: pytest.fail("read-only path relocated AP"))

    result = await app._reconcile_crashed_ap_read_only(locator)

    if failure == "multiple_matches":
        assert "multiple matching addresses" in result
    elif failure == "wrong_ap_identity":
        assert "no matching address" in result
    elif failure in {"non_answering_cpe", "timeout"}:
        assert "deadline expired" in result
        assert len(attempts) >= 3
    else:
        assert "deadline expired" in result


@pytest.mark.asyncio
async def test_crashed_ap_recovery_confirms_moved_address_without_writes(monkeypatch):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_deadline_utc": deadline,
        "reboot_started_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "ap_identity": {
            "device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
            "radio_mac": "02:00:00:00:00:20",
        },
        "cpe_locators": [{
            "ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 1, "parent_ap_ip": OLD_AP,
        }],
        "cpe_locator_count": 1,
    }
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: {
        "username": "cpe-user", "password": "cpe-pass",
    })
    checked = []

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:20"}]

    class APClient:
        def __init__(self, ip, *_args, **_kwargs):
            self.ip = ip
            checked.append(ip)

        async def connect(self):
            return self.ip != OLD_AP

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda ip, *_args, **_kwargs:
                        CPEClient() if ip == NEW_IP else APClient(ip))
    monkeypatch.setattr(app.db, "relocate_ap_address", lambda *_args: pytest.fail("read-only path relocated AP"))

    result = await app._reconcile_crashed_ap_read_only(locator)

    assert f"confirmed AP serial {SERIAL} at {NEW_AP}" in result
    assert set(checked) == {OLD_AP, NEW_AP}
    assert locator["ip"] == OLD_AP


@pytest.mark.asyncio
async def test_crashed_ap_recovery_fails_closed_when_deadline_expired():
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_deadline_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "reboot_started_utc": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locators": [{
            "ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 1, "parent_ap_ip": OLD_AP,
        }],
        "cpe_locator_count": 1,
    }
    assert "deadline expired" in await app._reconcile_crashed_ap_read_only(locator)


@pytest.mark.asyncio
async def test_crashed_ap_recovery_uses_saved_parent_ap_credential_fallback(monkeypatch):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_deadline_utc": deadline,
        "reboot_started_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 1,
        "cpe_locators": [{
            "ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 404, "parent_ap_ip": OLD_AP,
        }],
    }
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: None)
    used_credentials = []

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:20"}]

    class APClient:
        def __init__(self, ip, *_args, **_kwargs):
            self.ip = ip

        async def connect(self):
            return self.ip == NEW_AP

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    def make_client(ip, username, password, **kwargs):
        if ip == NEW_IP:
            used_credentials.append((username, password))
            return CPEClient()
        return APClient(ip, username, password, **kwargs)

    monkeypatch.setattr(app, "get_driver", lambda _vendor: make_client)
    result = await app._reconcile_crashed_ap_read_only(locator)

    assert f"confirmed AP serial {SERIAL} at {NEW_AP}" in result
    assert used_credentials == [("ap-user", "ap-pass")]


@pytest.mark.asyncio
async def test_crashed_ap_recovery_skips_offline_cpe_and_uses_another_saved_locator(monkeypatch):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_deadline_utc": deadline,
        "reboot_started_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 2,
        "cpe_locators": [
            {"ip": NEW_IP, "serial": "CPE-OFFLINE", "model": "TNA-303X",
             "credential_ref": 1, "parent_ap_ip": OLD_AP},
            {"ip": "192.0.2.30", "serial": "CPE-ONLINE", "model": "TNA-303X",
             "credential_ref": 2, "parent_ap_ip": OLD_AP},
        ],
    }
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: {
        "username": "cpe-user", "password": "cpe-pass",
    })

    class CPEClient:
        last_connected_aps_valid = True

        def __init__(self, ip):
            self.ip = ip

        async def connect(self):
            return self.ip != NEW_IP

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-ONLINE", model="TNA-303X")

        async def get_connected_aps(self):
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:20"}]

    class APClient:
        def __init__(self, ip):
            self.ip = ip

        async def connect(self):
            return self.ip == NEW_AP

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda ip, *_args, **_kwargs:
                        CPEClient(ip) if ip in {NEW_IP, "192.0.2.30"} else APClient(ip))

    result = await app._reconcile_crashed_ap_read_only(locator)

    assert f"confirmed AP serial {SERIAL} at {NEW_AP}" in result


@pytest.mark.asyncio
async def test_crashed_ap_recovery_retries_empty_peer_report_until_matching_report(monkeypatch):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_deadline_utc": deadline,
        "reboot_started_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 1,
        "cpe_locators": [{
            "ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 1, "parent_ap_ip": OLD_AP,
        }],
    }
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: {
        "username": "cpe-user", "password": "cpe-pass",
    })
    peer_reads = []

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            peer_reads.append(True)
            if len(peer_reads) == 1:
                return []
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:20"}]

    class APClient:
        def __init__(self, ip):
            self.ip = ip

        async def connect(self):
            return self.ip == NEW_AP

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(app.asyncio, "sleep", no_delay)
    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda ip, *_args, **_kwargs:
                        CPEClient() if ip == NEW_IP else APClient(ip))

    result = await app._reconcile_crashed_ap_read_only(locator)

    assert len(peer_reads) == 2
    assert f"confirmed AP serial {SERIAL} at {NEW_AP}" in result


@pytest.mark.asyncio
async def test_crashed_ap_retries_unavailable_cpe_until_valid_report(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_started_utc": started.isoformat(),
        "reboot_deadline_utc": (started + timedelta(seconds=90)).isoformat(),
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 1,
        "cpe_locators": [{
            "ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 1, "parent_ap_ip": OLD_AP,
        }],
    }
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: {
        "username": "cpe-user", "password": "cpe-pass",
    })
    clock = [200.0]
    sleeps = []

    async def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(app, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app.asyncio, "sleep", advance)
    attempts = []

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            attempts.append(clock[0])
            return len(attempts) > 1

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:20"}]

    class APClient:
        def __init__(self, ip):
            self.ip = ip

        async def connect(self):
            return self.ip == NEW_AP

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda ip, *_args, **_kwargs:
                        CPEClient() if ip == NEW_IP else APClient(ip))

    result = await app._reconcile_crashed_ap_read_only(locator)

    assert f"confirmed AP serial {SERIAL} at {NEW_AP}" in result
    assert attempts == [200.0, 230.0]
    assert sleeps == pytest.approx([30.0], abs=0.001)


@pytest.mark.asyncio
async def test_crashed_ap_recovery_caps_distinct_addresses_at_32(monkeypatch):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_deadline_utc": deadline,
        "reboot_started_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 1,
        "cpe_locators": [{
            "ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 1, "parent_ap_ip": OLD_AP,
        }],
    }
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: {
        "username": "cpe-user", "password": "cpe-pass",
    })

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            return [
                {"ip": f"198.51.100.{index}", "mac": "02:00:00:00:00:20"}
                for index in range(1, 34)
            ]

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda *_args, **_kwargs: CPEClient())
    result = await app._reconcile_crashed_ap_read_only(locator)

    assert "exceeded 32 distinct candidates" in result


@pytest.mark.asyncio
async def test_restart_ap_discovery_keeps_transport_timeout_for_slow_device(monkeypatch):
    started = datetime.now(timezone.utc)
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "reboot_started_utc": started.isoformat(),
        "reboot_deadline_utc": (started + timedelta(seconds=85)).isoformat(),
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 1,
        "cpe_locators": [{
            "ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 1, "parent_ap_ip": OLD_AP,
        }],
    }
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: {
        "username": "cpe-user", "password": "cpe-pass",
    })
    observed_timeouts = []

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:20"}]

    class APClient:
        def __init__(self, ip, *_args, timeout, **_kwargs):
            self.ip = ip
            self.timeout = timeout
            observed_timeouts.append(timeout)

        async def connect(self):
            return self.ip == NEW_AP

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda ip, *_args, **kwargs:
                        CPEClient() if ip == NEW_IP else APClient(ip, **kwargs))
    monkeypatch.setattr(app.db, "relocate_ap_address", lambda *_args: pytest.fail("restart moved AP"))
    monkeypatch.setattr(app, "_persist_recovery_locator", lambda *_args: pytest.fail("restart persisted state"))

    result = await app._reconcile_crashed_ap_read_only(locator)

    assert f"confirmed AP serial {SERIAL} at {NEW_AP}" in result
    assert observed_timeouts and max(observed_timeouts) >= 85


@pytest.mark.asyncio
async def test_ap_locator_retries_unready_reported_candidate_with_30_second_spacing(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(app, "monotonic", lambda: now[0])
    sleeps = []

    async def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(app.asyncio, "sleep", advance)
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 1,
        "cpe_locators": [{"ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X"}],
    }
    peer_reads = []
    candidate_reads = []
    candidate_attempts = {}

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            peer_reads.append(now[0])
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:20"}]

    class APClient:
        def __init__(self, ip):
            self.ip = ip

        async def connect(self):
            candidate_reads.append((self.ip, now[0]))
            candidate_attempts[self.ip] = candidate_attempts.get(self.ip, 0) + 1
            return self.ip == NEW_AP and candidate_attempts[self.ip] > 1

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    def factory(ip, *_args, **_kwargs):
        return CPEClient() if ip == NEW_IP else APClient(ip)

    monkeypatch.setattr(app, "get_driver", lambda _vendor: factory)
    result, reason = await app._locate_ap_read_only(
        locator, now[0] + 70, now[0], lambda _saved: ("u", "p"), ("u", "p"),
    )

    assert result == NEW_AP and reason is None
    assert len(peer_reads) == 2
    assert sleeps == [30]
    assert peer_reads[1] - peer_reads[0] == 30


@pytest.mark.asyncio
async def test_ap_locator_stops_after_twenty_rounds(monkeypatch):
    clock = [50.0]
    round_starts = []
    cpe_attempts = []

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(app, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app.asyncio, "sleep", advance)
    locator = {
        "serial": SERIAL, "model": "TNA-301", "ip": OLD_AP,
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 1,
        "cpe_locators": [{"ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X"}],
    }

    class CPEClient:
        async def connect(self):
            cpe_attempts.append(clock[0])
            return False

    class APClient:
        def __init__(self, ip):
            self.ip = ip

        async def connect(self):
            return False

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda ip, *_args, **_kwargs:
                        CPEClient() if ip == NEW_IP else APClient(ip))

    address, reason = await app._locate_ap_read_only(
        locator, clock[0] + 600, clock[0], lambda _saved: ("u", "p"), ("u", "p"),
        on_round_start=lambda number, started: round_starts.append((number, started)),
    )

    assert address is None
    assert reason == "AP recovery reached its 20-round limit"
    assert [number for number, _ in round_starts] == list(range(20))
    assert cpe_attempts == [50.0 + 30 * index for index in range(20)]


@pytest.mark.asyncio
async def test_crashed_ap_recovery_persists_round_before_restart(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    locator = {
        "original_ip": OLD_AP, "ip": OLD_AP, "serial": SERIAL, "model": "TNA-301",
        "reboot_started_utc": started.isoformat(),
        "reboot_deadline_utc": (started + timedelta(seconds=180)).isoformat(),
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "cpe_locator_count": 1,
        "cpe_locators": [{
            "ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X",
            "credential_ref": 1, "parent_ap_ip": OLD_AP,
        }],
    }
    payload = {"devices": [OLD_AP], "order": [OLD_AP], "recovery": {OLD_AP: locator}}
    store = {
        "row": {
            "job_id": "crashed-ap", "device_ips_json": json.dumps(payload),
            "in_flight_device_ip": OLD_AP, "firmware_name": "firmware.bin",
            "started_at": started.isoformat(),
        },
        "updates": 0,
    }

    class ActiveJobConnection:
        def execute(self, query, parameters):
            assert query.startswith("UPDATE active_jobs SET device_ips_json")
            store["row"]["device_ips_json"] = parameters[0]
            store["updates"] += 1
            return SimpleNamespace(rowcount=1)

    monkeypatch.setattr(app.db, "get_db", lambda: nullcontext(ActiveJobConnection()))
    monkeypatch.setattr(app.db, "get_active_jobs", lambda: [dict(store["row"])] if store["row"] else [])
    monkeypatch.setattr(app.db, "clear_active_job", lambda _job_id: store.update(row=None))
    monkeypatch.setattr(app.db, "get_access_point", lambda _ip: {
        "username": "ap-user", "password": "ap-pass",
    })
    monkeypatch.setattr(app.db, "get_device_by_id", lambda _device_id: {
        "username": "cpe-user", "password": "cpe-pass",
    })
    monkeypatch.setattr(app.db, "relocate_ap_address", lambda *_args: pytest.fail("startup relocated AP"))
    monkeypatch.setattr(app, "_persist_recovery_locator", lambda *_args: pytest.fail("startup changed locator"))
    monkeypatch.setattr(app.db, "save_device_update_history", lambda **_row: None)
    clock = [500.0]
    sleeps = []

    async def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(app, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app.asyncio, "sleep", advance)
    cpe_attempts = []

    class SimulatedProcessCrash(BaseException):
        pass

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            cpe_attempts.append(clock[0])
            if len(cpe_attempts) == 1:
                raise SimulatedProcessCrash
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            return [{"ip": NEW_AP, "mac": "02:00:00:00:00:20"}]

    class APClient:
        def __init__(self, ip):
            self.ip = ip

        async def connect(self):
            return self.ip == NEW_AP

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda ip, *_args, **_kwargs:
                        CPEClient() if ip == NEW_IP else APClient(ip))

    with pytest.raises(SimulatedProcessCrash):
        await app._recover_crashed_device_jobs()

    persisted = json.loads(store["row"]["device_ips_json"])["recovery"][OLD_AP]
    assert persisted["ap_recovery_rounds"] == 1
    assert datetime.fromisoformat(persisted["ap_recovery_next_round_utc"]) >= (
        datetime.now(timezone.utc) + timedelta(seconds=29)
    )
    assert store["updates"] == 1

    await app._recover_crashed_device_jobs()

    assert cpe_attempts == pytest.approx([500.0, 530.0], abs=0.001)
    assert sleeps == pytest.approx([30.0], abs=0.001)
    assert store["updates"] == 2
    assert store["row"] is None


@pytest.mark.asyncio
async def test_in_run_ap_ambiguity_does_not_move_ap(monkeypatch):
    job = recovery_job(role="ap")
    job.credentials = {OLD_IP: ("u", "p"), NEW_AP: ("u", "p")}
    locator = job.recovery_locators[OLD_IP]
    locator.update({
        "ap_identity": {"device_id": 7, "ethernet_mac": "02:00:00:00:00:21",
                        "radio_mac": "02:00:00:00:00:20"},
        "management_baseline": {"vlan": 12, "ip_mode": "dhcp", "address": OLD_IP},
        "cpe_locators": [{"ip": NEW_IP, "serial": "CPE-SERIAL", "model": "TNA-303X"}],
        "cpe_locator_count": 1,
    })

    class APClient:
        def __init__(self):
            self.ip = OLD_IP
            self._client = SimpleNamespace(ip=OLD_IP, _base_url=f"https://{OLD_IP}", _token=None)

        async def connect(self):
            return self.ip in {NEW_AP, "192.0.2.22"}

        async def get_device_info(self):
            return SimpleNamespace(serial=SERIAL, model="TNA-301")

    class CPEClient:
        last_connected_aps_valid = True

        async def connect(self):
            return True

        async def get_device_info(self):
            return SimpleNamespace(serial="CPE-SERIAL", model="TNA-303X")

        async def get_connected_aps(self):
            return [
                {"ip": NEW_AP, "mac": "02:00:00:00:00:20"},
                {"ip": "192.0.2.22", "mac": "02:00:00:00:00:21"},
            ]

    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda *_args, **_kwargs: CPEClient())
    monkeypatch.setattr(app.db, "relocate_ap_address", lambda *_args: pytest.fail("ambiguous AP moved"))
    persisted_round = MagicMock()
    monkeypatch.setattr(app, "_persist_recovery_locator", persisted_round)
    callback = app._make_recovery_callback(job, OLD_IP, APClient(), SERIAL, "TNA-301", RADIO_MAC)

    assert not await callback(app.monotonic() + 20, app.monotonic())
    assert "Ambiguous" in callback.failure_reason["reason"]
    assert locator["ip"] == OLD_IP
    persisted_round.assert_called_once()


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
