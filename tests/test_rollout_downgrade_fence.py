import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from updater import database
from updater.db.compatibility import (
    RolloutCompatibilityError,
    inspect_connection,
    inspect_database,
)
from updater.db.schema import build_schema


def _database(connection):
    build_schema(connection)
    database._migrate(connection)
    return connection


def _set_marker(connection, value):
    connection.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('rollout_format_version', ?)",
        (value,),
    )
    connection.commit()


def test_empty_database_bootstrap_sets_marker_and_keeps_scheduling_disabled():
    connection = sqlite3.connect(":memory:")
    _database(connection)

    values = dict(connection.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('rollout_format_version', 'schedule_enabled', 'scheduled_rollout_safety_ready')"
    ))

    assert values == {
        "rollout_format_version": "2",
        "schedule_enabled": "false",
        "scheduled_rollout_safety_ready": "false",
    }


@pytest.mark.parametrize(
    ("marker", "held", "allowed", "reason"),
    [
        ("1", False, True, None),
        ("2", True, True, None),
        ("1", True, False, "Saved waiting_hold rollout requires format 2 support"),
        ("3", False, False, "exceeds this image's supported format 2"),
        ("01", False, False, "malformed"),
        ("0", False, False, "malformed"),
        ("two", False, False, "malformed"),
    ],
)
def test_marker_and_held_rollout_compatibility(marker, held, allowed, reason):
    connection = _database(sqlite3.connect(":memory:"))
    _set_marker(connection, marker)
    if held:
        connection.execute(
            "INSERT INTO rollouts (firmware_file, status) VALUES ('fw.bin', 'waiting_hold')"
        )
        connection.commit()

    result = inspect_connection(connection)

    assert result.allowed is allowed
    if reason:
        assert reason in result.reason


def test_missing_marker_fails_closed():
    connection = _database(sqlite3.connect(":memory:"))
    connection.execute("DELETE FROM settings WHERE key='rollout_format_version'")
    connection.commit()

    result = inspect_connection(connection)

    assert not result.allowed
    assert result.reason == "Rollout compatibility marker is missing"


def test_unreadable_database_fails_closed(tmp_path):
    result = inspect_database(tmp_path)

    assert not result.allowed
    assert result.reason == "Rollout compatibility could not be read"


def test_read_only_preflight_sees_committed_wal_state(tmp_path):
    path = tmp_path / "wal.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.executescript(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE rollouts (status TEXT NOT NULL);"
    )
    writer.execute(
        "INSERT INTO settings (key, value) VALUES ('rollout_format_version', '2')"
    )
    writer.execute("INSERT INTO rollouts (status) VALUES ('waiting_hold')")
    writer.commit()

    result = inspect_database(path)

    writer.close()
    assert result.allowed
    assert result.format_version == 2


def test_init_db_refuses_newer_marker_before_any_database_write(tmp_path, monkeypatch):
    path = tmp_path / "manager.db"
    connection = _database(sqlite3.connect(path))
    _set_marker(connection, "3")
    connection.close()
    before = path.read_bytes()
    monkeypatch.setattr(database, "DB_PATH", path)

    with pytest.raises(RolloutCompatibilityError, match="exceeds this image's supported format 2"):
        database.init_db()

    assert path.read_bytes() == before


def test_module_import_refuses_newer_marker_before_startup_writes(tmp_path):
    source = Path(__file__).parents[1] / "updater"
    package = tmp_path / "updater"
    (package / "db").mkdir(parents=True)
    for relative in (
        "__init__.py",
        "database.py",
        "crypto.py",
        "logging_filter.py",
        "db/__init__.py",
        "db/compatibility.py",
        "db/schema.py",
    ):
        (package / relative).write_bytes((source / relative).read_bytes())

    data = tmp_path / "data"
    data.mkdir()
    connection = sqlite3.connect(data / "sixtyops.db")
    connection.executescript(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE rollouts (status TEXT NOT NULL);"
        "INSERT INTO settings VALUES ('rollout_format_version', '3');"
    )
    connection.close()
    result = subprocess.run(
        [sys.executable, "-c", "import updater.database"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Rollout format 3 exceeds this image's supported format 2" in result.stderr


def test_schema_and_direct_migration_helpers_refuse_newer_marker_before_writes():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("CREATE TABLE rollouts (status TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO settings (key, value) VALUES ('rollout_format_version', '3')"
    )
    connection.commit()
    before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    calls = [
        lambda: build_schema(connection),
        lambda: database._migrate(connection),
        lambda: database._migrate_cpe_rows(connection),
        lambda: database._migrate_identity(connection),
        lambda: database._migrate_to_devices_table(connection),
        lambda: database._migrate_encrypt_passwords(connection),
        lambda: database._migrate_encrypt_device_configs(connection),
        lambda: database._migrate_encrypt_config_templates(connection),
        lambda: database._migrate_encrypt_settings(connection),
    ]
    for call in calls:
        with pytest.raises(RolloutCompatibilityError, match="exceeds"):
            call()

    after = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert after == before


def test_membership_rollout_raises_marker_in_transaction_and_rolls_back_on_failure(
    mock_db,
):
    _set_marker(mock_db, "1")
    rollout_id = database.create_rollout(
        "fw.bin",
        members=[{"ip": "192.0.2.10", "device_type": "ap"}],
        settings_snapshot={"bank_mode": "both"},
    )
    assert database.get_rollout(rollout_id)["status"] == "waiting_hold"
    assert mock_db.execute(
        "SELECT value FROM settings WHERE key='rollout_format_version'"
    ).fetchone()[0] == "2"

    mock_db.execute("DELETE FROM rollout_devices WHERE rollout_id=?", (rollout_id,))
    mock_db.execute("DELETE FROM rollouts WHERE id=?", (rollout_id,))
    mock_db.execute(
        "UPDATE settings SET value='1' WHERE key='rollout_format_version'"
    )
    mock_db.execute(
        "CREATE TRIGGER reject_fence_member BEFORE INSERT ON rollout_devices "
        "BEGIN SELECT RAISE(ABORT, 'injected membership failure'); END"
    )
    mock_db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected membership failure"):
        database.create_rollout(
            "other-fw.bin",
            members=[{"ip": "192.0.2.11", "device_type": "ap"}],
            settings_snapshot={"bank_mode": "both"},
        )

    assert mock_db.execute(
        "SELECT value FROM settings WHERE key='rollout_format_version'"
    ).fetchone()[0] == "1"
    assert mock_db.execute(
        "SELECT COUNT(*) FROM rollouts WHERE firmware_file='other-fw.bin'"
    ).fetchone()[0] == 0
