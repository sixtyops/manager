"""Offline proof for AP recovery intent and reboot deadline checkpoints."""

import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from updater import app
from updater import database as db


IP = "192.0.2.10"
CPE = "192.0.2.20"
PARENT = "192.0.2.30"
ETHERNET = "02:11:22:33:44:50"
RADIO = "02:11:22:33:44:51"
FIRMWARE = "/tmp/tna-30x-1.15.1-r1.bin"


class Client:
    def __init__(self, ip=IP, *_credentials):
        self.ip = ip
        self._client = SimpleNamespace()
        self.updated = False
        self.events = []
        self.get_ap_radio_mac = AsyncMock(return_value=RADIO)

    async def connect(self):
        return True

    async def get_device_info(self):
        return SimpleNamespace(
            ip=self.ip, serial="ap-serial", model="TNA-301", mac=ETHERNET,
            current_version="1.15.1.1" if self.updated else "1.12.3",
            extra={"management_vlan_id": 12, "management_ip_mode": "dhcp",
                   "management_address": self.ip},
        )

    async def get_connected_cpes(self):
        return []

    async def get_config(self):
        return {"system": {"name": "intent-test"}}

    def get_hardware_id(self, _model):
        return "test-hardware"

    def get_reboot_timeout(self, _role):
        return 600

    def get_update_timeout(self, _role):
        return 900

    async def reboot(self, **_options):
        self.events.append(("reboot", saved_intent(self.job)))
        return True

    async def update_firmware(self, _path, _progress, *, serial_check, **_options):
        assert await serial_check("upload")
        self.events.append(("upload", saved_intent(self.job)))
        assert await serial_check("install")
        self.events.append(("install", saved_intent(self.job)))
        self.updated = True
        return app.UpdateResult(ip=self.ip, success=True, new_version="1.15.1.1")

    async def run_smoke_tests(self, **_options):
        return SimpleNamespace(passed=True, warnings=[], checks=[
            {"check": name, "passed": True}
            for name in ("device_responsive", "config_intact", "cpe_connectivity", "cpe_signal_levels")
        ])


def saved_intent(job):
    row = next(row for row in db.get_active_jobs() if row["job_id"] == job.job_id)
    return json.loads(row["device_ips_json"])["recovery"][IP]["ap_recovery"]


@pytest.fixture
def prepared(mock_db, monkeypatch):
    device_id = db.upsert_device(IP, "ap", "test-user", "test-password")
    job = app.UpdateJob(
        job_id="ap-intent", device_firmware_map={IP: FIRMWARE},
        credentials={IP: ("test-user", "test-password")}, device_order=[IP],
        devices={IP: app.DeviceStatus(ip=IP, role="ap", model="TNA-301")},
        recovery_locators={IP: {"ip": IP, "serial": "ap-serial", "model": "TNA-301"}},
    )
    client = Client()
    client.job = job
    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda *_args: client)
    monkeypatch.setattr(app, "broadcast", AsyncMock())
    monkeypatch.setattr(app, "_shutting_down", False)
    return job, client, device_id


async def capture(job, client, *, baseline=None, stage="install", info=None):
    await app._capture_ap_recovery_intent(
        job, IP, client, info or await client.get_device_info(),
        baseline or {"peers": {}}, "1.15.1", stage,
    )


