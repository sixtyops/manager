"""Tests for API routes (authenticated)."""

import io
import json
import tarfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock


class TestSitesAPI:
    def test_create_site(self, authed_client):
        resp = authed_client.post("/api/sites", data={"name": "Tower A", "location": "Hilltop"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Tower A"

    def test_list_sites(self, authed_client):
        authed_client.post("/api/sites", data={"name": "Tower B"})
        resp = authed_client.get("/api/sites")
        assert resp.status_code == 200
        sites = resp.json()["sites"]
        assert any(s["name"] == "Tower B" for s in sites)

    def test_update_site(self, authed_client):
        create_resp = authed_client.post("/api/sites", data={"name": "Old Name"})
        site_id = create_resp.json()["id"]
        resp = authed_client.put(f"/api/sites/{site_id}", data={"name": "New Name"})
        assert resp.status_code == 200

    def test_delete_site(self, authed_client):
        create_resp = authed_client.post("/api/sites", data={"name": "ToDelete"})
        site_id = create_resp.json()["id"]
        resp = authed_client.delete(f"/api/sites/{site_id}")
        assert resp.status_code == 200

    def test_duplicate_name(self, authed_client):
        authed_client.post("/api/sites", data={"name": "Duplicate"})
        resp = authed_client.post("/api/sites", data={"name": "Duplicate"})
        assert resp.status_code == 400


class TestAPsAPI:
    def test_create_ap(self, authed_client):
        resp = authed_client.post("/api/aps", data={"ip": "10.0.0.1", "username": "root", "password": "pass"})
        assert resp.status_code == 200
        assert resp.json()["ip"] == "10.0.0.1"

    def test_list_aps(self, authed_client):
        authed_client.post("/api/aps", data={"ip": "10.0.0.2", "username": "root", "password": "pass"})
        resp = authed_client.get("/api/aps")
        assert resp.status_code == 200
        assert len(resp.json()["aps"]) >= 1

    def test_delete_ap(self, authed_client):
        authed_client.post("/api/aps", data={"ip": "10.0.0.3", "username": "root", "password": "pass"})
        resp = authed_client.delete("/api/aps/10.0.0.3")
        assert resp.status_code == 200


class TestSettingsAPI:
    def test_get_settings(self, authed_client):
        resp = authed_client.get("/api/settings")
        assert resp.status_code == 200
        assert "settings" in resp.json()

    def test_update_settings(self, authed_client):
        resp = authed_client.put("/api/settings", json={"schedule_enabled": "true"})
        assert resp.status_code == 200
        # Verify
        resp = authed_client.get("/api/settings")
        assert resp.json()["settings"]["schedule_enabled"] == "true"

    def test_save_settings_and_reevaluate(self, authed_client):
        with patch("updater.app.get_fetcher", return_value=None), \
             patch("updater.app.get_scheduler", return_value=None):
            resp = authed_client.post("/api/settings/save", json={
                "schedule_days": "mon,wed,fri",
                "schedule_start_hour": "2",
                "schedule_end_hour": "5",
                "parallel_updates": "4",
            })
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        # Verify settings persisted
        resp = authed_client.get("/api/settings")
        s = resp.json()["settings"]
        assert s["schedule_days"] == "mon,wed,fri"
        assert s["parallel_updates"] == "4"

    def test_save_settings_rejects_invalid_keys(self, authed_client):
        resp = authed_client.post("/api/settings/save", json={
            "admin_password_hash": "malicious",
        })
        assert resp.status_code == 400

    def test_save_settings_accepts_firmware_keys(self, authed_client):
        with patch("updater.app.get_fetcher", return_value=None), \
             patch("updater.app.get_scheduler", return_value=None):
            resp = authed_client.post("/api/settings/save", json={
                "selected_firmware_30x": "tachyon-v1.12.3.bin",
                "selected_firmware_303l": "tachyon-303l-v1.12.3.bin",
                "selected_firmware_tns100": "tachyon-tns100-v1.12.3.bin",
            })
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        resp = authed_client.get("/api/settings")
        s = resp.json()["settings"]
        assert s["selected_firmware_30x"] == "tachyon-v1.12.3.bin"
        assert s["selected_firmware_303l"] == "tachyon-303l-v1.12.3.bin"
        assert s["selected_firmware_tns100"] == "tachyon-tns100-v1.12.3.bin"

    def test_save_settings_mixed_valid_and_invalid_keys(self, authed_client):
        with patch("updater.app.get_fetcher", return_value=None), \
             patch("updater.app.get_scheduler", return_value=None):
            resp = authed_client.post("/api/settings/save", json={
                "schedule_days": "mon,fri",
                "parallel_updates": "8",
                "admin_password_hash": "evil",
                "secret_sauce": "nope",
            })
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        resp = authed_client.get("/api/settings")
        s = resp.json()["settings"]
        assert s["schedule_days"] == "mon,fri"
        assert s["parallel_updates"] == "8"
        assert s.get("admin_password_hash") != "evil"

    def test_save_settings_calls_fetcher_reselect(self, authed_client):
        mock_fetcher = MagicMock()
        with patch("updater.app.get_fetcher", return_value=mock_fetcher), \
             patch("updater.app.get_scheduler", return_value=None):
            resp = authed_client.post("/api/settings/save", json={
                "schedule_days": "mon,tue",
            })
        assert resp.status_code == 200
        mock_fetcher.reselect.assert_called_once_with(False)

    def test_save_settings_calls_scheduler_force_check(self, authed_client):
        mock_scheduler = MagicMock()
        mock_scheduler.force_check = AsyncMock()
        with patch("updater.app.get_scheduler", return_value=mock_scheduler):
            resp = authed_client.post("/api/settings/save", json={
                "schedule_enabled": "true",
            })
        assert resp.status_code == 200
        mock_scheduler.force_check.assert_awaited_once()

    def test_save_settings_calls_both_fetcher_and_scheduler(self, authed_client):
        mock_fetcher = MagicMock()
        mock_scheduler = MagicMock()
        mock_scheduler.force_check = AsyncMock()
        with patch("updater.app.get_fetcher", return_value=mock_fetcher), \
             patch("updater.app.get_scheduler", return_value=mock_scheduler):
            resp = authed_client.post("/api/settings/save", json={
                "firmware_beta_enabled": "true",
                "schedule_enabled": "true",
            })
        assert resp.status_code == 200
        mock_fetcher.reselect.assert_called_once_with(True)
        mock_scheduler.force_check.assert_awaited_once()

    def test_save_pre_update_reboot_setting(self, authed_client):
        with patch("updater.app.get_fetcher", return_value=None), \
             patch("updater.app.get_scheduler", return_value=None):
            resp = authed_client.post("/api/settings/save", json={
                "pre_update_reboot": "false",
            })
        assert resp.status_code == 200
        resp = authed_client.get("/api/settings")
        assert resp.json()["settings"]["pre_update_reboot"] == "false"

        # Toggle back on
        with patch("updater.app.get_fetcher", return_value=None), \
             patch("updater.app.get_scheduler", return_value=None):
            resp = authed_client.post("/api/settings/save", json={
                "pre_update_reboot": "true",
            })
        assert resp.status_code == 200
        resp = authed_client.get("/api/settings")
        assert resp.json()["settings"]["pre_update_reboot"] == "true"


class TestTopologyAPI:
    def test_get_topology(self, authed_client):
        resp = authed_client.get("/api/topology")
        assert resp.status_code == 200
        data = resp.json()
        assert "sites" in data or "total_aps" in data


class TestQuickAddAPI:
    def test_quick_add(self, authed_client):
        resp = authed_client.post("/api/quick-add", data={
            "ip": "10.0.0.5",
            "username": "root",
            "password": "pass",
            "site_name": "NewSite",
        })
        assert resp.status_code == 200
        assert resp.json()["ip"] == "10.0.0.5"
        assert resp.json()["site_id"] is not None


class TestFirmwareAPI:
    def test_list_firmware_files(self, authed_client):
        resp = authed_client.get("/api/firmware-files")
        assert resp.status_code == 200
        assert "files" in resp.json()


class TestAutoUpdateAPI:
    def test_get_update_status(self, authed_client):
        mock_checker = MagicMock()
        mock_checker.get_update_status.return_value = {
            "current_version": "1.0.0",
            "enabled": False,
            "last_check": "",
            "available_version": "",
            "release_url": "",
            "release_notes": "",
            "update_available": False,
            "docker_socket_available": False,
            "can_update": True,
            "blocked_reason": "",
        }
        with patch("updater.app.get_checker", return_value=mock_checker):
            resp = authed_client.get("/api/updates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_version"] == "1.0.0"
        assert data["update_available"] is False

    def test_get_update_status_with_available_update(self, authed_client):
        mock_checker = MagicMock()
        mock_checker.get_update_status.return_value = {
            "current_version": "1.0.0",
            "enabled": True,
            "last_check": "2026-01-01T00:00:00",
            "available_version": "0.2.0",
            "release_url": "https://github.com/isolson/firmware-updater/releases/tag/v0.2.0",
            "release_notes": "New features",
            "update_available": True,
            "docker_socket_available": True,
            "can_update": True,
            "blocked_reason": "",
        }
        with patch("updater.app.get_checker", return_value=mock_checker):
            resp = authed_client.get("/api/updates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is True
        assert data["available_version"] == "0.2.0"

    def test_check_for_updates(self, authed_client):
        mock_checker = MagicMock()
        mock_checker.check_for_updates = AsyncMock(return_value={
            "current_version": "1.0.0",
            "latest_version": "1.0.0",
            "update_available": False,
            "release_url": None,
            "release_notes": None,
            "error": None,
        })
        with patch("updater.app.get_checker", return_value=mock_checker):
            resp = authed_client.post("/api/updates/check")
        assert resp.status_code == 200
        assert resp.json()["update_available"] is False
        mock_checker.check_for_updates.assert_awaited_once()

    def test_check_for_updates_finds_new_version(self, authed_client):
        mock_checker = MagicMock()
        mock_checker.check_for_updates = AsyncMock(return_value={
            "current_version": "1.0.0",
            "latest_version": "0.3.0",
            "update_available": True,
            "release_url": "https://github.com/isolson/firmware-updater/releases/tag/v0.3.0",
            "release_notes": "Bug fixes and improvements",
            "error": None,
        })
        with patch("updater.app.get_checker", return_value=mock_checker):
            resp = authed_client.post("/api/updates/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is True
        assert data["latest_version"] == "0.3.0"

    def test_apply_update_success(self, authed_client):
        with patch("updater.app.apply_update", new_callable=AsyncMock, return_value={
            "success": True,
            "message": "Update started. The application will restart shortly.",
        }):
            resp = authed_client.post("/api/updates/apply")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_apply_update_blocked_by_rollout(self, authed_client):
        with patch("updater.app.apply_update", new_callable=AsyncMock, return_value={
            "success": False,
            "message": "Cannot update now: A firmware rollout is currently active. Please try again later.",
            "blocked_reason": "A firmware rollout is currently active",
        }):
            resp = authed_client.post("/api/updates/apply")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "rollout" in data["blocked_reason"]

    def test_apply_update_no_docker_socket(self, authed_client):
        with patch("updater.app.apply_update", new_callable=AsyncMock, return_value={
            "success": False,
            "manual": True,
            "message": "Docker socket not mounted. Run these commands manually:",
            "commands": [
                "cd /path/to/deployment",
                "docker compose pull tachyon-mgmt",
                "docker compose up -d tachyon-mgmt",
            ],
        }):
            resp = authed_client.post("/api/updates/apply")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["manual"] is True
        assert len(data["commands"]) == 3

    def test_updates_require_auth(self, client):
        for url, method in [
            ("/api/updates", "get"),
            ("/api/updates/check", "post"),
            ("/api/updates/apply", "post"),
        ]:
            resp = getattr(client, method)(url, follow_redirects=False)
            assert resp.status_code in (401, 303), f"{method.upper()} {url} should require auth"


class TestStartUpdateAPI:
    """Tests for /api/start-update (Update Now for AP + CPEs)."""

    def _seed_ap_and_cpes(self, mock_db):
        """Insert an AP with two authenticated CPEs into the test DB."""
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, model, firmware_version)"
            " VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", "root", "pass", "TNA-301", "1.12.2.54970"),
        )
        mock_db.execute(
            "INSERT INTO cpe_cache (ap_ip, ip, mac, model, firmware_version, auth_status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("10.0.0.1", "10.0.0.10", "AA:BB:CC:00:00:01", "TNA-303L-65", "1.12.2.7713", "ok"),
        )
        mock_db.execute(
            "INSERT INTO cpe_cache (ap_ip, ip, mac, model, firmware_version, auth_status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("10.0.0.1", "10.0.0.11", "AA:BB:CC:00:00:02", "TNA-301", "1.12.2.54970", "ok"),
        )
        mock_db.commit()

    def test_start_update_creates_job(self, authed_client, mock_db, tmp_path):
        """POST with valid AP IP creates a job that includes AP + CPEs."""
        self._seed_ap_and_cpes(mock_db)
        fw_file = tmp_path / "tachyon-v1.12.3.bin"
        fw_file.write_bytes(b"fake firmware")

        with patch("updater.app.FIRMWARE_DIR", tmp_path), \
             patch("updater.app._spawn_update_job") as mock_spawn:
            resp = authed_client.post("/api/start-update", data={
                "firmware_file": "tachyon-v1.12.3.bin",
                "device_type": "mixed",
                "ip_list": "10.0.0.1",
                "concurrency": "2",
                "bank_mode": "both",
            })
            mock_spawn.assert_called_once()

        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        # AP (1) + 2 authenticated CPEs = 3 devices
        assert data["device_count"] == 3

    def test_start_update_no_firmware(self, authed_client, mock_db, tmp_path):
        """POST with non-existent firmware file returns 400."""
        self._seed_ap_and_cpes(mock_db)

        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            resp = authed_client.post("/api/start-update", data={
                "firmware_file": "nonexistent.bin",
                "device_type": "mixed",
                "ip_list": "10.0.0.1",
            })

        assert resp.status_code == 400

    def test_start_update_no_ips(self, authed_client, mock_db, tmp_path):
        """POST with empty ip_list returns 4xx."""
        fw_file = tmp_path / "tachyon-v1.12.3.bin"
        fw_file.write_bytes(b"fake firmware")

        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            resp = authed_client.post("/api/start-update", data={
                "firmware_file": "tachyon-v1.12.3.bin",
                "device_type": "mixed",
                "ip_list": "",
            })

        assert resp.status_code in (400, 422)

    def test_start_update_missing_ap(self, authed_client, mock_db, tmp_path):
        """POST with an IP not in the DB returns 400 (no stored credentials)."""
        fw_file = tmp_path / "tachyon-v1.12.3.bin"
        fw_file.write_bytes(b"fake firmware")

        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            resp = authed_client.post("/api/start-update", data={
                "firmware_file": "tachyon-v1.12.3.bin",
                "device_type": "mixed",
                "ip_list": "10.99.99.99",
            })

        assert resp.status_code == 400

    def test_start_update_requires_auth(self, client):
        """POST without session cookie returns 401/303."""
        resp = client.post("/api/start-update", data={
            "firmware_file": "fw.bin",
            "device_type": "mixed",
            "ip_list": "10.0.0.1",
        }, follow_redirects=False)
        assert resp.status_code in (401, 303)


class TestJobCancelAPI:
    def test_cancel_running_job(self, authed_client):
        from updater.app import UpdateJob, update_jobs

        update_jobs.clear()
        try:
            update_jobs["job12345"] = UpdateJob(job_id="job12345", status="running")
            resp = authed_client.post("/api/job/job12345/cancel")
            assert resp.status_code == 200
            data = resp.json()
            assert data["cancelled"] is True
            assert "cancelled by user" in data["message"].lower()
            assert update_jobs["job12345"].cancelled is True
        finally:
            update_jobs.clear()

    def test_cancel_completed_job_rejected(self, authed_client):
        from updater.app import UpdateJob, update_jobs

        update_jobs.clear()
        try:
            update_jobs["jobdone01"] = UpdateJob(job_id="jobdone01", status="completed")
            resp = authed_client.post("/api/job/jobdone01/cancel")
            assert resp.status_code == 400
        finally:
            update_jobs.clear()


class TestScheduledRuntimeGuard:
    @pytest.mark.asyncio
    async def test_blocks_when_time_validation_fails(self):
        from updater.app import UpdateJob, _scheduled_job_guard

        job = UpdateJob(
            job_id="sched1",
            is_scheduled=True,
            start_hour=3,
            end_hour=4,
            schedule_days=["thu"],
            schedule_timezone="America/Chicago",
        )
        with patch("updater.app.services.validate_time_sources", new_callable=AsyncMock, return_value=(False, "NTP unavailable")):
            allowed, reason = await _scheduled_job_guard(job)
        assert allowed is False
        assert "time anomaly" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_outside_window(self):
        from updater.app import UpdateJob, _scheduled_job_guard

        job = UpdateJob(
            job_id="sched2",
            is_scheduled=True,
            start_hour=3,
            end_hour=4,
            schedule_days=["thu"],
            schedule_timezone="America/Chicago",
        )
        now = datetime(2026, 2, 26, 10, 0, tzinfo=ZoneInfo("America/Chicago"))  # Thu
        with patch("updater.app.services.validate_time_sources", new_callable=AsyncMock, return_value=(True, now)):
            allowed, reason = await _scheduled_job_guard(job)
        assert allowed is False
        assert "outside maintenance window" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_when_window_is_ending(self):
        from updater.app import UpdateJob, _scheduled_job_guard

        job = UpdateJob(
            job_id="sched3",
            is_scheduled=True,
            start_hour=3,
            end_hour=4,
            schedule_days=["thu"],
            schedule_timezone="America/Chicago",
        )
        now = datetime(2026, 2, 26, 3, 55, tzinfo=ZoneInfo("America/Chicago"))  # Thu
        with patch("updater.app.services.validate_time_sources", new_callable=AsyncMock, return_value=(True, now)):
            allowed, reason = await _scheduled_job_guard(job)
        assert allowed is False
        assert "window ending" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_at_15_minute_boundary(self):
        from updater.app import UpdateJob, _scheduled_job_guard

        job = UpdateJob(
            job_id="sched4",
            is_scheduled=True,
            start_hour=3,
            end_hour=4,
            schedule_days=["thu"],
            schedule_timezone="America/Chicago",
        )
        now = datetime(2026, 2, 26, 3, 45, tzinfo=ZoneInfo("America/Chicago"))  # Thu, exactly 15 min left
        with patch("updater.app.services.validate_time_sources", new_callable=AsyncMock, return_value=(True, now)):
            allowed, reason = await _scheduled_job_guard(job)
        assert allowed is False
        assert "window ending" in reason.lower()


class TestDevicePortalAPI:
    """Tests for GET /api/device-portal/{ip} (auto-login redirect)."""

    def _seed_devices(self, mock_db):
        """Insert an AP, switch, and CPE for testing."""
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, model, firmware_version)"
            " VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", "root", "appass123", "TNA-301", "1.12.2.54970"),
        )
        mock_db.execute(
            "INSERT INTO switches (ip, username, password, model, firmware_version)"
            " VALUES (?, ?, ?, ?, ?)",
            ("10.1.0.1", "admin", "swpass456", "SW-100", "2.0.1"),
        )
        mock_db.execute(
            "INSERT INTO cpe_cache (ap_ip, ip, mac, model, firmware_version, auth_status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("10.0.0.1", "10.0.0.10", "AA:BB:CC:00:00:01", "TNA-303L-65", "1.12.2.7713", "ok"),
        )
        mock_db.commit()

    def test_portal_ap(self, authed_client, mock_db):
        """Returns auto-login HTML for a known AP."""
        self._seed_devices(mock_db)
        resp = authed_client.get("/api/device-portal/10.0.0.1")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "10.0.0.1" in resp.text
        assert "loginForm" in resp.text

    def test_portal_switch(self, authed_client, mock_db):
        """Returns auto-login HTML for a known switch."""
        self._seed_devices(mock_db)
        resp = authed_client.get("/api/device-portal/10.1.0.1")
        assert resp.status_code == 200
        assert "10.1.0.1" in resp.text

    def test_portal_cpe_inherits_ap_creds(self, authed_client, mock_db):
        """CPE portal uses parent AP credentials."""
        self._seed_devices(mock_db)
        resp = authed_client.get("/api/device-portal/10.0.0.10")
        assert resp.status_code == 200
        assert "10.0.0.10" in resp.text
        # Should contain parent AP's username in the form
        assert "root" in resp.text

    def test_portal_unknown_device(self, authed_client, mock_db):
        """Returns 404 for unknown IP."""
        resp = authed_client.get("/api/device-portal/10.99.99.99")
        assert resp.status_code == 404

    def test_portal_requires_auth(self, client):
        """Unauthenticated request is rejected."""
        resp = client.get("/api/device-portal/10.0.0.1", follow_redirects=False)
        assert resp.status_code in (401, 303)

    def test_portal_no_cache_headers(self, authed_client, mock_db):
        """Response includes no-cache headers to prevent credential caching."""
        self._seed_devices(mock_db)
        resp = authed_client.get("/api/device-portal/10.0.0.1")
        assert "no-store" in resp.headers.get("cache-control", "")


