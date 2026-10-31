"""Tests for the bridge-v1 export (#285). docs/bridge-format.md defines the format."""

import hashlib
import io
import json
import tarfile

import pytest
from cryptography.fernet import Fernet

from updater import backup, crypto, database as db


PASSPHRASE = "bridge-test-passphrase"


@pytest.fixture
def storage_key(monkeypatch, tmp_path):
    """Use a fresh storage key file for the test, never the real key."""
    key = Fernet.generate_key()
    key_file = tmp_path / ".encryption_key"
    key_file.write_bytes(key + b"\n")
    monkeypatch.setattr(crypto, "_KEY_PATH", key_file)
    monkeypatch.setattr(crypto, "_fernet", Fernet(key))
    return key


def _seed(mock_db):
    site_id = db.create_tower_site("North Tower")
    db.upsert_access_point("10.0.0.1", "root", "ap-secret-1", tower_site_id=site_id)
    db.upsert_access_point("10.0.0.2", "admin", "ap-secret-2")
    db.upsert_switch("10.0.0.3", "root", "sw-secret-3")
    mock_db.execute(
        "INSERT INTO users (username, password_hash, role, auth_method) VALUES (?, ?, ?, ?)",
        ("ops1", "$2b$12$examplehash", "operator", "local"),
    )
    db.set_setting("webhook_secret", "hook-secret")
    db.save_device_config("10.0.0.1", json.dumps({"a": 1}), "hash-live")
    db.save_device_config("10.0.0.2", json.dumps({"b": 2}), "hash-bin")
    db.delete_device("10.0.0.2")
    mock_db.commit()


def _open(blob: bytes) -> dict[str, bytes]:
    tar_bytes = backup.decrypt_bridge_export(blob, PASSPHRASE)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


def _source_rows(mock_db, table: str) -> list[dict]:
    return [dict(r) for r in mock_db.execute(f"SELECT * FROM {table} ORDER BY rowid")]


class TestBridgeExport:
    def test_archive_matches_seeded_source(self, mock_db, storage_key):
        _seed(mock_db)

        files = _open(backup.build_bridge_export(PASSPHRASE))

        expected_names = {"manifest.json", "key.bin"} | {f"{t}.json" for t in backup.BRIDGE_TABLES}
        assert set(files) == expected_names

        manifest = json.loads(files["manifest.json"])
        assert manifest["format"] == "bridge-v1"
        assert manifest["schema_version"] == backup.BRIDGE_SCHEMA_VERSION
        for table in backup.BRIDGE_TABLES:
            source_count = mock_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert manifest["tables"][table]["rows"] == source_count
            assert len(json.loads(files[f"{table}.json"])) == source_count
        for name, data in files.items():
            if name != "manifest.json":
                assert manifest["sha256"][name] == hashlib.sha256(data).hexdigest()

        # key.bin decrypts every device credential back to the source value.
        assert files["key.bin"] == storage_key + b"\n"
        archive_key = Fernet(files["key.bin"].strip())
        devices = {d["ip"]: d for d in json.loads(files["devices.json"])}
        source_devices = _source_rows(mock_db, "devices")
        assert set(devices) == {d["ip"] for d in source_devices}
        for src in source_devices:
            archived = devices[src["ip"]]
            assert archived["password"] == src["password"]
            assert archive_key.decrypt(archived["password"].encode()).decode() == crypto.decrypt_password(src["password"])
            assert archived["username"] == src["username"]
        assert archive_key.decrypt(devices["10.0.0.3"]["password"].encode()) == b"sw-secret-3"

        assert json.loads(files["users.json"]) == _source_rows(mock_db, "users")

        # Settings keep every key and the stored ciphertext.
        settings = {s["key"]: s["value"] for s in json.loads(files["settings.json"])}
        source_settings = {r["key"]: r["value"] for r in _source_rows(mock_db, "settings")}
        assert settings == source_settings
        assert archive_key.decrypt(settings["webhook_secret"].encode()) == b"hook-secret"

        # Snapshots in the recycle bin come along.
        configs = json.loads(files["device_configs.json"])
        assert any(c["deleted_at"] for c in configs)

    def test_excluded_tables_are_not_in_archive(self, mock_db, storage_key):
        files = _open(backup.build_bridge_export(PASSPHRASE))
        for table in ("radius_users", "sessions", "config_enforce_log", "rollouts", "link_metrics"):
            assert f"{table}.json" not in files

    def test_wrong_passphrase_fails(self, mock_db, storage_key):
        blob = backup.build_bridge_export(PASSPHRASE)
        with pytest.raises(ValueError, match="Wrong passphrase"):
            backup.decrypt_bridge_export(blob, "not-the-passphrase")

    def test_bad_header_fails(self):
        with pytest.raises(ValueError, match="not a bridge-v1 archive"):
            backup.decrypt_bridge_export(b"# salt=abc\nip,username\n", PASSPHRASE)

    def test_missing_key_file_fails(self, mock_db, storage_key, tmp_path, monkeypatch):
        monkeypatch.setattr(crypto, "_KEY_PATH", tmp_path / "missing-key")
        with pytest.raises(FileNotFoundError):
            backup.build_bridge_export(PASSPHRASE)

    def test_damaged_key_file_fails(self, mock_db, storage_key, tmp_path, monkeypatch):
        bad_key = tmp_path / "bad-key"
        bad_key.write_bytes(b"not-a-fernet-key\n")
        monkeypatch.setattr(crypto, "_KEY_PATH", bad_key)
        with pytest.raises(ValueError):
            backup.build_bridge_export(PASSPHRASE)

    def test_missing_table_fails(self, mock_db, storage_key):
        mock_db.execute("DROP TABLE freeze_windows")
        with pytest.raises(Exception, match="freeze_windows"):
            backup.build_bridge_export(PASSPHRASE)


class TestBridgeRoute:
    def test_admin_downloads_archive(self, authed_client, mock_db, storage_key):
        resp = authed_client.post("/api/backup/bridge", json={"passphrase": PASSPHRASE})
        assert resp.status_code == 200
        assert "sixtyops-bridge-" in resp.headers["content-disposition"]
        files = _open(resp.content)
        assert json.loads(files["manifest.json"])["format"] == "bridge-v1"

    def test_short_passphrase_rejected(self, authed_client, storage_key):
        resp = authed_client.post("/api/backup/bridge", json={"passphrase": "short"})
        assert resp.status_code == 400

    def test_operator_forbidden(self, operator_client, storage_key):
        resp = operator_client.post("/api/backup/bridge", json={"passphrase": PASSPHRASE})
        assert resp.status_code == 403
