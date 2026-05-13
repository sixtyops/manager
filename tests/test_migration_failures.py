"""DB migration failure-path tests.

`updater.database.init_db()` is the single migration entry point. Every
schema change is gated by a `PRAGMA table_info()` check, so a crashed
migration leaves the DB in a partial state that the next `init_db()`
call should resume from cleanly. These tests lock that contract in by
exercising the four failure modes that have caused customer DB wedging
in the field, or could in the future:

  * Re-init against an already-initialized DB (idempotency)
  * Re-init against a DB that lost a column (mid-migration crash)
  * Re-init against a DB that has an extra unknown column (downgrade)
  * Re-init while another writer holds a contention lock

See `docs/migration-recovery.md` for the operator-facing recovery runbook.
"""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from updater import database


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point updater.database at a fresh on-disk SQLite file."""
    db_file = tmp_path / "sixtyops.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    yield db_file


def _column_names(db_file: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_file))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _integrity_ok(db_file: Path) -> bool:
    conn = sqlite3.connect(str(db_file))
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


class TestIdempotency:
    """init_db() must be safe to run on every app start, including back-to-back."""

    def test_clean_init_then_reinit_preserves_schema(self, fresh_db):
        database.init_db()
        cols_before = _column_names(fresh_db, "devices")
        database.init_db()
        cols_after = _column_names(fresh_db, "devices")
        assert cols_before == cols_after

    def test_three_consecutive_inits_keep_integrity(self, fresh_db):
        for _ in range(3):
            database.init_db()
        assert _integrity_ok(fresh_db)


class TestPartialMigrationRecovery:
    """A crash mid-migration leaves the DB resumable on the next start."""

    def test_resume_after_dropped_column_restores_it(self, fresh_db):
        database.init_db()
        assert "last_config_poll_at" in _column_names(fresh_db, "devices")

        # Simulate a customer DB that lost a column due to a crashed migration
        # or rollback to a pre-migration snapshot. SQLite 3.35+ supports
        # ALTER TABLE ... DROP COLUMN; the project's runtime is well above that.
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute("ALTER TABLE devices DROP COLUMN last_config_poll_at")
            conn.commit()
        finally:
            conn.close()
        assert "last_config_poll_at" not in _column_names(fresh_db, "devices")

        database.init_db()
        assert "last_config_poll_at" in _column_names(fresh_db, "devices")

    def test_resume_restores_multiple_dropped_columns(self, fresh_db):
        database.init_db()
        # SQLite refuses to drop columns referenced by triggers, so drop the
        # cross-table sync triggers first. init_db() recreates them
        # idempotently via CREATE TRIGGER IF NOT EXISTS.
        conn = sqlite3.connect(str(fresh_db))
        try:
            for trigger in (
                "trg_ap_to_devices_insert",
                "trg_ap_to_devices_update",
                "trg_devices_to_legacy_insert",
                "trg_devices_to_legacy_update",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute("ALTER TABLE access_points DROP COLUMN bank1_version")
            conn.execute("ALTER TABLE access_points DROP COLUMN bank2_version")
            conn.execute("ALTER TABLE devices DROP COLUMN last_config_poll_status")
            conn.commit()
        finally:
            conn.close()

        database.init_db()
        ap_cols = _column_names(fresh_db, "access_points")
        dev_cols = _column_names(fresh_db, "devices")
        assert "bank1_version" in ap_cols
        assert "bank2_version" in ap_cols
        assert "last_config_poll_status" in dev_cols

    def test_existing_data_survives_reinit(self, fresh_db):
        """init_db() must not destroy customer data when called against a
        populated DB — this is the daily contract on every app restart."""
        database.init_db()
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute(
                "INSERT INTO devices (ip, role, username, password) VALUES (?, ?, ?, ?)",
                ("10.0.0.1", "ap", "admin", "gAAAAA-stub"),
            )
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("custom_setting", "custom_value"),
            )
            conn.commit()
        finally:
            conn.close()

        database.init_db()
        conn = sqlite3.connect(str(fresh_db))
        try:
            row = conn.execute(
                "SELECT ip, role FROM devices WHERE ip = ?", ("10.0.0.1",)
            ).fetchone()
            assert row == ("10.0.0.1", "ap")
            setting = conn.execute(
                "SELECT value FROM settings WHERE key = ?", ("custom_setting",)
            ).fetchone()
            assert setting == ("custom_value",)
        finally:
            conn.close()


class TestSchemaDowngrade:
    """A newer DB opened by older code (extra columns) must not crash or destroy data."""

    def test_unknown_column_survives_reinit(self, fresh_db):
        database.init_db()
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN future_field_v99 TEXT DEFAULT 'placeholder'")
            conn.commit()
        finally:
            conn.close()

        database.init_db()
        cols = _column_names(fresh_db, "devices")
        assert "future_field_v99" in cols, (
            "init_db() must not drop columns it doesn't recognize — that would "
            "destroy data on a downgrade after a customer rolls back an in-app update."
        )

    def test_unknown_column_does_not_break_reads(self, fresh_db):
        database.init_db()
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN future_field TEXT DEFAULT 'x'")
            conn.execute(
                "INSERT INTO devices (ip, role, username, password) VALUES (?, ?, ?, ?)",
                ("10.0.0.2", "ap", "admin", "gAAAAA-stub"),
            )
            conn.commit()
        finally:
            conn.close()

        database.init_db()
        conn = sqlite3.connect(str(fresh_db))
        try:
            row = conn.execute("SELECT ip, future_field FROM devices WHERE ip = ?", ("10.0.0.2",)).fetchone()
            assert row == ("10.0.0.2", "x")
        finally:
            conn.close()


class TestConcurrentWriterContention:
    """init_db() must not corrupt the DB if another writer is active."""

    def test_init_completes_after_writer_releases(self, fresh_db):
        database.init_db()
        # Drop a column so _migrate() will need to ALTER TABLE on the second pass.
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute("ALTER TABLE devices DROP COLUMN last_config_poll_error")
            conn.commit()
        finally:
            conn.close()

        # Hold an exclusive lock briefly, then release; init_db() should retry
        # within its 5s busy_timeout and complete cleanly.
        long_writer = sqlite3.connect(str(fresh_db), timeout=10)
        long_writer.execute("BEGIN EXCLUSIVE")
        long_writer.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)", ("contention_test", "1")
        )

        init_error = []
        def run_init():
            try:
                database.init_db()
            except Exception as exc:
                init_error.append(exc)

        t = threading.Thread(target=run_init)
        t.start()
        # Release the lock well within the busy_timeout window so init_db retries succeed.
        time.sleep(0.5)
        long_writer.commit()
        long_writer.close()
        t.join(timeout=15)

        assert not t.is_alive(), "init_db() did not return within 15s of lock release"
        assert not init_error, f"init_db() raised under contention: {init_error}"
        assert "last_config_poll_error" in _column_names(fresh_db, "devices")
        assert _integrity_ok(fresh_db)

    def test_init_during_unbreakable_lock_fails_without_corruption(self, fresh_db, monkeypatch):
        """If the lock is held longer than busy_timeout, init_db() raises — but
        the DB file is still structurally intact. This is the contract the
        recovery runbook depends on: a failed init_db() means 'try again', not
        'restore from backup'."""
        database.init_db()
        conn = sqlite3.connect(str(fresh_db))
        try:
            conn.execute("ALTER TABLE devices DROP COLUMN last_config_poll_status")
            conn.commit()
        finally:
            conn.close()

        # The patched get_db keeps the migration's own writes fast (200ms
        # busy_timeout). init_db()'s up-front check_conn opens its own
        # sqlite3.connect with timeout=10, so the wall time of this test is
        # still dominated by that first PRAGMA incremental_vacuum blocking
        # against the exclusive lock; expect ~10s.
        from contextlib import contextmanager

        @contextmanager
        def short_timeout_get_db():
            conn = sqlite3.connect(str(fresh_db), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=200")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        long_writer = sqlite3.connect(str(fresh_db), timeout=10)
        long_writer.execute("BEGIN EXCLUSIVE")
        long_writer.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)", ("unbreakable", "1")
        )

        try:
            monkeypatch.setattr(database, "get_db", short_timeout_get_db)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                database.init_db()
        finally:
            long_writer.commit()
            long_writer.close()

        # The key contract: even after a failed init_db(), the DB file is intact.
        assert _integrity_ok(fresh_db)
