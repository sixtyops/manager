"""Tests for long-running stability and crash recovery features."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock

import pytest


class TestJobCleanup:
    """Verify in-memory job dict cleanup handles all terminal states."""

    def test_stuck_job_force_removed(self, mock_db):
        """A job older than 24 hours with no completion should be force-removed."""
        from updater.app import _cleanup_completed_jobs, update_jobs

        class FakeJob:
            def __init__(self, status, started_at, completed_at=None):
                self.status = status
                self.started_at = started_at
                self.completed_at = completed_at

        update_jobs.clear()
        update_jobs["stuck-1"] = FakeJob(
            status="running",
            started_at=datetime.now() - timedelta(hours=25),
        )
        update_jobs["recent-1"] = FakeJob(
            status="running",
            started_at=datetime.now() - timedelta(hours=1),
        )

        _cleanup_completed_jobs(max_age_seconds=600)

        assert "stuck-1" not in update_jobs
        assert "recent-1" in update_jobs
        update_jobs.clear()

    def test_failed_cancelled_jobs_cleaned(self, mock_db):
        """Failed and cancelled jobs should be cleaned up, not just completed."""
        from updater.app import _cleanup_completed_jobs, update_jobs

        class FakeJob:
            def __init__(self, status, completed_at, started_at=None):
                self.status = status
                self.completed_at = completed_at
                self.started_at = started_at or completed_at

        update_jobs.clear()
        old = datetime.now() - timedelta(hours=2)
        update_jobs["failed-1"] = FakeJob(status="failed", completed_at=old)
        update_jobs["cancelled-1"] = FakeJob(status="cancelled", completed_at=old)
        update_jobs["completed-1"] = FakeJob(status="completed", completed_at=old)

        _cleanup_completed_jobs(max_age_seconds=600)

        assert "failed-1" not in update_jobs
        assert "cancelled-1" not in update_jobs
        assert "completed-1" not in update_jobs
        update_jobs.clear()


class TestCrashRecovery:
    """Verify crashed job detection and recovery on startup."""

    def test_recover_crashed_jobs(self, mock_db):
        """Active jobs from a previous crash should be marked as failed in history."""
        from updater.app import _recover_crashed_device_jobs
        import updater.database as db_mod

        # Insert a fake active job
        mock_db.execute(
            "INSERT INTO active_jobs (job_id, status, started_at, device_ips_json, firmware_name) "
            "VALUES (?, ?, ?, ?, ?)",
            ("crash-job-1", "running", datetime.now().isoformat(), '["10.0.0.1", "10.0.0.2"]', "fw.bin"),
        )
        mock_db.commit()

        _recover_crashed_device_jobs()

        # Active job should be cleared
        row = mock_db.execute("SELECT * FROM active_jobs WHERE job_id = 'crash-job-1'").fetchone()
        assert row is None

        # Device history should have failure records
        history = mock_db.execute(
            "SELECT * FROM device_update_history WHERE job_id = 'crash-job-1'"
        ).fetchall()
        assert len(history) == 2
        for h in history:
            assert h["status"] == "failed"
            assert "crashed" in h["error"].lower()

    def test_recover_no_active_jobs(self, mock_db):
        """No active jobs should be a no-op."""
        from updater.app import _recover_crashed_device_jobs

        # Should not raise
        _recover_crashed_device_jobs()

        rows = mock_db.execute("SELECT * FROM active_jobs").fetchall()
        assert len(rows) == 0


class TestSupervisedTaskBackoff:
    """Verify exponential backoff in supervised task restarts."""

    @pytest.mark.asyncio
    async def test_backoff_escalates(self):
        """Restart delay should escalate exponentially up to 300s cap."""
        from updater.app import _supervised_task

        delays = []
        call_count = 0

        async def crashing_task():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            delays.append(delay)
            if len(delays) >= 4:
                raise asyncio.CancelledError()
            await original_sleep(0)

        with patch("asyncio.sleep", side_effect=mock_sleep):
            task = asyncio.create_task(
                _supervised_task("test", crashing_task, restart_delay=10.0)
            )
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Delays: 10, 20, 40, 80... capped at 300
        assert delays[0] == 10.0
        assert delays[1] == 20.0
        assert delays[2] == 40.0

    @pytest.mark.asyncio
    async def test_backoff_resets_on_clean_run(self):
        """Counter should reset after a successful run."""
        from updater.app import _supervised_task

        delays = []
        call_count = 0

        async def intermittent_task():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("boom")
            if call_count == 3:
                # Succeed once (resets backoff), then next call crashes again
                return
            # call 4: crash again — delay should be reset to base (10)
            raise RuntimeError("boom again")

        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            delays.append(delay)
            if len(delays) >= 3:
                raise asyncio.CancelledError()
            await original_sleep(0)

        with patch("asyncio.sleep", side_effect=mock_sleep):
            task = asyncio.create_task(
                _supervised_task("test", intermittent_task, restart_delay=10.0)
            )
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Delays: 10 (crash 1), 20 (crash 2), then success resets, 10 (crash 4)
        assert len(delays) >= 3
        assert delays[0] == 10.0
        assert delays[1] == 20.0
        assert delays[2] == 10.0  # Reset after clean run


class TestDatabaseMaintenance:
    """Verify periodic database maintenance runs without error."""

    def test_periodic_maintenance(self, tmp_path):
        """periodic_maintenance() should run WAL checkpoint and vacuum without error."""
        import updater.database as db_mod

        original_path = db_mod.DB_PATH
        db_mod.DB_PATH = tmp_path / "test.db"
        try:
            db_mod.init_db()
            # Should not raise
            db_mod.periodic_maintenance()
        finally:
            db_mod.DB_PATH = original_path


class TestHealthCheck:
    """Verify the /healthz endpoint checks database health."""

    def test_healthz_ok(self, authed_client):
        """Should return 200 with db ok when database is accessible."""
        resp = authed_client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["db"] == "ok"

    def test_healthz_notification_degraded(self, mock_db, authed_client):
        """Should include notification status when failures exceed threshold."""
        import updater.database as db_mod

        db_mod.set_setting("notification_consecutive_failures", "10")

        resp = authed_client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert "notifications" in data
        assert "degraded" in data["notifications"]


class TestSchedulerDedupRecovery:
    """Verify scheduler _ran_today is recovered from DB on startup."""

    def test_ran_today_populated_from_db(self, mock_db):
        """Scheduler should detect today's run from schedule_log on start."""
        from updater.scheduler import AutoUpdateScheduler

        today = datetime.now().strftime("%Y-%m-%d")
        mock_db.execute(
            "INSERT INTO schedule_log (event, details, timestamp) VALUES (?, ?, ?)",
            ("job_started", f"Job test-123 for rollout 1", datetime.now().isoformat()),
        )
        mock_db.commit()

        scheduler = AutoUpdateScheduler(
            broadcast_func=AsyncMock(),
            start_update_func=AsyncMock(),
            check_interval=60,
        )

        # Simulate start() sync portion
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(scheduler.start())
            assert today in scheduler._ran_today
        finally:
            loop.run_until_complete(scheduler.stop())
            loop.close()

    def test_ran_today_empty_when_no_log(self, mock_db):
        """Scheduler should have empty _ran_today if no job ran today."""
        from updater.scheduler import AutoUpdateScheduler

        scheduler = AutoUpdateScheduler(
            broadcast_func=AsyncMock(),
            start_update_func=AsyncMock(),
            check_interval=60,
        )

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(scheduler.start())
            assert len(scheduler._ran_today) == 0
        finally:
            loop.run_until_complete(scheduler.stop())
            loop.close()


