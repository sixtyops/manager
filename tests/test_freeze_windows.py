"""Tests for maintenance freeze windows."""

from datetime import datetime, timedelta

import pytest


class TestFreezeWindowDB:
    """Tests for freeze window database operations."""

    def test_create_freeze_window(self, mock_db):
        from updater import database as db
        wid = db.create_freeze_window("Holiday Freeze", "2026-12-20", "2026-12-31", "Holiday maintenance window")
        assert wid > 0

    def test_list_freeze_windows(self, mock_db):
        from updater import database as db
        db.create_freeze_window("Window 1", "2026-01-01", "2026-01-05")
        db.create_freeze_window("Window 2", "2026-06-01", "2026-06-05")
        windows = db.list_freeze_windows()
        assert len(windows) == 2

    def test_get_freeze_window(self, mock_db):
        from updater import database as db
        wid = db.create_freeze_window("Test", "2026-03-01", "2026-03-10", "test reason")
        window = db.get_freeze_window(wid)
        assert window["name"] == "Test"
        assert window["reason"] == "test reason"
        assert window["enabled"] == 1

    def test_update_freeze_window(self, mock_db):
        from updater import database as db
        wid = db.create_freeze_window("Old Name", "2026-01-01", "2026-01-05")
        db.update_freeze_window(wid, name="New Name", enabled=0)
        window = db.get_freeze_window(wid)
        assert window["name"] == "New Name"
        assert window["enabled"] == 0

    def test_delete_freeze_window(self, mock_db):
        from updater import database as db
        wid = db.create_freeze_window("Delete Me", "2026-01-01", "2026-01-05")
        assert db.delete_freeze_window(wid)
        assert db.get_freeze_window(wid) is None

    def test_delete_nonexistent(self, mock_db):
        from updater import database as db
        assert not db.delete_freeze_window(999)

    def test_is_in_freeze_window_active(self, mock_db):
        from updater import database as db
        now = datetime.now()
        start = (now - timedelta(days=1)).isoformat()
        end = (now + timedelta(days=1)).isoformat()
        db.create_freeze_window("Active", start, end)
        freeze = db.is_in_freeze_window()
        assert freeze is not None
        assert freeze["name"] == "Active"

    def test_is_in_freeze_window_not_active(self, mock_db):
        from updater import database as db
        future_start = (datetime.now() + timedelta(days=10)).isoformat()
        future_end = (datetime.now() + timedelta(days=20)).isoformat()
        db.create_freeze_window("Future", future_start, future_end)
        freeze = db.is_in_freeze_window()
        assert freeze is None

    def test_is_in_freeze_window_disabled(self, mock_db):
        from updater import database as db
        now = datetime.now()
        start = (now - timedelta(days=1)).isoformat()
        end = (now + timedelta(days=1)).isoformat()
        wid = db.create_freeze_window("Disabled", start, end)
        db.update_freeze_window(wid, enabled=0)
        freeze = db.is_in_freeze_window()
        assert freeze is None

    def test_is_in_freeze_window_with_specific_time(self, mock_db):
        from updater import database as db
        db.create_freeze_window("Specific", "2026-06-01T00:00:00", "2026-06-15T23:59:59")
        assert db.is_in_freeze_window("2026-06-10T12:00:00") is not None
        assert db.is_in_freeze_window("2026-05-31T23:59:59") is None
        assert db.is_in_freeze_window("2026-06-16T00:00:00") is None


class TestFreezeWindowAPI:
    """Tests for freeze window API routes."""

    def test_list_empty(self, authed_client):
        resp = authed_client.get("/api/freeze-windows")
        assert resp.status_code == 200
        data = resp.json()
        assert data["windows"] == []
        assert data["active_freeze"] is None

    def test_create_window(self, authed_client):
        resp = authed_client.post("/api/freeze-windows", json={
            "name": "API Test",
            "start_date": "2026-12-01",
            "end_date": "2026-12-15",
            "reason": "Testing",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "API Test"

    def test_create_validation_no_name(self, authed_client):
        resp = authed_client.post("/api/freeze-windows", json={
            "name": "", "start_date": "2026-01-01", "end_date": "2026-01-05",
        })
        assert resp.status_code == 400

    def test_create_validation_end_before_start(self, authed_client):
        resp = authed_client.post("/api/freeze-windows", json={
            "name": "Bad", "start_date": "2026-06-01", "end_date": "2026-05-01",
        })
        assert resp.status_code == 400

    def test_delete_window(self, authed_client):
        resp = authed_client.post("/api/freeze-windows", json={
            "name": "Del", "start_date": "2026-01-01", "end_date": "2026-01-05",
        })
        wid = resp.json()["id"]
        resp = authed_client.delete(f"/api/freeze-windows/{wid}")
        assert resp.status_code == 200

    def test_delete_nonexistent(self, authed_client):
        resp = authed_client.delete("/api/freeze-windows/9999")
        assert resp.status_code == 404

    def test_update_window(self, authed_client):
        resp = authed_client.post("/api/freeze-windows", json={
            "name": "Update", "start_date": "2026-01-01", "end_date": "2026-01-05",
        })
        wid = resp.json()["id"]
        resp = authed_client.put(f"/api/freeze-windows/{wid}", json={"name": "Updated"})
        assert resp.status_code == 200

    def test_viewer_cannot_create(self, viewer_client):
        resp = viewer_client.post("/api/freeze-windows", json={
            "name": "Nope", "start_date": "2026-01-01", "end_date": "2026-01-05",
        })
        assert resp.status_code == 403
