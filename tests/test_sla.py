"""Tests for SLA / Uptime Tracking."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


class TestUptimeDatabase:
    """Test uptime tracking database functions."""

    def test_record_uptime_event(self, mock_db):
        from updater.database import record_uptime_event
        record_uptime_event("10.0.0.1", "ap", "down", details="Connection refused")
        events = mock_db.execute(
            "SELECT * FROM device_uptime_events WHERE ip = ? ORDER BY id DESC",
            ("10.0.0.1",),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["ip"] == "10.0.0.1"
        assert events[0]["device_type"] == "ap"
        assert events[0]["event"] == "down"
        assert events[0]["details"] == "Connection refused"

    def test_record_up_event(self, mock_db):
        from updater.database import record_uptime_event
        record_uptime_event("10.0.0.1", "ap", "down")
        record_uptime_event("10.0.0.1", "ap", "up")
        events = mock_db.execute(
            "SELECT * FROM device_uptime_events WHERE ip = ? ORDER BY id DESC",
            ("10.0.0.1",),
        ).fetchall()
        assert len(events) == 2
        assert events[0]["event"] == "up"  # Most recent first
        assert events[1]["event"] == "down"

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

    def test_uptime_invalid_days(self, authed_client):
        resp = authed_client.get("/api/uptime/fleet?days=0")
        assert resp.status_code == 400

    def test_uptime_days_too_high(self, authed_client):
        resp = authed_client.get("/api/uptime/fleet?days=999")
        assert resp.status_code == 400

    def test_viewer_can_read_uptime(self, viewer_client):
        resp = viewer_client.get("/api/uptime/fleet?days=30")
        assert resp.status_code == 200


    @pytest.mark.parametrize("client_fixture", ["client", "authed_client", "viewer_client"])
    @pytest.mark.parametrize("path", [
        "/api/uptime/device?ip=10.0.0.1&days=30",
        "/api/uptime/events?ip=10.0.0.1&days=30&limit=25",
    ])
    def test_retired_uptime_route_returns_404(self, request, client_fixture, path):
        client = request.getfixturevalue(client_fixture)
        assert client.get(path, follow_redirects=False).status_code == 404

    def test_fleet_requires_auth(self, client):
        assert client.get("/api/uptime/fleet", follow_redirects=False).status_code == 401

    @pytest.mark.parametrize("client_fixture", ["authed_client", "viewer_client"])
    def test_fleet_filter_reads_retained_events(self, request, client_fixture, mock_db):
        from updater.database import record_uptime_event

        record_uptime_event("10.0.0.1", "ap", "up")
        record_uptime_event("10.0.0.2", "switch", "up")
        client = request.getfixturevalue(client_fixture)
        all_devices = client.get("/api/uptime/fleet?days=30")
        assert all_devices.status_code == 200
        assert {row["ip"] for row in all_devices.json()["devices"]} == {"10.0.0.1", "10.0.0.2"}
        filtered = client.get("/api/uptime/fleet?device_type=ap&days=30")
        assert filtered.status_code == 200
        assert filtered.json()["devices"] == [{
            "ip": "10.0.0.1", "device_type": "ap", "availability_pct": 100.0,
            "downtime_seconds": 0, "events": 1, "window_days": 30,
        }]
