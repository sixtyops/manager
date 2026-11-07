"""Tests for updater.scheduler."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from updater import app as app_module
from updater import database as db
from updater import scheduler as scheduler_module
from updater.scheduler import (
    AutoUpdateScheduler,
    _cpe_auth_block_reason,
    _missing_serial_recovery_reason,
)


def _seed_rollout_devices():
    db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_access_point("10.0.0.11", "root", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_switch("10.0.1.5", "admin", "pass", enabled=True, firmware_version="1.0.0")
    db.upsert_switch("10.0.1.6", "admin", "pass", enabled=True, firmware_version="1.0.0")


class TestDerivedPendingQueue:
    @staticmethod
    def _settings():
        return {
            "schedule_scope": "all",
            "selected_firmware_30x": "tna-30x-2.0.0-r200.bin",
            "selected_firmware_303l": "tna-303l-2.0.0-r200.bin",
            "selected_firmware_tns100": "tns-100-2.0.0-r200.bin",
            "firmware_update_cooldown_days": "0",
        }

    def test_orders_cpes_then_aps_then_switches_by_normalized_ip(self, mock_db):
        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, model="TNA-301", firmware_version="1.0.0")
        db.upsert_access_point("10.0.0.2", "root", "pass", enabled=True, model="TNA-302", firmware_version="1.0.0")
        db.upsert_cpe("10.0.0.10", {"ip": "10.0.0.30", "model": "TNA-303L", "firmware_version": "1.0.0", "auth_status": "ok"})
        db.upsert_cpe("10.0.0.2", {"ip": "10.0.0.20", "model": "TNA-303L", "firmware_version": "1.0.0", "auth_status": "ok"})
        db.upsert_switch("10.0.1.2", "admin", "pass", enabled=True, model="TNS-100", firmware_version="1.0.0")
        db.upsert_switch("10.0.1.10", "admin", "pass", enabled=True, model="TNS-100", firmware_version="1.0.0")

        queue = AutoUpdateScheduler(AsyncMock(), AsyncMock())._derive_pending_queue(
            self._settings(), held_families=set()
        )

        assert [(item["device_type"], item["ip"]) for item in queue] == [
            ("cpe", "10.0.0.20"), ("cpe", "10.0.0.30"),
            ("ap", "10.0.0.2"), ("ap", "10.0.0.10"),
            ("switch", "10.0.1.2"), ("switch", "10.0.1.10"),
        ]

    def test_excludes_current_and_includes_new_returning_and_offline_inventory(self, mock_db):
        db.upsert_access_point("10.0.0.1", "root", "pass", enabled=True, model="TNA-301", firmware_version="2.0.0.200")
        db.upsert_access_point("10.0.0.2", "root", "pass", enabled=True, model="TNA-301", firmware_version="1.0.0")
        db.upsert_access_point("10.0.0.3", "root", "pass", enabled=True, model="TNA-301", firmware_version="1.0.0")
        db.update_ap_status("10.0.0.2", last_error="offline")
        db.update_ap_status("10.0.0.3", last_error=None)

        # Neither newly added nor returning inventory needs a saved rollout member.
        queue = AutoUpdateScheduler(AsyncMock(), AsyncMock())._derive_pending_queue(
            self._settings(), rollout={"id": -1}, held_families=set()
        )

        assert [item["ip"] for item in queue] == ["10.0.0.2", "10.0.0.3"]

    def test_resolved_failure_on_target_is_included_first_for_recovery(self, mock_db):
        failed_ip = "10.0.0.1"
        db.upsert_access_point(
            failed_ip, "root", "pass", enabled=True, model="TNA-301",
            firmware_version="2.0.0.200",
        )
        db.upsert_access_point(
            "10.0.0.2", "root", "pass", enabled=True, model="TNA-301",
            firmware_version="1.0.0",
        )

        queue = AutoUpdateScheduler(AsyncMock(), AsyncMock())._derive_pending_queue(
            self._settings(), held_families=set(), recovery_ip=failed_ip,
        )

        assert [item["ip"] for item in queue] == [failed_ip, "10.0.0.2"]
        assert queue[0]["verify_recovery_if_current"] is True
        assert queue[1]["verify_recovery_if_current"] is False

    def test_resolved_failure_without_serial_blocks_read_only_recovery(self, mock_db):
        ip = "10.0.0.1"
        prior = db.stop_firmware_window(
            ("firmware", "2026-10-27"), "old-job", ip, "snapshot",
            "Configuration snapshot failed",
        )
        db.resolve_firmware_window_stop(prior["id"], "admin", "Reviewed failure")

        reason = _missing_serial_recovery_reason(
            ip, {"verify_recovery_if_current": True},
            ("firmware", "2026-10-28"),
        )

        assert reason == (
            "Cannot verify 10.0.0.1: the prior failure has no saved serial, "
            "and the device already reports the target firmware"
        )

    def test_resolved_failure_without_serial_can_use_normal_update(self, mock_db):
        ip = "10.0.0.1"
        prior = db.stop_firmware_window(
            ("firmware", "2026-10-27"), "old-job", ip, "snapshot",
            "Configuration snapshot failed",
        )
        db.resolve_firmware_window_stop(prior["id"], "admin", "Reviewed failure")

        reason = _missing_serial_recovery_reason(
            ip, {"verify_recovery_if_current": False},
            ("firmware", "2026-10-28"),
        )

        assert reason is None

    def test_held_late_family_is_omitted_without_blocking_other_family(self, mock_db):
        db.upsert_access_point("10.0.0.1", "root", "pass", enabled=True, model="TNA-301", firmware_version="1.0.0")
        db.upsert_access_point("10.0.0.2", "root", "pass", enabled=True, model="TNA-303L", firmware_version="1.0.0")

        queue = AutoUpdateScheduler(AsyncMock(), AsyncMock())._derive_pending_queue(
            self._settings(), held_families={"tna-303l"}
        )

        assert [item["ip"] for item in queue] == ["10.0.0.1"]

    def test_default_selector_uses_firmware_hold_helper(self, mock_db, monkeypatch):
        db.upsert_access_point("10.0.0.1", "root", "pass", enabled=True, model="TNA-301", firmware_version="1.0.0")
        db.upsert_access_point("10.0.0.2", "root", "pass", enabled=True, model="TNA-303L", firmware_version="1.0.0")
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
        monkeypatch.setattr(
            scheduler, "_held_families",
            lambda settings, rollout: ({"tna-303l"}, {}),
        )

        queue = scheduler._derive_pending_queue(self._settings())

        assert [item["ip"] for item in queue] == ["10.0.0.1"]

    def test_unknown_model_holds_queue(self, mock_db):
        db.upsert_access_point("10.0.0.1", "root", "pass", enabled=True, model="TNA-305", firmware_version="1.0.0")

        with pytest.raises(ValueError, match="unknown model"):
            AutoUpdateScheduler(AsyncMock(), AsyncMock())._derive_pending_queue(
                self._settings(), held_families=set()
            )

    def test_duplicate_management_address_holds_queue(self, mock_db):
        db.upsert_access_point("10.0.0.1", "root", "pass", enabled=True, model="TNA-301", firmware_version="1.0.0")
        db.upsert_cpe("10.0.0.1", {"ip": "10.0.0.1", "model": "TNA-303L", "firmware_version": "1.0.0", "auth_status": "ok"})

        with pytest.raises(ValueError, match="duplicate management address"):
            AutoUpdateScheduler(AsyncMock(), AsyncMock())._derive_pending_queue(
                self._settings(), held_families=set()
            )

    def test_duplicate_normalized_cpe_address_under_same_parent_holds_queue(self, mock_db):
        parent = "10.0.0.1"
        db.upsert_access_point(parent, "root", "pass", enabled=True, model="TNA-301", firmware_version="1.0.0")
        db.upsert_cpe(parent, {"ip": "2001:db8::1", "model": "TNA-303L", "firmware_version": "1.0.0", "auth_status": "ok"})
        db.upsert_cpe(parent, {"ip": "2001:0db8:0:0:0:0:0:1", "model": "TNA-303L", "firmware_version": "1.0.0", "auth_status": "ok"})

        with pytest.raises(ValueError, match="duplicate management address"):
            AutoUpdateScheduler(AsyncMock(), AsyncMock())._derive_pending_queue(
                self._settings(), held_families=set()
            )


class TestCpeCredentialAdmission:
    @pytest.mark.asyncio
    async def test_concurrent_precheck_and_poll_share_one_fresh_login(self, mock_db):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from updater.poller import NetworkPoller, set_poller

        parent_ip = "10.0.0.10"
        cpe_ip = "10.0.0.20"
        db.upsert_access_point(parent_ip, "root", "current-password", enabled=True)
        db.upsert_cpe(parent_ip, {"ip": cpe_ip, "auth_status": "ok"})
        poller = NetworkPoller()
        login_started = asyncio.Event()
        finish_login = asyncio.Event()
        client = MagicMock()

        async def connect_once():
            login_started.set()
            await finish_login.wait()
            return True

        client.connect = AsyncMock(side_effect=connect_once)
        client.session_valid = AsyncMock(return_value="ok")
        driver = MagicMock(return_value=client)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
        settings = {
            "schedule_enabled": "true",
            "schedule_days": "tue",
            "schedule_start_hour": "3",
            "schedule_end_hour": "4",
            "schedule_timezone": "America/Chicago",
        }
        now = datetime(2026, 6, 23, 3, 0, tzinfo=ZoneInfo("America/Chicago"))

        set_poller(poller)
        try:
            with patch("updater.poller.get_driver", return_value=driver), \
                 patch("updater.poller.radius_config.is_device_auth_enabled", return_value=False):
                precheck = asyncio.create_task(
                    scheduler._precheck_cpe_credentials([parent_ip], settings, now)
                )
                await login_started.wait()
                poll = asyncio.create_task(
                    poller._probe_cpe_auth(cpe_ip, "root", "current-password", settings, now)
                )
                await asyncio.sleep(0)
                finish_login.set()
                assert await precheck == ()
                assert await poll == "ok"
        finally:
            set_poller(None)

        assert client.connect.await_count == 1
        assert client.session_valid.await_count == 1

    @pytest.mark.asyncio
    async def test_successful_login_is_reused_only_for_its_window(self, mock_db):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from updater.poller import NetworkPoller, set_poller

        parent_ip = "10.0.0.10"
        cpe_ip = "10.0.0.20"
        db.upsert_access_point(parent_ip, "root", "current-password", enabled=True)
        db.upsert_cpe(parent_ip, {"ip": cpe_ip, "auth_status": "ok"})
        poller = NetworkPoller()
        client = MagicMock()
        client.connect = AsyncMock(return_value=True)
        client.session_valid = AsyncMock(return_value="ok")
        driver = MagicMock(return_value=client)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
        settings = {
            "schedule_enabled": "true",
            "schedule_days": "tue,wed",
            "schedule_start_hour": "3",
            "schedule_end_hour": "4",
            "schedule_timezone": "America/Chicago",
        }
        first_window = datetime(2026, 6, 23, 3, 0, tzinfo=ZoneInfo("America/Chicago"))
        next_window = datetime(2026, 6, 24, 3, 0, tzinfo=ZoneInfo("America/Chicago"))

        set_poller(poller)
        try:
            with patch("updater.poller.get_driver", return_value=driver), \
                 patch("updater.poller.radius_config.is_device_auth_enabled", return_value=False):
                assert await scheduler._precheck_cpe_credentials(
                    [parent_ip], settings, first_window
                ) == ()
                assert await poller._probe_cpe_auth(
                    cpe_ip, "root", "current-password", settings, first_window
                ) == "ok"
                assert client.connect.await_count == 1
                assert await scheduler._precheck_cpe_credentials(
                    [parent_ip], settings, first_window
                ) == ()
                assert client.connect.await_count == 1

                assert await scheduler._precheck_cpe_credentials(
                    [parent_ip], settings, next_window
                ) == ()
        finally:
            set_poller(None)

        assert client.connect.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("login_result", "expected"), [
        ("Invalid credentials", "auth_failed"),
        (True, "ok"),
    ])
    async def test_fresh_login_result_controls_parent_wave_admission(
        self, mock_db, login_result, expected
    ):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from updater.poller import NetworkPoller, set_poller

        parent_ip = "10.0.0.10"
        cpe_ip = "10.0.0.20"
        db.upsert_access_point(parent_ip, "root", "current-password", enabled=True)
        db.upsert_cpe(parent_ip, {"ip": cpe_ip, "auth_status": "ok"})

        poller = NetworkPoller()
        cached = MagicMock()
        cached.session_valid = AsyncMock(return_value="ok")
        poller._cache_cpe_client(cpe_ip, cached)
        fresh_client = MagicMock()
        fresh_client.connect = AsyncMock(return_value=login_result)
        fresh_driver = MagicMock(return_value=fresh_client)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
        now = datetime(2026, 6, 23, 3, 0, tzinfo=ZoneInfo("America/Chicago"))
        settings = {
            "schedule_enabled": "true",
            "schedule_days": "tue",
            "schedule_start_hour": "3",
            "schedule_end_hour": "4",
            "schedule_timezone": "America/Chicago",
        }
        set_poller(poller)
        try:
            with patch("updater.poller.get_driver", return_value=fresh_driver), \
                 patch("updater.poller.radius_config.is_device_auth_enabled", return_value=False):
                failures = await scheduler._precheck_cpe_credentials(
                    [parent_ip], settings, now
                )
        finally:
            set_poller(None)

        assert cached.session_valid.await_count == 1
        assert fresh_client.connect.await_count == 1
        assert db.get_cpe_by_ip(cpe_ip)["auth_status"] == expected
        assert failures == (() if expected == "ok" else ((parent_ip, cpe_ip, "auth_failed"),))

    @pytest.mark.asyncio
    async def test_unreachable_fresh_login_does_not_mark_period_checked(self, mock_db):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from updater.poller import NetworkPoller, set_poller

        parent_ip = "10.0.0.10"
        cpe_ip = "10.0.0.20"
        db.upsert_access_point(parent_ip, "root", "current-password", enabled=True)
        db.upsert_cpe(parent_ip, {"ip": cpe_ip, "auth_status": "ok"})

        poller = NetworkPoller()
        cached = MagicMock()
        cached.session_valid = AsyncMock(return_value="unreachable")
        poller._cache_cpe_client(cpe_ip, cached)
        scheduler = AutoUpdateScheduler(AsyncMock(), AsyncMock())
        now = datetime(2026, 6, 23, 3, 0, tzinfo=ZoneInfo("America/Chicago"))
        settings = {
            "schedule_enabled": "true",
            "schedule_days": "tue",
            "schedule_start_hour": "3",
            "schedule_end_hour": "4",
            "schedule_timezone": "America/Chicago",
        }

        set_poller(poller)
        try:
            failures = await scheduler._precheck_cpe_credentials([parent_ip], settings, now)
        finally:
            set_poller(None)

        assert failures == ((parent_ip, cpe_ip, "unreachable"),)
        assert db.get_cpe_by_ip(cpe_ip)["auth_status"] == "unreachable"
        assert poller._fresh_cpe_login_due(cpe_ip, settings, now)

    def test_cpe_auth_block_reason_preserves_ipv6_addresses(self):
        assert _cpe_auth_block_reason(
            "2001:db8::10", "2001:db8::20", "unreachable"
        ) == (
            "CPE 2001:db8::20 under AP 2001:db8::10 failed the fresh login check "
            "(unreachable); this wave is held"
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

        with patch("updater.scheduler.db.get_all_access_points_dict", return_value={"10.0.0.1": ap}), \
             patch("updater.scheduler.db.get_all_cpes_grouped", return_value={}):
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

        with patch("updater.scheduler.db.get_all_access_points_dict", return_value={"10.0.0.1": ap}), \
             patch("updater.scheduler.db.get_all_cpes_grouped", return_value={}):
            result = scheduler._filter_devices_needing_update(
                ["10.0.0.1"],
                {"tna-30x": "1.12.2.54970"},
                allow_downgrade=False,
            )

        assert result == ["10.0.0.1"]

class TestSchedulerCanaries:
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
        db.set_setting("scheduled_rollout_safety_ready", "true")
        firmware_dir = tmp_path / "firmware"
        firmware_dir.mkdir()
        fw_name = "tna-30x-1.2.3-r123.bin"
        (firmware_dir / fw_name).write_bytes(b"test")

        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, model="TNA-301", firmware_version="1.0.0")

        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()

        def _discard_task(coro):
            coro.close()
            return mock_task

        with patch.object(app_module, "FIRMWARE_DIR", firmware_dir), \
             patch.object(app_module, "broadcast", AsyncMock()), \
             patch.object(app_module.asyncio, "create_task", _discard_task):
            job_id = await app_module._start_scheduled_update(
                ap_ips=[],
                firmware_file=fw_name,
                schedule_timezone="America/Chicago",
                enforce_window_cutoff=False,
                device_entries=[{"ip": "10.0.0.10", "role": "ap"}],
            )

        assert app_module.update_jobs[job_id].enforce_window_cutoff is False
        app_module.update_jobs.pop(job_id, None)


class TestSchedulerStartFailure:
    @pytest.mark.asyncio
    async def test_failed_queue_admission_does_not_consume_timing(self, mock_db):
        """A rejected runner admission leaves the target ordinal unchanged."""
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo
        from updater import services

        fw = "tna-30x-1.2.3-r123.bin"
        db.set_settings({
            "schedule_enabled": "true",
            "scheduled_rollout_safety_ready": "true",
            "selected_firmware_30x": fw,
            "timezone": "America/Chicago",
            "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
            "schedule_start_hour": "0",
            "schedule_end_hour": "23",
            "firmware_canary_hold_days": "0",
        })
        db.register_firmware(fw, sha256="a" * 64)
        db.upsert_access_point(
            "10.0.0.10", "root", "pass", enabled=True, model="TNA-301",
            firmware_version="1.0.0",
        )
        rollout_id = db.create_rollout(fw)
        rollout = db.get_rollout(rollout_id)

        now = dt(2026, 6, 23, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
        start = AsyncMock(side_effect=RuntimeError(
            "Cannot start update: no matching firmware selected for TNA-303L"
        ))
        scheduler = AutoUpdateScheduler(AsyncMock(), start)

        with patch("updater.services.validate_time_sources", AsyncMock(return_value=(True, now))), \
             patch.object(scheduler, "_firmware_changed", return_value=False), \
             patch.object(scheduler, "_held_families", return_value=(set(), {})), \
             patch.object(scheduler, "_resolve_scope", return_value=["10.0.0.10"]), \
             patch.object(scheduler, "_resolve_switch_scope", return_value=[]), \
             patch.object(scheduler, "_scheduled_admission_allowed", return_value=True):
            await scheduler._check_and_run()

        start.assert_awaited_once()
        assert start.await_args.kwargs["device_entries"] == [
            {"ip": "10.0.0.10", "role": "ap", "parent_ap": None}
        ]
        with db.get_db() as conn:
            timing = conn.execute(
                "SELECT ordinal FROM firmware_window_timing WHERE family='tna-30x'"
            ).fetchone()
        assert timing is None
        assert db.get_rollout(rollout_id)["last_phase_window"] is None
        assert scheduler._current_job_id is None
        assert scheduler._state == "waiting"

    @pytest.mark.asyncio
    async def test_readiness_closed_before_admission_does_not_consume_timing(self, mock_db):
        """A readiness change after queue preparation does not use a ramp window."""
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo

        fw = "tna-30x-1.2.3-r123.bin"
        db.set_settings({
            "schedule_enabled": "true",
            "scheduled_rollout_safety_ready": "true",
            "selected_firmware_30x": fw,
            "timezone": "America/Chicago",
            "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
            "schedule_start_hour": "0",
            "schedule_end_hour": "23",
            "firmware_canary_hold_days": "0",
        })
        db.register_firmware(fw, sha256="a" * 64)
        db.upsert_access_point(
            "10.0.0.10", "root", "pass", enabled=True, model="TNA-301",
            firmware_version="1.0.0",
        )
        db.create_rollout(fw)
        start = AsyncMock(return_value="must-not-start")
        scheduler = AutoUpdateScheduler(AsyncMock(), start)
        readiness = [True, False]
        now = dt(2026, 6, 23, 12, 0, tzinfo=ZoneInfo("America/Chicago"))

        with patch("updater.services.validate_time_sources", AsyncMock(return_value=(True, now))), \
             patch.object(scheduler, "_firmware_changed", return_value=False), \
             patch.object(scheduler, "_held_families", return_value=(set(), {})), \
             patch.object(scheduler, "_resolve_scope", return_value=["10.0.0.10"]), \
             patch.object(scheduler, "_resolve_switch_scope", return_value=[]), \
             patch.object(scheduler, "_scheduled_admission_allowed", side_effect=readiness):
            await scheduler._check_and_run()

        start.assert_not_awaited()
        with db.get_db() as conn:
            timing = conn.execute(
                "SELECT ordinal FROM firmware_window_timing WHERE family='tna-30x'"
            ).fetchone()
        assert timing is None

    @pytest.mark.asyncio
    async def test_job_starting_defaults_to_one_bank_when_setting_is_missing(self, mock_db):
        """A missing stored mode uses one bank for the log and admitted job."""
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo

        fw = "tna-30x-1.2.3-r123.bin"
        db.set_settings({
            "schedule_enabled": "true",
            "scheduled_rollout_safety_ready": "true",
            "selected_firmware_30x": fw,
            "timezone": "America/Chicago",
            "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
            "schedule_start_hour": "0",
            "schedule_end_hour": "23",
            "firmware_canary_hold_days": "0",
        })
        db.register_firmware(fw, sha256="a" * 64)
        db.upsert_access_point(
            "10.0.0.10", "root", "pass", enabled=True, model="TNA-301",
            firmware_version="1.0.0",
        )
        settings = db.get_all_settings()
        settings.pop("bank_mode", None)
        rollout_id = db.create_rollout(fw)
        start = AsyncMock(return_value="job-bank-mode")
        scheduler = AutoUpdateScheduler(AsyncMock(), start)

        def resolve_scope(_settings):
            return ["10.0.0.10"]

        now = dt(2026, 6, 23, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
        with patch("updater.scheduler.db.get_all_settings", return_value=settings), \
             patch("updater.services.validate_time_sources", AsyncMock(return_value=(True, now))), \
             patch.object(scheduler, "_firmware_changed", return_value=False), \
             patch.object(scheduler, "_held_families", return_value=(set(), {})), \
             patch.object(scheduler, "_resolve_scope", side_effect=resolve_scope), \
             patch.object(scheduler, "_resolve_switch_scope", return_value=[]), \
             patch.object(scheduler, "_scheduled_admission_allowed", return_value=True), \
             patch("updater.scheduler.db.log_schedule_event", wraps=db.log_schedule_event) as log_event:
            await scheduler._check_and_run()

        log_event.assert_any_call(
            "job_starting",
            "Derived queue: 1 device(s), concurrency=1",
        )
        start.assert_awaited_once()
        assert start.await_args.kwargs["bank_mode"] == "one"
        assert start.await_args.kwargs["concurrency"] == 1
        assert db.get_rollout(rollout_id)["last_phase_window"] is None

    @pytest.mark.asyncio
    async def test_successful_job_start_assigns_pending_devices(self, mock_db):
        """The happy path still assigns the wave's devices so the completion
        handler can transition them out of pending."""
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo

        fw = "tna-30x-1.2.3-r123.bin"
        db.set_settings({
            "schedule_enabled": "true",
            "scheduled_rollout_safety_ready": "true",
            "selected_firmware_30x": fw,
            "timezone": "America/Chicago",
            "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
            "schedule_start_hour": "0",
            "schedule_end_hour": "23",
            "firmware_canary_hold_days": "0",
        })
        db.register_firmware(fw, sha256="a" * 64)
        db.upsert_access_point(
            "10.0.0.10", "root", "pass", enabled=True, model="TNA-301",
            firmware_version="1.0.0",
        )
        rollout_id = db.create_rollout(fw)

        now = dt(2026, 6, 23, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
        start = AsyncMock(return_value="job-123")
        scheduler = AutoUpdateScheduler(AsyncMock(), start)

        with patch("updater.services.validate_time_sources", AsyncMock(return_value=(True, now))), \
             patch.object(scheduler, "_firmware_changed", return_value=False), \
             patch.object(scheduler, "_held_families", return_value=(set(), {})), \
             patch.object(scheduler, "_resolve_scope", return_value=["10.0.0.10"]), \
             patch.object(scheduler, "_resolve_switch_scope", return_value=[]), \
             patch.object(scheduler, "_scheduled_admission_allowed", return_value=True):
            await scheduler._check_and_run()

        start.assert_awaited_once()
        assert start.await_args.kwargs["device_entries"] == [
            {"ip": "10.0.0.10", "role": "ap", "parent_ap": None}
        ]
        assert db.get_rollout(rollout_id)["last_phase_window"] is None
        assert scheduler._current_job_id == "job-123"
        with db.get_db() as conn:
            timing = conn.execute(
                "SELECT ordinal FROM firmware_window_timing WHERE family='tna-30x'"
            ).fetchone()
        assert timing["ordinal"] == 1

        await scheduler._check_and_run()

        with db.get_db() as conn:
            timing = conn.execute(
                "SELECT ordinal FROM firmware_window_timing WHERE family='tna-30x'"
            ).fetchone()
        assert timing["ordinal"] == 1


def test_status_reads_persisted_cutoff_and_ramp_ordinal_for_current_target(tmp_path, monkeypatch):
    """Status reports timing from the real SQLite row for the pending target."""
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(scheduler_module, "datetime", FixedDateTime)
    database_path = tmp_path / "scheduler-status.db"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    db.init_db()
    db.set_settings({
        "schedule_days": "mon,tue,wed,thu,fri,sat,sun",
        "schedule_start_hour": "0",
        "schedule_end_hour": "23",
        "timezone": "UTC",
        "firmware_canary_hold_days": "0",
        "selected_firmware_30x": "tna-30x-2.0.0-r200.bin",
        "selected_firmware_303l": "",
        "selected_firmware_tns100": "",
        "schedule_scope": "all",
    })
    firmware = "tna-30x-2.0.0-r200.bin"
    target_sha256 = "a" * 64
    db.register_firmware(firmware, sha256=target_sha256)
    db.upsert_access_point(
        "10.0.0.10", "root", "pass", enabled=True, model="TNA-301",
        firmware_version="1.0.0",
    )
    db.create_rollout(firmware)

    from updater import rollout_gate

    current_key = rollout_gate.firmware_window_key(
        db.get_all_settings(), datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
    )
    assert current_key is not None
    previous_key = (current_key[0], "2000-01-01")
    cutoff_utc = "2099-01-01T00:00:00+00:00"
    db.record_firmware_window_admission(
        "tna-30x", target_sha256, previous_key, "2098-01-01T00:00:00+00:00"
    )
    db.record_firmware_window_admission(
        "tna-30x", target_sha256, current_key, cutoff_utc
    )

    status = AutoUpdateScheduler(AsyncMock(), AsyncMock()).get_status()

    assert status["rollout"]["cutoff_utc"] == cutoff_utc
    assert status["rollout"]["ramp_ordinal"] == 2


class TestSchedulerSettingsSafety:
    @pytest.mark.asyncio
    async def test_malformed_settings_do_not_crash_check_and_run(self, mock_db, monkeypatch):
        """The scheduler tick must not raise on malformed numeric settings."""
        _seed_rollout_devices()
        db.set_settings({
            "schedule_enabled": "true",
            "scheduled_rollout_safety_ready": "true",
            "timezone": "America/Chicago",
            "selected_firmware_30x": "firmware.bin",
            "weather_check_enabled": "false",
            "parallel_updates": "auto",
            "min_temperature_c": "",
            "firmware_canary_hold_days": "",
            "schedule_end_hour": "not-a-number",
        })

        start_update = AsyncMock(return_value="job-safe")
        scheduler = AutoUpdateScheduler(AsyncMock(), start_update)

        monkeypatch.setattr(
            "updater.scheduler.services.validate_time_sources",
            AsyncMock(return_value=(True, datetime(2026, 3, 5, 3, 15, 0))),
        )
        monkeypatch.setattr("updater.scheduler.services.is_in_schedule_window", lambda *a, **k: True)
        monkeypatch.setattr("updater.scheduler.services.minutes_until_window_end", lambda *a, **k: 45)

        # Must complete without raising.
        await scheduler._check_and_run()


class TestFinalizeCrashedJobRegression:
    """Regression for P0-1: _finalize_crashed_job must call
    scheduler.on_job_completed with the correct keyword name
    (``learned_versions``, plural)."""

    @pytest.mark.asyncio
    async def test_does_not_typeerror_on_scheduler_call(self, mock_db, monkeypatch):
        from datetime import datetime as dt

        job = app_module.UpdateJob(
            job_id="crash-test-1",
            started_at=dt.now(),
            is_scheduled=True,
            schedule_timezone="America/Chicago",
        )
        job.devices["10.0.0.10"] = app_module.DeviceStatus(ip="10.0.0.10", role="ap")

        recorded = {}

        class _StubScheduler:
            def on_job_completed(self_inner, *args, **kwargs):
                recorded["args"] = args
                recorded["kwargs"] = kwargs

        monkeypatch.setattr(app_module, "get_scheduler", lambda: _StubScheduler())
        monkeypatch.setattr(app_module, "broadcast", AsyncMock())
        monkeypatch.setattr(
            "updater.app.services.get_timezone",
            AsyncMock(return_value="America/Chicago"),
        )

        await app_module._finalize_crashed_job(job, RuntimeError("simulated crash"))

        assert "kwargs" in recorded, "scheduler.on_job_completed was not invoked"
        kwargs = recorded["kwargs"]
        assert "learned_versions" in kwargs
        assert "learned_version" not in kwargs


class TestSchedulerCooldown:
    def test_device_skips_during_cooldown(self):
        from updater.scheduler import _device_needs_update
        from datetime import datetime, timedelta

        # Last update was 10 days ago, cooldown is 30 days
        last_upd = (datetime.now() - timedelta(days=10)).isoformat()
        assert _device_needs_update(
            current_version="1.0.0",
            target_version="1.1.0",
            allow_downgrade=False,
            last_update_iso=last_upd,
            cooldown_days=30
        ) is False

    def test_device_included_after_cooldown(self):
        from updater.scheduler import _device_needs_update
        from datetime import datetime, timedelta

        # Last update was 31 days ago, cooldown is 30 days
        last_upd = (datetime.now() - timedelta(days=31)).isoformat()
        assert _device_needs_update(
            current_version="1.0.0",
            target_version="1.1.0",
            allow_downgrade=False,
            last_update_iso=last_upd,
            cooldown_days=30
        ) is True

    def test_device_included_when_cooldown_disabled(self):
        from updater.scheduler import _device_needs_update
        from datetime import datetime, timedelta

        # Last update was 1 day ago, but cooldown is 0
        last_upd = (datetime.now() - timedelta(days=1)).isoformat()
        assert _device_needs_update(
            current_version="1.0.0",
            target_version="1.1.0",
            allow_downgrade=False,
            last_update_iso=last_upd,
            cooldown_days=0
        ) is True



class TestExtractVersionFromFilename:
    """Pulled out separately because the regex has historically dropped
    information silently — the bug that motivates these tests is that
    a TNS-100 switch's firmware filename uses a bare `tns-` prefix
    rather than `tns-100-`, the strict regex missed it, the fallback
    captured only `1.12.8` (no revision), and `_compare_versions` then
    saw a device on `1.12.8.54729` as *ahead* of target `1.12.8` —
    triggering the up-arrow on the Updates tab once `allow_downgrade`
    was on. Test both the app and scheduler copies to keep them
    drift-free."""

    @pytest.fixture(params=[
        "updater.app._extract_version_from_filename",
        "updater.scheduler._extract_version_from_filename",
    ])
    def extract(self, request):
        from importlib import import_module
        modpath, attr = request.param.rsplit(".", 1)
        return getattr(import_module(modpath), attr)

    def test_tna_30x_prefix(self, extract):
        assert extract("tna-30x-1.12.3-r55002-20260219-tn-110-prs-squashfs-sysupgrade.bin") == "1.12.3.55002"

    def test_tna_303l_prefix(self, extract):
        assert extract("tna-303l-1.12.4-r7782-20251209-sysupgrade.bin") == "1.12.4.7782"

    def test_tns_bare_prefix_keeps_revision(self, extract):
        # The bug: this filename uses `tns-` (no model number) as the
        # leading prefix, with `tns-100` appearing only later in the
        # filename. The strict regex must accept the bare prefix or the
        # fallback must keep the `-rN` revision — either way the
        # returned version must include `.54729`.
        result = extract("tns-1.12.8-r54729-20251121-tns-100-squashfs-sysupgrade.bin")
        assert result == "1.12.8.54729"

    def test_tns_100_explicit_prefix_still_works(self, extract):
        # If the filename naming convention ever changes to include the
        # model number explicitly, that path must keep working.
        assert extract("tns-100-1.12.8-r54729-sysupgrade.bin") == "1.12.8.54729"

    def test_unknown_prefix_keeps_revision_via_fallback(self, extract):
        # Future-proofing: a vendor adds a new model whose filename we
        # don't have an alternate for yet. The fallback should still
        # capture the build revision instead of dropping it silently.
        assert extract("tnp-9000-2.0.1-r12345-sysupgrade.bin") == "2.0.1.12345"

    def test_unknown_prefix_no_revision(self, extract):
        # Filename without a `-rN` revision — fallback returns just
        # the three-segment version. Unchanged from prior behavior.
        assert extract("tnp-9000-2.0.1-sysupgrade.bin") == "2.0.1"

    def test_empty_returns_empty(self, extract):
        assert extract("") == ""
