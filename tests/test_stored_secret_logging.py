"""Check stored-secret hooks with synthetic data and local log output."""

import ast
import base64
import io
import logging
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from updater import backup, crypto, database as db, logging_filter as scrub


@pytest.fixture(autouse=True)
def synthetic_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(scrub, "_secrets", set())
    monkeypatch.setattr(scrub, "_pattern", None)
    monkeypatch.setattr(crypto, "_KEY_PATH", tmp_path / "synthetic-key")
    monkeypatch.setattr(crypto, "_fernet", None)


@pytest.fixture
def output(monkeypatch):
    from updater import app

    root = logging.getLogger()
    old_level = root.level
    stream = io.StringIO()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(sys, "stderr", stream)
    app.configure_logging()
    yield stream
    root.handlers[0].close()
    root.setLevel(old_level)


def assert_hidden(output, *values):
    logger = logging.getLogger("updater.synthetic_sources")
    for value in values:
        logger.warning("Stored source failed: %s; retry 2", value)
        try:
            raise ValueError(f"Synthetic failure: {value}")
        except ValueError:
            logger.exception("Read failed; retry 2")
    text = output.getvalue()
    assert "retry 2" in text
    assert "ValueError: Synthetic failure: [REDACTED]" in text
    assert all(value not in text for value in values)


@pytest.mark.parametrize("existing", [True, False])
def test_key_load_and_generation(output, monkeypatch, existing):
    key = Fernet.generate_key()
    if existing:
        crypto._KEY_PATH.write_bytes(key + b"\n")
    else:
        monkeypatch.setattr(Fernet, "generate_key", lambda: key)
    crypto.get_fernet()
    assert key.decode() in scrub._secrets
    pattern = scrub._pattern
    crypto.reset_cache()
    crypto.get_fernet()
    assert scrub._pattern is pattern
    assert_hidden(output, key.decode())


def test_crypto_registers_before_encrypt_and_after_decrypt(output, monkeypatch):
    key = Fernet.generate_key()
    crypto._KEY_PATH.write_bytes(key)
    secret = "synthetic-encrypt-password"
    original = Fernet.encrypt

    def failing_encrypt(self, data):
        assert secret in scrub._secrets
        raise ValueError(secret)

    monkeypatch.setattr(Fernet, "encrypt", failing_encrypt)
    with pytest.raises(ValueError):
        crypto.encrypt_password(secret)
    monkeypatch.setattr(Fernet, "encrypt", original)
    decrypted = "synthetic-decrypt-password"
    token = Fernet(key).encrypt(decrypted.encode()).decode()
    assert crypto.decrypt_password(token) == decrypted
    assert_hidden(output, secret, decrypted, key.decode())


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("role", ["ap", "switch"])
def test_device_read_hooks(output, mock_db, encrypted, role):
    secret = f"synthetic-{role}-{encrypted}-password"
    stored = secret
    if encrypted:
        crypto.get_fernet()
        stored = Fernet(crypto._KEY_PATH.read_bytes().strip()).encrypt(secret.encode()).decode()
    mock_db.execute(
        "INSERT INTO devices (ip, username, password, role) VALUES (?, ?, ?, ?)",
        ("192.0.2.1", "synthetic-user", stored, role),
    )
    read_one = db.get_access_point if role == "ap" else db.get_switch
    read_all = db.get_access_points if role == "ap" else db.get_switches
    assert read_one("192.0.2.1")["password"] == secret
    pattern = scrub._pattern
    assert read_all()[0]["password"] == secret
    assert scrub._pattern is pattern
    assert_hidden(output, secret)


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("key", sorted(db.SECRET_SETTINGS_KEYS) + ["oidc_id_token_synthetic"])
def test_setting_read_hooks(output, mock_db, encrypted, key):
    secret = f"synthetic-setting-{key}-{encrypted}"
    stored = secret
    if encrypted:
        crypto.get_fernet()
        stored = Fernet(crypto._KEY_PATH.read_bytes().strip()).encrypt(secret.encode()).decode()
    mock_db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, stored))
    assert db.get_setting(key) == secret
    pattern = scrub._pattern
    assert db.get_all_settings()[key] == secret
    assert db.get_setting(key) == secret
    assert scrub._pattern is pattern
    assert_hidden(output, secret)


def test_empty_and_nonsecret_reads_do_not_register(mock_db):
    mock_db.execute("INSERT INTO devices (ip, username, password, role) VALUES ('192.0.2.1', 'synthetic-user', '', 'ap')")
    mock_db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('email_smtp_password', '')")
    mock_db.execute("INSERT INTO settings (key, value) VALUES ('synthetic_public', 'public-context')")
    assert db.get_access_point("192.0.2.1")["password"] == ""
    assert db.get_setting("email_smtp_password") == ""
    assert db.get_setting("synthetic_public") == "public-context"
    crypto.encrypt_password("")
    assert "" not in scrub._secrets
    assert "public-context" not in scrub._secrets


def test_backup_export_import_and_failure_hooks(output, mock_db, monkeypatch):
    passphrase = "synthetic-backup-passphrase"
    salt = b"synthetic-salt16"
    original_urandom = backup.os.urandom
    monkeypatch.setattr(backup.os, "urandom",
                        lambda length: salt if length == 16 else original_urandom(length))
    db.upsert_access_point("192.0.2.2", "synthetic-user", "synthetic-export-password")
    csv_content, salt_b64 = backup.build_csv_export(passphrase)
    assert salt_b64 == base64.b64encode(salt).decode()
    key = backup._derive_key(passphrase, salt).decode()
    pattern = scrub._pattern
    result = backup.process_csv_import(csv_content, passphrase, conflict_mode="update")
    assert result["devices"]["updated"] == 1
    assert result["devices"]["failed"] == 0
    assert db.get_access_point("192.0.2.2")["password"] == "synthetic-export-password"
    assert scrub._pattern is pattern
    with pytest.raises(ValueError, match="missing the salt"):
        backup.process_csv_import("invalid", "synthetic-invalid-import-passphrase")
    wrong = "synthetic-wrong-backup-passphrase"
    assert backup.process_csv_import(csv_content, wrong)["devices"]["failed"] == 1
    assert_hidden(output, passphrase, key, wrong, "synthetic-invalid-import-passphrase")


def test_logging_is_configured_before_application_imports():
    from updater import app

    tree = ast.parse(Path(app.__file__).read_text())
    configure_line = next(n.lineno for n in tree.body if isinstance(n, ast.Expr)
                          and isinstance(n.value, ast.Call)
                          and isinstance(n.value.func, ast.Name)
                          and n.value.func.id == "configure_logging")
    source_imports = [n.lineno for n in tree.body if isinstance(n, ast.ImportFrom)
                      and n.level and n.module != "logging_filter"]
    assert all(configure_line < line for line in source_imports)
