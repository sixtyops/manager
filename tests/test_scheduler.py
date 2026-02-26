"""Tests for updater.scheduler."""

import asyncio
from unittest.mock import patch

import pytest


class TestSchedulerCompletionHandling:
    @pytest.mark.asyncio
    async def test_cancelled_no_progress_does_not_advance_phase(self):
        from updater.scheduler import AutoUpdateScheduler

        scheduler = AutoUpdateScheduler(broadcast_func=None, start_update_func=None)
        scheduler._current_job_id = "job123"

        rollout = {
            "id": 7,
            "phase": "canary",
            "status": "active",
            "last_job_id": "job123",
        }

        with patch("updater.scheduler.db.get_active_rollout", return_value=rollout), \
             patch("updater.scheduler.db.mark_rollout_device") as mark_rollout_device, \
             patch("updater.scheduler.db.complete_rollout_phase") as complete_rollout_phase, \
             patch("updater.scheduler.db.pause_rollout") as pause_rollout, \
             patch("updater.scheduler.db.log_schedule_event") as log_schedule_event:
            scheduler.on_job_completed(
                job_id="job123",
                success_count=0,
                failed_count=0,
                device_statuses={"10.0.0.1": "skipped"},
                cancel_reason="Outside maintenance window",
            )

            await asyncio.sleep(0)

        mark_rollout_device.assert_called_once_with(7, "10.0.0.1", "skipped")
        complete_rollout_phase.assert_not_called()
        pause_rollout.assert_not_called()
        log_schedule_event.assert_any_call(
            "job_deferred",
            "Outside maintenance window",
            job_id="job123",
        )