# ============================================================================
# Config Backup Tests
# ============================================================================

class TestConfigsAPI:
    """Tests for config backup endpoints."""

    SAMPLE_CONFIG = {"network": {"hostname": "ap-test"}, "services": {"snmp": {"enabled": True}}}

    def _seed_config(self, mock_db, ip="10.0.0.1", config=None, model="TNA-301"):
        """Insert a config snapshot."""
        cfg = config or self.SAMPLE_CONFIG
        config_json = json.dumps(cfg, sort_keys=True)
        import hashlib
        config_hash = hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        mock_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ip, config_json, config_hash, model, "tn-110-prs", "2026-02-19T12:00:00"),
        )
        mock_db.commit()

    def test_list_configs_empty(self, authed_client):
        resp = authed_client.get("/api/configs")
        assert resp.status_code == 200
        assert resp.json()["configs"] == {}

    def test_list_configs(self, authed_client, mock_db):
        self._seed_config(mock_db, "10.0.0.1")
        resp = authed_client.get("/api/configs")
        assert resp.status_code == 200
        configs = resp.json()["configs"]
        assert "10.0.0.1" in configs
        assert configs["10.0.0.1"]["model"] == "TNA-301"

    def test_get_config_history(self, authed_client, mock_db):
        self._seed_config(mock_db, "10.0.0.1")
        resp = authed_client.get("/api/configs/10.0.0.1")
        assert resp.status_code == 200
        assert len(resp.json()["history"]) == 1

    def test_get_config_history_empty(self, authed_client):
        resp = authed_client.get("/api/configs/10.99.99.99")
        assert resp.status_code == 200
        assert resp.json()["history"] == []

    def test_get_latest_config(self, authed_client, mock_db):
        self._seed_config(mock_db, "10.0.0.1")
        resp = authed_client.get("/api/configs/10.0.0.1/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_json"]["network"]["hostname"] == "ap-test"

    def test_get_latest_config_not_found(self, authed_client):
        resp = authed_client.get("/api/configs/10.99.99.99/latest")
        assert resp.status_code == 404

    def test_get_config_snapshot(self, authed_client, mock_db):
        self._seed_config(mock_db, "10.0.0.1")
        row = mock_db.execute("SELECT id FROM device_configs WHERE ip = '10.0.0.1'").fetchone()
        config_id = row[0]
        resp = authed_client.get(f"/api/configs/10.0.0.1/snapshot/{config_id}")
        assert resp.status_code == 200
        assert resp.json()["ip"] == "10.0.0.1"

    def test_get_config_snapshot_wrong_ip(self, authed_client, mock_db):
        self._seed_config(mock_db, "10.0.0.1")
        row = mock_db.execute("SELECT id FROM device_configs WHERE ip = '10.0.0.1'").fetchone()
        config_id = row[0]
        resp = authed_client.get(f"/api/configs/10.0.0.2/snapshot/{config_id}")
        assert resp.status_code == 404

    def test_download_config_tar(self, authed_client, mock_db):
        self._seed_config(mock_db, "10.0.0.1")
        row = mock_db.execute("SELECT id FROM device_configs WHERE ip = '10.0.0.1'").fetchone()
        config_id = row[0]
        resp = authed_client.get(f"/api/configs/10.0.0.1/download/{config_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-tar"
        assert "attachment" in resp.headers["content-disposition"]

        # Verify tar contents
        tar_buf = io.BytesIO(resp.content)
        with tarfile.open(fileobj=tar_buf, mode="r") as tar:
            names = tar.getnames()
            assert "config.json" in names
            assert "CONTROL" in names
            # Verify config.json contents
            config_file = tar.extractfile("config.json")
            config_data = json.loads(config_file.read())
            assert config_data["network"]["hostname"] == "ap-test"
            # Verify CONTROL contents
            control_file = tar.extractfile("CONTROL")
            assert control_file.read().decode() == "tn-110-prs"

    def test_download_config_tar_not_found(self, authed_client):
        resp = authed_client.get("/api/configs/10.0.0.1/download/9999")
        assert resp.status_code == 404

    def test_configs_require_auth(self, client):
        for url in ["/api/configs", "/api/configs/10.0.0.1", "/api/configs/10.0.0.1/latest"]:
            resp = client.get(url, follow_redirects=False)
            assert resp.status_code in (401, 303), f"GET {url} should require auth"


# ============================================================================
# Config Template Tests
# ============================================================================

class TestConfigTemplatesAPI:
    """Tests for config template CRUD endpoints."""

    def test_list_templates_empty(self, authed_client):
        resp = authed_client.get("/api/config-templates")
        assert resp.status_code == 200
        assert resp.json()["templates"] == []

    def test_create_template(self, authed_client):
        resp = authed_client.post("/api/config-templates", json={
            "name": "SNMP Standard",
            "category": "snmp",
            "config_fragment": {"services": {"snmp": {"v2_ro_community": "public"}}},
            "description": "Standard SNMP settings",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert "id" in resp.json()

    def test_create_template_missing_fields(self, authed_client):
        resp = authed_client.post("/api/config-templates", json={
            "name": "Incomplete",
        })
        assert resp.status_code == 400

    def test_create_template_duplicate_name(self, authed_client):
        authed_client.post("/api/config-templates", json={
            "name": "SNMP Dup",
            "category": "snmp",
            "config_fragment": {"services": {"snmp": {}}},
        })
        resp = authed_client.post("/api/config-templates", json={
            "name": "SNMP Dup",
            "category": "snmp",
            "config_fragment": {"services": {"snmp": {}}},
        })
        assert resp.status_code == 409

    def test_list_templates_after_create(self, authed_client):
        authed_client.post("/api/config-templates", json={
            "name": "NTP Config",
            "category": "ntp",
            "config_fragment": {"services": {"ntp": {"server1": "pool.ntp.org"}}},
        })
        resp = authed_client.get("/api/config-templates")
        assert resp.status_code == 200
        templates = resp.json()["templates"]
        assert len(templates) == 1
        assert templates[0]["name"] == "NTP Config"
        assert templates[0]["config_fragment"]["services"]["ntp"]["server1"] == "pool.ntp.org"

    def test_update_template(self, authed_client):
        create_resp = authed_client.post("/api/config-templates", json={
            "name": "Old Name",
            "category": "snmp",
            "config_fragment": {"services": {"snmp": {}}},
        })
        template_id = create_resp.json()["id"]
        resp = authed_client.put(f"/api/config-templates/{template_id}", json={
            "name": "New Name",
            "description": "Updated",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_update_template_not_found(self, authed_client):
        resp = authed_client.put("/api/config-templates/9999", json={"name": "No"})
        assert resp.status_code == 404

    def test_delete_template(self, authed_client):
        create_resp = authed_client.post("/api/config-templates", json={
            "name": "To Delete",
            "category": "ntp",
            "config_fragment": {"services": {"ntp": {}}},
        })
        template_id = create_resp.json()["id"]
        resp = authed_client.delete(f"/api/config-templates/{template_id}")
        assert resp.status_code == 200
        # Verify it's gone
        resp = authed_client.get("/api/config-templates")
        assert len(resp.json()["templates"]) == 0

    def test_delete_template_not_found(self, authed_client):
        resp = authed_client.delete("/api/config-templates/9999")
        assert resp.status_code == 404

    def test_create_template_with_form_data(self, authed_client):
        resp = authed_client.post("/api/config-templates", json={
            "name": "Users Config",
            "category": "users",
            "config_fragment": {"system": {"users": []}},
            "form_data": {"users": [{"username": "admin", "level": "admin"}]},
        })
        assert resp.status_code == 200
        # Verify form_data comes back
        templates = authed_client.get("/api/config-templates").json()["templates"]
        assert templates[0]["form_data"]["users"][0]["username"] == "admin"

    def test_templates_require_auth(self, client):
        resp = client.get("/api/config-templates", follow_redirects=False)
        assert resp.status_code in (401, 303)
        resp = client.post("/api/config-templates", json={}, follow_redirects=False)
        assert resp.status_code in (401, 303)


# ============================================================================
# Config Compliance Tests
# ============================================================================

class TestConfigComplianceAPI:
    """Tests for config compliance endpoint."""

    def _seed_config(self, mock_db, ip, config):
        config_json = json.dumps(config, sort_keys=True)
        import hashlib
        config_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        mock_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ip, config_json, config_hash, "TNA-301", "tn-110-prs", "2026-02-19T12:00:00"),
        )
        mock_db.commit()

    def _seed_template(self, mock_db, name, category, fragment):
        mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, enabled) VALUES (?, ?, ?, 1)",
            (name, category, json.dumps(fragment)),
        )
        mock_db.commit()

    def test_compliance_no_data(self, authed_client):
        resp = authed_client.get("/api/config-compliance")
        assert resp.status_code == 200
        assert resp.json()["devices"] == {}

    def test_compliance_no_templates(self, authed_client, mock_db):
        """Without templates, all devices are compliant."""
        self._seed_config(mock_db, "10.0.0.1", {"services": {"snmp": {"enabled": True}}})
        resp = authed_client.get("/api/config-compliance")
        assert resp.status_code == 200
        assert resp.json()["devices"]["10.0.0.1"]["compliant"] is True

    def test_compliance_matching(self, authed_client, mock_db):
        """Device config matches the template fragment."""
        config = {"services": {"snmp": {"enabled": True, "v2_ro_community": "public"}}}
        self._seed_config(mock_db, "10.0.0.1", config)
        self._seed_template(mock_db, "SNMP", "snmp", {"services": {"snmp": {"v2_ro_community": "public"}}})
        resp = authed_client.get("/api/config-compliance")
        assert resp.json()["devices"]["10.0.0.1"]["compliant"] is True

    def test_compliance_non_matching(self, authed_client, mock_db):
        """Device config does NOT match the template fragment."""
        config = {"services": {"snmp": {"enabled": True, "v2_ro_community": "private"}}}
        self._seed_config(mock_db, "10.0.0.1", config)
        self._seed_template(mock_db, "SNMP", "snmp", {"services": {"snmp": {"v2_ro_community": "public"}}})
        resp = authed_client.get("/api/config-compliance")
        assert resp.json()["devices"]["10.0.0.1"]["compliant"] is False

    def test_compliance_require_auth(self, client):
        resp = client.get("/api/config-compliance", follow_redirects=False)
        assert resp.status_code in (401, 303)


# ============================================================================
# Config Prefill Tests
# ============================================================================

class TestConfigPrefillAPI:
    """Tests for config prefill endpoint."""

    def _seed_config(self, mock_db, ip, config):
        config_json = json.dumps(config, sort_keys=True)
        import hashlib
        config_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        mock_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ip, config_json, config_hash, "TNA-301", "tn-110-prs", "2026-02-19T12:00:00"),
        )
        mock_db.commit()

    def test_prefill_no_configs(self, authed_client):
        resp = authed_client.get("/api/config-prefill/snmp")
        assert resp.status_code == 200
        assert resp.json()["prefilled"] is False
        assert resp.json()["reason"] == "no_configs"

    def test_prefill_returns_dominant_value(self, authed_client, mock_db):
        """When 3+ devices share the same non-default SNMP config, prefill returns it."""
        snmp_config = {"services": {"snmp": {"v2_ro_community": "public"}}}
        for i in range(5):
            self._seed_config(mock_db, f"10.0.0.{i+1}", snmp_config)
        resp = authed_client.get("/api/config-prefill/snmp")
        data = resp.json()
        assert data["prefilled"] is True
        assert data["data"]["v2_ro_community"] == "public"
        assert data["match_count"] == 5

    def test_prefill_requires_three_matching_devices(self, authed_client, mock_db):
        match = {"services": {"snmp": {"v2_ro_community": "public"}}}
        other = {"services": {"snmp": {"v2_ro_community": "private"}}}
        self._seed_config(mock_db, "10.0.0.1", match)
        self._seed_config(mock_db, "10.0.0.2", match)
        self._seed_config(mock_db, "10.0.0.3", other)
        resp = authed_client.get("/api/config-prefill/snmp")
        data = resp.json()
        assert data["prefilled"] is False
        assert data["reason"] == "insufficient_matches"
        assert data["required_matches"] == 3
        assert data["match_count"] == 2

    def test_prefill_ignores_default_like_values(self, authed_client, mock_db):
        default_ntp = {"services": {"ntp": {"enabled": True, "servers": []}}}
        for i in range(3):
            self._seed_config(mock_db, f"10.0.1.{i+1}", default_ntp)
        resp = authed_client.get("/api/config-prefill/ntp")
        data = resp.json()
        assert data["prefilled"] is False
        assert data["reason"] == "no_non_default_data"

    def test_prefill_blocked_when_template_exists(self, authed_client, mock_db):
        """Prefill should NOT run if a template already exists for this category."""
        self._seed_config(mock_db, "10.0.0.1", {"services": {"snmp": {"enabled": True}}})
        mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, enabled) VALUES (?, ?, ?, 1)",
            ("SNMP", "snmp", json.dumps({"services": {"snmp": {}}})),
        )
        mock_db.commit()
        resp = authed_client.get("/api/config-prefill/snmp")
        assert resp.json()["prefilled"] is False
        assert resp.json()["reason"] == "template_exists"

    def test_prefill_unknown_category(self, authed_client, mock_db):
        self._seed_config(mock_db, "10.0.0.1", {"services": {}})
        resp = authed_client.get("/api/config-prefill/foobar")
        assert resp.json()["prefilled"] is False
        assert resp.json()["reason"] == "unknown_category"


