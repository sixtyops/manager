"""Tests for SLA / Uptime Tracking."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


class TestUptimeDatabase:
    """Test uptime tracking database functions."""

    def test_record_uptime_event(self, mock_db):
        from updater.database import record_uptime_event, get_uptime_events
        record_uptime_event("10.0.0.1", "ap", "down", details="Connection refused")
        events = get_uptime_events("10.0.0.1", days=30)
        assert len(events) == 1
        assert events[0]["ip"] == "10.0.0.1"
        assert events[0]["device_type"] == "ap"
        assert events[0]["event"] == "down"
        assert events[0]["details"] == "Connection refused"

    def test_record_up_event(self, mock_db):
        from updater.database import record_uptime_event, get_uptime_events
        record_uptime_event("10.0.0.1", "ap", "down")
        record_uptime_event("10.0.0.1", "ap", "up")
        events = get_uptime_events("10.0.0.1", days=30)
        assert len(events) == 2
        assert events[0]["event"] == "up"  # Most recent first
        assert events[1]["event"] == "down"

    def test_get_uptime_events_filters_by_ip(self, mock_db):
        from updater.database import record_uptime_event, get_uptime_events
        record_uptime_event("10.0.0.1", "ap", "down")
        record_uptime_event("10.0.0.2", "ap", "down")
        events = get_uptime_events("10.0.0.1", days=30)
        assert len(events) == 1
        assert events[0]["ip"] == "10.0.0.1"

    def test_get_uptime_events_respects_limit(self, mock_db):
        from updater.database import record_uptime_event, get_uptime_events
        for i in range(5):
            record_uptime_event("10.0.0.1", "ap", "down" if i % 2 == 0 else "up")
        events = get_uptime_events("10.0.0.1", days=30, limit=3)
        assert len(events) == 3

    def test_device_availability_no_events(self, mock_db):
        from updater.database import get_device_availability
        result = get_device_availability("10.0.0.1", days=30)
        assert result["ip"] == "10.0.0.1"
        assert result["availability_pct"] == 100.0
        assert result["downtime_seconds"] == 0
        assert result["events"] == 0

    def test_device_availability_with_downtime(self, mock_db):
        from updater.database import get_device_availability
        now = datetime.now()
        # Device went down 2 hours ago and came back up 1 hour ago
        mock_db.execute(
            "INSERT INTO device_uptime_events (ip, device_type, event, occurred_at) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "ap", "down", (now - timedelta(hours=2)).isoformat()),
        )
        mock_db.execute(
            "INSERT INTO device_uptime_events (ip, device_type, event, occurred_at) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "ap", "up", (now - timedelta(hours=1)).isoformat()),
        )
        mock_db.commit()
        result = get_device_availability("10.0.0.1", days=1)
        assert result["availability_pct"] < 100.0
        assert result["downtime_seconds"] > 0
        assert result["events"] == 2

    def test_device_availability_currently_down(self, mock_db):
        from updater.database import get_device_availability
        now = datetime.now()
        # Device went down 1 hour ago, still down
        mock_db.execute(
            "INSERT INTO device_uptime_events (ip, device_type, event, occurred_at) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "ap", "down", (now - timedelta(hours=1)).isoformat()),
        )
        mock_db.commit()
        result = get_device_availability("10.0.0.1", days=1)
        assert result["availability_pct"] < 100.0
        assert result["downtime_seconds"] >= 3500  # ~1 hour

    def test_fleet_availability(self, mock_db):
        from updater.database import record_uptime_event, get_fleet_availability
        record_uptime_event("10.0.0.1", "ap", "down")
        record_uptime_event("10.0.0.2", "switch", "down")
        # All devices
        result = get_fleet_availability(days=30)
        assert len(result) == 2
        # Filter by type
        result = get_fleet_availability(device_type="ap", days=30)
        assert len(result) == 1
        assert result[0]["ip"] == "10.0.0.1"

    def test_fleet_availability_sorted_by_worst(self, mock_db):
        from updater.database import get_fleet_availability
        now = datetime.now()
        # Device 1: down for 2 hours (worse)
        mock_db.execute(
            "INSERT INTO device_uptime_events (ip, device_type, event, occurred_at) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "ap", "down", (now - timedelta(hours=2)).isoformat()),
        )
        # Device 2: down for 30 minutes (better)
        mock_db.execute(
            "INSERT INTO device_uptime_events (ip, device_type, event, occurred_at) VALUES (?, ?, ?, ?)",
            ("10.0.0.2", "ap", "down", (now - timedelta(minutes=30)).isoformat()),
        )
        mock_db.commit()
        result = get_fleet_availability(days=1)
        assert len(result) == 2
        # Worst first
        assert result[0]["ip"] == "10.0.0.1"
        assert result[0]["availability_pct"] < result[1]["availability_pct"]

    def test_cleanup_old_events(self, mock_db):
        from updater.database import cleanup_old_uptime_events
        old_date = (datetime.now() - timedelta(days=200)).isoformat()
        recent_date = datetime.now().isoformat()
        mock_db.execute(
            "INSERT INTO device_uptime_events (ip, device_type, event, occurred_at) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "ap", "down", old_date),
        )
        mock_db.execute(
            "INSERT INTO device_uptime_events (ip, device_type, event, occurred_at) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "ap", "up", recent_date),
        )
        mock_db.commit()
        cleanup_old_uptime_events(max_age_days=180)
        rows = mock_db.execute("SELECT COUNT(*) FROM device_uptime_events").fetchone()[0]
        assert rows == 1


class TestPollerUptimeTransition:
    """Test uptime transition detection in poller."""

    def test_transition_down(self):
        from updater.poller import NetworkPoller
        poller = NetworkPoller.__new__(NetworkPoller)
        with patch("updater.database.record_uptime_event") as mock_record:
            poller._check_uptime_transition("10.0.0.1", "ap", None, "Connection refused")
            mock_record.assert_called_once_with("10.0.0.1", "ap", "down", details="Connection refused")

    def test_transition_up(self):
        from updater.poller import NetworkPoller
        poller = NetworkPoller.__new__(NetworkPoller)
        with patch("updater.database.record_uptime_event") as mock_record:
            poller._check_uptime_transition("10.0.0.1", "ap", "was down", None)
            mock_record.assert_called_once_with("10.0.0.1", "ap", "up")

    def test_no_transition_still_up(self):
        from updater.poller import NetworkPoller
        poller = NetworkPoller.__new__(NetworkPoller)
        with patch("updater.database.record_uptime_event") as mock_record:
            poller._check_uptime_transition("10.0.0.1", "ap", None, None)
            mock_record.assert_not_called()

    def test_no_transition_still_down(self):
        from updater.poller import NetworkPoller
        poller = NetworkPoller.__new__(NetworkPoller)
        with patch("updater.database.record_uptime_event") as mock_record:
            poller._check_uptime_transition("10.0.0.1", "ap", "err1", "err2")
            mock_record.assert_not_called()


class TestUptimeAPI:
    """Test uptime API endpoints."""

    def test_device_uptime_endpoint(self, authed_client):
        resp = authed_client.get("/api/uptime/device?ip=10.0.0.1&days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert "availability_pct" in data
        assert "downtime_seconds" in data

    def test_fleet_uptime_endpoint(self, authed_client):
        resp = authed_client.get("/api/uptime/fleet?days=30")
        assert resp.status_code == 200
        assert "devices" in resp.json()

    def test_fleet_uptime_filter_type(self, authed_client):
        resp = authed_client.get("/api/uptime/fleet?device_type=ap&days=30")
        assert resp.status_code == 200

    def test_fleet_uptime_invalid_type(self, authed_client):
        resp = authed_client.get("/api/uptime/fleet?device_type=invalid")
        assert resp.status_code == 400

    def test_uptime_events_endpoint(self, authed_client):
        resp = authed_client.get("/api/uptime/events?ip=10.0.0.1&days=30")
        assert resp.status_code == 200
        assert "events" in resp.json()

    def test_uptime_invalid_days(self, authed_client):
        resp = authed_client.get("/api/uptime/device?ip=10.0.0.1&days=0")
        assert resp.status_code == 400

    def test_uptime_days_too_high(self, authed_client):
        resp = authed_client.get("/api/uptime/device?ip=10.0.0.1&days=999")
        assert resp.status_code == 400

    def test_viewer_can_read_uptime(self, viewer_client):
        resp = viewer_client.get("/api/uptime/fleet?days=30")
        assert resp.status_code == 200
