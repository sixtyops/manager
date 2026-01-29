"""SQLite database for persistent storage."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# Database file location
DB_PATH = Path(__file__).parent.parent / "data" / "tachyon.db"


def init_db():
    """Initialize the database schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tower_sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                location TEXT,
                latitude REAL,
                longitude REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS access_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL UNIQUE,
                tower_site_id INTEGER,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                system_name TEXT,
                model TEXT,
                mac TEXT,
                firmware_version TEXT,
                location TEXT,
                last_seen TEXT,
                last_error TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tower_site_id) REFERENCES tower_sites(id)
            );

            CREATE TABLE IF NOT EXISTS cpe_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ap_ip TEXT NOT NULL,
                ip TEXT NOT NULL,
                mac TEXT,
                system_name TEXT,
                model TEXT,
                firmware_version TEXT,
                link_distance REAL,
                rx_power REAL,
                combined_signal REAL,
                last_local_rssi REAL,
                tx_rate REAL,
                rx_rate REAL,
                mcs INTEGER,
                link_uptime INTEGER,
                signal_health TEXT,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ap_ip, ip)
            );

            CREATE INDEX IF NOT EXISTS idx_cpe_ap ON cpe_cache(ap_ip);

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                ip_address TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Insert default settings if not exists
        defaults = {
            "schedule_enabled": "false",
            "schedule_days": "tue,wed,thu",
            "schedule_start_hour": "3",
            "schedule_end_hour": "4",
            "parallel_updates": "2",
            "bank_mode": "both",
            "timezone": "auto",
            "zip_code": "",
            "weather_check_enabled": "true",
            "min_temperature_c": "-10",
        }
        for key, value in defaults.items():
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )


@contextmanager
def get_db():
    """Get database connection context manager."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Tower Site operations
def get_tower_sites() -> list[dict]:
    """Get all tower sites."""
    with get_db() as db:
        rows = db.execute("SELECT * FROM tower_sites ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def get_tower_site(site_id: int) -> Optional[dict]:
    """Get a tower site by ID."""
    with get_db() as db:
        row = db.execute("SELECT * FROM tower_sites WHERE id = ?", (site_id,)).fetchone()
        return dict(row) if row else None


def create_tower_site(name: str, location: str = None, latitude: float = None, longitude: float = None) -> int:
    """Create a new tower site."""
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO tower_sites (name, location, latitude, longitude) VALUES (?, ?, ?, ?)",
            (name, location, latitude, longitude)
        )
        return cursor.lastrowid


def update_tower_site(site_id: int, **kwargs):
    """Update a tower site."""
    allowed = {"name", "location", "latitude", "longitude"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    with get_db() as db:
        db.execute(f"UPDATE tower_sites SET {set_clause} WHERE id = ?", (*updates.values(), site_id))


def delete_tower_site(site_id: int):
    """Delete a tower site (APs will have tower_site_id set to NULL)."""
    with get_db() as db:
        db.execute("UPDATE access_points SET tower_site_id = NULL WHERE tower_site_id = ?", (site_id,))
        db.execute("DELETE FROM tower_sites WHERE id = ?", (site_id,))


# Access Point operations
def get_access_points(tower_site_id: int = None, enabled_only: bool = True) -> list[dict]:
    """Get access points, optionally filtered by tower site."""
    with get_db() as db:
        query = "SELECT * FROM access_points WHERE 1=1"
        params = []

        if tower_site_id is not None:
            query += " AND tower_site_id = ?"
            params.append(tower_site_id)

        if enabled_only:
            query += " AND enabled = 1"

        query += " ORDER BY ip"
        rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_access_point(ip: str) -> Optional[dict]:
    """Get an access point by IP."""
    with get_db() as db:
        row = db.execute("SELECT * FROM access_points WHERE ip = ?", (ip,)).fetchone()
        return dict(row) if row else None


def upsert_access_point(ip: str, username: str, password: str, tower_site_id: int = None, **kwargs) -> int:
    """Create or update an access point."""
    with get_db() as db:
        existing = db.execute("SELECT id FROM access_points WHERE ip = ?", (ip,)).fetchone()

        if existing:
            # Update
            updates = {"username": username, "password": password, "tower_site_id": tower_site_id}
            allowed = {"system_name", "model", "mac", "firmware_version", "location", "last_seen", "last_error", "enabled"}
            updates.update({k: v for k, v in kwargs.items() if k in allowed})

            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            db.execute(f"UPDATE access_points SET {set_clause} WHERE ip = ?", (*updates.values(), ip))
            return existing["id"]
        else:
            # Insert
            db.execute(
                """INSERT INTO access_points (ip, username, password, tower_site_id, system_name, model, mac, firmware_version, location)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ip, username, password, tower_site_id,
                 kwargs.get("system_name"), kwargs.get("model"), kwargs.get("mac"),
                 kwargs.get("firmware_version"), kwargs.get("location"))
            )
            return db.execute("SELECT last_insert_rowid()").fetchone()[0]