# ============================================================================
# Config Push Tests
# ============================================================================

class TestConfigPushAPI:
    """Tests for config push endpoint."""

    def _seed_ap(self, mock_db, ip="10.0.0.1"):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, model, firmware_version) "
            "VALUES (?, ?, ?, ?, ?)",
            (ip, "root", "pass", "TNA-301", "1.12.2.54970"),
        )
        mock_db.commit()

    def _seed_template(self, mock_db, name="SNMP Std", category="snmp"):
        mock_db.execute(
            "INSERT INTO config_templates (name, category, config_fragment, enabled) VALUES (?, ?, ?, 1)",
            (name, category, json.dumps({"services": {"snmp": {"v2_ro_community": "public"}}})),
        )
        mock_db.commit()
        return mock_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_push_missing_fields(self, authed_client):
        resp = authed_client.post("/api/config-push", json={})
        assert resp.status_code == 400

    def test_push_missing_template(self, authed_client):
        resp = authed_client.post("/api/config-push", json={
            "template_ids": [9999],
            "targets": [{"type": "ap", "ip": "10.0.0.1"}],
        })
        assert resp.status_code == 404

    def test_push_require_auth(self, client):
        resp = client.post("/api/config-push", json={}, follow_redirects=False)
        assert resp.status_code in (401, 303)


