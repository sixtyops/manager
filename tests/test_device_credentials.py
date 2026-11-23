"""Check default credential precedence and database compatibility."""

import pytest

from updater import crypto, database as db
from updater.device import credentials


@pytest.mark.parametrize("supplied, expected", [
    ((None, None), ("", "")),
    (("test-device", None), ("test-device", "")),
    (("test-device", "test-device-password"), ("test-device", "test-device-password")),
])
def test_credentials_without_stored_defaults(mock_db, supplied, expected):
    assert credentials.get_device_credentials(*supplied) == expected


def test_startup_encrypts_legacy_defaults_in_place(tmp_path, monkeypatch):
    db_path = tmp_path / "manager.db"
    key_path = tmp_path / ".encryption_key"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "_settings_cache", None)
    monkeypatch.setattr(db, "_settings_cache_time", 0)
    monkeypatch.setattr(crypto, "_KEY_PATH", key_path)
    monkeypatch.setattr(crypto, "_fernet", None)
    db.init_db()
    default_user, default_password = "test-default", "test-legacy-password"

    with db.get_db() as conn:
        conn.executemany("UPDATE settings SET value = ? WHERE key = ?", [
            ("true", "device_default_auth_enabled"),
            (default_user, "device_default_username"),
            (default_password, "device_default_password"),
        ])

    def stored_password():
        with db.get_db() as conn:
            return conn.execute(
                "SELECT value FROM settings WHERE key = 'device_default_password'"
            ).fetchone()[0]

    db.init_db()
    ciphertext = stored_password()
    assert crypto.is_encrypted(ciphertext)
    assert crypto.decrypt_password(ciphertext) == default_password
    assert credentials.get_device_credentials() == (default_user, default_password)
    assert credentials.get_device_credentials("test-device") == (default_user, default_password)
    assert credentials.get_device_credentials("test-device", "test-device-password") == (
        "test-device", "test-device-password",
    )

    db.init_db()
    assert stored_password() == ciphertext

    credentials.set_device_auth_config(credentials.DeviceAuthConfig(
        enabled=False, username=default_user, password=default_password,
    ))
    assert crypto.is_encrypted(stored_password())
    assert credentials.get_device_credentials("test-device") == ("test-device", "")
