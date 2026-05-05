"""Tests for the config-snapshot recycle bin: soft-delete, restore, purge,
hardware-id auto-rebind, and manager-backup round-trip."""

import json

import pytest

from updater import backup, database as db


# ────────────────────────────────────────────────────────────────────────────
# Soft-delete cascade on device removal
# ────────────────────────────────────────────────────────────────────────────

def _seed_snapshots(ip: str, hardware_id: str, hashes: list[str], mac: str = None):
    for h in hashes:
        db.save_device_config(
            ip,
            json.dumps({"hash": h}),
            h,
            model="tn-110-prs",
            hardware_id=hardware_id,
            mac=mac,
        )


class TestSoftDeleteCascade:
    def test_delete_device_soft_deletes_snapshots(self, mock_db):
        db.upsert_access_point("10.0.0.1", "root", "pass")
        # Set a system_name so we can verify it's captured as device_label
        mock_db.execute(
            "UPDATE devices SET system_name = ? WHERE ip = ?",
            ("AP-Lobby", "10.0.0.1"),
        )
        mock_db.commit()
        _seed_snapshots("10.0.0.1", "tn-110-prs", ["hash-a", "hash-b"])

        assert len(db.get_device_config_history("10.0.0.1")) == 2

        db.delete_device("10.0.0.1")

        # Live history empty
        assert db.get_device_config_history("10.0.0.1") == []
        assert db.get_latest_device_config("10.0.0.1") is None
        assert db.get_latest_config_hash("10.0.0.1") is None

        # Recycle-bin entry exists with the captured label
        bin_entries = db.get_recycle_bin_summary()
        assert len(bin_entries) == 1
        assert bin_entries[0]["ip"] == "10.0.0.1"
        assert bin_entries[0]["device_label"] == "AP-Lobby"
        assert bin_entries[0]["snapshot_count"] == 2

    def test_cleanup_ignores_soft_deleted_rows(self, mock_db):
        db.upsert_access_point("10.0.0.2", "root", "pass")
        _seed_snapshots("10.0.0.2", "tn-110-prs", [f"h{i}" for i in range(5)])
        db.delete_device("10.0.0.2")

        # Add a new device at a new IP with many snapshots, exceeding the cap
        db.upsert_access_point("10.0.0.3", "root", "pass")
        _seed_snapshots("10.0.0.3", "tn-110-prs", [f"x{i}" for i in range(10)])

        db.cleanup_old_device_configs(max_per_device=3)

        # Live history trimmed to 3
        assert len(db.get_device_config_history("10.0.0.3")) == 3
        # Recycle-bin entry untouched
        bin_history = db.get_recycle_bin_history("10.0.0.2")
        assert len(bin_history) == 5

    def test_restore_recycle_bin_returns_history(self, mock_db):
        db.upsert_access_point("10.0.0.4", "root", "pass")
        _seed_snapshots("10.0.0.4", "tn-110-prs", ["a", "b"])
        db.delete_device("10.0.0.4")
        assert db.get_device_config_history("10.0.0.4") == []

        restored = db.restore_recycle_bin("10.0.0.4")
        assert restored == 2
        assert len(db.get_device_config_history("10.0.0.4")) == 2
        assert db.get_recycle_bin_summary() == []

    def test_cpe_snapshots_cascade_to_recycle_bin(self, mock_db):
        # CPE snapshots should also move to the recycle bin when the parent AP
        # is deleted, keyed by each CPE's own IP and labeled with its name.
        db.upsert_access_point("10.0.0.1", "root", "pass")
        mock_db.execute(
            "UPDATE devices SET system_name = ? WHERE ip = ?",
            ("AP-Lobby", "10.0.0.1"),
        )
        # Two attached CPEs, each with their own snapshots
        mock_db.execute(
            "INSERT INTO cpe_cache (ap_ip, ip, mac, system_name) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "10.0.1.10", "AA:BB:CC:00:01:01", "CPE-Tenant-A"),
        )
        mock_db.execute(
            "INSERT INTO cpe_cache (ap_ip, ip, mac, system_name) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "10.0.1.11", "AA:BB:CC:00:01:02", "CPE-Tenant-B"),
        )
        mock_db.commit()
        _seed_snapshots("10.0.0.1", "tn-110-prs", ["ap-h"])
        _seed_snapshots("10.0.1.10", "tn-303-l", ["cpe-a-h"])
        _seed_snapshots("10.0.1.11", "tn-303-l", ["cpe-b-h"])

        db.delete_device("10.0.0.1")

        # All three IPs' snapshots are in the recycle bin
        bin_summary = {e["ip"]: e for e in db.get_recycle_bin_summary()}
        assert set(bin_summary.keys()) == {"10.0.0.1", "10.0.1.10", "10.0.1.11"}
        assert bin_summary["10.0.0.1"]["device_label"] == "AP-Lobby"
        assert bin_summary["10.0.1.10"]["device_label"] == "CPE-Tenant-A"
        assert bin_summary["10.0.1.11"]["device_label"] == "CPE-Tenant-B"

        # Live history is empty for all three IPs
        for ip_ in ("10.0.0.1", "10.0.1.10", "10.0.1.11"):
            assert db.get_device_config_history(ip_) == []

    def test_purge_recycle_bin_hard_deletes(self, mock_db):
        db.upsert_access_point("10.0.0.5", "root", "pass")
        _seed_snapshots("10.0.0.5", "tn-110-prs", ["a", "b", "c"])
        db.delete_device("10.0.0.5")

        purged = db.purge_recycle_bin("10.0.0.5")
        assert purged == 3
        assert db.get_recycle_bin_summary() == []
        # And the rows are truly gone — no soft-deleted history to restore
        assert db.restore_recycle_bin("10.0.0.5") == 0