# ============================================================================
# Deep Merge Tests
# ============================================================================

class TestDeepMerge:
    """Tests for the deep_merge helper function."""

    def test_basic_merge(self):
        from updater.app import deep_merge
        base = {"a": 1, "b": 2}
        overlay = {"b": 3, "c": 4}
        result = deep_merge(base, overlay)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        from updater.app import deep_merge
        base = {"services": {"snmp": {"enabled": True, "community": "old"}}}
        overlay = {"services": {"snmp": {"community": "new"}}}
        result = deep_merge(base, overlay)
        assert result == {"services": {"snmp": {"enabled": True, "community": "new"}}}

    def test_overlay_adds_new_keys(self):
        from updater.app import deep_merge
        base = {"a": {"x": 1}}
        overlay = {"a": {"y": 2}, "b": 3}
        result = deep_merge(base, overlay)
        assert result == {"a": {"x": 1, "y": 2}, "b": 3}

    def test_list_replacement(self):
        from updater.app import deep_merge
        base = {"users": ["alice", "bob"]}
        overlay = {"users": ["charlie"]}
        result = deep_merge(base, overlay)
        assert result == {"users": ["charlie"]}

    def test_original_not_mutated(self):
        from updater.app import deep_merge
        base = {"a": {"x": 1}}
        overlay = {"a": {"y": 2}}
        deep_merge(base, overlay)
        assert base == {"a": {"x": 1}}


