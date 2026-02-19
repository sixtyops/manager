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


class TestDeviceUpdateHistory:
    def test_save_and_get_by_ip(self, mock_db):
        db.save_device_update_history(
            job_id="job-1", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.0", new_version="1.1",
            model="TNA-30X", error=None, failed_stage=None,
            stages=[{"stage": "connecting", "started_at": "2026-01-01T00:00:00",
                     "completed_at": "2026-01-01T00:00:02", "success": True}],
            duration_seconds=120.5,
            started_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:02:00",
        )
        history = db.get_device_update_history(ip="10.0.0.1")
        assert len(history) == 1
        assert history[0]["status"] == "success"
        assert history[0]["ip"] == "10.0.0.1"
        assert history[0]["stages"][0]["stage"] == "connecting"
        assert history[0]["model"] == "TNA-30X"

    def test_get_empty(self, mock_db):
        history = db.get_device_update_history(ip="10.0.0.99")
        assert history == []

    def test_filter_by_status(self, mock_db):
        db.save_device_update_history(
            job_id="job-1", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.0", new_version="1.1",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:00",
        )
        db.save_device_update_history(
            job_id="job-2", ip="10.0.0.1", role="ap", pass_number=1,
            status="failed", old_version="1.0", new_version=None,
            model=None, error="Reboot timeout", failed_stage="rebooting",
            stages=[], duration_seconds=300,
            started_at="2026-01-02T00:00:00", completed_at="2026-01-02T00:05:00",
        )
        failed = db.get_device_update_history(status="failed")
        assert len(failed) == 1
        assert failed[0]["failed_stage"] == "rebooting"
        success = db.get_device_update_history(status="success")
        assert len(success) == 1

    def test_filter_by_action(self, mock_db):
        db.save_device_update_history(
            job_id="job-1", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.0", new_version="1.1",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:00", action="firmware_update",
        )
        db.save_device_update_history(
            job_id=None, ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version=None, new_version=None,
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=5, started_at="2026-01-02T00:00:00",
            completed_at="2026-01-02T00:00:05", action="config_push",
        )
        fw = db.get_device_update_history(action="firmware_update")
        assert len(fw) == 1
        cfg = db.get_device_update_history(action="config_push")
        assert len(cfg) == 1

    def test_ordering_newest_first(self, mock_db):
        db.save_device_update_history(
            job_id="job-old", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.0", new_version="1.1",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:00",
        )
        db.save_device_update_history(
            job_id="job-new", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.1", new_version="1.2",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2026-01-02T00:00:00",
            completed_at="2026-01-02T00:01:00",
        )
        history = db.get_device_update_history(ip="10.0.0.1")
        assert len(history) == 2
        assert history[0]["job_id"] == "job-new"  # newest first

    def test_pagination(self, mock_db):
        for i in range(5):
            db.save_device_update_history(
                job_id=f"job-{i}", ip="10.0.0.1", role="ap", pass_number=1,
                status="success", old_version="1.0", new_version="1.1",
                model=None, error=None, failed_stage=None, stages=[],
                duration_seconds=60,
                started_at=f"2026-01-0{i+1}T00:00:00",
                completed_at=f"2026-01-0{i+1}T00:01:00",
            )
        page1 = db.get_device_update_history(limit=2, offset=0)
        assert len(page1) == 2
        page2 = db.get_device_update_history(limit=2, offset=2)
        assert len(page2) == 2
        assert page1[0]["job_id"] != page2[0]["job_id"]

    def test_get_by_job(self, mock_db):
        db.save_device_update_history(
            job_id="job-1", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.0", new_version="1.1",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:00",
        )
        db.save_device_update_history(
            job_id="job-1", ip="10.0.0.2", role="cpe", pass_number=1,
            status="failed", old_version="1.0", new_version=None,
            model=None, error="Timeout", failed_stage="rebooting",
            stages=[], duration_seconds=300,
            started_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:05:00",
        )
        records = db.get_device_update_history_by_job("job-1")
        assert len(records) == 2

    def test_cleanup(self, mock_db):
        # Old record
        db.save_device_update_history(
            job_id="job-old", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.0", new_version="1.1",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2024-01-01T00:00:00",
            completed_at="2024-01-01T00:01:00",
        )
        # Recent record
        db.save_device_update_history(
            job_id="job-new", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.1", new_version="1.2",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:00",
        )
        db.cleanup_old_device_update_history(max_age_days=180)
        remaining = db.get_device_update_history()
        assert len(remaining) == 1
        assert remaining[0]["job_id"] == "job-new"

    def test_multi_pass(self, mock_db):
        db.save_device_update_history(
            job_id="job-1", ip="10.0.0.1", role="ap", pass_number=1,
            status="success", old_version="1.0", new_version="1.1",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:00",
        )
        db.save_device_update_history(
            job_id="job-1", ip="10.0.0.1", role="ap", pass_number=2,
            status="success", old_version="1.1", new_version="1.1",
            model=None, error=None, failed_stage=None, stages=[],
            duration_seconds=60, started_at="2026-01-01T00:02:00",
            completed_at="2026-01-01T00:03:00",
        )
        records = db.get_device_update_history_by_job("job-1")
        assert len(records) == 2
        assert records[0]["pass_number"] == 1
        assert records[1]["pass_number"] == 2


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
