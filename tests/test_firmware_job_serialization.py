"""Firmware job admission and manager worker-boundary tests."""

import asyncio
import json
import threading
import types
from datetime import datetime

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock

from updater import app as app_module


def test_update_job_defaults_to_one_bank():
    assert app_module.UpdateJob(job_id="job-default-bank").bank_mode == "one"


@pytest.fixture(autouse=True)
def reset_firmware_job_owner(monkeypatch):
    monkeypatch.setattr(app_module, "_firmware_job_owner_guard", threading.Lock())
    monkeypatch.setattr(app_module, "_firmware_job_owner", None)


@pytest.mark.asyncio
async def test_manual_admission_is_denied_while_scheduled_job_owns_write_sequence():
    app_module._firmware_job_owner = "scheduled-job"

    with pytest.raises(HTTPException, match="Another firmware job is active") as error:
        await app_module.start_update(
            firmware_file="unused.bin", device_type="mixed", ip_list="10.0.0.1",
            concurrency=2, bank_mode="one",
        )

    assert error.value.status_code == 409
    assert app_module._firmware_job_owner == "scheduled-job"
    app_module._release_firmware_job_owner("scheduled-job")


@pytest.mark.asyncio
async def test_scheduled_admission_is_denied_while_manual_job_owns_write_sequence():
    app_module._firmware_job_owner = "manual-job"

    with pytest.raises(RuntimeError, match="Another firmware job is active"):
        await app_module._start_scheduled_update(
            ap_ips=[], firmware_file="unused.bin", bank_mode="one",
        )

    assert app_module._firmware_job_owner == "manual-job"