# ────────────────────────────────────────────────────────────────────────────
# MAC-based auto-rebind orphan lookup
# ────────────────────────────────────────────────────────────────────────────

MAC_A = "AA:BB:CC:00:00:01"
MAC_B = "AA:BB:CC:00:00:02"


class TestMacRebind:
    def test_orphan_lookup_finds_unmanaged_ip(self, mock_db):
        # Old device existed, was hard-deleted from devices table directly
        # (simulate: snapshot rows exist under 10.0.0.10, but device row is gone).
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["old-hash"], mac=MAC_A)
        # Current poll is for a newly-managed device at 10.0.0.20 with the same MAC
        db.upsert_access_point("10.0.0.20", "root", "pass")

        orphans = db.find_orphan_snapshots_by_mac(MAC_A, "10.0.0.20")
        assert orphans == ["10.0.0.10"]

    def test_orphan_lookup_is_case_insensitive(self, mock_db):
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["h"], mac=MAC_A)
        db.upsert_access_point("10.0.0.20", "root", "pass")

        # Lower-case query should still match upper-case stored MAC
        orphans = db.find_orphan_snapshots_by_mac(MAC_A.lower(), "10.0.0.20")
        assert orphans == ["10.0.0.10"]

    def test_orphan_lookup_excludes_managed_ips(self, mock_db):
        # If the old IP is still a managed device, it's not an orphan
        db.upsert_access_point("10.0.0.10", "root", "pass")
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["h1"], mac=MAC_A)
        db.upsert_access_point("10.0.0.20", "root", "pass")

        # Same MAC under a managed IP should not be flagged as an orphan
        orphans = db.find_orphan_snapshots_by_mac(MAC_A, "10.0.0.20")
        assert orphans == []

    def test_orphan_lookup_excludes_managed_cpe_ips(self, mock_db):
        # If the old IP is still in cpe_cache (managed CPE), it's not an orphan
        db.upsert_access_point("10.0.0.1", "root", "pass")
        mock_db.execute(
            "INSERT INTO cpe_cache (ap_ip, ip, mac) VALUES (?, ?, ?)",
            ("10.0.0.1", "10.0.0.10", MAC_A),
        )
        mock_db.commit()
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["h"], mac=MAC_A)

        orphans = db.find_orphan_snapshots_by_mac(MAC_A, "10.0.0.99")
        assert orphans == []

    def test_orphan_lookup_only_matches_by_mac_not_model(self, mock_db):
        # Two unrelated devices of the same model should not collide
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["h1"], mac=MAC_A)
        _seed_snapshots("10.0.0.11", "tn-110-prs", ["h2"], mac=MAC_B)
        db.upsert_access_point("10.0.0.20", "root", "pass")

        orphans = db.find_orphan_snapshots_by_mac(MAC_A, "10.0.0.20")
        assert orphans == ["10.0.0.10"]
        orphans = db.find_orphan_snapshots_by_mac(MAC_B, "10.0.0.20")
        assert orphans == ["10.0.0.11"]

    def test_orphan_lookup_excludes_soft_deleted(self, mock_db):
        # Soft-deleted snapshots stay in the recycle bin and shouldn't trigger auto-rebind
        db.upsert_access_point("10.0.0.30", "root", "pass")
        _seed_snapshots("10.0.0.30", "tn-110-prs", ["h"], mac=MAC_A)
        db.delete_device("10.0.0.30")

        db.upsert_access_point("10.0.0.40", "root", "pass")
        orphans = db.find_orphan_snapshots_by_mac(MAC_A, "10.0.0.40")
        assert orphans == []

    def test_orphan_lookup_returns_empty_for_null_mac(self, mock_db):
        assert db.find_orphan_snapshots_by_mac(None, "10.0.0.1") == []
        assert db.find_orphan_snapshots_by_mac("", "10.0.0.1") == []

    def test_rebind_moves_snapshots(self, mock_db):
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["old1", "old2"], mac=MAC_A)
        db.upsert_access_point("10.0.0.20", "root", "pass")

        moved = db.rebind_snapshots("10.0.0.10", "10.0.0.20", MAC_A)
        assert moved == 2

        # Snapshots now live under the new IP
        assert len(db.get_device_config_history("10.0.0.20")) == 2
        # And the old IP has nothing
        assert db.get_device_config_history("10.0.0.10") == []

    def test_rebind_does_not_move_other_macs(self, mock_db):
        # Two units' snapshots co-existing under the same old IP (shouldn't happen,
        # but defensively confirm rebind only moves the matching MAC's rows).
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["a"], mac=MAC_A)
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["b"], mac=MAC_B)

        moved = db.rebind_snapshots("10.0.0.10", "10.0.0.20", MAC_A)
        assert moved == 1

        remaining = db.get_device_config_history("10.0.0.10")
        assert len(remaining) == 1
        assert remaining[0]["config_hash"] == "b"


