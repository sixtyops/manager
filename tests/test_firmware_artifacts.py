"""firmware_artifacts: one immutable row per set of firmware bytes (#301)."""

import hashlib
import sqlite3
from unittest.mock import patch

import pytest

from updater import database as db

FW = "tna-30x-1.15.0-r55151-20260609-tn-110-prs-squashfs-sysupgrade.bin"
FW_OTHER_NAME = "tna-30x-1.15.0-r55151-20260609-copy-sysupgrade.bin"


def _artifact_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM firmware_artifacts").fetchone()[0]


class TestUpload:
    def _upload(self, client, name, body):
        return client.post(
            "/api/upload-firmware",
            files={"file": (name, body, "application/octet-stream")},
        )

    def test_upload_creates_one_artifact_and_reupload_reuses_it(
        self, authed_client, mock_db, tmp_path
    ):
        body = b"uploaded-firmware-bytes"
        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            assert self._upload(authed_client, FW, body).status_code == 200
            first = db.get_firmware_artifacts()
            assert self._upload(authed_client, FW, body).status_code == 200
            assert self._upload(authed_client, FW_OTHER_NAME, body).status_code == 200

        rows = db.get_firmware_artifacts()
        assert len(rows) == 1
        assert rows == first
        row = rows[0]
        assert row["sha256"] == hashlib.sha256(body).hexdigest()
        assert row["source"] == "uploaded"
        assert row["family"] == "tna-30x"
        assert row["version"] == "1.15.0.55151"
        assert row["release_date"] == "2026-06-09"
        assert row["path"] == FW
        # The upload route does not pass uploaded_by yet, so the row is not
        # verified. That is the follow-up that needs app.py.
        assert row["verified_at"] is None


class TestRegister:
    def test_new_bytes_under_same_name_get_a_new_row(self, mock_db):
        db.register_firmware(FW, source="manual", sha256="a" * 64)
        db.register_firmware(FW, source="manual", sha256="b" * 64)

        assert _artifact_count(mock_db) == 2
        assert db.get_firmware_sha256(FW) == "b" * 64
        assert db.get_firmware_artifact("a" * 64)["path"] == FW

    def test_no_hash_writes_no_artifact(self, mock_db):
        db.register_firmware(FW, source="auto")
        assert _artifact_count(mock_db) == 0

    def test_uploaded_by_sets_verified_at(self, mock_db):
        db.register_firmware(FW, source="manual", sha256="a" * 64, uploaded_by="alice")
        row = db.get_firmware_artifact("a" * 64)
        assert row["uploaded_by"] == "alice"
        assert row["verified_at"]

    def test_uploaded_by_is_ignored_for_fetched_files(self, mock_db):
        db.register_firmware(FW, source="auto", sha256="a" * 64, uploaded_by="alice")
        row = db.get_firmware_artifact("a" * 64)
        assert row["source"] == "fetched"
        assert row["uploaded_by"] is None
        assert row["verified_at"] is None

    def test_unmatched_vendor_checksum_is_not_recorded(self, mock_db):
        db.register_firmware(
            FW, source="auto", sha256="a" * 64,
            vendor_checksum="f" * 32, vendor_checksum_verified=False,
        )
        row = db.get_firmware_artifact("a" * 64)
        assert row["vendor_checksum"] is None
        assert row["verified_at"] is None

    def test_later_vendor_match_verifies_existing_row(self, mock_db):
        db.register_firmware(FW, source="auto", sha256="a" * 64)
        db.register_firmware(
            FW, source="auto", sha256="a" * 64,
            vendor_checksum="F" * 32, vendor_checksum_source="https://kb",
            vendor_checksum_verified=True,
        )
        row = db.get_firmware_artifact("a" * 64)
        assert _artifact_count(mock_db) == 1
        assert row["vendor_checksum"] == "f" * 32
        assert row["vendor_checksum_source"] == "https://kb"
        assert row["verified_at"]

    def test_conflicting_vendor_checksum_does_not_verify(self, mock_db):
        mock_db.execute(
            "INSERT INTO firmware_artifacts (family, sha256, vendor_checksum, source, path)"
            " VALUES ('tna-30x', ?, ?, 'fetched', ?)",
            ("a" * 64, "1" * 32, FW),
        )
        db.register_firmware(
            FW, source="auto", sha256="a" * 64,
            vendor_checksum="2" * 32, vendor_checksum_verified=True,
        )
        row = db.get_firmware_artifact("a" * 64)
        assert row["vendor_checksum"] == "1" * 32
        assert row["verified_at"] is None

    def test_invalid_hash_is_rejected(self, mock_db):
        with pytest.raises(sqlite3.IntegrityError):
            db.register_firmware(FW, source="manual", sha256="not-a-hash")
        assert db.get_firmware_sha256(FW) is None


