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
        assert rows[0]["verified_at"] >= first[0]["verified_at"]
        assert {k: v for k, v in rows[0].items() if k != "verified_at"} == {
            k: v for k, v in first[0].items() if k != "verified_at"
        }
        row = rows[0]
        assert row["sha256"] == hashlib.sha256(body).hexdigest()
        assert row["source"] == "uploaded"
        assert row["family"] == "tna-30x"
        assert row["version"] == "1.15.0.55151"
        assert row["release_date"] == "2026-06-09"
        assert row["path"] == FW
        # The upload route records the signed-in user as the uploader.
        assert row["uploaded_by"] == "admin"
        assert row["verified_at"]

    def test_operator_upload_does_not_attest(self, operator_client, mock_db, tmp_path):
        body = b"operator-firmware-bytes"
        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            assert self._upload(operator_client, FW, body).status_code == 200

        row = db.get_firmware_artifact(hashlib.sha256(body).hexdigest())
        assert row["source"] == "uploaded"
        assert row["uploaded_by"] is None
        assert row["verified_at"] is None
        audit = mock_db.execute(
            "SELECT username FROM audit_log WHERE action = 'firmware.upload'"
        ).fetchone()
        assert audit["username"] == "operator1"

    @pytest.mark.parametrize("vendor_verified", [False, True])
    def test_admin_upload_attests_existing_fetched_bytes(
        self, authed_client, mock_db, tmp_path, vendor_verified
    ):
        body = b"fetched-firmware-bytes"
        sha256 = hashlib.sha256(body).hexdigest()
        db.register_firmware(
            FW, source="auto", sha256=sha256,
            vendor_checksum="f" * 32 if vendor_verified else None,
            vendor_checksum_source="https://kb.example/firmware" if vendor_verified else None,
            vendor_checksum_verified=vendor_verified,
        )
        first = db.get_firmware_artifact(sha256)
        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            assert self._upload(authed_client, FW_OTHER_NAME, body).status_code == 200
            attested = db.get_firmware_artifact(sha256)
            assert self._upload(authed_client, FW, body).status_code == 200

        assert _artifact_count(mock_db) == 1
        latest = db.get_firmware_artifact(sha256)
        assert latest["verified_at"] >= attested["verified_at"]
        assert attested["uploaded_by"] == "admin"
        assert attested["verified_at"]
        assert attested["verified_at"] >= (first["verified_at"] or "")
        for column in (
            "id", "family", "version", "sha256", "source", "path", "release_date",
            "vendor_checksum", "vendor_checksum_source",
        ):
            assert attested[column] == first[column]

    def test_operator_upload_does_not_attest_existing_fetched_bytes(
        self, operator_client, mock_db, tmp_path
    ):
        body = b"fetched-firmware-bytes"
        sha256 = hashlib.sha256(body).hexdigest()
        db.register_firmware(FW, source="auto", sha256=sha256)
        first = db.get_firmware_artifact(sha256)
        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            assert self._upload(operator_client, FW_OTHER_NAME, body).status_code == 200
        assert db.get_firmware_artifact(sha256) == first


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
    def _install_pre_release_artifact_table(self, conn):
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'firmware_artifacts'"
        ).fetchone()[0]
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'firmware_artifacts_immutable'"
        ).fetchone()[0]
        conn.execute("DROP TABLE firmware_artifacts")
        schema = schema.replace("            historical_uploaded_by TEXT,\n", "")
        schema = schema.replace("            historical_verified_at TEXT,\n", "")
        conn.execute(schema.replace(
            "OR uploaded_by IS NOT NULL",
            "OR (source = 'uploaded' AND uploaded_by IS NOT NULL)",
        ))
        conn.execute(trigger)

    def test_pre_release_constraint_upgrade_preserves_rows_and_identity_guard(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "sixtyops.db")
        db.init_db()
        with db.get_db() as conn:
            self._install_pre_release_artifact_table(conn)
        db.register_firmware(FW, source="auto", sha256="a" * 64)
        db.register_firmware(
            FW_OTHER_NAME, source="manual", sha256="b" * 64, uploaded_by="admin"
        )
        before = db.get_firmware_artifacts()
        with db.get_db() as conn:
            conn.execute("UPDATE sqlite_sequence SET seq = 100 WHERE name = 'firmware_artifacts'")

        db.init_db()
        db.init_db()
        after = db.get_firmware_artifacts()
        for old, new in zip(before, after):
            for key in ("id", "sha256", "source", "path", "family", "version", "release_date"):
                assert new[key] == old[key]
        assert after[0]["historical_uploaded_by"] == "admin"
        assert after[0]["historical_verified_at"] == before[0]["verified_at"]
        assert after[0]["uploaded_by"] is None
        assert after[0]["verified_at"] is None
        db.register_firmware("new.bin", sha256="c" * 64)
        assert db.get_firmware_artifact("c" * 64)["id"] == 101
        db.register_firmware(FW, source="manual", sha256="a" * 64, uploaded_by="admin")
        attested = db.get_firmware_artifact("a" * 64)
        assert attested["source"] == "fetched"
        assert attested["uploaded_by"] == "admin"
        assert attested["verified_at"]
        with db.get_db() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("UPDATE firmware_artifacts SET source = 'uploaded' WHERE sha256 = ?", ("a" * 64,))

    @pytest.mark.parametrize("prior_actor", ["operator1", "admin"])
    def test_old_upload_mark_is_unproven_until_fresh_admin_upload(
        self, authed_client, mock_db, tmp_path, prior_actor
    ):
        self._install_pre_release_artifact_table(mock_db)
        body = b"historical-upload-bytes"
        sha256 = hashlib.sha256(body).hexdigest()
        mock_db.execute(
            "INSERT INTO firmware_artifacts (family, sha256, source, path, uploaded_by, verified_at)"
            " VALUES ('tna-30x', ?, 'uploaded', ?, ?, '2020-01-01T00:00:00+00:00')",
            (sha256, FW, prior_actor),
        )
        db.log_audit(prior_actor, "firmware.upload", "firmware", FW, "historical upload", None)
        old = db.get_firmware_artifact(sha256)
        db._migrate_firmware_artifact_attestation(mock_db)
        migrated = db.get_firmware_artifact(sha256)
        assert migrated["uploaded_by"] is None
        assert migrated["verified_at"] is None
        assert migrated["historical_uploaded_by"] == prior_actor
        assert migrated["historical_verified_at"] == old["verified_at"]
        db._migrate_firmware_artifact_attestation(mock_db)
        assert db.get_firmware_artifact(sha256) == migrated
        with patch("updater.app.FIRMWARE_DIR", tmp_path):
            assert TestUpload()._upload(authed_client, FW, body).status_code == 200
        attested = db.get_firmware_artifact(sha256)
        assert attested["id"] == old["id"]
        assert attested["source"] == old["source"]
        assert attested["uploaded_by"] == "admin"
        assert attested["verified_at"] > old["verified_at"]
        assert attested["historical_uploaded_by"] == prior_actor
        assert attested["historical_verified_at"] == old["verified_at"]
        actors = [r[0] for r in mock_db.execute(
            "SELECT username FROM audit_log WHERE action = 'firmware.upload' ORDER BY id"
        )]
        assert actors == [prior_actor, "admin"]

    def test_old_vendor_verification_survives_unproven_upload_mark(self, mock_db):
        self._install_pre_release_artifact_table(mock_db)
        mock_db.execute(
            "INSERT INTO firmware_artifacts (family, sha256, source, path, vendor_checksum,"
            " uploaded_by, verified_at) VALUES ('tna-30x', ?, 'uploaded', ?, ?,"
            " 'operator1', '2020-01-01T00:00:00+00:00')",
            ("a" * 64, FW, "f" * 32),
        )
        db._migrate_firmware_artifact_attestation(mock_db)
        row = db.get_firmware_artifact("a" * 64)
        assert row["uploaded_by"] is None
        assert row["historical_uploaded_by"] == "operator1"
        assert row["verified_at"] == row["historical_verified_at"]
        assert row["vendor_checksum"] == "f" * 32

    @pytest.mark.parametrize("populated", [False, True])
    def test_upgrade_failure_rolls_back_table_rows_sequence_and_trigger(self, mock_db, populated):
        self._install_pre_release_artifact_table(mock_db)
        mock_db.execute(
            "INSERT INTO firmware_artifacts (family, sha256, source, path) VALUES ('tna-30x', ?, 'fetched', ?)",
            ("a" * 64, FW),
        )
        mock_db.execute("UPDATE sqlite_sequence SET seq = 100 WHERE name = 'firmware_artifacts'")
        if not populated:
            mock_db.execute("DELETE FROM firmware_artifacts")
        def snapshot():
            return (
                [tuple(r) for r in mock_db.execute("SELECT * FROM firmware_artifacts")],
                [tuple(r) for r in mock_db.execute("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'firmware_artifacts' ORDER BY name")],
                mock_db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'firmware_artifacts'").fetchone()[0],
            )
        before = snapshot()
        class FailingConnection:
            def execute(self, sql, parameters=()):
                if sql.startswith("ALTER TABLE firmware_artifacts_upgrade"):
                    raise sqlite3.OperationalError("injected table swap failure")
                return mock_db.execute(sql, parameters)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            db._migrate_firmware_artifact_attestation(FailingConnection())
        assert snapshot() == before
        assert not mock_db.execute("SELECT 1 FROM sqlite_master WHERE name = 'firmware_artifacts_upgrade'").fetchone()
        db._migrate_firmware_artifact_attestation(mock_db)
        assert mock_db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'firmware_artifacts'").fetchone()[0] == 100
        if populated:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                mock_db.execute("UPDATE firmware_artifacts SET source = 'uploaded'")

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