# ────────────────────────────────────────────────────────────────────────────
# Manager backup round-trip preserves snapshots and recycle bin
# ────────────────────────────────────────────────────────────────────────────

class TestBackupRoundTrip:
    def test_export_import_preserves_snapshots(self, mock_db):
        db.upsert_access_point("10.0.0.50", "root", "pass")
        _seed_snapshots("10.0.0.50", "tn-110-prs", ["h-live-1", "h-live-2"])

        # And one device that ends up in the recycle bin
        db.upsert_access_point("10.0.0.51", "root", "pass")
        _seed_snapshots("10.0.0.51", "tn-110-prs", ["h-bin-1"])
        db.delete_device("10.0.0.51")

        passphrase = "test-passphrase"
        csv_content, _ = backup.build_csv_export(passphrase)

        # Wipe snapshot table (simulate manager DR)
        with db.get_db() as conn:
            conn.execute("DELETE FROM device_configs")

        # Import — reuses passphrase
        results = backup.process_csv_import(csv_content, passphrase, conflict_mode="update")

        assert results["device_configs"]["added"] == 3
        assert results["device_configs"]["failed"] == 0

        # Live snapshots restored
        live = db.get_device_config_history("10.0.0.50")
        assert len(live) == 2
        assert {r["config_hash"] for r in live} == {"h-live-1", "h-live-2"}

        # Recycle-bin restored
        bin_entries = db.get_recycle_bin_summary()
        assert len(bin_entries) == 1
        assert bin_entries[0]["ip"] == "10.0.0.51"

    def test_reimport_is_idempotent(self, mock_db):
        db.upsert_access_point("10.0.0.60", "root", "pass")
        _seed_snapshots("10.0.0.60", "tn-110-prs", ["h"])

        passphrase = "test-passphrase"
        csv_content, _ = backup.build_csv_export(passphrase)

        # Wipe and import twice — second time the (ip, fetched_at) keys already exist
        with db.get_db() as conn:
            conn.execute("DELETE FROM device_configs")
        first = backup.process_csv_import(csv_content, passphrase, conflict_mode="update")
        second = backup.process_csv_import(csv_content, passphrase, conflict_mode="update")

        assert first["device_configs"]["added"] == 1
        assert second["device_configs"]["added"] == 0
        assert second["device_configs"]["skipped"] == 1
        assert len(db.get_device_config_history("10.0.0.60")) == 1

    def test_export_import_preserves_mac(self, mock_db):
        # Critical: MAC must survive the round-trip so auto-rebind keeps working
        # for restored history after manager DR.
        db.upsert_access_point("10.0.0.70", "root", "pass")
        _seed_snapshots("10.0.0.70", "tn-110-prs", ["h-mac"], mac=MAC_A)

        passphrase = "test-passphrase"
        csv_content, _ = backup.build_csv_export(passphrase)

        with db.get_db() as conn:
            conn.execute("DELETE FROM device_configs")
        backup.process_csv_import(csv_content, passphrase, conflict_mode="update")

        with db.get_db() as conn:
            row = conn.execute(
                "SELECT mac FROM device_configs WHERE ip = ?", ("10.0.0.70",)
            ).fetchone()
        assert row is not None
        assert row["mac"] == MAC_A.upper()

    def test_csv_injection_in_device_label_is_neutralized(self, mock_db):
        # A malicious system_name like "=2+3" should not survive as a formula
        # in the exported CSV. The export prefixes it with a single-quote and
        # the import strips that prefix back off.
        db.upsert_access_point("10.0.0.80", "root", "pass")
        with db.get_db() as conn:
            conn.execute(
                "UPDATE devices SET system_name = ? WHERE ip = ?",
                ("=cmd|'/c calc'!A1", "10.0.0.80"),
            )
        _seed_snapshots("10.0.0.80", "tn-110-prs", ["h"])
        db.delete_device("10.0.0.80")  # captures system_name as device_label

        passphrase = "test-passphrase"
        csv_content, _ = backup.build_csv_export(passphrase)

        # Find the row in the raw CSV and confirm it's defanged
        section_marker = "# section=device_configs"
        assert section_marker in csv_content
        post_marker = csv_content.split(section_marker, 1)[1]
        assert "'=cmd|" in post_marker  # leading apostrophe added
        assert "\n=cmd|" not in post_marker  # no naked formula

        # Round-trip restores the original label
        with db.get_db() as conn:
            conn.execute("DELETE FROM device_configs")
        backup.process_csv_import(csv_content, passphrase, conflict_mode="update")
        bin_summary = db.get_recycle_bin_summary()
        assert any(e["device_label"] == "=cmd|'/c calc'!A1" for e in bin_summary)


