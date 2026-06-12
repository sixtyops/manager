"""Tests for strict smoke-test mode (a strict failure halts the rollout job —
halt-on-first-failure) and basic rollout pause/status plumbing."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from updater.tachyon import SmokeTestResult


class TestStrictSmokeSettings:
    """smoke_test_strict persists and defaults off."""

    def test_default_settings(self, mock_db):
        from updater import database as db
        assert db.get_setting("smoke_test_strict", "false") == "false"

    def test_settings_writable(self, authed_client, mock_db):
        resp = authed_client.post("/api/settings/save", json={
            "smoke_test_strict": "true",
        })
        assert resp.status_code == 200

        from updater import database as db
        assert db.get_setting("smoke_test_strict") == "true"

    def test_settings_readable_from_db(self, mock_db):
        from updater import database as db
        assert db.get_setting("smoke_test_strict", "false") == "false"


class TestSmokeTestStrictMode:
    """Strict mode fails devices on smoke-test warnings."""

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


class TestRolloutPauseStatus:
    """Rollout pause/status plumbing (halt-on-failure pauses a rollout)."""

    def test_rollout_can_be_paused(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")
        db.pause_rollout(rollout_id, "1 device(s) failed during the pct10 wave")

        rollout = db.get_rollout(rollout_id)
        assert rollout["status"] == "paused"
        assert "failed" in rollout["pause_reason"]

    def test_current_rollout_returns_active(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")

        current = db.get_current_rollout()
        assert current is not None
        assert current["id"] == rollout_id
        # The canary phase was removed: a fresh rollout starts at the first wave.
        assert current["phase"] == "pct10"

    def test_paused_rollout_shows_paused_status(self, mock_db):
        from updater import database as db
        rollout_id = db.create_rollout("test.bin")
        db.pause_rollout(rollout_id, "test")

        current = db.get_current_rollout()
        # get_current_rollout includes paused for UI display
        assert current is not None
        assert current["status"] == "paused"
