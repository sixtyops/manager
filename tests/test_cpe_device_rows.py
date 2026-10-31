"""S4a migration tests for legacy CPE cache rows."""

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
        ap_ip, ip, f"AA:BB:CC:00:00:{name[-1:]}", name, "TNA-301", "1.2.3",
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


@pytest.mark.parametrize("collision", ["existing-device", "same-cpe-ip"])
def test_startup_collision_aborts_and_rolls_back_all_cpe_copies(tmp_path, monkeypatch, collision):
    path = tmp_path / f"{collision}.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    cpes = [_cpe("192.0.2.1", "192.0.2.101", "CPE1"),
            _cpe("192.0.2.1", "192.0.2.102", "CPE2")]
    devices = [("192.0.2.1", "ap")]
    if collision == "existing-device":
        devices.append(("192.0.2.102", "switch"))
    else:
        devices.append(("192.0.2.2", "ap"))
        cpes[1] = _cpe("192.0.2.2", "192.0.2.101", "CPE2")
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