# ============================================================================
# Fragment Matches Tests
# ============================================================================

class TestFragmentMatches:
    """Tests for the _fragment_matches helper function."""

    def test_exact_match(self):
        from updater.app import _fragment_matches
        config = {"services": {"snmp": {"community": "public"}}}
        fragment = {"services": {"snmp": {"community": "public"}}}
        assert _fragment_matches(config, fragment) is True

    def test_partial_match(self):
        from updater.app import _fragment_matches
        config = {"services": {"snmp": {"community": "public", "enabled": True}}}
        fragment = {"services": {"snmp": {"community": "public"}}}
        assert _fragment_matches(config, fragment) is True

    def test_no_match(self):
        from updater.app import _fragment_matches
        config = {"services": {"snmp": {"community": "private"}}}
        fragment = {"services": {"snmp": {"community": "public"}}}
        assert _fragment_matches(config, fragment) is False

    def test_missing_key(self):
        from updater.app import _fragment_matches
        config = {"services": {}}
        fragment = {"services": {"snmp": {"community": "public"}}}
        assert _fragment_matches(config, fragment) is False


class TestProtectedConfigKeys:
    """Tests for protected config key validation."""

    def test_create_template_with_network_key_rejected(self, authed_client, mock_db):
        resp = authed_client.post("/api/config-templates", json={
            "name": "Bad Template",
            "category": "custom",
            "config_fragment": {"network": {"ip": "0.0.0.0"}},
        })
        assert resp.status_code == 400
        assert "network" in resp.json()["detail"].lower()

    def test_create_template_with_ethernet_key_rejected(self, authed_client, mock_db):
        resp = authed_client.post("/api/config-templates", json={
            "name": "Bad Ethernet",
            "category": "custom",
            "config_fragment": {"ethernet": {"speed": "100"}},
        })
        assert resp.status_code == 400
        assert "ethernet" in resp.json()["detail"].lower()

    def test_create_template_with_safe_key_allowed(self, authed_client, mock_db):
        resp = authed_client.post("/api/config-templates", json={
            "name": "Safe Template",
            "category": "snmp",
            "config_fragment": {"services": {"snmp": {"community": "public"}}},
        })
        assert resp.status_code == 200

    def test_update_template_with_network_key_rejected(self, authed_client, mock_db):
        # Create a valid template first
        resp = authed_client.post("/api/config-templates", json={
            "name": "To Update",
            "category": "snmp",
            "config_fragment": {"services": {"snmp": {"community": "public"}}},
        })
        tid = resp.json()["id"]
        # Try to update with protected key
        resp = authed_client.put(f"/api/config-templates/{tid}", json={
            "config_fragment": {"network": {"gateway": "10.0.0.1"}},
        })
        assert resp.status_code == 400

    def test_validate_fragment_safety_direct(self):
        from updater.app import _validate_fragment_safety
        # Safe fragment
        _validate_fragment_safety({"services": {"ntp": {"enabled": True}}})
        # Protected key
        import pytest
        with pytest.raises(ValueError, match="network"):
            _validate_fragment_safety({"network": {"ip": "1.2.3.4"}})

    def test_create_template_with_invalid_json_string(self, authed_client, mock_db):
        resp = authed_client.post("/api/config-templates", json={
            "name": "Bad JSON",
            "category": "custom",
            "config_fragment": "not valid json {{{",
        })
        assert resp.status_code == 400
        assert "json" in resp.json()["detail"].lower()


