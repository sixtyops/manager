"""Firmware job admission and manager worker-boundary tests."""

import asyncio
import threading
import types
from datetime import datetime

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock

from updater import app as app_module


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
            concurrency=2,
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
async def test_unavailable_owner_state_denies_admission(monkeypatch):
    monkeypatch.setattr(app_module, "_firmware_job_owner_guard", None)

    with pytest.raises(HTTPException, match="ownership is unavailable") as error:
        await app_module.start_update(
            firmware_file="unused.bin", device_type="mixed", ip_list="10.0.0.1",
            concurrency=2,
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
            concurrency=2,
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
