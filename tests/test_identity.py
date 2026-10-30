import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from updater.core.identity import match_observation, record_identity


def _device(db, ip):
    cur = db.execute(
        "INSERT INTO devices(ip,username,password) VALUES(?,?,?)",
        (ip, "admin", "secret"),
    )
    db.commit()
    return cur.lastrowid


def test_serial_and_named_interface_macs_resolve_one_stable_id(memory_db):
    device_id = _device(memory_db, "192.0.2.1")
    result = record_identity(memory_db, device_id, "  SN-1 ", {
        "radio": "001122334455", "eth0": "00:11:22:33:44:66",
        "mgmt": "00-11-22-33-44-77",
    })
    assert result["status"] == "unverified"
    assert result["verified"] is False
    assert tuple(memory_db.execute(
        "SELECT uid,uid_source FROM devices WHERE id=?", (device_id,)
    ).fetchone()) == ("SN-1", "serial")
    assert {r[0] for r in memory_db.execute("SELECT mac FROM device_macs WHERE device_id=?", (device_id,))} == {
        "00:11:22:33:44:55", "00:11:22:33:44:66", "00:11:22:33:44:77"
    }
    assert match_observation(memory_db, "SN-1")["device_ids"] == [device_id]


def test_repeats_update_last_seen_without_duplicate_observations(memory_db):
    device_id = _device(memory_db, "192.0.2.2")
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)
    record_identity(memory_db, device_id, "SN-2", {"eth0": "001122334455"}, now=first)
    record_identity(memory_db, device_id, "SN-2", {"eth0": "001122334455"}, now=later)
    row = memory_db.execute("SELECT COUNT(*),first_seen,last_seen FROM device_macs").fetchone()
    assert tuple(row) == (1, first.isoformat(), later.isoformat())


def test_ip_and_mac_changes_do_not_change_device_id(memory_db):
    device_id = _device(memory_db, "192.0.2.3")
    record_identity(memory_db, device_id, "SN-3", {"eth0": "001122334455"})
    record_identity(memory_db, device_id, "SN-3", {"eth0": "001122334466"})
    row = memory_db.execute("SELECT id,ip FROM devices WHERE id=?", (device_id,)).fetchone()
    assert tuple(row) == (device_id, "192.0.2.3")
    assert len(match_observation(memory_db, "SN-3")["device_ids"]) == 1


def test_duplicate_serial_holds_both_claimants_in_either_order(memory_db):
    ids = [_device(memory_db, "192.0.2.4"), _device(memory_db, "192.0.2.5")]
    for order in (ids, list(reversed(ids))):
        for device_id in order:
            result = record_identity(memory_db, device_id, "SN-SHARED")
            assert result["status"] in {"unverified", "conflict"}
        found = match_observation(memory_db, "SN-SHARED")
        assert found["status"] == "conflict"
        assert found["device_ids"] == sorted(ids)
        assert len({r[0] for r in memory_db.execute("SELECT uid FROM devices WHERE id IN (?,?)", ids)}) == 2
        conflict = memory_db.execute(
            "SELECT device_ids FROM identity_conflicts WHERE kind='serial' AND value='SN-SHARED' AND state='open'"
        ).fetchone()
        assert json.loads(conflict[0]) == sorted(ids)
        assert all(match_observation(memory_db, serial=None, macs=[])["verified"] is False for _ in ids)


def test_duplicate_serial_reverse_arrival_order(memory_db):
    first, second = _device(memory_db, "192.0.2.41"), _device(memory_db, "192.0.2.42")
    record_identity(memory_db, second, "SN-REVERSE")
    record_identity(memory_db, first, "SN-REVERSE")
    assert match_observation(memory_db, "SN-REVERSE") == {
        "device_ids": [first, second], "status": "conflict", "verified": False
    }


def test_duplicate_mac_holds_both_devices(memory_db):
    first, second = _device(memory_db, "192.0.2.6"), _device(memory_db, "192.0.2.7")
    record_identity(memory_db, first, "SN-6", {"radio": "001122334455"})
    result = record_identity(memory_db, second, "SN-7", {"eth0": "00:11:22:33:44:55"})
    assert result["status"] == "conflict"
    assert match_observation(memory_db, macs={"radio": "001122334455"}) == {
        "device_ids": [first, second], "status": "conflict", "verified": False
    }