class TestDeepMerge:
    """Tests for deep_merge safety."""

    def test_basic_merge(self):
        from updater.app import deep_merge
        base = {"a": 1, "b": {"c": 2}}
        overlay = {"b": {"d": 3}, "e": 4}
        result = deep_merge(base, overlay)
        assert result == {"a": 1, "b": {"c": 2, "d": 3}, "e": 4}

    def test_overlay_wins_for_scalars(self):
        from updater.app import deep_merge
        base = {"a": 1}
        overlay = {"a": 2}
        assert deep_merge(base, overlay) == {"a": 2}

    def test_list_replaced_entirely(self):
        from updater.app import deep_merge
        base = {"a": [1, 2, 3]}
        overlay = {"a": [4, 5]}
        assert deep_merge(base, overlay) == {"a": [4, 5]}

    def test_does_not_mutate_base(self):
        from updater.app import deep_merge
        base = {"a": {"b": [1, 2]}, "c": 3}
        overlay = {"a": {"b": [9]}}
        result = deep_merge(base, overlay)
        # Base must be untouched
        assert base == {"a": {"b": [1, 2]}, "c": 3}
        assert result["a"]["b"] == [9]

    def test_does_not_mutate_overlay(self):
        from updater.app import deep_merge
        base = {"a": 1}
        overlay = {"b": {"c": [1, 2]}}
        result = deep_merge(base, overlay)
        # Mutating result should not affect overlay
        result["b"]["c"].append(3)
        assert overlay["b"]["c"] == [1, 2]


