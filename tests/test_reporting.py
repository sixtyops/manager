"""Tests for reporting feature."""

import json
from datetime import datetime, timedelta

from updater import database as db


# ---------------------------------------------------------------------------
# Database layer tests
# ---------------------------------------------------------------------------

class TestUpdateSummary:
    def test_empty_summary(self, mock_db):
        summary = db.get_update_summary(30)
        assert summary["total_jobs"] == 0
        assert summary["total_device_success"] == 0
        assert summary["period_days"] == 30

    def test_summary_with_jobs(self, mock_db):
        now = datetime.now().isoformat()
        mock_db.execute(
            "INSERT INTO job_history (job_id, started_at, completed_at, duration, "
            "bank_mode, success_count, failed_count, skipped_count, cancelled_count, "
            "devices_json, ap_cpe_map_json, device_roles_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job1", now, now, 120.0, "both", 5, 1, 0, 0, "[]", "{}", "{}"),
        )
        mock_db.execute(
            "INSERT INTO job_history (job_id, started_at, completed_at, duration, "
            "bank_mode, success_count, failed_count, skipped_count, cancelled_count, "
            "devices_json, ap_cpe_map_json, device_roles_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job2", now, now, 60.0, "both", 3, 0, 2, 0, "[]", "{}", "{}"),
        )
        mock_db.commit()

        summary = db.get_update_summary(30)
        assert summary["total_jobs"] == 2
        assert summary["total_device_success"] == 8
        assert summary["total_device_failed"] == 1
        assert summary["total_device_skipped"] == 2

    def test_summary_respects_days_filter(self, mock_db):
        old = (datetime.now() - timedelta(days=60)).isoformat()
        mock_db.execute(
            "INSERT INTO job_history (job_id, started_at, completed_at, duration, "
            "bank_mode, success_count, failed_count, skipped_count, cancelled_count, "
            "devices_json, ap_cpe_map_json, device_roles_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old_job", old, old, 60.0, "both", 10, 0, 0, 0, "[]", "{}", "{}"),
        )
        mock_db.commit()

        summary = db.get_update_summary(30)
        assert summary["total_jobs"] == 0

    def test_summary_with_device_history(self, mock_db):
        now = datetime.now().isoformat()
        for i, status in enumerate(["success", "success", "failed"]):
            mock_db.execute(
                "INSERT INTO device_update_history (job_id, ip, role, status, "
                "duration_seconds, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("j1", f"10.0.0.{i}", "ap", status, 30.0 + i * 10, now),
            )
        mock_db.commit()

        summary = db.get_update_summary(30)
        assert summary["device_updates"] == 3
        assert summary["device_success"] == 2
        assert summary["device_failed"] == 1
        assert summary["unique_devices_updated"] == 2


class TestFleetStatus:
    def test_empty_fleet(self, mock_db):
        status = db.get_fleet_status()
        assert status["access_points"]["total"] == 0
        assert status["switches"]["total"] == 0

    def test_fleet_with_devices(self, mock_db):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, firmware_version, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.1", "u", "p", "3.5.1", 1),
        )
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, firmware_version, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.2", "u", "p", "3.5.1", 1),
        )
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, firmware_version, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.3", "u", "p", "3.4.0", 1),
        )
        mock_db.execute(
            "INSERT INTO switches (ip, username, password, firmware_version, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.1.1", "u", "p", "2.0.0", 1),
        )
        mock_db.commit()

        status = db.get_fleet_status()
        assert status["access_points"]["total"] == 3
        assert len(status["access_points"]["versions"]) == 2
        # Most common version first
        assert status["access_points"]["versions"][0]["version"] == "3.5.1"
        assert status["access_points"]["versions"][0]["count"] == 2
        assert status["switches"]["total"] == 1

    def test_disabled_devices_excluded(self, mock_db):
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, firmware_version, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("10.0.0.9", "u", "p", "3.5.1", 0),
        )
        mock_db.commit()
        status = db.get_fleet_status()
        assert status["access_points"]["total"] == 0