def test_mac_only_and_missing_serial_are_never_verified(memory_db):
    device_id = _device(memory_db, "192.0.2.8")
    memory_db.execute(
        "UPDATE devices SET uid='legacy-ip:192.0.2.8', uid_source='ip_legacy' WHERE id=?",
        (device_id,),
    )
    memory_db.commit()
    record_identity(memory_db, device_id, None, {"eth0": "001122334455"})
    assert tuple(memory_db.execute(
        "SELECT uid,uid_source FROM devices WHERE id=?", (device_id,)
    ).fetchone()) == ("legacy-ip:192.0.2.8", "ip_legacy")
    assert match_observation(memory_db, macs={"eth0": "001122334455"})["status"] == "unverified"
    assert match_observation(memory_db, serial="unknown", macs={})["status"] == "no_match"


def test_duplicate_mac_records_conflict_without_changing_either_uid(memory_db):
    first, second = _device(memory_db, "192.0.2.81"), _device(memory_db, "192.0.2.82")
    for device_id, ip in ((first, "192.0.2.81"), (second, "192.0.2.82")):
        memory_db.execute(
            "UPDATE devices SET uid=?, uid_source='ip_legacy' WHERE id=?",
            (f"legacy-ip:{ip}", device_id),
        )
    memory_db.commit()

    record_identity(memory_db, first, macs={"radio": "001122334455"})
    result = record_identity(memory_db, second, macs={"eth0": "00:11:22:33:44:55"})

    assert result["status"] == "conflict"
    assert [tuple(row) for row in memory_db.execute(
        "SELECT uid,uid_source FROM devices WHERE id IN (?,?) ORDER BY id", (first, second)
    )] == [
        ("legacy-ip:192.0.2.81", "ip_legacy"),
        ("legacy-ip:192.0.2.82", "ip_legacy"),
    ]
    conflict = memory_db.execute(
        "SELECT device_ids FROM identity_conflicts WHERE kind='mac' "
        "AND value='00:11:22:33:44:55' AND state='open'"
    ).fetchone()
    assert json.loads(conflict[0]) == sorted([first, second])


def test_serial_observation_sets_uid(memory_db):
    device_id = _device(memory_db, "192.0.2.83")

    result = record_identity(memory_db, device_id, serial="SN-STABLE")

    assert result["status"] == "unverified"
    assert tuple(memory_db.execute(
        "SELECT uid,uid_source FROM devices WHERE id=?", (device_id,)
    ).fetchone()) == ("SN-STABLE", "serial")


def test_failed_conflict_write_rolls_back_claim_and_alias(memory_db):
    first, second = _device(memory_db, "192.0.2.9"), _device(memory_db, "192.0.2.10")
    record_identity(memory_db, first, "SN-9")
    memory_db.execute("""CREATE TRIGGER reject_conflict BEFORE INSERT ON identity_conflicts
        BEGIN SELECT RAISE(ABORT, 'injected conflict failure'); END""")
    result = record_identity(memory_db, second, "SN-9", {"eth0": "001122334455"})
    assert result["status"] == "failure"
    assert memory_db.execute("SELECT uid FROM devices WHERE id=?", (second,)).fetchone()[0] is None
    assert memory_db.execute("SELECT COUNT(*) FROM device_macs WHERE device_id=?", (second,)).fetchone()[0] == 0
    assert memory_db.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0] == 0


def test_generation_defaults_to_zero_and_identity_does_not_advance_it(memory_db):
    device_id = _device(memory_db, "192.0.2.11")
    assert memory_db.execute("SELECT address_generation FROM devices WHERE id=?", (device_id,)).fetchone()[0] == 0
    record_identity(memory_db, device_id, "SN-11", {"eth0": "001122334455"})
    assert memory_db.execute("SELECT address_generation FROM devices WHERE id=?", (device_id,)).fetchone()[0] == 0


def test_duplicate_serial_is_atomic_across_connections_and_survives_reopen(tmp_path, memory_db):
    path = tmp_path / "identity.sqlite"
    target = sqlite3.connect(path)
    memory_db.backup(target)
    target.close()
    setup = sqlite3.connect(path)
    ids = [_device(setup, "192.0.2.12"), _device(setup, "192.0.2.13")]
    setup.commit()
    setup.close()

    def claim(device_id):
        conn = sqlite3.connect(path, timeout=5)
        try:
            return record_identity(conn, device_id, "SN-RACE")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(claim, ids))
    reopened = sqlite3.connect(path)
    assert match_observation(reopened, "SN-RACE") == {
        "device_ids": sorted(ids), "status": "conflict", "verified": False
    }
    assert len({r[0] for r in reopened.execute("SELECT uid FROM devices WHERE id IN (?,?)", ids)}) == 2
    assert json.loads(reopened.execute(
        "SELECT device_ids FROM identity_conflicts WHERE kind='serial' AND value='SN-RACE'"
    ).fetchone()[0]) == sorted(ids)
    reopened.close()
