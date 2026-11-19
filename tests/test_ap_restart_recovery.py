"""Offline restart proof with real Tachyon readers and mocked transport."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import pytest_asyncio

from updater import app, ap_locator, database as db
from updater.vendors.tachyon.client import TachyonClient
from tests.test_ap_recovery_intent import prepared, capture, IP, CPE, RADIO

NEW = "192.0.2.11"


class Crash(BaseException):
    pass


@pytest_asyncio.fixture
async def restart(prepared, monkeypatch):
    job, client, device_id = prepared
    with db.get_db() as conn:
        conn.execute("UPDATE devices SET uid='ap-serial', uid_source='serial' WHERE ip=?", (IP,))
    await capture(job, client)
    app._arm_ap_reboot_deadline(job, IP, client, "install")
    saved = job.recovery_locators[IP]["ap_recovery"]
    wall = datetime.now(timezone.utc)
    saved.update({
        "deadline": 1600, "reboot_started": 1000, "next_probe_at": 1130,
        "deadline_utc": (wall + timedelta(seconds=500)).isoformat(),
        "checkpoint_utc": wall.isoformat(), "remaining_s": 500,
        "rounds": 1, "cpes": [{"ip": CPE, "serial": "cpe-serial", "model": "TNA-302",
                                 "credential_ref": f"ap:{device_id}"}],
    })
    app._persist_recovery_locator(job, IP)
    clock = {"now": 5000., "events": [], "reports": [NEW],
             "relocate": db.relocate_ap_address}

    class WallClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = wall + timedelta(seconds=clock["now"] - 5000)
            return value if tz else value.replace(tzinfo=None)

    async def sleep(delay):
        assert delay > 0
        clock["now"] += delay

    async def login(probe):
        clock["events"].append((probe.ip, "login", clock["now"]))
        row = db.get_active_jobs()[0]
        progress = json.loads(row["device_ips_json"])["recovery"][row["in_flight_device_ip"]]["ap_recovery"]
        assert progress["rounds"] >= 2
        assert progress["state"] == "locating"
        if probe.ip == IP:
            return False
        assert probe.ip == CPE or probe.ip in progress["addresses"]
        return True

    async def curl(probe, method, endpoint):
        assert method == "GET"
        clock["events"].append((probe.ip, endpoint, clock["now"]))
        if endpoint == "/cgi.lua/status?type=system":
            return 200, json.dumps({"serial": "cpe-serial" if probe.ip == CPE else "ap-serial",
                                    "model": "TNA-302" if probe.ip == CPE else "TNA-301"})
        if endpoint == "/cgi.lua/status?type=wireless,zones":
            return 200, json.dumps({"wireless": {"peers": [
                {"mac": RADIO, "ipv4": ip, "model": "TNA-301", "system_name": "lab"}
                for ip in clock["reports"]]}})
        assert endpoint in {"/cgi.lua/bootbank", "/cgi.lua/status?type=interfaces"}
        return 200, "{}"

    async def locate(**options):
        return await ap_locator.locate_ap(**options, clock=lambda: clock["now"], sleep=sleep)

    monkeypatch.setattr(app, "datetime", WallClock)
    monkeypatch.setattr(app, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(app, "locate_ap", locate)
    monkeypatch.setattr(TachyonClient, "login", login)
    monkeypatch.setattr(TachyonClient, "_curl", curl)
    for name in ("_update_single_device", "run_update_job"):
        if hasattr(app, name):
            monkeypatch.setattr(app, name, Mock(side_effect=AssertionError("queue resumed")))
    move = Mock(side_effect=AssertionError("startup moved inventory"))
    monkeypatch.setattr(db, "relocate_ap_address", move)
    return job, client, saved, clock


def edit_saved(change):
    row = db.get_active_jobs()[0]
    payload = json.loads(row["device_ips_json"])
    change(payload["recovery"][row["in_flight_device_ip"]]["ap_recovery"])
    db.save_active_job(row["job_id"], "running", json.dumps(payload), "fw", row["in_flight_device_ip"])


async def recover(restart):
    job, client, _, _ = restart
    await app._recover_crashed_device_jobs()
    history = db.get_device_update_history_by_job(job.job_id)
    assert len(history) == 1
    assert history[0]["status"] == "failed"
    assert history[0]["failed_stage"] == "crash_recovery"
    assert "operator action required" in history[0]["error"]
    assert client.events == []
    db.relocate_ap_address.assert_not_called()
    app.run_update_job.assert_not_called()
    app._update_single_device.assert_not_called()
    assert db.get_active_jobs() == []
    assert db.get_device(IP)["ip"] == IP
    assert db.get_device(NEW) is None
    return history[0]


@pytest.mark.asyncio
async def test_moved_ap_is_observed_without_move_or_resume(restart):
    row = await recover(restart)
    assert f"at {NEW}" in row["error"]
    assert row["stages"][0]["evidence"]["observed_ip"] == NEW
    assert restart[3]["events"][0][2] == pytest.approx(5030)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    lambda s: s.update(state="captured"),
    lambda s: s.pop("deadline_utc"),
    lambda s: s.update(remaining_s=0),
    lambda s: s.update(checkpoint_utc="2099-01-01T00:00:00+00:00"),
    lambda s: s.update(rounds=20),
    lambda s: s.pop("baseline"),
    lambda s: s.pop("next_probe_at"),
    lambda s: s.update(credential_ref="ap:999"),
    lambda s: s["cpes"][0].update(credential_ref="ap:999"),
    lambda s: s.update(addresses=[IP] + [f"192.0.2.{i}" for i in range(40, 72)]),
])
async def test_invalid_or_exhausted_state_never_probes(restart, change):
    edit_saved(change)
    row = await recover(restart)
    assert "stopped" in row["error"]
    assert restart[3]["events"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("password", ""), ("role", "switch"), ("uid", "replaced-ap")])
async def test_missing_or_changed_credential_source_never_probes(restart, field, value):
    with db.get_db() as conn:
        conn.execute(f"UPDATE devices SET {field}=? WHERE ip=?", (value, IP))
    row = await recover(restart)
    assert "source" in row["error"]
    assert restart[3]["events"] == []


@pytest.mark.asyncio
async def test_checkpoint_failure_stops_before_transport(restart, monkeypatch):
    monkeypatch.setattr(db, "save_active_job", lambda *_a: False)
    row = await recover(restart)
    assert "checkpoint failed" in row["error"]
    assert restart[3]["events"] == []


@pytest.mark.asyncio
async def test_repeated_crashes_preserve_rounds_and_spacing(restart, monkeypatch):
    original = db.save_active_job
    crashes = 0

    def checkpoint(*args):
        nonlocal crashes
        result = original(*args)
        saved = json.loads(args[2])["recovery"][IP]["ap_recovery"]
        if saved["state"] == "locating":
            crashes += 1
            raise Crash()
        return result

    monkeypatch.setattr(db, "save_active_job", checkpoint)
    for expected, now in ((2, 5030), (3, 5060)):
        with pytest.raises(Crash):
            await app._recover_crashed_device_jobs()
        saved = json.loads(db.get_active_jobs()[0]["device_ips_json"])["recovery"][IP]["ap_recovery"]
        assert saved["rounds"] == expected
        assert restart[3]["now"] == pytest.approx(now)
    assert crashes == 2 and restart[3]["events"] == []
    monkeypatch.setattr(db, "save_active_job", original)
    await recover(restart)
    assert restart[3]["events"][0][2] == pytest.approx(5090)


@pytest.mark.asyncio
async def test_crash_at_last_round_cannot_grant_another_round(restart, monkeypatch):
    edit_saved(lambda s: s.update(rounds=19))
    original = db.save_active_job

    def crash(*args):
        original(*args)
        if json.loads(args[2])["recovery"][IP]["ap_recovery"]["state"] == "locating":
            raise Crash()

    monkeypatch.setattr(db, "save_active_job", crash)
    with pytest.raises(Crash):
        await app._recover_crashed_device_jobs()
    monkeypatch.setattr(db, "save_active_job", original)
    row = await recover(restart)
    assert "round limit" in row["error"]
    assert restart[3]["events"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["write_pending", "locating", "verified", "committed"])
async def test_crash_before_or_after_relocation_keeps_inventory_at_saved_address(restart, monkeypatch, state):
    job, client, _, clock = restart
    edit_saved(lambda s: s.update(state=state))
    if state == "committed":
        # Model the durable transaction before the post-move checkpoint.
        assert clock["relocate"](job.recovery_locators[IP]["ap_recovery"]["device_id"],
                                 "ap-serial", IP, NEW)
        # A fresh startup must use the transaction's rewritten recovery keys.
        await app._recover_crashed_device_jobs()
        history = db.get_device_update_history_by_job(job.job_id)[0]
        assert history["stages"][0]["evidence"]["observed_ip"] == NEW
        assert db.get_device(IP) is None
        assert db.get_device(NEW)["uid"] == "ap-serial"
        assert db.get_device_update_history_by_job(job.job_id)[0]["status"] == "failed"
        assert client.events == []
    else:
        row = await recover(restart)
        assert f"at {NEW}" in row["error"]


@pytest.mark.asyncio
async def test_candidate_checkpoint_survives_crash_at_address_cap(restart, monkeypatch):
    addresses = [IP, NEW] + [f"192.0.2.{i}" for i in range(40, 70)]
    edit_saved(lambda s: s.update(addresses=addresses[:-1]))
    restart[3]["reports"] = [addresses[-1]]
    original = db.save_active_job

    def crash(*args):
        original(*args)
        saved = json.loads(args[2])["recovery"][IP]["ap_recovery"]
        if len(saved["addresses"]) == 32:
            raise Crash()

    monkeypatch.setattr(db, "save_active_job", crash)
    with pytest.raises(Crash):
        await app._recover_crashed_device_jobs()
    assert not any(ip != CPE for ip, _, _ in restart[3]["events"])
    monkeypatch.setattr(db, "save_active_job", original)
    restart[3]["reports"] = ["192.0.2.200"]
    restart[3]["events"].clear()
    row = await recover(restart)
    assert "address limit" in row["error"]
    assert not any(ip != CPE for ip, _, _ in restart[3]["events"])


@pytest.mark.asyncio
async def test_expired_utc_deadline_never_probes(restart):
    edit_saved(lambda s: s.update(deadline_utc="2000-01-01T00:00:00+00:00"))
    await recover(restart)
    assert restart[3]["events"] == []


@pytest.mark.asyncio
async def test_shorter_saved_budget_does_not_advance_next_probe(restart):
    edit_saved(lambda s: s.update(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()))
    await recover(restart)
    assert restart[3]["events"] == []
    assert restart[3]["now"] == pytest.approx(5010, abs=0.1)
