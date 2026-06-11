"""Seed data must never poison a real install.

`scripts/seed_dev_data.py` runs in the production container whenever SEED_DATA=1.
It used to plant a known admin password, mark setup complete, and turn auto-update
on — so a stray SEED_DATA=1 in a production .env handed a live host a known login
and an auto-pushing scheduler. These tests pin that it (a) refuses to touch a
configured database and (b) plants no credentials or auto-update on a fresh one.
"""

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_seed():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "seed_dev_data", root / "scripts" / "seed_dev_data.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _minimal_db(path: Path):
    con = sqlite3.connect(str(path))
    con.executescript(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);"
        "CREATE TABLE devices (id INTEGER PRIMARY KEY, ip TEXT);"
        "CREATE TABLE tower_sites (id INTEGER PRIMARY KEY, name TEXT);"
    )
    con.commit()
    return con


def test_is_configured_detects_each_signal(tmp_path):
    seed = _load_seed()
    con = _minimal_db(tmp_path / "db.sqlite")
    assert seed._is_configured(con) is False

    con.execute("INSERT INTO settings VALUES ('setup_completed', 'true')")
    con.commit()
    assert seed._is_configured(con) is True

    con.execute("DELETE FROM settings")
    con.execute("INSERT INTO settings VALUES ('admin_password_hash', 'x')")
    con.commit()
    assert seed._is_configured(con) is True

    con.execute("DELETE FROM settings")
    con.execute("INSERT INTO devices (ip) VALUES ('10.0.0.1')")
    con.commit()
    assert seed._is_configured(con) is True
    con.close()


@pytest.mark.parametrize("configured_sql", [
    "INSERT INTO settings VALUES ('setup_completed', 'true')",
    "INSERT INTO settings VALUES ('admin_password_hash', 'deadbeef')",
    "INSERT INTO users (username) VALUES ('realadmin')",
    "INSERT INTO devices (ip) VALUES ('10.0.0.1')",
])
def test_seed_refuses_configured_db(tmp_path, configured_sql):
    seed = _load_seed()
    db_path = tmp_path / "sixtyops.db"
    con = _minimal_db(db_path)
    con.execute(configured_sql)
    con.commit()
    con.close()

    with patch.object(seed, "DB_PATH", db_path):
        seed.seed()

    con = sqlite3.connect(str(db_path))
    # Refused before seeding: no sample sites inserted, no creds touched.
    assert con.execute("SELECT COUNT(*) FROM tower_sites").fetchone()[0] == 0
    assert con.execute(
        "SELECT value FROM settings WHERE key='setup_completed'"
    ).fetchone() in (None, ("true",))  # unchanged
    con.close()


def test_fresh_seed_plants_no_credentials(tmp_path):
    """On a clean DB, seeding adds sample devices but no admin / setup-complete /
    auto-update — login stays the operator's responsibility."""
    seed = _load_seed()
    from updater import database

    db_path = tmp_path / "sixtyops.db"
    with patch.object(database, "DB_PATH", db_path):
        database.init_db()
    with patch.object(seed, "DB_PATH", db_path):
        seed.seed()

    con = sqlite3.connect(str(db_path))

    def setting(key):
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    # Sample inventory WAS seeded.
    assert con.execute("SELECT COUNT(*) FROM devices").fetchone()[0] > 0
    # But nothing dangerous was planted: no admin, no setup bypass, and the
    # seed did not flip the firmware scheduler on (it stays at its init
    # default of "false"). autoupdate_enabled is a separate system default
    # ("true" from init_db) that the seed no longer touches.
    assert setting("setup_completed") != "true"
    assert setting("admin_password_hash") == ""
    assert setting("schedule_enabled") == "false"
    assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    con.close()