# ────────────────────────────────────────────────────────────────────────────
# HTTP endpoint coverage for the recycle-bin routes
# ────────────────────────────────────────────────────────────────────────────

class TestRecycleBinEndpoints:
    def test_list_requires_auth(self, client):
        resp = client.get("/api/configs/recycle-bin")
        assert resp.status_code in (401, 403)

    def test_list_returns_entries_for_admin(self, authed_client, mock_db):
        db.upsert_access_point("10.0.0.90", "root", "pass")
        _seed_snapshots("10.0.0.90", "tn-110-prs", ["h"])
        db.delete_device("10.0.0.90")

        resp = authed_client.get("/api/configs/recycle-bin")
        assert resp.status_code == 200
        ips = {e["ip"] for e in resp.json()["entries"]}
        assert "10.0.0.90" in ips

    def test_history_returns_soft_deleted_snapshots(self, authed_client, mock_db):
        db.upsert_access_point("10.0.0.91", "root", "pass")
        _seed_snapshots("10.0.0.91", "tn-110-prs", ["a", "b"])
        db.delete_device("10.0.0.91")

        resp = authed_client.get("/api/configs/recycle-bin/10.0.0.91")
        assert resp.status_code == 200
        history = resp.json()["history"]
        assert len(history) == 2
        for row in history:
            assert row["deleted_at"] is not None

    def test_restore_requires_admin_role(self, operator_client, mock_db):
        db.upsert_access_point("10.0.0.92", "root", "pass")
        _seed_snapshots("10.0.0.92", "tn-110-prs", ["h"])
        db.delete_device("10.0.0.92")

        # Operator (not admin) should be forbidden from restore
        resp = operator_client.post("/api/configs/recycle-bin/10.0.0.92/restore")
        assert resp.status_code == 403

    def test_restore_succeeds_for_admin(self, authed_client, mock_db):
        db.upsert_access_point("10.0.0.93", "root", "pass")
        _seed_snapshots("10.0.0.93", "tn-110-prs", ["a", "b"])
        db.delete_device("10.0.0.93")

        resp = authed_client.post("/api/configs/recycle-bin/10.0.0.93/restore")
        assert resp.status_code == 200
        assert resp.json()["snapshots_restored"] == 2
        assert len(db.get_device_config_history("10.0.0.93")) == 2

    def test_restore_404_for_unknown_ip(self, authed_client, mock_db):
        resp = authed_client.post("/api/configs/recycle-bin/10.99.99.99/restore")
        assert resp.status_code == 404

    def test_purge_requires_admin_role(self, operator_client, mock_db):
        db.upsert_access_point("10.0.0.94", "root", "pass")
        _seed_snapshots("10.0.0.94", "tn-110-prs", ["h"])
        db.delete_device("10.0.0.94")

        resp = operator_client.delete("/api/configs/recycle-bin/10.0.0.94")
        assert resp.status_code == 403

    def test_purge_succeeds_for_admin(self, authed_client, mock_db):
        db.upsert_access_point("10.0.0.95", "root", "pass")
        _seed_snapshots("10.0.0.95", "tn-110-prs", ["a", "b", "c"])
        db.delete_device("10.0.0.95")

        resp = authed_client.delete("/api/configs/recycle-bin/10.0.0.95")
        assert resp.status_code == 200
        assert resp.json()["snapshots_purged"] == 3
        assert db.get_recycle_bin_summary() == []


