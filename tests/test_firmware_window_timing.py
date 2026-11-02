import sqlite3

import pytest

from updater import database as db
from updater.db.schema import build_schema


SHA_A = "a" * 64
SHA_B = "b" * 64


def test_duplicate_admission_reuses_timing_and_artifacts_have_separate_rows(mock_db):
    first = db.record_firmware_window_admission(
        "ap", SHA_A, (1, "2026-11-03"), "2026-11-03T03:30:00+00:00"
    )
    duplicate = db.record_firmware_window_admission(
        "ap", SHA_A, (1, "2026-11-03"), "2026-11-03T03:45:00+00:00"
    )
    second_window = db.record_firmware_window_admission(
        "ap", SHA_A, (1, "2026-11-04"), "2026-11-04T05:00:00+00:00"
    )
    other_artifact = db.record_firmware_window_admission(
        "ap", SHA_B, (1, "2026-11-04"), "2026-11-04T03:30:00+00:00"
    )

    assert first["ordinal"] == 1
    assert duplicate == first
    assert second_window["ordinal"] == 2
    assert second_window["cutoff_utc"] == "2026-11-04T05:00:00+00:00"
    assert other_artifact["ordinal"] == 1
    assert mock_db.execute("SELECT COUNT(*) FROM firmware_window_timing").fetchone()[0] == 2


def test_timing_update_failure_rolls_back_existing_row(mock_db):
    original = db.record_firmware_window_admission(
        "ap", SHA_A, (1, "2026-11-03"), "2026-11-03T03:30:00+00:00"
    )
    mock_db.execute(
        "CREATE TRIGGER reject_timing_update BEFORE UPDATE ON firmware_window_timing "
        "BEGIN SELECT RAISE(ABORT, 'timing update rejected'); END"
    )
    mock_db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="timing update rejected"):
        db.record_firmware_window_admission(
            "ap", SHA_A, (1, "2026-11-04"), "2026-11-04T05:00:00+00:00"
        )

    row = mock_db.execute(
        "SELECT ordinal, last_window_key, cutoff_utc FROM firmware_window_timing"
    ).fetchone()
    assert tuple(row) == (original["ordinal"], original["last_window_key"], original["cutoff_utc"])


def test_timing_row_survives_database_reopen(tmp_path, monkeypatch):
    path = tmp_path / "manager.sqlite3"
    conn = sqlite3.connect(path)
    build_schema(conn)
    db._migrate(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)

    first = db.record_firmware_window_admission(
        "switch", SHA_A, (1, "2026-11-03"), "2026-11-03T03:30:00+00:00"
    )
    reopened = sqlite3.connect(path)
    row = reopened.execute(
        "SELECT ordinal, last_window_key, cutoff_utc FROM firmware_window_timing"
    ).fetchone()
    reopened.close()

    assert tuple(row) == (first["ordinal"], first["last_window_key"], first["cutoff_utc"])


def test_active_job_stores_the_in_flight_device(mock_db):
    db.save_active_job("job-1", "running", '["192.0.2.1","192.0.2.2"]', "target.bin", "192.0.2.2")

    job = db.get_active_jobs()[0]

    assert job["device_ips_json"] == '["192.0.2.1","192.0.2.2"]'
    assert job["in_flight_device_ip"] == "192.0.2.2"


def test_legacy_upgrade_closes_readiness_once_and_keeps_pause_history(mock_db):
    mock_db.execute(
        "INSERT OR REPLACE INTO settings (key, value) "
        "VALUES ('scheduled_rollout_safety_ready', 'true')"
    )
    mock_db.execute(
        "INSERT INTO rollouts (firmware_file, phase, status) VALUES ('old.bin', 'pct50', 'paused')"
    )
    mock_db.execute(
        "INSERT INTO firmware_window_stops "
        "(window_key, job_id, device_ip, gate, reason, stopped_at) "
        "VALUES ('[1,\"2026-11-03\"]', 'job-old', '192.0.2.1', 'flash', 'failed', '2026-11-03T03:00:00Z')"
    )
    mock_db.execute(
        "INSERT INTO job_history (job_id, started_at, completed_at, success_count) "
        "VALUES ('job-old', '2026-11-03T03:00:00Z', '2026-11-03T03:10:00Z', 1)"
    )
    mock_db.execute("ALTER TABLE active_jobs DROP COLUMN in_flight_device_ip")
    mock_db.commit()

    db._migrate(mock_db)

    assert mock_db.execute(
        "SELECT value FROM settings WHERE key = 'scheduled_rollout_safety_ready'"
    ).fetchone()[0] == "false"
    assert mock_db.execute("SELECT status FROM rollouts").fetchone()[0] == "paused"
    assert mock_db.execute("SELECT COUNT(*) FROM firmware_window_stops").fetchone()[0] == 1
    assert mock_db.execute("SELECT COUNT(*) FROM job_history").fetchone()[0] == 1
    mock_db.execute(
        "UPDATE settings SET value = 'true' WHERE key = 'scheduled_rollout_safety_ready'"
    )
    mock_db.commit()

    db._migrate(mock_db)

    assert mock_db.execute(
        "SELECT value FROM settings WHERE key = 'scheduled_rollout_safety_ready'"
    ).fetchone()[0] == "true"
