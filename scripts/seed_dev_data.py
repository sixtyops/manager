#!/usr/bin/env python3
"""Seed the database with sample data for local development/testing.

Run automatically on container start when SEED_DATA=1 is set.
Only inserts data if the devices table is empty (idempotent).
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sixtyops.db"


def seed():
    if not DB_PATH.exists():
        print("seed: database not found yet, skipping (app will create it on first run)")
        return

    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row

    # Idempotent: skip if devices already exist
    count = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    if count > 0:
        print(f"seed: database already has {count} devices, skipping")
        db.close()
        return

    now = datetime.now(timezone.utc).isoformat()

    # ── Sites ──
    sites = [
        ("Hilltop Tower", "Rural hilltop, north sector", 40.1234, -89.5678),
        ("Downtown Tower", "Commercial district rooftop", 40.0987, -89.4321),
        ("Lakeside Tower", "Near lake shore, elevated", 40.2345, -89.6789),
    ]
    for name, loc, lat, lon in sites:
        db.execute(
            "INSERT OR IGNORE INTO tower_sites (name, location, latitude, longitude, created_at) VALUES (?,?,?,?,?)",
            (name, loc, lat, lon, now),
        )
    db.commit()

    site_ids = {r[0]: r[1] for r in db.execute("SELECT name, id FROM tower_sites").fetchall()}

    # ── Devices (APs + Switches) ──
    devices = [
        # Hilltop Tower
        ("10.0.1.1", "tachyon", "ap", "Hilltop Tower", "admin", "seedpass", "HT-AP-01", "TNA-301", "AA:BB:CC:01:01:01", "1.12.2", now, None, 1, "1.12.2", "1.11.0", 1, "Primary AP"),
        ("10.0.1.2", "tachyon", "ap", "Hilltop Tower", "admin", "seedpass", "HT-AP-02", "TNA-301", "AA:BB:CC:01:01:02", "1.12.3", now, None, 1, "1.12.3", "1.12.2", 1, "Recently updated"),
        ("10.0.1.3", "tachyon", "ap", "Hilltop Tower", "admin", "seedpass", "HT-AP-03", "TNA-303L", "AA:BB:CC:01:01:03", "1.11.0", now, None, 1, "1.11.0", "1.10.5", 1, "Needs update"),
        ("10.0.1.10", "tachyon", "switch", "Hilltop Tower", "admin", "seedpass", "HT-SW-01", "TNS-100", "AA:BB:CC:01:02:01", "2.3.1", now, None, 1, "2.3.1", "2.2.0", 1, "Tower switch"),
        # Downtown Tower
        ("10.0.2.1", "tachyon", "ap", "Downtown Tower", "admin", "seedpass", "DT-AP-01", "TNA-301", "AA:BB:CC:02:01:01", "1.12.3", now, None, 1, "1.12.3", "1.12.2", 1, None),
        ("10.0.2.2", "tachyon", "ap", "Downtown Tower", "admin", "seedpass", "DT-AP-02", "TNA-301", "AA:BB:CC:02:01:02", "1.12.2", now, None, 1, "1.12.2", "1.11.0", 1, None),
        ("10.0.2.3", "tachyon", "ap", "Downtown Tower", "admin", "seedpass", "DT-AP-03", "TNA-303L", "AA:BB:CC:02:01:03", "1.12.3", now, None, 1, "1.12.3", "1.12.2", 1, None),
        ("10.0.2.4", "tachyon", "ap", "Downtown Tower", "admin", "seedpass", "DT-AP-04", "TNA-301", "AA:BB:CC:02:01:04", "1.10.5", None, "Connection refused", 1, "1.10.5", "1.10.0", 1, "Offline since Tuesday"),
        ("10.0.2.10", "tachyon", "switch", "Downtown Tower", "admin", "seedpass", "DT-SW-01", "TNS-100", "AA:BB:CC:02:02:01", "2.3.1", now, None, 1, "2.3.1", "2.3.0", 1, None),
        # Lakeside Tower
        ("10.0.3.1", "tachyon", "ap", "Lakeside Tower", "admin", "seedpass", "LS-AP-01", "TNA-301", "AA:BB:CC:03:01:01", "1.12.2", now, None, 1, "1.12.2", "1.11.0", 1, None),
        ("10.0.3.2", "tachyon", "ap", "Lakeside Tower", "admin", "seedpass", "LS-AP-02", "TNA-303L", "AA:BB:CC:03:01:02", "1.11.0", now, None, 1, "1.11.0", "1.10.5", 1, "Long range unit"),
        ("10.0.3.3", "tachyon", "ap", "Lakeside Tower", "admin", "seedpass", "LS-AP-03", "TNA-301", "AA:BB:CC:03:01:03", "1.12.3", now, None, 1, "1.12.3", "1.12.2", 1, None),
        ("10.0.3.10", "tachyon", "switch", "Lakeside Tower", "admin", "seedpass", "LS-SW-01", "TNS-100", "AA:BB:CC:03:02:01", "2.2.0", now, None, 1, "2.2.0", "2.1.0", 1, "Needs update"),
    ]
    for d in devices:
        ip, vendor, role, site_name, user, pw, sname, model, mac, fw, seen, err, enabled, b1, b2, ab, notes = d
        sid = site_ids.get(site_name)
        db.execute(
            """INSERT OR IGNORE INTO devices
            (ip, vendor, role, tower_site_id, username, password, system_name, model, mac,
             firmware_version, last_seen, last_error, enabled, bank1_version, bank2_version,
             active_bank, notes, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ip, vendor, role, sid, user, pw, sname, model, mac, fw, seen, err, enabled, b1, b2, ab, notes, now),
        )
    db.commit()

    # ── CPEs ──
    cpes = [
        ("10.0.1.1", "10.0.1.101", "AA:BB:CC:01:03:01", "HT-CPE-01", "TNA-301", "1.12.2", 2.3, -55.0, -52.0, -48.0, 300.0, 250.0, 9, 86400, "green"),
        ("10.0.1.1", "10.0.1.102", "AA:BB:CC:01:03:02", "HT-CPE-02", "TNA-301", "1.11.0", 5.1, -68.0, -65.0, -62.0, 150.0, 120.0, 5, 43200, "yellow"),
        ("10.0.2.1", "10.0.2.101", "AA:BB:CC:02:03:01", "DT-CPE-01", "TNA-301", "1.12.3", 0.8, -42.0, -40.0, -38.0, 450.0, 400.0, 11, 172800, "green"),
        ("10.0.2.1", "10.0.2.102", "AA:BB:CC:02:03:02", "DT-CPE-02", "TNA-303L", "1.10.5", 12.4, -75.0, -72.0, -70.0, 80.0, 60.0, 3, 3600, "red"),
        ("10.0.3.1", "10.0.3.101", "AA:BB:CC:03:03:01", "LS-CPE-01", "TNA-301", "1.12.2", 3.7, -58.0, -55.0, -52.0, 280.0, 230.0, 8, 259200, "green"),
    ]
    for ap_ip, ip, mac, sname, model, fw, dist, rx, combined, rssi, tx_rate, rx_rate, mcs, uptime, health in cpes:
        db.execute(
            """INSERT OR IGNORE INTO cpe_cache
            (ap_ip, ip, mac, system_name, model, firmware_version, link_distance, rx_power,
             combined_signal, last_local_rssi, tx_rate, rx_rate, mcs, link_uptime, signal_health, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ap_ip, ip, mac, sname, model, fw, dist, rx, combined, rssi, tx_rate, rx_rate, mcs, uptime, health, now),
        )
    db.commit()

    # ── Config Templates ──
    templates = [
        ("SNMP Community", "snmp", {"services": {"snmp": {"v2_ro_community": "public", "v2_rw_community": "private"}}}, "Standard SNMP v2c community strings", 1),
        ("NTP Servers", "ntp", {"services": {"ntp": {"servers": ["pool.ntp.org", "time.google.com"]}}}, "Default NTP server config", 1),
        ("Syslog Remote", "syslog", {"services": {"syslog": {"remote_host": "10.0.0.100", "port": 514}}}, "Remote syslog forwarding", 0),
    ]
    for name, cat, frag, desc, enabled in templates:
        db.execute(
            """INSERT OR IGNORE INTO config_templates
            (name, category, config_fragment, description, enabled, scope, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (name, cat, json.dumps(frag), desc, enabled, "global", now, now),
        )
    db.commit()

    # ── Job History ──
    jobs = [
        (["10.0.1.1", "10.0.1.2", "10.0.1.3"], 3, 0),
        (["10.0.2.1", "10.0.2.2", "10.0.2.4"], 2, 1),
        (["10.0.3.1", "10.0.3.2", "10.0.3.3", "10.0.3.10"], 3, 1),
    ]
    for i, (ips, sc, fc) in enumerate(jobs):
        job_id = str(uuid.uuid4())
        ts = (datetime.now(timezone.utc) - timedelta(days=i + 1)).isoformat()
        te = (datetime.now(timezone.utc) - timedelta(days=i + 1) + timedelta(minutes=45)).isoformat()
        devices_json = json.dumps(
            {ip: {"status": "success" if j < sc else "failed", "firmware": "tachyon-v1.12.3.bin"} for j, ip in enumerate(ips)}
        )
        db.execute(
            """INSERT INTO job_history
            (job_id, started_at, completed_at, duration, bank_mode, success_count, failed_count, skipped_count, cancelled_count, devices_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (job_id, ts, te, 2700.0, "both", sc, fc, 0, 0, devices_json),
        )
        for j, ip in enumerate(ips):
            s = "success" if j < sc else "failed"
            db.execute(
                """INSERT INTO device_update_history
                (job_id, ip, role, action, status, old_version, new_version, model, duration_seconds, started_at, completed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, ip, "ap", "firmware_update", s, "1.12.2", "1.12.3", "TNA-301", 180.0, ts, te),
            )
    db.commit()

    # ── Admin user + setup complete ──
    admin_pw = "admin123"
    pw_hash = bcrypt.hashpw(admin_pw.encode(), bcrypt.gensalt()).decode()
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("admin_password_hash", pw_hash),
    )
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("setup_completed", "true"),
    )
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("schedule_enabled", "true"),
    )
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("autoupdate_enabled", "true"),
    )
    # Create admin user in users table
    existing = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if not existing:
        db.execute(
            """INSERT INTO users (username, password_hash, role, auth_method, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("admin", pw_hash, "admin", "local", 1, now, now),
        )
    db.commit()

    dev_count = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    cpe_count = db.execute("SELECT COUNT(*) FROM cpe_cache").fetchone()[0]
    site_count = db.execute("SELECT COUNT(*) FROM tower_sites").fetchone()[0]
    tmpl_count = db.execute("SELECT COUNT(*) FROM config_templates").fetchone()[0]
    hist_count = db.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
    print(f"seed: inserted {site_count} sites, {dev_count} devices, {cpe_count} CPEs, {tmpl_count} templates, {hist_count} jobs")
    print(f"seed: admin user created (username: admin, password: {admin_pw})")
    db.close()


if __name__ == "__main__":
    seed()