# ────────────────────────────────────────────────────────────────────────────
# Concurrent rebind: two new IPs racing to claim the same orphan
# ────────────────────────────────────────────────────────────────────────────

class TestConcurrentRebind:
    def test_only_one_concurrent_rebind_moves_rows(self, mock_db):
        # Simulate the race window: two new IPs both look up the orphan IP
        # via find_orphan_snapshots_by_mac, then each call rebind_snapshots.
        # The first wins; the second should report moved == 0.
        _seed_snapshots("10.0.0.10", "tn-110-prs", ["old1", "old2"], mac=MAC_A)

        # Both new IPs see the orphan
        orphans_for_a = db.find_orphan_snapshots_by_mac(MAC_A, "10.0.0.20")
        orphans_for_b = db.find_orphan_snapshots_by_mac(MAC_A, "10.0.0.21")
        assert orphans_for_a == ["10.0.0.10"]
        assert orphans_for_b == ["10.0.0.10"]

        moved_first = db.rebind_snapshots("10.0.0.10", "10.0.0.20", MAC_A)
        moved_second = db.rebind_snapshots("10.0.0.10", "10.0.0.21", MAC_A)

        assert moved_first == 2
        assert moved_second == 0  # second call's UPDATE matches nothing
        # Snapshots ended up under the first IP, not the second
        assert len(db.get_device_config_history("10.0.0.20")) == 2
        assert db.get_device_config_history("10.0.0.21") == []