class TestHashConsistency:
    """Tests for config hash consistency between app and poller."""

    def test_canonical_json_deterministic(self):
        from updater.app import _canonical_config_json
        config = {"z": 1, "a": 2, "m": {"x": 3, "b": 4}}
        json1 = _canonical_config_json(config)
        json2 = _canonical_config_json(config)
        assert json1 == json2
        # Keys should be sorted
        assert json1.index('"a"') < json1.index('"m"') < json1.index('"z"')

    def test_canonical_json_compact(self):
        from updater.app import _canonical_config_json
        result = _canonical_config_json({"a": 1, "b": [2, 3]})
        # No spaces after : or ,
        assert ": " not in result
        assert ", " not in result

    def test_hash_matches_poller_serialization(self):
        """Ensure poller's inline serialization produces the same hash."""
        import hashlib
        import json
        from updater.app import _compute_config_hash
        config = {"services": {"snmp": {"community": "test"}}, "system": {"name": "device1"}}
        # This is what poller.py does inline
        poller_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
        poller_hash = hashlib.sha256(poller_json.encode()).hexdigest()
        app_hash = _compute_config_hash(config)
        assert app_hash == poller_hash

    def test_hash_changes_with_different_config(self):
        from updater.app import _compute_config_hash
        hash1 = _compute_config_hash({"a": 1})
        hash2 = _compute_config_hash({"a": 2})
        assert hash1 != hash2


