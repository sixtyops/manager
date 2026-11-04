"""API and monitor contracts for serial firmware queue state."""

from pathlib import Path
from unittest.mock import MagicMock

from updater import app as app_module


def test_scheduler_status_exposes_queue_state_without_wave_fields(authed_client, monkeypatch):
    job_id = "queue-status-job"
    job = app_module.UpdateJob(
        job_id=job_id,
        device_order=["192.0.2.10"],
        devices={
            "192.0.2.10": app_module.DeviceStatus(
                ip="192.0.2.10", role="cpe", status="uploading",
            ),
        },
    )
    monkeypatch.setitem(app_module.update_jobs, job_id, job)
    scheduler = MagicMock()
    scheduler.get_status.return_value = {
        "state": "running",
        "current_job_id": job_id,
        "next_window": "03:00-04:00 on tue",
        "next_window_iso": "2026-11-04T03:00:00-06:00",
        "rollout": {
            "status": "active",
            "phase": "pct10",
            "progress": {"updated": 1, "total": 4},
            "predictions": {"estimated_completion_date": "2026-11-20"},
            "remaining_devices": 3,
            "pause_reason": None,
            "cutoff_utc": "2026-11-04T09:30:00+00:00",
            "ramp_ordinal": 1,
        },
    }
    monkeypatch.setattr(app_module, "get_scheduler", lambda: scheduler)

    response = authed_client.get("/api/scheduler/status")

    assert response.status_code == 200
    queue = response.json()["rollout"]
    assert queue == {
        "status": "active",
        "remaining_devices": 3,
        "current_device": {"ip": "192.0.2.10", "role": "cpe", "status": "uploading"},
        "pause_reason": None,
        "cutoff_utc": "2026-11-04T09:30:00+00:00",
        "ramp_ordinal": 1,
        "next_window": "2026-11-04T03:00:00-06:00",
    }
    assert "phase" not in queue
    assert "predictions" not in queue
    assert "progress" not in queue


def test_monitor_uses_serial_queue_labels_without_wave_controls():
    template = Path("updater/templates/monitor.html").read_text(encoding="utf-8")

    assert 'id="barQueueRemaining"' in template
    assert 'id="barQueueCurrent"' in template
    assert 'id="barQueueCutoff"' in template
    assert 'id="barQueueNextWindow"' in template
    assert 'id="barQueueReason"' in template
    assert "renderQueueStatus(status.rollout)" in template
    assert "barPhaseIndicator" not in template
    assert "barResumeBtn" not in template
    assert "barCancelBtn" not in template
    assert "estimated_completion_date" not in template


def test_resolved_queue_failure_moves_first_in_later_window(mock_db):
    from updater import database as db

    failed_window = ("firmware", "2026-11-02")
    failed_ip = "192.0.2.20"
    stop = db.stop_firmware_window(failed_window, "failed-job", failed_ip, "flash", "Upload failed")
    db.resolve_firmware_window_stop(stop["id"], "admin", "Device recovery verified")
    entries = [
        {"ip": "192.0.2.10", "role": "ap"},
        {"ip": failed_ip, "role": "ap"},
    ]

    ordered = app_module._retry_resolved_failure_first(entries, ("firmware", "2026-11-03"))

    assert [entry["ip"] for entry in ordered] == [failed_ip, "192.0.2.10"]
    assert [entry["ip"] for entry in entries] == ["192.0.2.10", failed_ip]


async def _start_exact_ap_job(monkeypatch, tmp_path, parent_ip):
    firmware = "tna-30x-1.2.3-r123.bin"
    (tmp_path / firmware).write_bytes(b"firmware")
    ap = {
        "ip": parent_ip,
        "username": "user",
        "password": "pass",
        "model": "TNA-301",
        "firmware_version": "1.0.0",
    }
    monkeypatch.setattr(app_module, "FIRMWARE_DIR", tmp_path)
    monkeypatch.setattr(app_module.db, "get_access_point", lambda ip: ap if ip == parent_ip else None)
    monkeypatch.setattr(
        app_module.db, "get_cpes_for_ap",
        lambda ip: (_ for _ in ()).throw(AssertionError("AP entry must not expand to CPE siblings")),
    )
    monkeypatch.setattr(app_module, "_select_firmware_for_model", lambda model, files: files["tna-30x"])
    monkeypatch.setattr(app_module, "_extract_version_from_filename", lambda name: "1.2.3.123")
    monkeypatch.setattr(app_module, "_device_needs_update", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module, "_scheduled_action_decision", lambda job: (True, None))
    monkeypatch.setattr(app_module, "_spawn_update_job", lambda job, concurrency: None)
    return await app_module._start_scheduled_update(
        ap_ips=[], firmware_file=firmware,
        device_entries=[{"ip": parent_ip, "role": "ap"}],
    )


def test_exact_scheduled_ap_entry_does_not_expand_cpe_siblings(mock_db, monkeypatch, tmp_path):
    import asyncio

    job_id = asyncio.run(_start_exact_ap_job(monkeypatch, tmp_path, "192.0.2.10"))

    job = app_module.update_jobs.pop(job_id)
    assert job.device_order == ["192.0.2.10"]
    assert list(job.devices) == ["192.0.2.10"]


def test_scheduled_runner_rejects_legacy_ap_batch_input(mock_db, monkeypatch, tmp_path):
    import asyncio
    import pytest

    firmware = "tna-30x-1.2.3-r123.bin"
    (tmp_path / firmware).write_bytes(b"firmware")
    monkeypatch.setattr(app_module, "FIRMWARE_DIR", tmp_path)

    with pytest.raises(RuntimeError, match="device_entries"):
        asyncio.run(app_module._start_scheduled_update(
            ap_ips=["192.0.2.10"], firmware_file=firmware,
        ))
