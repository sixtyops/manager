from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from updater import app as app_module
from updater import database as db
from updater.scheduler import AutoUpdateScheduler


def _seed_rollout_devices():
    db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_access_point("10.0.0.11", "root", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_switch("10.0.1.5", "admin", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_switch("10.0.1.6", "admin", "pass", enabled=True, firmware_version="1.0.0")


class TestSchedulerCanaries:
    def test_phase_selection_prefers_configured_canaries(self, mock_db):
        _seed_rollout_devices()
        rollout_id = db.create_rollout("firmware.bin")
        rollout = db.get_rollout(rollout_id)
        settings = {
            "rollout_canary_aps": "10.0.0.11",
            "rollout_canary_switches": "10.0.1.6",
        }

        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())

        ap_batch = scheduler._get_devices_for_phase(
            rollout,
            ["10.0.0.10", "10.0.0.11"],
            False,
            settings,
        )
        switch_batch = scheduler._get_switches_for_phase(
            rollout,
            ["10.0.1.5", "10.0.1.6"],
            False,
            settings,
        )

        assert ap_batch == ["10.0.0.11"]
        assert switch_batch == ["10.0.1.6"]

    @pytest.mark.asyncio
    async def test_manual_canary_trigger_uses_preferred_devices_without_marking_ran_today(self, mock_db, monkeypatch):
        _seed_rollout_devices()
        db.set_settings({
            "schedule_enabled": "true",
            "timezone": "America/Chicago",
            "selected_firmware_30x": "firmware.bin",
            "selected_firmware_tns100": "switch.bin",
            "rollout_canary_aps": "10.0.0.11",
            "rollout_canary_switches": "10.0.1.6",
            "weather_check_enabled": "false",
        })

        start_update = AsyncMock(return_value="job-1234")
        scheduler = AutoUpdateScheduler(AsyncMock(), start_update)

        monkeypatch.setattr("updater.scheduler.services.validate_time_sources", AsyncMock(return_value=(True, datetime(2026, 3, 5, 13, 0, 0))))

        await scheduler.trigger_canary_now()

        start_update.assert_awaited_once()
        kwargs = start_update.await_args.kwargs
        assert kwargs["ap_ips"] == ["10.0.0.11"]
        assert kwargs["switch_ips"] == ["10.0.1.6"]
        assert scheduler._ran_today == set()

        rollout = db.get_active_rollout()
        devices = db.get_rollout_devices(rollout["id"])
        assigned = {(row["ip"], row["device_type"]) for row in devices}
        assert assigned == {("10.0.0.11", "ap"), ("10.0.1.6", "switch")}

    @pytest.mark.asyncio
    async def test_manual_canary_completion_waits_for_maintenance_window(self, mock_db):
        _seed_rollout_devices()
        rollout_id = db.create_rollout("firmware.bin")
        db.assign_device_to_rollout(rollout_id, "10.0.0.10", "ap", "canary")
        db.assign_device_to_rollout(rollout_id, "10.0.1.5", "switch", "canary")
        db.set_rollout_job_id(rollout_id, "job-1234")

        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
        scheduler._current_job_id = "job-1234"
        scheduler._manual_canary_job_ids.add("job-1234")

        scheduler.on_job_completed("job-1234", 2, 0, learned_versions={"tna-30x": "1.2.3"})

        assert scheduler._state == "idle"
        assert scheduler._block_reason == "Canary complete; next phase waits for the maintenance window"

    def test_ap_candidate_includes_current_ap_with_behind_cpe(self, mock_db):
        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, model="TNA-301", firmware_version="1.2.3.123")
        db.upsert_cpe("10.0.0.10", {
            "ip": "10.0.0.20",
            "model": "TNA-303L",
            "firmware_version": "1.0.0.1",
            "auth_status": "ok",
        })

        rollout_id = db.create_rollout("tna-30x-1.2.3-r123.bin", "tna-303l-2.5.0-r456.bin")
        rollout = db.get_rollout(rollout_id)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())

        batch = scheduler._get_devices_for_phase(
            rollout,
            ["10.0.0.10"],
            False,
            {
                "selected_firmware_30x": "tna-30x-1.2.3-r123.bin",
                "selected_firmware_303l": "tna-303l-2.5.0-r456.bin",
                "selected_firmware_tns100": "",
                "rollout_canary_aps": "10.0.0.10",
            },
        )

        assert batch == ["10.0.0.10"]

    def test_switch_scope_follows_selected_sites(self, mock_db):
        site_a = db.create_tower_site("Site A")
        site_b = db.create_tower_site("Site B")
        db.upsert_access_point("10.0.0.10", "root", "pass", tower_site_id=site_a, enabled=True)
        db.upsert_switch("10.0.1.5", "admin", "pass", tower_site_id=site_a, enabled=True)
        db.upsert_switch("10.0.1.6", "admin", "pass", tower_site_id=site_b, enabled=True)

        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
        scope = scheduler._resolve_switch_scope({
            "schedule_scope": "sites",
            "schedule_scope_data": str(site_a),
        })

        assert scope == ["10.0.1.5"]

    def test_switch_phase_selection_uses_switch_family_target(self, mock_db):
        db.upsert_switch("10.0.1.5", "admin", "pass", enabled=True, model="TNS-100", firmware_version="3.4.5.678")
        rollout_id = db.create_rollout("tna-30x-1.2.3-r123.bin", None, "tns-100-3.4.5-r678.bin")
        rollout = db.get_rollout(rollout_id)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())

        batch = scheduler._get_switches_for_phase(
            rollout,
            ["10.0.1.5"],
            False,
            {
                "selected_firmware_30x": "tna-30x-1.2.3-r123.bin",
                "selected_firmware_tns100": "tns-100-3.4.5-r678.bin",
            },
        )

        assert batch == []

    def test_on_job_completed_persists_learned_versions_per_family(self, mock_db):
        _seed_rollout_devices()
        rollout_id = db.create_rollout("firmware.bin", "303l.bin", "switch.bin")
        db.assign_device_to_rollout(rollout_id, "10.0.0.10", "ap", "canary")
        db.set_rollout_job_id(rollout_id, "job-1234")

        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
        scheduler._current_job_id = "job-1234"

        def _discard_task(coro):
            coro.close()
            class _DummyTask:
                def add_done_callback(self, _cb):
                    return None
            return _DummyTask()

        with patch("updater.scheduler.asyncio.create_task", _discard_task):
            scheduler.on_job_completed(
                "job-1234",
                1,
                0,
                learned_versions={
                    "tna-30x": "1.2.3.123",
                    "tna-303l": "2.5.0.456",
                    "tns-100": "3.4.5.678",
                },
                device_statuses={"10.0.0.10": "success"},
            )

        rollout = db.get_rollout(rollout_id)
        assert rollout["target_version"] == "1.2.3.123"
        assert rollout["target_version_303l"] == "2.5.0.456"
        assert rollout["target_version_tns100"] == "3.4.5.678"

    @pytest.mark.asyncio
    async def test_manual_canary_job_disables_window_cutoff(self, mock_db, tmp_path):
        firmware_dir = tmp_path / "firmware"
        firmware_dir.mkdir()
        (firmware_dir / "firmware.bin").write_bytes(b"test")

        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")

        def _discard_task(coro):
            coro.close()
            return None

        with patch.object(app_module, "FIRMWARE_DIR", firmware_dir), \
             patch.object(app_module, "broadcast", AsyncMock()), \
             patch.object(app_module.asyncio, "create_task", _discard_task):
            job_id = await app_module._start_scheduled_update(
                ap_ips=["10.0.0.10"],
                firmware_file="firmware.bin",
                schedule_timezone="America/Chicago",
                enforce_window_cutoff=False,
            )

        assert app_module.update_jobs[job_id].enforce_window_cutoff is False
        app_module.update_jobs.pop(job_id, None)