_UNSET = object()

def update_ap_status(ip: str, last_seen: str = None, last_error: str = _UNSET, **kwargs):
    """Update AP status after a poll."""
    with get_db() as db:
        updates = {}
        if last_seen:
            updates["last_seen"] = last_seen
        if last_error is not _UNSET:
            updates["last_error"] = last_error

        allowed = {"system_name", "model", "mac", "firmware_version", "location"}
        updates.update({k: v for k, v in kwargs.items() if k in allowed})

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            db.execute(f"UPDATE access_points SET {set_clause} WHERE ip = ?", (*updates.values(), ip))


def delete_access_point(ip: str):
    """Delete an access point and its cached CPEs."""
    with get_db() as db:
        db.execute("DELETE FROM cpe_cache WHERE ap_ip = ?", (ip,))
        db.execute("DELETE FROM access_points WHERE ip = ?", (ip,))


# CPE Cache operations
def upsert_cpe(ap_ip: str, cpe_data: dict):
    """Update or insert a CPE record."""
    with get_db() as db:
        db.execute("""
            INSERT INTO cpe_cache (ap_ip, ip, mac, system_name, model, firmware_version,
                                   link_distance, rx_power, combined_signal, last_local_rssi,
                                   tx_rate, rx_rate, mcs, link_uptime, signal_health, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ap_ip, ip) DO UPDATE SET
                mac = excluded.mac,
                system_name = excluded.system_name,
                model = excluded.model,
                firmware_version = excluded.firmware_version,
                link_distance = excluded.link_distance,
                rx_power = excluded.rx_power,
                combined_signal = excluded.combined_signal,
                last_local_rssi = excluded.last_local_rssi,
                tx_rate = excluded.tx_rate,
                rx_rate = excluded.rx_rate,
                mcs = excluded.mcs,
                link_uptime = excluded.link_uptime,
                signal_health = excluded.signal_health,
                last_updated = excluded.last_updated
        """, (
            ap_ip, cpe_data.get("ip"), cpe_data.get("mac"), cpe_data.get("system_name"),
            cpe_data.get("model"), cpe_data.get("firmware_version"),
            cpe_data.get("link_distance"), cpe_data.get("rx_power"),
            cpe_data.get("combined_signal"), cpe_data.get("last_local_rssi"),
            cpe_data.get("tx_rate"), cpe_data.get("rx_rate"),
            cpe_data.get("mcs"), cpe_data.get("link_uptime"),
            cpe_data.get("signal_health"), datetime.now().isoformat()
        ))


def get_cpes_for_ap(ap_ip: str) -> list[dict]:
    """Get cached CPEs for an AP."""
    with get_db() as db:
        rows = db.execute("SELECT * FROM cpe_cache WHERE ap_ip = ? ORDER BY ip", (ap_ip,)).fetchall()
        return [dict(row) for row in rows]


def get_all_cpes() -> list[dict]:
    """Get all cached CPEs."""
    with get_db() as db:
        rows = db.execute("SELECT * FROM cpe_cache ORDER BY ap_ip, ip").fetchall()
        return [dict(row) for row in rows]


def clear_cpes_for_ap(ap_ip: str):
    """Clear cached CPEs for an AP (before refresh)."""
    with get_db() as db:
        db.execute("DELETE FROM cpe_cache WHERE ap_ip = ?", (ap_ip,))


def get_health_summary() -> dict:
    """Get overall signal health summary."""
    with get_db() as db:
        rows = db.execute("""
            SELECT signal_health, COUNT(*) as count
            FROM cpe_cache
            GROUP BY signal_health
        """).fetchall()

        summary = {"green": 0, "yellow": 0, "red": 0}
        for row in rows:
            if row["signal_health"] in summary:
                summary[row["signal_health"]] = row["count"]
        return summary


# Settings operations
def get_setting(key: str, default: str = None) -> Optional[str]:
    """Get a setting value."""
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    """Set a setting value."""
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.now().isoformat())
        )


def get_all_settings() -> dict:
    """Get all settings as a dictionary."""
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def set_settings(settings: dict):
    """Set multiple settings at once."""
    with get_db() as db:
        for key, value in settings.items():
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, str(value), datetime.now().isoformat())
            )


# Session operations
def create_session(session_id: str, username: str, ip_address: str, expires_at: str):
    """Create a new session."""
    with get_db() as db:
        db.execute(
            "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, username, ip_address, expires_at)
        )


def get_session(session_id: str) -> Optional[dict]:
    """Get a session by ID, returns None if expired or not found."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND expires_at > ?",
            (session_id, datetime.now().isoformat())
        ).fetchone()
        return dict(row) if row else None


def delete_session(session_id: str):
    """Delete a session."""
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def cleanup_expired_sessions():
    """Remove all expired sessions."""
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (datetime.now().isoformat(),))


# Initialize on import
init_db()