@pytest.mark.asyncio
async def test_simultaneous_admissions_leave_one_owner():
    async def attempt_admission():
        try:
            owner = app_module._claim_firmware_job_owner()
            await asyncio.sleep(0)
            return owner
        except HTTPException as exc:
            return exc

    first, second = await asyncio.gather(attempt_admission(), attempt_admission())
    admitted = [result for result in (first, second) if isinstance(result, str)]
    denied = [result for result in (first, second) if isinstance(result, HTTPException)]

    assert len(admitted) == 1
    assert len(denied) == 1
    assert denied[0].status_code == 409
    assert app_module._firmware_job_owner == admitted[0]
    app_module._release_firmware_job_owner(admitted[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("device_status", ["success", "failed"])
async def test_owner_is_released_after_job_completion_or_failure(
    mock_db, monkeypatch, device_status,
):
    job_id = f"job-{device_status}"
    app_module._firmware_job_owner = job_id
    job = app_module.UpdateJob(
        job_id=job_id,
        started_at=datetime.now(),
        devices={"10.0.0.1": app_module.DeviceStatus(ip="10.0.0.1", status=device_status)},
    )
    monkeypatch.setattr(app_module, "_scheduled_job_guard", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(app_module.slack, "notify_job_completed", AsyncMock())
    monkeypatch.setattr(app_module.snmp, "notify_job_completed", AsyncMock())
    monkeypatch.setattr(app_module.webhooks, "notify_job_completed", AsyncMock())
    monkeypatch.setattr(app_module.email_notifier, "notify_job_completed", AsyncMock())

    await app_module.run_update_job(job, concurrency=1)

    assert job.status == "completed"
    assert app_module._firmware_job_owner is None


@pytest.mark.asyncio
async def test_exact_device_order_runs_one_result_at_a_time(mock_db, monkeypatch):
    events = []
    active = 0
    max_active = 0
    job = app_module.UpdateJob(
        job_id="exact-order", started_at=datetime.now(),
        device_order=["cpe", "ap", "switch"],
        devices={
            ip: app_module.DeviceStatus(ip=ip, role=role)
            for ip, role in (("cpe", "cpe"), ("ap", "ap"), ("switch", "switch"))
        },
    )
    app_module._firmware_job_owner = job.job_id

    async def update_one(job, ip, pass_number=1):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        events.append(("update", ip))
        await asyncio.sleep(0)
        job.devices[ip].status = "success"
        active -= 1

    monkeypatch.setattr(app_module, "_update_single_device", update_one)
    monkeypatch.setattr(
        app_module, "_persist_device_result",
        lambda job, ip: events.append(("persist", ip)),
    )
    monkeypatch.setattr(app_module, "_scheduled_job_guard", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())
    monkeypatch.setattr(app_module.services, "get_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(app_module.slack, "notify_job_completed", AsyncMock())
    monkeypatch.setattr(app_module.snmp, "notify_job_completed", AsyncMock())
    monkeypatch.setattr(app_module.webhooks, "notify_job_completed", AsyncMock())
    monkeypatch.setattr(app_module.email_notifier, "notify_job_completed", AsyncMock())

    await app_module.run_update_job(job, concurrency=8)

    assert events == [
        ("update", "cpe"), ("persist", "cpe"),
        ("update", "ap"), ("persist", "ap"),
        ("update", "switch"), ("persist", "switch"),
    ]
    assert max_active == 1


def test_exact_order_persists_result_before_clearing_in_flight_device(mock_db):
    job = app_module.UpdateJob(
        job_id="durable-order", device_order=["192.0.2.10"],
        device_firmware_map={"192.0.2.10": "/firmware/update.bin"},
        devices={"192.0.2.10": app_module.DeviceStatus(ip="192.0.2.10", role="ap")},
        recovery_locators={"192.0.2.10": {"serial": "EXPECTED"}},
    )

    app_module._persist_recovery_locator(job, "192.0.2.10")
    active = app_module.db.get_active_jobs()[0]
    assert active["in_flight_device_ip"] == "192.0.2.10"

    app_module._persist_device_result(job, "192.0.2.10")
    active = app_module.db.get_active_jobs()[0]
    payload = json.loads(active["device_ips_json"])
    assert active["in_flight_device_ip"] is None
    assert payload["order"] == ["192.0.2.10"]
    assert payload["recovery"]["192.0.2.10"]["serial"] == "EXPECTED"


@pytest.mark.asyncio
async def test_exact_scheduled_entries_do_not_expand_ap_siblings(
    mock_db, monkeypatch, tmp_path,
):
    firmware = "tna-30x-1.2.3-r123.bin"
    (tmp_path / firmware).write_bytes(b"firmware")
    parent_ip, cpe_ip = "192.0.2.10", "192.0.2.20"
    parent = {"ip": parent_ip, "username": "user", "password": "pass"}
    cpe = {"ip": cpe_ip, "ap_ip": parent_ip, "auth_status": "ok", "model": "TNA-301"}
    monkeypatch.setattr(app_module, "FIRMWARE_DIR", tmp_path)
    monkeypatch.setattr(app_module.db, "get_cpe_by_ip", lambda ip: cpe if ip == cpe_ip else None)
    monkeypatch.setattr(app_module.db, "get_access_point", lambda ip: parent if ip == parent_ip else None)
    monkeypatch.setattr(
        app_module.db, "get_cpes_for_ap",
        lambda ip: pytest.fail("exact entries must not enumerate AP siblings"),
    )
    monkeypatch.setattr(app_module, "_select_firmware_for_model", lambda model, files: files["tna-30x"])
    monkeypatch.setattr(app_module, "_extract_version_from_filename", lambda name: "1.2.3.123")
    monkeypatch.setattr(app_module, "_device_needs_update_for_bank_mode", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module, "_scheduled_action_decision", lambda job: (True, None))
    monkeypatch.setattr(app_module, "_spawn_update_job", lambda job, concurrency: None)
    monkeypatch.setattr(app_module, "broadcast", AsyncMock())

    job_id = await app_module._start_scheduled_update(
        ap_ips=[], firmware_file=firmware,
        device_entries=[{"ip": cpe_ip, "role": "cpe", "parent_ap": parent_ip}],
    )

    job = app_module.update_jobs.pop(job_id)
    assert list(job.devices) == [cpe_ip]
    assert job.device_order == [cpe_ip]
    assert job.bank_mode == "one"


@pytest.mark.asyncio
async def test_unavailable_owner_state_denies_admission(monkeypatch):
    monkeypatch.setattr(app_module, "_firmware_job_owner_guard", None)

    with pytest.raises(HTTPException, match="ownership is unavailable") as error:
        await app_module.start_update(
            firmware_file="unused.bin", device_type="mixed", ip_list="10.0.0.1",
            concurrency=2, bank_mode="one",
        )

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_existing_window_stop_gate_still_denies_and_releases_reservation(mock_db, monkeypatch):
    monkeypatch.setattr(
        app_module, "_check_firmware_window_admission",
        lambda window_key: (_ for _ in ()).throw(HTTPException(409, "window stopped")),
    )

    with pytest.raises(HTTPException, match="window stopped"):
        await app_module.start_update(
            firmware_file="unused.bin", device_type="mixed", ip_list="10.0.0.1",
            concurrency=2, bank_mode="one",
        )

    assert app_module._firmware_job_owner is None


def test_manager_starts_with_at_most_one_uvicorn_worker(monkeypatch):
    calls = []
    monkeypatch.setitem(
        __import__("sys").modules,
        "uvicorn",
        types.SimpleNamespace(run=lambda *args, **kwargs: calls.append(kwargs)),
    )

    app_module.main()

    assert len(calls) == 1
    assert calls[0].get("workers", 1) in (None, 1)
