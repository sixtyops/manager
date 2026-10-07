"""Tests for retained exports and retired report routes."""

from datetime import datetime

import pytest

from updater import database as db


# ---------------------------------------------------------------------------
# Database layer tests
# ---------------------------------------------------------------------------

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
        assert "stages_json" in rows[0]


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------

class TestRetiredReportAPI:
    """Test removed reports and retained protected routes."""

    @pytest.mark.parametrize("client_fixture", ["authed_client", "viewer_client"])
    @pytest.mark.parametrize("endpoint", ["update-summary", "fleet-status"])
    @pytest.mark.parametrize("query", ["", "?days=7", "?days=0", "?days=999", "?days=invalid"])
    def test_retired_report_returns_404(self, request, client_fixture, endpoint, query):
        client = request.getfixturevalue(client_fixture)
        assert client.get(f"/api/reports/{endpoint}{query}").status_code == 404

    @pytest.mark.parametrize("endpoint", ["update-summary", "fleet-status"])
    def test_retired_report_absent_without_auth(self, client, endpoint):
        assert client.get(f"/api/reports/{endpoint}").status_code == 404

    @pytest.mark.parametrize("client_fixture", ["authed_client", "viewer_client"])
    @pytest.mark.parametrize("path", [
        "/api/reports/export/jobs", "/api/reports/export/devices",
        "/api/uptime/fleet", "/api/fleet-status",
    ])
    def test_retained_routes_accessible(self, request, client_fixture, path):
        client = request.getfixturevalue(client_fixture)
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize("path", [
        "/api/reports/export/jobs", "/api/reports/export/devices",
        "/api/uptime/fleet", "/api/fleet-status",
    ])
    def test_retained_routes_require_auth(self, client, path):
        assert client.get(path, follow_redirects=False).status_code == 401


class TestReportingAPI:
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
