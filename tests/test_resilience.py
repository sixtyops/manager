"""Tests for self-healing and resilience features."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


class TestBroadcastTimeout:
    """Verify broadcast handles slow/stuck WebSocket clients."""

    @pytest.mark.asyncio
    async def test_slow_client_disconnected(self, mock_db):
        """A WebSocket that times out on send should be removed."""
        from updater.app import broadcast, active_websockets

        fast_ws = AsyncMock()
        slow_ws = AsyncMock()
        slow_ws.send_text = AsyncMock(side_effect=asyncio.TimeoutError)

        active_websockets.clear()
        active_websockets.add(fast_ws)
        active_websockets.add(slow_ws)

        await broadcast({"type": "test"})

        fast_ws.send_text.assert_called_once()
        assert slow_ws not in active_websockets
        assert fast_ws in active_websockets

        active_websockets.clear()

    @pytest.mark.asyncio
    async def test_erroring_client_disconnected(self, mock_db):
        """A WebSocket that raises on send should be removed."""
        from updater.app import broadcast, active_websockets

        good_ws = AsyncMock()
        bad_ws = AsyncMock()
        bad_ws.send_text = AsyncMock(side_effect=ConnectionError)

        active_websockets.clear()
        active_websockets.add(good_ws)
        active_websockets.add(bad_ws)

        await broadcast({"type": "test"})

        assert bad_ws not in active_websockets
        assert good_ws in active_websockets

        active_websockets.clear()

    @pytest.mark.asyncio
    async def test_broadcast_completes_with_all_slow_clients(self, mock_db):
        """Broadcast should complete even if all clients are slow."""
        from updater.app import broadcast, active_websockets

        slow1 = AsyncMock()
        slow1.send_text = AsyncMock(side_effect=asyncio.TimeoutError)
        slow2 = AsyncMock()
        slow2.send_text = AsyncMock(side_effect=asyncio.TimeoutError)

        active_websockets.clear()
        active_websockets.add(slow1)
        active_websockets.add(slow2)

        await broadcast({"type": "test"})

        assert len(active_websockets) == 0

        active_websockets.clear()


class TestSupervisedTask:
    """Verify the supervised task wrapper restarts crashed tasks."""

    @pytest.mark.asyncio
    async def test_restarts_after_crash(self):
        """A task that crashes should be restarted after delay."""
        from updater.app import _supervised_task

        call_count = 0

        async def flaky_task():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("simulated crash")
            # On 3rd call, stay alive until cancelled
            await asyncio.sleep(3600)

        task = asyncio.create_task(
            _supervised_task("test_task", flaky_task, restart_delay=0.01)
        )
        # Wait for the task to crash twice and succeed on the third
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert call_count >= 3

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        """CancelledError should not be caught by the supervisor."""
        from updater.app import _supervised_task

        async def long_running():
            await asyncio.sleep(3600)

        task = asyncio.create_task(
            _supervised_task("cancel_test", long_running)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_passes_args_to_coroutine(self):
        """Arguments should be forwarded to the wrapped coroutine."""
        from updater.app import _supervised_task

        received_args = []

        async def capture_args(a, b):
            received_args.append((a, b))
            # Stay alive until cancelled
            await asyncio.sleep(3600)

        task = asyncio.create_task(
            _supervised_task("args_test", capture_args, "hello", 42, restart_delay=0.01)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(received_args) >= 1
        assert received_args[0] == ("hello", 42)


class TestStuckUpdateRecovery:
    """Verify stuck update state is cleared after timeout."""

    def _set_setting(self, mock_db, key, value):
        mock_db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        mock_db.commit()

    def _get_setting(self, mock_db, key):
        row = mock_db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else ""

    @pytest.mark.asyncio
    async def test_stuck_pending_update_cleared(self, mock_db):
        """Pending update older than 15 minutes should be cleared as stuck."""
        from updater.release_checker import verify_update_on_startup

        self._set_setting(mock_db, "autoupdate_pending_version", "9.9.9")
        twenty_min_ago = (datetime.now() - timedelta(minutes=20)).isoformat()
        self._set_setting(mock_db, "autoupdate_pending_at", twenty_min_ago)

        broadcast_mock = AsyncMock()
        with patch("updater.release_checker.__version__", "1.0.1"):
            await verify_update_on_startup(broadcast_mock)

        assert self._get_setting(mock_db, "autoupdate_pending_version") == ""
        assert self._get_setting(mock_db, "autoupdate_pending_at") == ""
        broadcast_mock.assert_called_once()
        msg = broadcast_mock.call_args[0][0]
        assert msg["type"] == "update_failed"
        assert msg["reason"] == "Update timed out without completing"

    @pytest.mark.asyncio
    async def test_recent_pending_not_cleared(self, mock_db):
        """Pending update less than 15 minutes old should NOT be cleared."""
        from updater.release_checker import verify_update_on_startup

        self._set_setting(mock_db, "autoupdate_pending_version", "9.9.9")
        five_min_ago = (datetime.now() - timedelta(minutes=5)).isoformat()
        self._set_setting(mock_db, "autoupdate_pending_at", five_min_ago)

        broadcast_mock = AsyncMock()
        with patch("updater.release_checker.__version__", "1.0.1"):
            await verify_update_on_startup(broadcast_mock)

        # Should fall through to the rollback path (version mismatch)
        assert self._get_setting(mock_db, "autoupdate_pending_version") == ""
        broadcast_mock.assert_called_once()
        msg = broadcast_mock.call_args[0][0]
        assert msg["type"] == "update_rolled_back"

    @pytest.mark.asyncio
    async def test_successful_update_clears_pending_at(self, mock_db):
        """Successful update should clear the pending_at timestamp."""
        from updater.release_checker import verify_update_on_startup
        from updater import __version__

        self._set_setting(mock_db, "autoupdate_pending_version", __version__)
        self._set_setting(mock_db, "autoupdate_pending_at", datetime.now().isoformat())

        broadcast_mock = AsyncMock()
        await verify_update_on_startup(broadcast_mock)

        assert self._get_setting(mock_db, "autoupdate_pending_version") == ""
        assert self._get_setting(mock_db, "autoupdate_pending_at") == ""
        broadcast_mock.assert_called_once()
        msg = broadcast_mock.call_args[0][0]
        assert msg["type"] == "update_completed"


class TestDatabasePragmas:
    """Verify SQLite hardening pragmas are configured."""

    def test_wal_mode_on_disk_db(self, tmp_path):
        """WAL mode and busy_timeout should be set on real connections."""
        import updater.database as db_mod

        original_path = db_mod.DB_PATH
        db_mod.DB_PATH = tmp_path / "test.db"
        try:
            db_mod.init_db()
            with db_mod.get_db() as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                assert mode == "wal"
                timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                assert timeout == 5000
        finally:
            db_mod.DB_PATH = original_path

    def test_integrity_check_on_existing_db(self, tmp_path):
        """Integrity check should run on existing databases without error."""
        import updater.database as db_mod

        original_path = db_mod.DB_PATH
        db_mod.DB_PATH = tmp_path / "test.db"
        try:
            db_mod.init_db()
            # Run init again to trigger integrity check on existing DB
            db_mod.init_db()  # Should not raise
        finally:
            db_mod.DB_PATH = original_path