@pytest.mark.asyncio
async def test_capture_allows_zero_locators_and_saves_no_credentials(prepared):
    job, client, device_id = prepared
    await capture(job, client)
    saved = saved_intent(job)
    assert saved["device_id"] == device_id
    assert saved["credential_ref"] == f"ap:{device_id}"
    assert saved["serial"] == "ap-serial"
    assert saved["model"] == "TNA-301"
    assert saved["ip"] == IP
    assert saved["expected_version"] == "1.15.1"
    assert saved["ethernet_mac"] == ETHERNET
    assert saved["radio_mac"] == RADIO
    assert saved["cpes"] == []
    assert saved["rounds"] == 0 and saved["next_probe_at"] == 0
    assert saved["addresses"] == [IP]
    assert saved["state"] == "captured" and "deadline_utc" not in saved
    assert saved["management"] == {"vlan": 12, "ip_mode": "dhcp", "address": IP}
    assert not any(value in json.dumps(saved) for value in job.credentials[IP])


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["missing", None, "failure", "", ETHERNET[:-1], "01:11:22:33:44:50"])
@pytest.mark.parametrize("pass_number", [1, 2])
async def test_bad_radio_reader_blocks_all_device_writes(prepared, reader, pass_number):
    job, client, _ = prepared
    if reader == "missing":
        del client.get_ap_radio_mac
    elif reader is None:
        client.get_ap_radio_mac = None
    else:
        client.get_ap_radio_mac = AsyncMock(
            side_effect=OSError("radio read failed") if reader == "failure" else None,
            return_value=reader,
        )
    await app._update_single_device(job, IP, pass_number=pass_number)
    assert job.devices[IP].status == "failed"
    assert client.events == []
    assert "radio" in job.devices[IP].error.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("serial", None), ("model", None), ("mac", None),
    ("management_vlan_id", None), ("management_ip_mode", "unknown"),
    ("management_address", "192.0.2.99"),
])
async def test_invalid_identity_or_management_baseline_blocks_capture(prepared, field, value):
    job, client, _ = prepared
    info = await client.get_device_info()
    if field.startswith("management_"):
        info.extra[field] = value
    else:
        setattr(info, field, value)
    with pytest.raises(RuntimeError):
        await capture(job, client, info=info)
    assert client.events == []
    assert "ap_recovery" not in job.recovery_locators[IP]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,source", [(True, PARENT), (False, IP)])
