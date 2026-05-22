"""Tests for per-device next-attempt computation and the schedule helper."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from updater import database as db
from updater.scheduler import (
    AutoUpdateScheduler,
    upcoming_window_starts,
)


SCHEDULE_DAYS = ["tue", "wed", "thu"]
START_HOUR = 3
END_HOUR = 4


class TestUpcomingWindowStarts:
    def test_returns_today_when_inside_active_day_before_end(self):
        # Tuesday at 02:30 — today's 03:00 window has not yet ended.
        now = datetime(2026, 5, 19, 2, 30)  # Tuesday
        windows = upcoming_window_starts(now, SCHEDULE_DAYS, START_HOUR, END_HOUR, count=1)
        assert windows == [datetime(2026, 5, 19, 3, 0)]

    def test_returns_next_active_day_when_window_already_ended(self):
        # Tuesday at 05:00 — today's window is over, next is Wednesday.
        now = datetime(2026, 5, 19, 5, 0)
        windows = upcoming_window_starts(now, SCHEDULE_DAYS, START_HOUR, END_HOUR, count=1)
        assert windows == [datetime(2026, 5, 20, 3, 0)]

    def test_returns_count_consecutive_active_windows(self):
        now = datetime(2026, 5, 18, 12, 0)  # Monday — skip to Tue/Wed/Thu
        windows = upcoming_window_starts(now, SCHEDULE_DAYS, START_HOUR, END_HOUR, count=3)
        assert windows == [
            datetime(2026, 5, 19, 3, 0),
            datetime(2026, 5, 20, 3, 0),
            datetime(2026, 5, 21, 3, 0),
        ]

    def test_skips_inactive_weekend_to_next_active_day(self):
        now = datetime(2026, 5, 22, 12, 0)  # Friday — next active day is Tuesday
        windows = upcoming_window_starts(now, SCHEDULE_DAYS, START_HOUR, END_HOUR, count=1)
        assert windows == [datetime(2026, 5, 26, 3, 0)]

    def test_empty_when_no_schedule_days(self):
        now = datetime(2026, 5, 19, 12, 0)
        assert upcoming_window_starts(now, [], START_HOUR, END_HOUR, count=3) == []

    def test_earliest_after_pushes_result_past_hold(self):
        # Tuesday at 02:30 — but earliest_after points to next Friday, so the
        # next active window after that is the following Tuesday.
        now = datetime(2026, 5, 19, 2, 30)
        earliest = datetime(2026, 5, 22, 6, 0)  # Friday
        windows = upcoming_window_starts(
            now, SCHEDULE_DAYS, START_HOUR, END_HOUR, count=1, earliest_after=earliest,
        )
        assert windows == [datetime(2026, 5, 26, 3, 0)]


def _make_scheduler():
    return AutoUpdateScheduler(broadcast_func=AsyncMock(), start_update_func=AsyncMock())


class TestComputeNextAttempt:
    def test_returns_off_when_scheduler_disabled(self, mock_db):
        mock_db.execute("UPDATE settings SET value = 'false' WHERE key = 'schedule_enabled'")
        mock_db.commit()

        scheduler = _make_scheduler()
        result = scheduler.compute_next_attempt("10.0.0.10", role="ap")

        assert result == {
            "auto_update_eligible": False,
            "next_attempt_iso": None,
            "reason": "Auto-update is off",
        }

    def test_returns_not_in_scope_when_device_outside_scope(self, mock_db):
        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")
        db.upsert_access_point("10.0.0.11", "root", "pass", enabled=True, firmware_version="1.0.0")
        # Narrow scope to a single different AP.
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('schedule_scope', 'aps')"
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('schedule_scope_data', '10.0.0.11')"
        )
        mock_db.commit()

        scheduler = _make_scheduler()
        result = scheduler.compute_next_attempt("10.0.0.10", role="ap")

        assert result["auto_update_eligible"] is False
        assert result["reason"] == "Not in auto-update scope"

    def test_returns_waiting_when_no_firmware_selected(self, mock_db):
        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")
        # default settings have no selected_firmware_30x — leave as-is

        scheduler = _make_scheduler()
        result = scheduler.compute_next_attempt("10.0.0.10", role="ap")

        assert result["auto_update_eligible"] is True
        assert result["next_attempt_iso"] is None
        assert result["reason"] == "Waiting for firmware to be selected"

    def test_returns_next_window_when_no_active_rollout(self, mock_db):
        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('selected_firmware_30x', 'fw-1.0.bin')"
        )
        # Disable quarantine for this test.
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('firmware_quarantine_days', '0')"
        )
        mock_db.commit()

        scheduler = _make_scheduler()
        # Pin "now" to a Tuesday at 02:30 so today's window is the answer.
        fake_now = datetime(2026, 5, 19, 2, 30)
        with patch("updater.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            result = scheduler.compute_next_attempt("10.0.0.10", role="ap")

        assert result["auto_update_eligible"] is True
        assert result["next_attempt_iso"] == datetime(2026, 5, 19, 3, 0).isoformat()
        assert result["reason"] is None

    def test_device_assigned_to_pct50_gets_third_window(self, mock_db):
        # Seed an active rollout currently in canary, with a device pre-assigned to pct50.
        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('selected_firmware_30x', 'fw-1.0.bin')"
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('firmware_quarantine_days', '0')"
        )
        mock_db.commit()

        rollout_id = db.create_rollout("fw-1.0.bin")
        db.assign_device_to_rollout(rollout_id, "10.0.0.10", "ap", "pct50")
        rollout = db.get_rollout(rollout_id)

        scheduler = _make_scheduler()
        fake_now = datetime(2026, 5, 18, 12, 0)  # Monday noon
        with patch("updater.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            result = scheduler.compute_next_attempt(
                "10.0.0.10", role="ap", rollout=rollout,
                rollout_devices_by_ip={"10.0.0.10": {
                    "ip": "10.0.0.10", "phase_assigned": "pct50", "status": "pending",
                }},
            )

        # canary→pct10→pct50 means 3rd upcoming window from Monday = Thursday.
        assert result["next_attempt_iso"] == datetime(2026, 5, 21, 3, 0).isoformat()

    def test_cpe_inherits_parent_ap_eligibility(self, mock_db):
        # Parent AP not in scope → CPE not in scope either.
        db.upsert_access_point("10.0.0.10", "root", "pass", enabled=True, firmware_version="1.0.0")
        db.upsert_access_point("10.0.0.99", "root", "pass", enabled=True, firmware_version="1.0.0")
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('schedule_scope', 'aps')"
        )
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('schedule_scope_data', '10.0.0.99')"
        )
        mock_db.commit()

        scheduler = _make_scheduler()
        result = scheduler.compute_next_attempt(
            "192.168.5.5", role="cpe", parent_ap_ip="10.0.0.10",
        )

        assert result["auto_update_eligible"] is False
        assert result["reason"] == "Not in auto-update scope"
