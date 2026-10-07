"""Tests for retained analytics queries and retired API routes."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


class TestAnalyticsSummaryDB:
    """Test the database analytics query functions."""

    def test_empty_summary(self, memory_db):
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_summary
            result = get_analytics_summary(90)
            assert result["total_jobs"] == 0
            assert result["success_rate"] == 0.0

    def test_summary_with_data(self, memory_db):
        now = datetime.now().isoformat()
        with memory_db as conn:
            conn.execute(
                "INSERT INTO job_history (job_id, started_at, completed_at, duration, success_count, failed_count, skipped_count, cancelled_count, devices_json, ap_cpe_map_json, device_roles_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("j1", now, now, 120.0, 5, 1, 0, 0, "{}", "{}", "{}")
            )
            conn.execute(
                "INSERT INTO job_history (job_id, started_at, completed_at, duration, success_count, failed_count, skipped_count, cancelled_count, devices_json, ap_cpe_map_json, device_roles_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("j2", now, now, 60.0, 3, 0, 1, 0, "{}", "{}", "{}")
            )
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_summary
            result = get_analytics_summary(90)
            assert result["total_jobs"] == 2
            assert result["total_success"] == 8
            assert result["total_failed"] == 1
            assert result["success_rate"] == pytest.approx(88.9, abs=0.1)

    def test_trends_empty(self, memory_db):
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_trends
            result = get_analytics_trends(30)
            assert result == []

    def test_trends_with_data(self, memory_db):
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().isoformat()
        with memory_db as conn:
            conn.execute(
                "INSERT INTO job_history (job_id, started_at, completed_at, duration, success_count, failed_count, skipped_count, cancelled_count, devices_json, ap_cpe_map_json, device_roles_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("j1", now, now, 100, 3, 1, 0, 0, "{}", "{}", "{}")
            )
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_trends
            result = get_analytics_trends(7)
            assert len(result) == 1
            assert result[0]["date"] == today
            assert result[0]["success"] == 3
            assert result[0]["failed"] == 1

    def test_by_model_empty(self, memory_db):
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_by_model
            result = get_analytics_by_model(90)
            assert result == []

    def test_by_model_with_data(self, memory_db):
        now = datetime.now().isoformat()
        with memory_db as conn:
            conn.execute(
                "INSERT INTO device_update_history (job_id, ip, role, action, pass_number, status, model, duration_seconds, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("j1", "10.0.0.1", "ap", "firmware_update", 1, "success", "T5c", 120.0, now, now)
            )
            conn.execute(
                "INSERT INTO device_update_history (job_id, ip, role, action, pass_number, status, model, duration_seconds, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("j1", "10.0.0.2", "ap", "firmware_update", 1, "failed", "T5c", 60.0, now, now)
            )
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_by_model
            result = get_analytics_by_model(90)
            assert len(result) == 1
            assert result[0]["model"] == "T5c"
            assert result[0]["success"] == 1
            assert result[0]["failed"] == 1

    def test_errors_empty(self, memory_db):
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_errors
            result = get_analytics_errors(90)
            assert result == []

    def test_errors_with_data(self, memory_db):
        now = datetime.now().isoformat()
        with memory_db as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO device_update_history (job_id, ip, role, action, pass_number, status, error, failed_stage, duration_seconds, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"j{i}", f"10.0.0.{i}", "ap", "firmware_update", 1, "failed", "Connection timeout", "upload", 10.0, now, now)
                )
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_errors
            result = get_analytics_errors(90)
            assert len(result) == 1
            assert result[0]["error"] == "Connection timeout"
            assert result[0]["count"] == 3

    def test_reliability_needs_min_updates(self, memory_db):
        now = datetime.now().isoformat()
        with memory_db as conn:
            # Only one update for this device - shouldn't appear
            conn.execute(
                "INSERT INTO device_update_history (job_id, ip, role, action, pass_number, status, model, duration_seconds, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("j1", "10.0.0.1", "ap", "firmware_update", 1, "failed", "T5c", 10.0, now, now)
            )
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_device_reliability
            result = get_analytics_device_reliability(90)
            assert len(result) == 0  # Need >= 2 updates

    def test_reliability_with_enough_data(self, memory_db):
        now = datetime.now().isoformat()
        with memory_db as conn:
            conn.execute(
                "INSERT INTO device_update_history (job_id, ip, role, action, pass_number, status, model, duration_seconds, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("j1", "10.0.0.1", "ap", "firmware_update", 1, "failed", "T5c", 10.0, now, now)
            )
            conn.execute(
                "INSERT INTO device_update_history (job_id, ip, role, action, pass_number, status, model, duration_seconds, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("j2", "10.0.0.1", "ap", "firmware_update", 1, "success", "T5c", 120.0, now, now)
            )
        with patch("updater.database.get_db", return_value=memory_db):
            from updater.database import get_analytics_device_reliability
            result = get_analytics_device_reliability(90)
            assert len(result) == 1
            assert result[0]["ip"] == "10.0.0.1"
            assert result[0]["failed"] == 1
            assert result[0]["success"] == 1


class TestRetiredAnalyticsAPI:
    """Retired analytics routes stay absent for both read roles."""

    @pytest.mark.parametrize("client_fixture", ["authed_client", "viewer_client"])
    @pytest.mark.parametrize("endpoint", ["summary", "trends", "models", "errors", "reliability"])
    @pytest.mark.parametrize("query", ["", "?days=30&limit=10", "?days=0", "?days=999", "?days=invalid"])
    def test_retired_endpoint_returns_404(self, request, client_fixture, endpoint, query):
        client = request.getfixturevalue(client_fixture)
        resp = client.get(f"/api/analytics/{endpoint}{query}")
        assert resp.status_code == 404

    def test_retired_route_absent_without_auth(self, client):
        resp = client.get("/api/analytics/summary", follow_redirects=False)
        assert resp.status_code == 404
        retained = client.get("/api/uptime/fleet", follow_redirects=False)
        assert retained.status_code == 401