async def test_cpe_locator_saves_authenticated_selected_source(prepared, monkeypatch, selected, source):
    job, client, _ = prepared
    if selected:
        job.credentials[CPE] = ("selected-user", "selected-password")
        job.device_parent[CPE] = PARENT
    source_id = db.upsert_device(source, "ap", *job.credentials[CPE if selected else IP])
    observed = []
    cpe = SimpleNamespace(connect=AsyncMock(return_value=True), get_device_info=AsyncMock(
        return_value=SimpleNamespace(serial="cpe-serial", model="TNA-302"),
    ))

    def factory(ip, *credentials):
        observed.append((ip, credentials))
        return cpe

    monkeypatch.setattr(app, "get_driver", lambda _vendor: factory)
    monkeypatch.setattr(db, "get_cpe_by_ip", lambda _ip: {"model": "TNA-302", "ap_ip": "192.0.2.99"})
    await capture(job, client, baseline={"peers": {RADIO: {"ip": CPE, "eth0_link_was_up": True}}})
    assert observed == [(CPE, job.credentials[CPE if selected else IP])]
    assert saved_intent(job)["cpes"] == [{
        "ip": CPE, "serial": "cpe-serial", "model": "TNA-302",
        "credential_ref": f"ap:{source_id}",
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("auth,serial,model", [(False, "cpe", "TNA-302"), (True, None, "TNA-302"), (True, "cpe", "wrong")])
async def test_unconfirmed_cpe_is_omitted(prepared, monkeypatch, auth, serial, model):
    job, client, _ = prepared
    cpe = SimpleNamespace(connect=AsyncMock(return_value=auth), get_device_info=AsyncMock(
        return_value=SimpleNamespace(serial=serial, model=model),
    ))
    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda *_args: cpe)
    monkeypatch.setattr(db, "get_cpe_by_ip", lambda _ip: {"model": "TNA-302"})
    await capture(job, client, baseline={"peers": {RADIO: {"ip": CPE}}})
    assert saved_intent(job)["cpes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_state", ["captured", "write_pending"])
@pytest.mark.parametrize("failure", [False, "exception"])
@pytest.mark.parametrize("pass_number", [1, 2])
async def test_checkpoint_failure_blocks_reboot_or_install(prepared, monkeypatch, failed_state, failure, pass_number):
    job, client, _ = prepared
    original = db.save_active_job

    def checkpoint(*args):
        saved = json.loads(args[2])["recovery"][IP].get("ap_recovery", {})
        if saved.get("state") == failed_state:
            if failure == "exception":
                raise OSError("checkpoint unavailable")
            return False
        return original(*args)

    monkeypatch.setattr(db, "save_active_job", checkpoint)
    await app._update_single_device(job, IP, pass_number=pass_number)
    assert job.devices[IP].status == "failed"
    assert "checkpoint" in job.devices[IP].error.lower()
    assert [name for name, _ in client.events] == (
        ["upload"] if failed_state == "write_pending" and pass_number == 2 else []
    )


@pytest.mark.asyncio
async def test_reboot_arms_before_request_and_install_arms_after_upload(prepared):
    job, client, _ = prepared
    await app._update_single_device(job, IP)
    assert job.devices[IP].status == "success", job.devices[IP].error
    assert [name for name, _ in client.events] == ["reboot", "upload", "install"]
    reboot, upload, install = [saved for _, saved in client.events]
    for saved, stage in ((reboot, "reboot"), (install, "install")):
        assert saved["state"] == "write_pending" and saved["write_stage"] == stage
        assert saved["deadline"] - saved["reboot_started"] == pytest.approx(600)
        assert saved["next_probe_at"] - saved["reboot_started"] == pytest.approx(90)
        assert saved["remaining_s"] == 600
        assert (datetime.fromisoformat(saved["deadline_utc"])
                - datetime.fromisoformat(saved["checkpoint_utc"])).total_seconds() == pytest.approx(600)
    assert upload["state"] == "captured"
    assert "deadline" not in upload and "deadline_utc" not in upload
    assert install["reboot_started"] >= reboot["reboot_started"]


@pytest.mark.asyncio
async def test_armed_intent_cannot_renew_deadline(prepared):
    job, client, _ = prepared
    await capture(job, client)
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    before = saved_intent(job)
    with pytest.raises(RuntimeError, match="intent is unavailable"):
        app._arm_ap_reboot_deadline(job, IP, client, "install")
    assert saved_intent(job) == before


@pytest.mark.asyncio
async def test_missing_inventory_device_blocks_capture(prepared, monkeypatch):
    job, client, _ = prepared
    monkeypatch.setattr(db, "get_device", lambda _ip: None)
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        await capture(job, client)
    assert "ap_recovery" not in job.recovery_locators[IP]


@pytest.mark.asyncio
async def test_cpe_with_changed_known_serial_is_omitted(prepared, monkeypatch):
    job, client, _ = prepared
    db.upsert_device(CPE, "cpe", "test-user", "test-password")
    with db.get_db() as conn:
        conn.execute("UPDATE devices SET uid='expected-cpe', uid_source='serial' WHERE ip=?", (CPE,))
    cpe = SimpleNamespace(connect=AsyncMock(return_value=True), get_device_info=AsyncMock(
        return_value=SimpleNamespace(serial="other-cpe", model="TNA-302"),
    ))
    monkeypatch.setattr(app, "get_driver", lambda _vendor: lambda *_args: cpe)
    await capture(job, client, baseline={"peers": {RADIO: {"ip": CPE}}})
    assert saved_intent(job)["cpes"] == []


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True])
@pytest.mark.asyncio
async def test_invalid_budget_does_not_arm(prepared, budget):
    job, client, _ = prepared
    await capture(job, client)
    client.get_reboot_timeout = lambda _role: budget
    with pytest.raises(RuntimeError, match="budget is invalid"):
        app._arm_ap_reboot_deadline(job, IP, client, "install")
    assert saved_intent(job)["state"] == "captured"


@pytest.mark.parametrize("result", [False, True])
def test_persistence_projects_current_addresses_and_preserves_runtime_keys(prepared, result):
    job, _, _ = prepared
    current = "192.0.2.11"
    job.recovery_locators[IP]["ip"] = current
    before = deepcopy(job)
    if result:
        app._persist_device_result(job, IP)
    else:
        app._persist_recovery_locator(job, IP)
    row = db.get_active_jobs()[0]
    payload = json.loads(row["device_ips_json"])
    assert payload["devices"] == payload["order"] == [current]
    assert set(payload["recovery"]) == {current}
    assert row["in_flight_device_ip"] == (None if result else current)
    assert job == before


def test_persistence_rejects_colliding_confirmed_addresses(prepared):
    job, _, _ = prepared
    job.recovery_locators[CPE] = {"ip": IP}
    with pytest.raises(RuntimeError, match="addresses are ambiguous"):
        app._persist_recovery_locator(job, IP)
    assert db.get_active_jobs() == []