class TestCSVExport:
    def test_job_csv_rows_empty(self, mock_db):
        rows = db.get_job_history_csv_rows(30)
        assert rows == []

    def test_job_csv_rows(self, mock_db):
        now = datetime.now().isoformat()
        mock_db.execute(
            "INSERT INTO job_history (job_id, started_at, completed_at, duration, "
            "bank_mode, success_count, failed_count, skipped_count, cancelled_count, "
            "devices_json, ap_cpe_map_json, device_roles_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", now, now, 90.0, "both", 3, 0, 0, 0, "[]", "{}", "{}"),
        )
        mock_db.commit()

        rows = db.get_job_history_csv_rows(30)
        assert len(rows) == 1
        assert rows[0]["job_id"] == "j1"
        # CSV rows should NOT include raw JSON columns
        assert "devices_json" not in rows[0]

    def test_device_csv_rows(self, mock_db):
        now = datetime.now().isoformat()
        mock_db.execute(
            "INSERT INTO device_update_history (job_id, ip, role, action, status, "
            "old_version, new_version, duration_seconds, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", "10.0.0.1", "ap", "firmware_update", "success",
             "3.4.0", "3.5.1", 45.0, now),
        )
        mock_db.commit()

        rows = db.get_device_history_csv_rows(30)
        assert len(rows) == 1
        assert rows[0]["ip"] == "10.0.0.1"
        assert "stages_json" not in rows[0]


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------

class TestReportingAPI:
    def test_update_summary(self, authed_client):
        resp = authed_client.get("/api/reports/update-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_jobs" in data
        assert "period_days" in data

    def test_update_summary_custom_days(self, authed_client):
        resp = authed_client.get("/api/reports/update-summary?days=7")
        assert resp.status_code == 200
        assert resp.json()["period_days"] == 7

    def test_update_summary_invalid_days(self, authed_client):
        resp = authed_client.get("/api/reports/update-summary?days=0")
        assert resp.status_code == 400

    def test_update_summary_days_too_large(self, authed_client):
        resp = authed_client.get("/api/reports/update-summary?days=999")
        assert resp.status_code == 400

    def test_fleet_status(self, authed_client):
        resp = authed_client.get("/api/reports/fleet-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "access_points" in data
        assert "switches" in data

    def test_export_jobs_csv_empty(self, authed_client):
        resp = authed_client.get("/api/reports/export/jobs")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "No data" in resp.text

    def test_export_jobs_csv_with_data(self, authed_client, mock_db):
        now = datetime.now().isoformat()
        mock_db.execute(
            "INSERT INTO job_history (job_id, started_at, completed_at, duration, "
            "bank_mode, success_count, failed_count, skipped_count, cancelled_count, "
            "devices_json, ap_cpe_map_json, device_roles_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", now, now, 90.0, "both", 3, 0, 0, 0, "[]", "{}", "{}"),
        )
        mock_db.commit()

        resp = authed_client.get("/api/reports/export/jobs")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "job_id" in resp.text
        assert "j1" in resp.text

    def test_export_devices_csv_empty(self, authed_client):
        resp = authed_client.get("/api/reports/export/devices")
        assert resp.status_code == 200
        assert "No data" in resp.text

    def test_export_devices_csv_with_data(self, authed_client, mock_db):
        now = datetime.now().isoformat()
        mock_db.execute(
            "INSERT INTO device_update_history (job_id, ip, role, action, status, "
            "duration_seconds, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("j1", "10.0.0.1", "ap", "firmware_update", "success", 45.0, now),
        )
        mock_db.commit()

        resp = authed_client.get("/api/reports/export/devices")
        assert resp.status_code == 200
        assert "10.0.0.1" in resp.text
        assert "content-disposition" in resp.headers

    def test_export_invalid_days(self, authed_client):
        resp = authed_client.get("/api/reports/export/jobs?days=0")
        assert resp.status_code == 400

    def test_unauthenticated_denied(self, client):
        resp = client.get("/api/reports/update-summary")
        assert resp.status_code == 401

    def test_viewer_can_access(self, viewer_client):
        resp = viewer_client.get("/api/reports/update-summary")
        assert resp.status_code == 200