class TestActiveJobTracking:
    """Verify active job CRUD in database."""

    def test_save_and_get_active_job(self, mock_db):
        """Should persist and retrieve active jobs."""
        import updater.database as db_mod

        db_mod.save_active_job("job-1", "running", '["10.0.0.1"]', "fw.bin")
        jobs = db_mod.get_active_jobs()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-1"
        assert jobs[0]["firmware_name"] == "fw.bin"

    def test_clear_active_job(self, mock_db):
        """Should remove active job after completion."""
        import updater.database as db_mod

        db_mod.save_active_job("job-2", "running", '["10.0.0.1"]', "fw.bin")
        db_mod.clear_active_job("job-2")
        jobs = db_mod.get_active_jobs()
        assert len(jobs) == 0

    def test_get_enabled_device_ips(self, mock_db):
        """Should return only enabled device IPs."""
        import updater.database as db_mod

        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "admin", "pass", 1),
        )
        mock_db.execute(
            "INSERT INTO access_points (ip, username, password, enabled) VALUES (?, ?, ?, ?)",
            ("10.0.0.2", "admin", "pass", 0),
        )
        mock_db.commit()

        ips = db_mod.get_enabled_device_ips()
        assert "10.0.0.1" in ips
        assert "10.0.0.2" not in ips
