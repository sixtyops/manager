"""S4a migration tests for legacy CPE cache rows."""

import importlib
import os
import sqlite3

import pytest

from updater import database


def _legacy_database(path, cpes, devices=()):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tower_sites (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT NOT NULL UNIQUE,
            vendor TEXT NOT NULL DEFAULT 'tachyon', role TEXT NOT NULL DEFAULT 'ap',
            tower_site_id INTEGER, username TEXT NOT NULL, password TEXT NOT NULL,
            system_name TEXT, model TEXT, mac TEXT, firmware_version TEXT, location TEXT,
            last_seen TEXT, last_error TEXT, enabled INTEGER DEFAULT 1,
            bank1_version TEXT, bank2_version TEXT, active_bank INTEGER,
            last_firmware_update TEXT, notes TEXT, last_config_poll_at TEXT,
            last_config_poll_status TEXT, last_config_poll_error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE cpe_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ap_ip TEXT NOT NULL, ip TEXT NOT NULL,
            mac TEXT, system_name TEXT, model TEXT, firmware_version TEXT,
            link_distance REAL, rx_power REAL, combined_signal REAL, last_local_rssi REAL,
            tx_rate REAL, rx_rate REAL, mcs INTEGER, link_uptime INTEGER, signal_health TEXT,
            last_updated TEXT, last_config_poll_at TEXT, last_config_poll_status TEXT,
            last_config_poll_error TEXT, target_rssi_dbm REAL, snr_db REAL,
            sector_tx INTEGER, sector_rx INTEGER, antenna_kit TEXT, max_rain_mm_hr REAL,
            auth_status TEXT, bank1_version TEXT, bank2_version TEXT, active_bank INTEGER,
            bank_last_fetched TEXT, vendor TEXT DEFAULT 'tachyon',
            UNIQUE(ap_ip, ip)
        );
    """)
    for ip, role in devices:
        conn.execute(
            "INSERT INTO devices(ip,role,username,password) VALUES(?,?,?,?)",
            (ip, role, "parent-user", "gAAAAA-test-ciphertext"),
        )
    columns = (
        "ap_ip,ip,mac,system_name,model,firmware_version,link_distance,rx_power,"
        "combined_signal,last_local_rssi,tx_rate,rx_rate,mcs,link_uptime,signal_health,"
        "last_updated,last_config_poll_at,last_config_poll_status,last_config_poll_error,"
        "target_rssi_dbm,snr_db,sector_tx,sector_rx,antenna_kit,max_rain_mm_hr,auth_status,"
        "bank1_version,bank2_version,active_bank,bank_last_fetched,vendor"
    )
    conn.executemany(
        f"INSERT INTO cpe_cache({columns}) VALUES ({','.join('?' for _ in range(31))})",
        cpes,
    )
    conn.commit()
    conn.close()


def _cpe(ap_ip, ip, name):
    return (
        ap_ip, ip, f"AA:BB:CC:00:00:0{name[-1:]}", name, "TNA-301", "1.2.3",
        12.5, -51.0, -49.0, -47.0, 300.0, 250.0, 9, 86400, "green",
        "2026-10-28T10:00:00", "2026-10-27T10:00:00", "ok", None,
        -60.0, 22.0, 1, 2, "wide", 8.5, "ok", "1.2.3", "1.2.2", 1,
        "2026-10-27T10:00:00", "tachyon",
    )


def _read(path, sql, args=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_startup_copies_every_legacy_cpe_and_repeats_without_duplicates(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    _legacy_database(path, [_cpe("192.0.2.1", "192.0.2.101", "CPE1"),
                            _cpe("192.0.2.1", "192.0.2.102", "CPE2")],
                     [("192.0.2.1", "ap")])

    database.init_db()
    first = _read(path, "SELECT id,ip,parent_device_id,role,system_name,mac,model,firmware_version,"
                        "auth_status,bank1_version,bank2_version,active_bank,bank_last_fetched,"
                        "last_seen,last_config_poll_at,last_config_poll_status,last_config_poll_error "
                        "FROM devices WHERE role='cpe' ORDER BY ip")
    metrics = _read(path, "SELECT device_id,link_distance,rx_power,combined_signal,last_local_rssi,"
                           "tx_rate,rx_rate,mcs,link_uptime,signal_health,target_rssi_dbm,snr_db,"
                           "sector_tx,sector_rx,antenna_kit,max_rain_mm_hr "
                           "FROM link_metrics ORDER BY device_id")
    mapping = _read(path, "SELECT old_cpe_id,device_id FROM cpe_migration_map ORDER BY old_cpe_id")
    source_count = _read(path, "SELECT COUNT(*) AS n FROM cpe_cache")[0]["n"]
    assert len(first) == len(metrics) == len(mapping) == source_count == 2
    assert [r["system_name"] for r in first] == ["CPE1", "CPE2"]
    assert all(r["parent_device_id"] == 1 for r in first)
    assert all(r["auth_status"] == "ok" and r["bank1_version"] == "1.2.3" for r in first)
    assert all(r["last_config_poll_status"] == "ok" for r in first)
    assert all(r["last_seen"] == "2026-10-28T10:00:00" for r in first)
    assert all(r["signal_health"] == "green" and r["link_distance"] == 12.5 for r in metrics)
    assert all(r["max_rain_mm_hr"] == 8.5 and r["rx_power"] == -51.0 for r in metrics)

    database.init_db()
    assert [tuple(r) for r in _read(path, "SELECT id,ip,parent_device_id,role,system_name,mac,model,firmware_version,"
                                           "auth_status,bank1_version,bank2_version,active_bank,bank_last_fetched,"
                                           "last_seen,last_config_poll_at,last_config_poll_status,last_config_poll_error "
                                           "FROM devices WHERE role='cpe' ORDER BY ip")] == [tuple(r) for r in first]
    assert len(_read(path, "SELECT * FROM cpe_migration_map")) == 2
    assert len(_read(path, "SELECT * FROM link_metrics")) == 2
    assert _read(path, "SELECT COUNT(*) AS n FROM cpe_cache")[0]["n"] == 2


def test_import_does_not_reconcile_duplicate_legacy_cache_rows(tmp_path, monkeypatch):
    path = tmp_path / "duplicate-cache.db"
    older = list(_cpe("192.0.2.1", "192.0.2.101", "CPE1"))
    newer = list(_cpe("192.0.2.2", "192.0.2.101", "CPE2"))
    newer[15] = "2026-10-29T10:00:00"
    _legacy_database(path, [tuple(older), tuple(newer)],
                     [("192.0.2.1", "ap"), ("192.0.2.2", "ap")])
    before = [tuple(row) for row in _read(path, "SELECT id,ap_ip,ip,last_updated FROM cpe_cache ORDER BY id")]

    old_db_path = database.DB_PATH
    monkeypatch.setenv("SIXTYOPS_DB_PATH", str(path))
    try:
        importlib.reload(database)
        after = [tuple(row) for row in _read(path, "SELECT id,ap_ip,ip,last_updated FROM cpe_cache ORDER BY id")]
        assert after == before
    finally:
        monkeypatch.delenv("SIXTYOPS_DB_PATH", raising=False)
        importlib.reload(database)
        database.DB_PATH = old_db_path


def test_startup_keeps_newest_cpe_row_when_legacy_parents_collide(tmp_path, monkeypatch, caplog):
    path = tmp_path / "duplicate-cpe.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    older = list(_cpe("192.0.2.1", "192.0.2.101", "CPE1"))
    older[15] = "2026-10-27T10:00:00"
    older[6] = 10.0
    newer = list(_cpe("192.0.2.2", "192.0.2.101", "CPE2"))
    newer[2] = older[2]
    newer[15] = "2026-10-28T10:00:00"
    newer[6] = 25.0
    _legacy_database(path, [tuple(older), tuple(newer)],
                     [("192.0.2.1", "ap"), ("192.0.2.2", "ap")])

    with caplog.at_level("WARNING", logger=database.__name__):
        database.init_db()

    cpe = _read(path, "SELECT id,ip,parent_device_id,system_name,last_seen FROM devices WHERE role='cpe'")
    metrics = _read(path, "SELECT device_id,link_distance FROM link_metrics")
    cache = _read(path, "SELECT id,ap_ip,ip FROM cpe_cache")
    mapping = _read(path, "SELECT old_cpe_id,device_id FROM cpe_migration_map")
    assert len(cpe) == len(metrics) == len(cache) == len(mapping) == 1
    assert cpe[0]["parent_device_id"] == 2
    assert cpe[0]["system_name"] == "CPE2"
    assert cpe[0]["last_seen"] == "2026-10-28T10:00:00"
    assert metrics[0]["device_id"] == cpe[0]["id"]
    assert metrics[0]["link_distance"] == 25.0
    assert tuple(cache[0]) == (2, "192.0.2.2", "192.0.2.101")
    assert mapping[0]["old_cpe_id"] == 2
    collision_logs = [record.getMessage() for record in caplog.records
                      if "Resolved duplicate legacy CPE rows" in record.getMessage()]
    assert len(collision_logs) == 1
    assert "192.0.2.101" not in collision_logs[0]

    database.init_db()
    assert _read(path, "SELECT id,ip,parent_device_id,system_name,last_seen FROM devices WHERE role='cpe'") == cpe
    assert len(_read(path, "SELECT * FROM link_metrics")) == 1
    assert len(_read(path, "SELECT * FROM cpe_migration_map")) == 1
    assert _read(path, "SELECT id,ap_ip,ip FROM cpe_cache") == cache


def test_startup_duplicate_parent_tie_keeps_lower_legacy_id(tmp_path, monkeypatch):
    path = tmp_path / "duplicate-cpe-tie.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    first = list(_cpe("192.0.2.1", "192.0.2.101", "CPE1"))
    second = list(_cpe("192.0.2.2", "192.0.2.101", "CPE2"))
    second[2] = first[2]
    _legacy_database(path, [tuple(first), tuple(second)],
                     [("192.0.2.1", "ap"), ("192.0.2.2", "ap")])

    database.init_db()

    cpe = _read(path, "SELECT parent_device_id,system_name FROM devices WHERE role='cpe'")
    assert len(cpe) == 1
    assert cpe[0]["parent_device_id"] == 1
    assert cpe[0]["system_name"] == "CPE1"
    assert [tuple(row) for row in _read(path, "SELECT id FROM cpe_cache")] == [(1,)]


def test_startup_fails_closed_for_distinct_hardware_reusing_an_ip(tmp_path, monkeypatch):
    path = tmp_path / "reused-ip.db"
    first = _cpe("192.0.2.1", "192.0.2.101", "CPE1")
    second = _cpe("192.0.2.2", "192.0.2.101", "CPE2")
    second = (*second[:15], "2026-10-29T10:00:00", *second[16:])
    _legacy_database(path, [first, second], [("192.0.2.1", "ap"), ("192.0.2.2", "ap")])
    monkeypatch.setattr(database, "DB_PATH", path)

    with pytest.raises(ValueError, match="conflicting hardware identity"):
        database.init_db()

    assert [tuple(row) for row in _read(path, "SELECT ap_ip,ip,mac FROM cpe_cache ORDER BY id")] == [
        (first[0], first[1], first[2]),
        (second[0], second[1], second[2]),
    ]
    assert _read(path, "SELECT id FROM devices WHERE role='cpe'") == []


@pytest.mark.parametrize("pending_is_newer", [True, False])
def test_startup_maps_roamed_row_to_already_migrated_cpe(tmp_path, monkeypatch, caplog, pending_is_newer):
    path = tmp_path / "roamed-cpe.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    first = _cpe("192.0.2.1", "192.0.2.101", "CPE1")
    _legacy_database(path, [first], [("192.0.2.1", "ap"), ("192.0.2.2", "ap")])
    database.init_db()
    conn = sqlite3.connect(path)
    conn.execute("CREATE UNIQUE INDEX old_map_device_unique ON cpe_migration_map(device_id)")
    conn.commit()
    conn.close()
    device_fields = ("ip, vendor, role, parent_device_id, username, password, system_name, model, mac, "
                     "firmware_version, last_seen, enabled, bank1_version, bank2_version, active_bank, "
                     "bank_last_fetched, last_config_poll_at, last_config_poll_status, "
                     "last_config_poll_error, auth_status, created_at")
    device_before = tuple(_read(path, f"SELECT {device_fields} FROM devices WHERE role='cpe'")[0])
    metrics_before = tuple(_read(path, "SELECT * FROM link_metrics")[0])
    pending = list(_cpe("192.0.2.2", "192.0.2.101", "CPE2"))
    pending[2] = first[2]
    pending[15] = "2026-10-29T10:00:00" if pending_is_newer else "2026-10-27T10:00:00"
    pending[6] = 25.0
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO cpe_cache(ap_ip,ip,mac,system_name,model,firmware_version,link_distance,rx_power,"
        "combined_signal,last_local_rssi,tx_rate,rx_rate,mcs,link_uptime,signal_health,last_updated,"
        "last_config_poll_at,last_config_poll_status,last_config_poll_error,target_rssi_dbm,snr_db,"
        "sector_tx,sector_rx,antenna_kit,max_rain_mm_hr,auth_status,bank1_version,bank2_version,"
        "active_bank,bank_last_fetched,vendor) VALUES (" + ",".join("?" for _ in range(31)) + ")",
        pending,
    )
    conn.commit()
    conn.close()

    with caplog.at_level("INFO", logger=database.__name__):
        database.init_db()

    cpe = _read(path, "SELECT id,ip,parent_device_id,system_name,mac,last_seen FROM devices WHERE role='cpe'")
    metrics = _read(path, "SELECT device_id,link_distance FROM link_metrics")
    mapping = _read(path, "SELECT old_cpe_id,device_id FROM cpe_migration_map ORDER BY old_cpe_id")
    assert len(cpe) == len(metrics) == 1
    assert len(mapping) == 2 and mapping[0]["device_id"] == mapping[1]["device_id"] == cpe[0]["id"]
    assert cpe[0]["ip"] == first[1]
    assert cpe[0]["system_name"] == first[3] and cpe[0]["mac"] == first[2]
    assert cpe[0]["parent_device_id"] == (2 if pending_is_newer else 1)
    assert cpe[0]["last_seen"] == (pending[15] if pending_is_newer else first[15])
    assert metrics[0]["link_distance"] == (25.0 if pending_is_newer else first[6])
    if not pending_is_newer:
        assert tuple(_read(path, f"SELECT {device_fields} FROM devices WHERE role='cpe'")[0]) == device_before
        assert tuple(_read(path, "SELECT * FROM link_metrics")[0]) == metrics_before
    case_logs = [record.getMessage() for record in caplog.records
                 if "migrated CPE" in record.getMessage()]
    assert len(case_logs) == 1
    assert "192.0.2.101" not in case_logs[0]


def test_moved_cpe_reconciles_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "moved-cpe.db"
    first = _cpe("192.0.2.1", "192.0.2.101", "CPE1")
    _legacy_database(path, [first], [("192.0.2.1", "ap"), ("192.0.2.2", "ap")])
    monkeypatch.setattr(database, "DB_PATH", path)

    database.init_db()
    before = _read(path, "SELECT id,parent_device_id,firmware_version FROM devices WHERE role='cpe'")[0]
    assert before["parent_device_id"] == 1
    assert before["firmware_version"] == "1.2.3"

    database.upsert_cpe("192.0.2.2", {
        "ip": first[1], "mac": first[2], "system_name": "CPE1",
        "model": first[4], "firmware_version": "2.0.0",
    })
    database.init_db()

    device = _read(path, "SELECT id,parent_device_id,firmware_version,last_seen FROM devices WHERE role='cpe'")
    cache = _read(path, "SELECT ap_ip,ip,mac,firmware_version FROM cpe_cache")
    mapping = _read(path, "SELECT device_id FROM cpe_migration_map ORDER BY old_cpe_id")
    assert len(device) == len(cache) == 1
    assert device[0]["id"] == before["id"]
    assert device[0]["parent_device_id"] == 2
    assert device[0]["firmware_version"] == "2.0.0"
    assert cache[0]["ap_ip"] == "192.0.2.2"
    assert cache[0]["firmware_version"] == "2.0.0"
    assert {row["device_id"] for row in mapping} == {device[0]["id"]}


@pytest.mark.parametrize("conflict_role", ["ap", "switch"])
def test_startup_collision_aborts_and_rolls_back_all_cpe_copies(tmp_path, monkeypatch, conflict_role):
    path = tmp_path / "existing-device.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    cpes = [_cpe("192.0.2.1", "192.0.2.101", "CPE1"),
            _cpe("192.0.2.1", "192.0.2.102", "CPE2")]
    devices = [("192.0.2.1", "ap"), ("192.0.2.102", conflict_role)]
    _legacy_database(path, cpes, devices)
    before = _read(path, "SELECT id,ip,role,username,password FROM devices ORDER BY id")

    for _ in range(2):
        with pytest.raises(ValueError, match="CPE migration collision"):
            database.init_db()
        assert [tuple(r) for r in _read(path, "SELECT id,ip,role,username,password FROM devices ORDER BY id")] == [tuple(r) for r in before]
        assert _read(path, "SELECT id FROM devices WHERE role='cpe'") == []
        assert _read(path, "SELECT * FROM cpe_migration_map") == []
        assert _read(path, "SELECT * FROM link_metrics") == []
        assert [tuple(r) for r in _read(path, "SELECT id,ap_ip,ip FROM cpe_cache ORDER BY id")] == [
            (1, cpes[0][0], cpes[0][1]),
            (2, cpes[1][0], cpes[1][1]),
        ]
