"""Tests for updater.database."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from updater import database as db


class TestTowerSites:
    def test_create_and_get(self, mock_db):
        site_id = db.create_tower_site("Site A", "Location A", 40.0, -90.0)
        assert site_id is not None
        site = db.get_tower_site(site_id)
        assert site["name"] == "Site A"
        assert site["location"] == "Location A"

    def test_list(self, mock_db):
        db.create_tower_site("Site B")
        db.create_tower_site("Site A")
        sites = db.get_tower_sites()
        assert len(sites) == 2
        assert sites[0]["name"] == "Site A"  # ordered by name

    def test_update(self, mock_db):
        site_id = db.create_tower_site("Old Name")
        db.update_tower_site(site_id, name="New Name")
        site = db.get_tower_site(site_id)
        assert site["name"] == "New Name"

    def test_delete(self, mock_db):
        site_id = db.create_tower_site("ToDelete")
        db.delete_tower_site(site_id)
        assert db.get_tower_site(site_id) is None

    def test_unique_constraint(self, mock_db):
        db.create_tower_site("Unique")
        with pytest.raises(Exception, match="UNIQUE"):
            db.create_tower_site("Unique")


class TestAccessPoints:
    def test_create_and_list(self, mock_db):
        ap_id = db.upsert_access_point("10.0.0.1", "root", "pass")
        assert ap_id is not None
        aps = db.get_access_points(enabled_only=False)
        assert len(aps) == 1
        assert aps[0]["ip"] == "10.0.0.1"

    def test_upsert_updates(self, mock_db):
        db.upsert_access_point("10.0.0.1", "root", "pass")
        db.upsert_access_point("10.0.0.1", "admin", "newpass")
        aps = db.get_access_points(enabled_only=False)
        assert len(aps) == 1
        assert aps[0]["username"] == "admin"

    def test_filter_by_site(self, mock_db):
        site_id = db.create_tower_site("TestSite")
        db.upsert_access_point("10.0.0.1", "root", "pass", site_id)
        db.upsert_access_point("10.0.0.2", "root", "pass")
        aps = db.get_access_points(tower_site_id=site_id, enabled_only=False)
        assert len(aps) == 1
        assert aps[0]["ip"] == "10.0.0.1"

    def test_enabled_only(self, mock_db):
        db.upsert_access_point("10.0.0.1", "root", "pass")
        # Disable the first AP via direct update, then upsert to set enabled=0
        db.upsert_access_point("10.0.0.1", "root", "pass", enabled=0)
        db.upsert_access_point("10.0.0.2", "root", "pass")
        aps = db.get_access_points(enabled_only=True)
        assert len(aps) == 1
        assert aps[0]["ip"] == "10.0.0.2"

    def test_delete(self, mock_db):
        db.upsert_access_point("10.0.0.1", "root", "pass")
        db.delete_access_point("10.0.0.1")
        assert db.get_access_point("10.0.0.1") is None


class TestCPECache:
    def test_upsert_and_get(self, mock_db):
        db.upsert_cpe("10.0.0.1", {"ip": "1.1.1.1", "signal_health": "green"})
        cpes = db.get_cpes_for_ap("10.0.0.1")
        assert len(cpes) == 1
        assert cpes[0]["ip"] == "1.1.1.1"

    def test_upsert_updates(self, mock_db):
        db.upsert_cpe("10.0.0.1", {"ip": "1.1.1.1", "signal_health": "green"})
        db.upsert_cpe("10.0.0.1", {"ip": "1.1.1.1", "signal_health": "red"})
        cpes = db.get_cpes_for_ap("10.0.0.1")
        assert len(cpes) == 1
        assert cpes[0]["signal_health"] == "red"

    def test_clear(self, mock_db):
        db.upsert_cpe("10.0.0.1", {"ip": "1.1.1.1", "signal_health": "green"})
        db.clear_cpes_for_ap("10.0.0.1")
        assert len(db.get_cpes_for_ap("10.0.0.1")) == 0


class TestSettings:
    def test_get_default(self, mock_db):
        val = db.get_setting("schedule_enabled")
        assert val == "false"

    def test_get_missing(self, mock_db):
        assert db.get_setting("nonexistent") is None
        assert db.get_setting("nonexistent", "default") == "default"

    def test_set_and_get(self, mock_db):
        db.set_setting("custom_key", "custom_value")
        assert db.get_setting("custom_key") == "custom_value"

    def test_batch_set(self, mock_db):
        db.set_settings({"key1": "val1", "key2": "val2"})
        assert db.get_setting("key1") == "val1"
        assert db.get_setting("key2") == "val2"

    def test_get_all(self, mock_db):
        settings = db.get_all_settings()
        assert "schedule_enabled" in settings
        assert settings["schedule_enabled"] == "false"


class TestHealthSummary:
    def test_summary(self, mock_db):
        db.upsert_cpe("10.0.0.1", {"ip": "1.1.1.1", "signal_health": "green"})
        db.upsert_cpe("10.0.0.1", {"ip": "1.1.1.2", "signal_health": "yellow"})
        db.upsert_cpe("10.0.0.1", {"ip": "1.1.1.3", "signal_health": "red"})
        summary = db.get_health_summary()
        assert summary == {"green": 1, "yellow": 1, "red": 1}


class TestSessions:
    def test_create_and_get(self, mock_db):
        expires = (datetime.now() + timedelta(hours=24)).isoformat()
        db.create_session("sess-1", "admin", "127.0.0.1", expires)
        session = db.get_session("sess-1")
        assert session is not None
        assert session["username"] == "admin"

    def test_get_expired(self, mock_db):
        expires = (datetime.now() - timedelta(hours=1)).isoformat()
        db.create_session("sess-expired", "admin", "127.0.0.1", expires)
        assert db.get_session("sess-expired") is None

    def test_get_nonexistent(self, mock_db):
        assert db.get_session("does-not-exist") is None

    def test_delete(self, mock_db):
        expires = (datetime.now() + timedelta(hours=24)).isoformat()
        db.create_session("sess-del", "admin", "127.0.0.1", expires)
        db.delete_session("sess-del")
        assert db.get_session("sess-del") is None

    def test_cleanup_expired(self, mock_db):
        future = (datetime.now() + timedelta(hours=24)).isoformat()
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        db.create_session("sess-valid", "admin", "127.0.0.1", future)
        db.create_session("sess-old", "admin", "127.0.0.1", past)
        db.cleanup_expired_sessions()
        assert db.get_session("sess-valid") is not None
        # The expired one was cleaned up - verify by direct query
        row = mock_db.execute("SELECT * FROM sessions WHERE session_id = 'sess-old'").fetchone()
        assert row is None
