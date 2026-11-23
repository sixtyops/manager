"""Check default credential precedence and database compatibility."""

import pytest

from updater import crypto, database as db
from updater.device import credentials


@pytest.mark.parametrize("stored_defaults", [False, True])
@pytest.mark.parametrize("supplied", [(None, None), ("test-device", None), ("test-device", "test-device-password")])
def test_effective_device_credentials(mock_db, stored_defaults, supplied):
    defaults = credentials.DeviceAuthConfig(True, "test-default", "test-default-password")
    if stored_defaults:
        credentials.set_device_auth_config(defaults)

    if stored_defaults and not all(supplied):
        expected = defaults.username, defaults.password
    else:
        expected = supplied[0] or "", supplied[1] or ""
    assert credentials.get_device_credentials(*supplied) == expected


def test_reads_defaults_from_legacy_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "manager.db")
    monkeypatch.setattr(db, "_settings_cache", None)
    monkeypatch.setattr(db, "_settings_cache_time", 0)
    monkeypatch.setattr(crypto, "_KEY_PATH", tmp_path / ".encryption_key")
    monkeypatch.setattr(crypto, "_fernet", None)
    db.init_db()

    defaults = "test-default", "test-legacy-password"
    with db.get_db() as conn:
        conn.executemany("UPDATE settings SET value = ? WHERE key = ?", [
            ("true", "device_default_auth_enabled"),
            (defaults[0], "device_default_username"),
            (defaults[1], "device_default_password"),
        ])

    db.init_db()
    assert credentials.get_device_credentials() == defaults
