"""Tests for API routes (authenticated)."""

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
             patch("updater.app.asyncio") as mock_asyncio:
            mock_asyncio.create_task = MagicMock()
            resp = authed_client.post("/api/start-update", data={
                "firmware_file": "tachyon-v1.12.3.bin",
                "device_type": "mixed",
                "ip_list": "10.0.0.1",
                "concurrency": "2",
                "bank_mode": "both",
            })

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
