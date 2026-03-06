"""Tests for updater.scheduler."""

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updater import app as app_module
from updater import database as db
from updater.scheduler import AutoUpdateScheduler


def _seed_rollout_devices():
    db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_access_point("10.0.0.11", "root", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_switch("10.0.1.5", "admin", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_switch("10.0.1.6", "admin", "pass", enabled=True, firmware_version="1.0.0")


class TestSchedulerCompletionHandling:
    @pytest.mark.asyncio
    async def test_cancelled_no_progress_does_not_advance_phase(self):
        scheduler = AutoUpdateScheduler(broadcast_func=None, start_update_func=None)
        scheduler._current_job_id = "job123"

        rollout = {
            "id": 7,
            "phase": "canary",
            "status": "active",
            "last_job_id": "job123",
        }

        with patch("updater.scheduler.db.get_active_rollout", return_value=rollout), \
             patch("updater.scheduler.db.mark_rollout_device") as mark_rollout_device, \
             patch("updater.scheduler.db.complete_rollout_phase") as complete_rollout_phase, \
             patch("updater.scheduler.db.pause_rollout") as pause_rollout, \
             patch("updater.scheduler.db.log_schedule_event") as log_schedule_event:
            scheduler.on_job_completed(
                job_id="job123",
                success_count=0,
                failed_count=0,
                device_statuses={"10.0.0.1": "skipped"},
                cancel_reason="Outside maintenance window",
            )

            await asyncio.sleep(0)

        mark_rollout_device.assert_called_once_with(7, "10.0.0.1", "skipped")
        complete_rollout_phase.assert_not_called()
        pause_rollout.assert_not_called()
        log_schedule_event.assert_any_call(
            "job_deferred",
            "Outside maintenance window",
            job_id="job123",
        )


class TestSchedulerBankModeFiltering:
    def test_device_already_on_target_is_skipped(self):
        scheduler = AutoUpdateScheduler(broadcast_func=None, start_update_func=None)
        ap = {
            "ip": "10.0.0.1",
            "model": "TNA-301",
            "firmware_version": "1.12.2.54970",
            "bank1_version": "1.12.2.54970",
            "bank2_version": "1.12.1.54000",
            "active_bank": 1,
        }

        with patch("updater.scheduler.db.get_access_point", return_value=ap), \
             patch("updater.scheduler.db.get_cpes_for_ap", return_value=[]):
            result = scheduler._filter_devices_needing_update(
                ["10.0.0.1"],
                {"tna-30x": "1.12.2.54970"},
                allow_downgrade=False,
            )

        assert result == []

    def test_device_behind_target_is_included(self):
        scheduler = AutoUpdateScheduler(broadcast_func=None, start_update_func=None)
        ap = {
            "ip": "10.0.0.1",
            "model": "TNA-301",
            "firmware_version": "1.12.1.54000",
            "bank1_version": "1.12.1.54000",
            "bank2_version": "1.12.1.54000",
            "active_bank": 1,
        }

        with patch("updater.scheduler.db.get_access_point", return_value=ap), \
             patch("updater.scheduler.db.get_cpes_for_ap", return_value=[]):
            result = scheduler._filter_devices_needing_update(
                ["10.0.0.1"],
                {"tna-30x": "1.12.2.54970"},
                allow_downgrade=False,
            )

        assert result == ["10.0.0.1"]


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

    def test_firmware_changed_detects_303l_change(self, mock_db):
        """Changing 303L firmware should be detected as a firmware change."""
        rollout_id = db.create_rollout("tna-30x-1.0.0-r100.bin", "tna-303l-1.0.0-r100.bin", None)
        rollout = db.get_rollout(rollout_id)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())

        # Same firmware - no change
        assert not scheduler._firmware_changed(
            rollout, "tna-30x-1.0.0-r100.bin", "tna-303l-1.0.0-r100.bin", ""
        )

        # Different 303L firmware - should detect change
        assert scheduler._firmware_changed(
            rollout, "tna-30x-1.0.0-r100.bin", "tna-303l-2.0.0-r200.bin", ""
        )

    def test_firmware_changed_detects_tns100_change(self, mock_db):
        """Changing TNS-100 firmware should be detected as a firmware change."""
        rollout_id = db.create_rollout("tna-30x-1.0.0-r100.bin", None, "tns-100-1.0.0-r100.bin")
        rollout = db.get_rollout(rollout_id)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())

        # Same firmware - no change
        assert not scheduler._firmware_changed(
            rollout, "tna-30x-1.0.0-r100.bin", "", "tns-100-1.0.0-r100.bin"
        )

        # Different TNS-100 firmware - should detect change
        assert scheduler._firmware_changed(
            rollout, "tna-30x-1.0.0-r100.bin", "", "tns-100-2.0.0-r200.bin"
        )

    def test_firmware_changed_detects_added_or_removed_firmware(self, mock_db):
        """Adding or removing a firmware type should be detected as a change."""
        rollout_id = db.create_rollout("tna-30x-1.0.0-r100.bin", None, None)
        rollout = db.get_rollout(rollout_id)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())

        # Adding 303L where none existed
        assert scheduler._firmware_changed(
            rollout, "tna-30x-1.0.0-r100.bin", "tna-303l-2.0.0-r200.bin", ""
        )

    @pytest.mark.asyncio
    async def test_canary_cancels_rollout_on_303l_change(self, mock_db, monkeypatch):
        """Changing 303L firmware should cancel active rollout during canary trigger."""
        _seed_rollout_devices()
        # Create a rollout with old 303L firmware
        rollout_id = db.create_rollout(
            "tna-30x-1.0.0-r100.bin", "tna-303l-1.0.0-r100.bin", None
        )

        db.set_settings({
            "schedule_enabled": "true",
            "timezone": "America/Chicago",
            "selected_firmware_30x": "tna-30x-1.0.0-r100.bin",
            "selected_firmware_303l": "tna-303l-2.0.0-r200.bin",  # Changed!
            "weather_check_enabled": "false",
        })

        start_update = AsyncMock(return_value="job-cancel-test")
        scheduler = AutoUpdateScheduler(AsyncMock(), start_update)

        monkeypatch.setattr(
            "updater.scheduler.services.validate_time_sources",
            AsyncMock(return_value=(True, datetime(2026, 3, 5, 13, 0, 0))),
        )

        await scheduler.trigger_canary_now()

        # Old rollout should be cancelled
        old_rollout = db.get_rollout(rollout_id)
        assert old_rollout["status"] == "cancelled"

        # New rollout should be created with updated 303L firmware
        new_rollout = db.get_active_rollout()
        assert new_rollout is not None
        assert new_rollout["firmware_file_303l"] == "tna-303l-2.0.0-r200.bin"

    def test_get_last_rollout_for_firmware_set_matches_all_columns(self, mock_db):
        """get_last_rollout_for_firmware_set should match on all three firmware files."""
        db.create_rollout("30x-v1.bin", "303l-v1.bin", "tns-v1.bin")

        # Exact match
        result = db.get_last_rollout_for_firmware_set("30x-v1.bin", "303l-v1.bin", "tns-v1.bin")
        assert result is not None

        # Different 303L - no match
        result = db.get_last_rollout_for_firmware_set("30x-v1.bin", "303l-v2.bin", "tns-v1.bin")
        assert result is None

        # Different TNS-100 - no match
        result = db.get_last_rollout_for_firmware_set("30x-v1.bin", "303l-v1.bin", "tns-v2.bin")
        assert result is None

    def test_get_last_rollout_for_firmware_set_handles_nulls(self, mock_db):
        """NULL and empty string should be treated equivalently."""
        db.create_rollout("30x-v1.bin", None, None)

        # Empty strings should match NULLs
        result = db.get_last_rollout_for_firmware_set("30x-v1.bin", "", "")
        assert result is not None

        result = db.get_last_rollout_for_firmware_set("30x-v1.bin", None, None)
        assert result is not None

    @pytest.mark.asyncio
    async def test_manual_canary_job_disables_window_cutoff(self, mock_db, tmp_path):
        firmware_dir = tmp_path / "firmware"
        firmware_dir.mkdir()
        (firmware_dir / "firmware.bin").write_bytes(b"test")

        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")

        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()

        def _discard_task(coro):
            coro.close()
            return mock_task

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
