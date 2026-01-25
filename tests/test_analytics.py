"""Tests for the update analytics dashboard."""

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


class TestAnalyticsAPI:
    """Test the analytics API endpoints."""

    def test_summary_endpoint(self, authed_client):
        resp = authed_client.get("/api/analytics/summary?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_jobs" in data
        assert "success_rate" in data

    def test_trends_endpoint(self, authed_client):
        resp = authed_client.get("/api/analytics/trends?days=30")
        assert resp.status_code == 200
        assert "trends" in resp.json()

    def test_models_endpoint(self, authed_client):
        resp = authed_client.get("/api/analytics/models?days=90")
        assert resp.status_code == 200
        assert "models" in resp.json()

    def test_errors_endpoint(self, authed_client):
        resp = authed_client.get("/api/analytics/errors?days=90")
        assert resp.status_code == 200
        assert "errors" in resp.json()

    def test_reliability_endpoint(self, authed_client):
        resp = authed_client.get("/api/analytics/reliability?days=90")
        assert resp.status_code == 200
        assert "devices" in resp.json()

    def test_invalid_days_too_high(self, authed_client):
        resp = authed_client.get("/api/analytics/summary?days=999")
        assert resp.status_code == 400

    def test_invalid_days_zero(self, authed_client):
        resp = authed_client.get("/api/analytics/trends?days=0")
        assert resp.status_code == 400

    def test_viewer_can_read_analytics(self, viewer_client):
        resp = viewer_client.get("/api/analytics/summary?days=30")
        assert resp.status_code == 200

    def test_unauthenticated_denied(self):
        from fastapi.testclient import TestClient
        from updater.app import app
        client = TestClient(app)
        resp = client.get("/api/analytics/summary")
        assert resp.status_code in (401, 403, 307)
