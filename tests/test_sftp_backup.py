"""Backup must be restorable: the credential encryption key travels with the DB.

Before this, the SFTP backup shipped the database (which stores device/RADIUS
passwords as Fernet ciphertext) but never the key that decrypts them. Restoring
onto a fresh host — the exact disaster-recovery case — produced a manager that
could not authenticate to any device, while the UI implied the backup was
complete. These tests pin the round-trip closed.
"""

import io
import sqlite3
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet, InvalidToken

from updater import crypto
from updater import sftp_backup


def _paths(tmp_path):
    data = tmp_path / "data"
    staging = tmp_path / "staging"
    data.mkdir()
    staging.mkdir()
    return data, staging


def _patched(tmp_path):
    data, staging = _paths(tmp_path)
    return patch.multiple(
        "updater.sftp_backup",
        STAGING_DIR=staging,
        DATA_DIR=data,
        DB_FILE=data / "sixtyops.db",
    ), patch("updater.crypto._KEY_PATH", data / ".encryption_key"), data, staging


def _make_db(db_file: Path, token: str):
    con = sqlite3.connect(str(db_file))
    con.execute("CREATE TABLE creds (v TEXT)")
    con.execute("INSERT INTO creds VALUES (?)", (token,))
    con.commit()
    con.close()


def test_backup_archive_includes_encryption_key(tmp_path):
    p_mod, p_key, data, staging = _patched(tmp_path)
    with p_mod, p_key:
        crypto.reset_cache()
        # Generates the key file as a side effect.
        crypto.encrypt_password("x")
        _make_db(data / "sixtyops.db", "tok")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            sftp_backup._add_database(tar)
            sftp_backup._add_encryption_key(tar)

        with tarfile.open(fileobj=io.BytesIO(buf.getvalue()), mode="r:gz") as tar:
            names = tar.getnames()
            assert "sixtyops.db" in names
            assert ".encryption_key" in names
            key_bytes = tar.extractfile(".encryption_key").read()
        assert key_bytes == (data / ".encryption_key").read_bytes()
    crypto.reset_cache()


def test_restore_round_trip_decrypts_on_fresh_host(tmp_path):
    """The core guarantee: a device password backed up under key A still
    decrypts after restoring onto a host that had its own key B."""
    p_mod, p_key, data, staging = _patched(tmp_path)
    key_file = data / ".encryption_key"
    db_file = data / "sixtyops.db"
    with p_mod, p_key:
        crypto.reset_cache()
        secret = "device-pw-123"
        token = crypto.encrypt_password(secret)   # creates key A
        key_a = key_file.read_bytes()
        _make_db(db_file, token)

        # Build the backup archive (DB + key A).
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            sftp_backup._add_database(tar)
            sftp_backup._add_encryption_key(tar)
        archive = staging / "sixtyops-backup-test.tar.gz"
        archive.write_bytes(buf.getvalue())

        # Simulate a FRESH host: different key B, no prior DB.
        db_file.unlink()
        key_file.write_bytes(Fernet.generate_key())
        crypto.reset_cache()
        with pytest.raises(InvalidToken):
            crypto.decrypt_password(token)   # key B can't read key A's ciphertext

        ok, msg = sftp_backup._restore_from_archive(archive)
        assert ok is True

        # Key A is restored and the running process picks it up (cache reset).
        assert key_file.read_bytes() == key_a
        con = sqlite3.connect(str(db_file))
        restored_token = con.execute("SELECT v FROM creds").fetchone()[0]
        con.close()
        assert crypto.decrypt_password(restored_token) == secret
    crypto.reset_cache()


def test_legacy_archive_without_key_restores_db_and_keeps_local_key(tmp_path):
    """An older archive (no key member) must still restore the DB and must not
    clobber the host's existing key."""
    p_mod, p_key, data, staging = _patched(tmp_path)
    key_file = data / ".encryption_key"
    db_file = data / "sixtyops.db"
    with p_mod, p_key:
        crypto.reset_cache()
        _make_db(db_file, "tok")

        # Legacy archive: DB only, no .encryption_key.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            sftp_backup._add_database(tar)
        archive = staging / "sixtyops-backup-legacy.tar.gz"
        archive.write_bytes(buf.getvalue())

        local_key = Fernet.generate_key()
        key_file.write_bytes(local_key)
        db_file.unlink()

        ok, msg = sftp_backup._restore_from_archive(archive)
        assert ok is True
        assert db_file.exists()
        assert key_file.read_bytes() == local_key  # untouched
    crypto.reset_cache()