class TestConstraints:
    def test_verified_at_needs_checksum_or_uploader(self, mock_db):
        with pytest.raises(sqlite3.IntegrityError):
            mock_db.execute(
                "INSERT INTO firmware_artifacts (family, sha256, source, path, verified_at)"
                " VALUES ('tna-30x', ?, 'fetched', ?, '2026-01-01')",
                ("a" * 64, FW),
            )

    def test_source_must_be_fetched_or_uploaded(self, mock_db):
        with pytest.raises(sqlite3.IntegrityError):
            mock_db.execute(
                "INSERT INTO firmware_artifacts (family, sha256, source, path)"
                " VALUES ('tna-30x', ?, 'legacy', ?)",
                ("a" * 64, FW),
            )

    @pytest.mark.parametrize("column", ["family", "version", "sha256", "source", "path"])
    def test_identity_columns_are_immutable(self, mock_db, column):
        db.register_firmware(FW, source="manual", sha256="a" * 64)
        value = "fetched" if column == "source" else "b" * 64
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            mock_db.execute(f"UPDATE firmware_artifacts SET {column} = ?", (value,))


class TestMigration:
    def _old_db(self, path):
        """A database from before #301: registry rows, no artifacts table."""
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE firmware_registry (
                filename TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                sha256 TEXT DEFAULT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO firmware_registry VALUES (?, ?, ?, ?)",
            [
                (FW, "2026-06-10T00:00:00", "auto", "A" * 64),
                ("manual-1.0.bin", "2026-06-11T00:00:00", "manual", "b" * 64),
                ("legacy.bin", "2020-01-01T00:00:00", "legacy", "c" * 64),
                ("unhashed.bin", "2020-01-01T00:00:00", "legacy", None),
                ("bad-hash.bin", "2020-01-01T00:00:00", "manual", "not-a-hash"),
            ],
        )
        conn.commit()
        conn.close()

    def _snapshot(self):
        with db.get_db() as conn:
            registry = [tuple(r) for r in conn.execute(
                "SELECT filename, source, sha256 FROM firmware_registry ORDER BY filename"
            )]
            artifacts = [tuple(r) for r in conn.execute(
                "SELECT id, family, version, sha256, source, uploaded_by, release_date,"
                " path, verified_at FROM firmware_artifacts ORDER BY id"
            )]
        return registry, artifacts

    def test_old_database_is_backfilled_once_without_data_loss(self, tmp_path, monkeypatch):
        path = tmp_path / "data" / "old.db"
        path.parent.mkdir()
        self._old_db(path)
        monkeypatch.setattr(db, "DB_PATH", path)

        db.init_db()
        registry, artifacts = self._snapshot()

        assert [r[0] for r in registry] == sorted(
            [FW, "manual-1.0.bin", "legacy.bin", "unhashed.bin", "bad-hash.bin"]
        )
        by_sha = {a[3]: a for a in artifacts}
        assert set(by_sha) == {"a" * 64, "b" * 64, "c" * 64}
        assert by_sha["a" * 64][1:3] == ("tna-30x", "1.15.0.55151")
        assert by_sha["a" * 64][4] == "fetched"
        assert by_sha["a" * 64][6] == "2026-06-09"
        assert by_sha["b" * 64][4] == "uploaded"
        assert by_sha["c" * 64][4] == "uploaded"
        # The old registry did not record a vendor match or an uploader.
        assert all(a[8] is None for a in artifacts)

        db.init_db()
        assert self._snapshot() == (registry, artifacts)
