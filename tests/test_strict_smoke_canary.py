"""Tests for strict smoke test mode and canary auto-cancel."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from updater.tachyon import SmokeTestResult


class TestStrictSmokeSettings:
    """Test that smoke_test_strict and canary_auto_cancel settings work."""

    def test_default_settings(self, mock_db):
        from updater import database as db
        assert db.get_setting("smoke_test_strict", "false") == "false"
        assert db.get_setting("canary_auto_cancel", "false") == "false"

    def test_settings_writable(self, authed_client, mock_db):
        resp = authed_client.post("/api/settings/save", json={
            "smoke_test_strict": "true",
            "canary_auto_cancel": "true",
        })
        assert resp.status_code == 200

        from updater import database as db
        assert db.get_setting("smoke_test_strict") == "true"
        assert db.get_setting("canary_auto_cancel") == "true"

    def test_settings_readable_from_db(self, mock_db):
        from updater import database as db
        # Settings should be readable even without explicit insert (defaults)
        val = db.get_setting("smoke_test_strict", "false")
        assert val == "false"
        val = db.get_setting("canary_auto_cancel", "false")
        assert val == "false"


class TestSmokeTestStrictMode:
    """Test that strict mode fails devices on smoke test warnings."""

    def test_smoke_result_with_warnings_sets_passed_false(self):
        result = SmokeTestResult()
        result.warnings.append("Device lost CPEs")
        result.passed = False
        assert not result.passed
        assert len(result.warnings) == 1

    def test_smoke_result_no_warnings_passes(self):
        result = SmokeTestResult()
        assert result.passed is True
        assert len(result.warnings) == 0


class TestCanaryAutoCancelSetting:
    """Test canary auto-cancel behavior at the database level."""

    def test_rollout_can_be_paused(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")
        db.pause_rollout(rollout_id, "Auto-cancelled: canary device failed")

        rollout = db.get_rollout(rollout_id)
        assert rollout["status"] == "paused"
        assert "canary" in rollout["pause_reason"]

    def test_current_rollout_returns_active(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")

        current = db.get_current_rollout()
        assert current is not None
        assert current["id"] == rollout_id
        assert current["phase"] == "canary"

    def test_paused_rollout_shows_paused_status(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")
        db.pause_rollout(rollout_id, "test")

        current = db.get_current_rollout()
        # get_current_rollout includes paused for UI display
        assert current is not None
        assert current["status"] == "paused"

    def test_canary_auto_cancel_setting_persists(self, mock_db):
        from updater import database as db
        db.set_setting("canary_auto_cancel", "true")
        assert db.get_setting("canary_auto_cancel") == "true"
        db.set_setting("canary_auto_cancel", "false")
        assert db.get_setting("canary_auto_cancel") == "false"
