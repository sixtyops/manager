#!/usr/bin/env python3
"""Seed the database with SAMPLE DEVICE DATA for local development/testing.

Run automatically on container start when SEED_DATA=1 is set. Inserts sample
sites/devices/CPEs/templates/history only — it deliberately does NOT create an
admin login, mark setup complete, or enable auto-update/scheduling. Those used
to be planted here, which meant a stray SEED_DATA=1 in a production .env would
hand a real host a known admin password and turn auto-update on. Login is the
operator's job (first-run setup, or the ADMIN_PASSWORD env used by dev.sh).

Refuses to run against a database that is already configured (setup done, an
admin exists, or devices are present), so it can never overwrite a live install.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sixtyops.db"

# The development seed uses only these RFC 5737 documentation addresses. This
# lets dev.sh recognize its existing sample database without trusting arbitrary
# configured databases that happen to contain devices.
DEV_DEVICE_IPS = {
    "192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.10",
    "198.51.100.1", "198.51.100.2", "198.51.100.3", "198.51.100.4",
    "198.51.100.10", "203.0.113.1", "203.0.113.2", "203.0.113.3",
    "203.0.113.10",
}


def _is_dev_seeded(db) -> bool:
    try:
        ips = {row[0] for row in db.execute("SELECT ip FROM devices").fetchall()}
    except sqlite3.Error:
        return False
    return ips == DEV_DEVICE_IPS


def _is_configured(db) -> bool:
    """True if the DB shows any sign of a real/configured install.

    Guards against seeding (and thus polluting) a host that is already set up.
    Tolerates a partially-initialised schema on a brand-new DB.
    """
    def _setting(key):
        try:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else ""
        except sqlite3.Error:
            return ""

    def _has_rows(sql):
        try:
            return db.execute(sql).fetchone()[0] > 0
        except sqlite3.Error:
            return False

    return (
        _setting("setup_completed") == "true"
        or bool(_setting("admin_password_hash"))
        or _has_rows("SELECT COUNT(*) FROM users")
        or _has_rows("SELECT COUNT(*) FROM devices")
    )


def seed():
    if not DB_PATH.exists():
        print("seed: database not found yet, skipping (app will create it on first run)")
        return

    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row

    # Refuse to seed a configured install — never overwrite a real host's data
    # or credentials (production safety, not just idempotency).
    if _is_configured(db):
        if _is_dev_seeded(db):
            print("seed: development database is already seeded")
            db.close()
            return True
        print("seed: database is already configured (setup done / admin / devices "
              "present) — refusing to seed")
        db.close()
        return False

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
    # All seed addresses come from the RFC 5737 documentation ranges. No real
    # device uses them, so the real poller in dev.sh cannot reach a LAN device.
    devices = [
        # Hilltop Tower
        ("192.0.2.1", "tachyon", "ap", "Hilltop Tower", "admin", "seedpass", "HT-AP-01", "TNA-301", "AA:BB:CC:01:01:01", "1.12.2", now, None, 1, "1.12.2", "1.11.0", 1, "Primary AP"),
        ("192.0.2.2", "tachyon", "ap", "Hilltop Tower", "admin", "seedpass", "HT-AP-02", "TNA-301", "AA:BB:CC:01:01:02", "1.12.3", now, None, 1, "1.12.3", "1.12.2", 1, "Recently updated"),
        ("192.0.2.3", "tachyon", "ap", "Hilltop Tower", "admin", "seedpass", "HT-AP-03", "TNA-303L", "AA:BB:CC:01:01:03", "1.11.0", now, None, 1, "1.11.0", "1.10.5", 1, "Needs update"),
        ("192.0.2.10", "tachyon", "switch", "Hilltop Tower", "admin", "seedpass", "HT-SW-01", "TNS-100", "AA:BB:CC:01:02:01", "2.3.1", now, None, 1, "2.3.1", "2.2.0", 1, "Tower switch"),
        # Downtown Tower
        ("198.51.100.1", "tachyon", "ap", "Downtown Tower", "admin", "seedpass", "DT-AP-01", "TNA-301", "AA:BB:CC:02:01:01", "1.12.3", now, None, 1, "1.12.3", "1.12.2", 1, None),
        ("198.51.100.2", "tachyon", "ap", "Downtown Tower", "admin", "seedpass", "DT-AP-02", "TNA-301", "AA:BB:CC:02:01:02", "1.12.2", now, None, 1, "1.12.2", "1.11.0", 1, None),
        ("198.51.100.3", "tachyon", "ap", "Downtown Tower", "admin", "seedpass", "DT-AP-03", "TNA-303L", "AA:BB:CC:02:01:03", "1.12.3", now, None, 1, "1.12.3", "1.12.2", 1, None),
        ("198.51.100.4", "tachyon", "ap", "Downtown Tower", "admin", "seedpass", "DT-AP-04", "TNA-301", "AA:BB:CC:02:01:04", "1.10.5", None, "Connection refused", 1, "1.10.5", "1.10.0", 1, "Offline since Tuesday"),
        ("198.51.100.10", "tachyon", "switch", "Downtown Tower", "admin", "seedpass", "DT-SW-01", "TNS-100", "AA:BB:CC:02:02:01", "2.3.1", now, None, 1, "2.3.1", "2.3.0", 1, None),
        # Lakeside Tower
        ("203.0.113.1", "tachyon", "ap", "Lakeside Tower", "admin", "seedpass", "LS-AP-01", "TNA-301", "AA:BB:CC:03:01:01", "1.12.2", now, None, 1, "1.12.2", "1.11.0", 1, None),
        ("203.0.113.2", "tachyon", "ap", "Lakeside Tower", "admin", "seedpass", "LS-AP-02", "TNA-303L", "AA:BB:CC:03:01:02", "1.11.0", now, None, 1, "1.11.0", "1.10.5", 1, "Long range unit"),
        ("203.0.113.3", "tachyon", "ap", "Lakeside Tower", "admin", "seedpass", "LS-AP-03", "TNA-301", "AA:BB:CC:03:01:03", "1.12.3", now, None, 1, "1.12.3", "1.12.2", 1, None),
        ("203.0.113.10", "tachyon", "switch", "Lakeside Tower", "admin", "seedpass", "LS-SW-01", "TNS-100", "AA:BB:CC:03:02:01", "2.2.0", now, None, 1, "2.2.0", "2.1.0", 1, "Needs update"),
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
        ("192.0.2.1", "192.0.2.101", "AA:BB:CC:01:03:01", "HT-CPE-01", "TNA-301", "1.12.2", 2.3, -55.0, -52.0, -48.0, 300.0, 250.0, 9, 86400, "green"),
        ("192.0.2.1", "192.0.2.102", "AA:BB:CC:01:03:02", "HT-CPE-02", "TNA-301", "1.11.0", 5.1, -68.0, -65.0, -62.0, 150.0, 120.0, 5, 43200, "yellow"),
        ("198.51.100.1", "198.51.100.101", "AA:BB:CC:02:03:01", "DT-CPE-01", "TNA-301", "1.12.3", 0.8, -42.0, -40.0, -38.0, 450.0, 400.0, 11, 172800, "green"),
        ("198.51.100.1", "198.51.100.102", "AA:BB:CC:02:03:02", "DT-CPE-02", "TNA-303L", "1.10.5", 12.4, -75.0, -72.0, -70.0, 80.0, 60.0, 3, 3600, "red"),
        ("203.0.113.1", "203.0.113.101", "AA:BB:CC:03:03:01", "LS-CPE-01", "TNA-301", "1.12.2", 3.7, -58.0, -55.0, -52.0, 280.0, 230.0, 8, 259200, "green"),
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
        ("Syslog Remote", "syslog", {"services": {"syslog": {"remote_host": "192.0.2.200", "port": 514}}}, "Remote syslog forwarding", 0),
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
        (["192.0.2.1", "192.0.2.2", "192.0.2.3"], 3, 0),
        (["198.51.100.1", "198.51.100.2", "198.51.100.4"], 2, 1),
        (["203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.10"], 3, 1),
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

    # NOTE: seeding intentionally does NOT create an admin, mark setup
    # complete, or enable auto-update/scheduling. Sign in via first-run setup,
    # or the ADMIN_PASSWORD env var (dev.sh / dev compose). This keeps a stray
    # SEED_DATA=1 from planting a known password or turning auto-update on.
    db.commit()

    dev_count = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    cpe_count = db.execute("SELECT COUNT(*) FROM cpe_cache").fetchone()[0]
    site_count = db.execute("SELECT COUNT(*) FROM tower_sites").fetchone()[0]
    tmpl_count = db.execute("SELECT COUNT(*) FROM config_templates").fetchone()[0]
    hist_count = db.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
    print(f"seed: inserted {site_count} sites, {dev_count} devices, {cpe_count} CPEs, {tmpl_count} templates, {hist_count} jobs")
    print("seed: no admin created — sign in via first-run setup or the ADMIN_PASSWORD env var")
    db.close()
    return True


if __name__ == "__main__":
    if seed() is False:
        raise SystemExit(1)