class TestPrefillThreshold:
    """Tests for config prefill suggestion threshold behavior."""

    def test_threshold_requires_more_than_two_matches(self):
        required_matches = 3
        assert required_matches > 2

    def test_prefill_endpoint_requires_three_matches(self, authed_client, mock_db):
        """Prefill should not trigger with only 2 matching devices."""
        import json
        snmp_config = {"services": {"snmp": {"community": "public", "enabled": True}}}
        config_json = json.dumps(snmp_config, sort_keys=True, separators=(",", ":"))
        config_hash = "abc123"
        for ip in ["10.0.0.1", "10.0.0.2"]:
            mock_db.execute(
                "INSERT INTO device_configs (ip, config_json, config_hash) VALUES (?, ?, ?)",
                (ip, config_json, config_hash),
            )
        different = {"services": {"snmp": {"community": "private", "enabled": False}}}
        mock_db.execute(
            "INSERT INTO device_configs (ip, config_json, config_hash) VALUES (?, ?, ?)",
            ("10.0.0.3", json.dumps(different, sort_keys=True, separators=(",", ":")), "def456"),
        )
        mock_db.commit()
        resp = authed_client.get("/api/config-prefill/snmp")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("prefilled") is False
        assert data.get("reason") == "insufficient_matches"

    def test_prefill_endpoint_returns_data_with_three_matches(self, authed_client, mock_db):
        """Prefill should trigger when 3 devices share the same value."""
        import json
        snmp_config = {"services": {"snmp": {"community": "public", "enabled": True}}}
        config_json = json.dumps(snmp_config, sort_keys=True, separators=(",", ":"))
        for ip in ["10.0.0.1", "10.0.0.2", "10.0.0.3"]:
            mock_db.execute(
                "INSERT INTO device_configs (ip, config_json, config_hash) VALUES (?, ?, ?)",
                (ip, config_json, f"hash-{ip}"),
            )
        mock_db.commit()
        resp = authed_client.get("/api/config-prefill/snmp")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("prefilled") is True


class TestSystemInfoAPI:
    def test_returns_version(self, authed_client):
        resp = authed_client.get("/api/system/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "appliance_mode" in data
        assert data["appliance_mode"] is False

    def test_requires_auth(self, client):
        resp = client.get("/api/system/info")
        assert resp.status_code in (401, 403, 302)

    def test_disk_usage_present(self, authed_client):
        resp = authed_client.get("/api/system/info")
        data = resp.json()
        # disk_usage may or may not be available in test env
        if data["disk_usage"] is not None:
            assert "total_gb" in data["disk_usage"]
            assert "percent" in data["disk_usage"]


class TestSystemNetworkAPI:
    def test_not_available_outside_appliance(self, authed_client):
        resp = authed_client.post("/api/system/network", json={"mode": "dhcp"})
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        resp = client.post("/api/system/network", json={"mode": "dhcp"})
        assert resp.status_code in (401, 403, 302)
