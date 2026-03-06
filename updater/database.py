"""SQLite database for persistent storage."""

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .crypto import encrypt_password, decrypt_password, is_encrypted

logger = logging.getLogger(__name__)

# Database file location
DB_PATH = Path(__file__).parent.parent / "data" / "tachyon.db"

# Settings cache — short TTL to reduce DB hits during poll cycles
_settings_cache: Optional[dict] = None
_settings_cache_time: float = 0
_SETTINGS_CACHE_TTL = 5  # seconds


def _migrate(db):
    """Run schema migrations."""
    # Check if auth_status column exists on cpe_cache
    columns = [row[1] for row in db.execute("PRAGMA table_info(cpe_cache)").fetchall()]
    if "auth_status" not in columns:
        db.execute("ALTER TABLE cpe_cache ADD COLUMN auth_status TEXT DEFAULT NULL")

    # Add timezone column to job_history
    jh_columns = [row[1] for row in db.execute("PRAGMA table_info(job_history)").fetchall()]
    if "timezone" not in jh_columns:
        db.execute("ALTER TABLE job_history ADD COLUMN timezone TEXT DEFAULT NULL")

    # Add bank columns to access_points
    ap_columns = [row[1] for row in db.execute("PRAGMA table_info(access_points)").fetchall()]
    for col in ("bank1_version", "bank2_version"):
        if col not in ap_columns:
            db.execute(f"ALTER TABLE access_points ADD COLUMN {col} TEXT DEFAULT NULL")
    if "active_bank" not in ap_columns:
        db.execute("ALTER TABLE access_points ADD COLUMN active_bank INTEGER DEFAULT NULL")
    if "last_firmware_update" not in ap_columns:
        db.execute("ALTER TABLE access_points ADD COLUMN last_firmware_update TEXT DEFAULT NULL")

    # Add last_firmware_update to switches
    sw_columns = [row[1] for row in db.execute("PRAGMA table_info(switches)").fetchall()]
    if "last_firmware_update" not in sw_columns:
        db.execute("ALTER TABLE switches ADD COLUMN last_firmware_update TEXT DEFAULT NULL")

    # Add bank columns to cpe_cache
    if "bank1_version" not in columns:
        db.execute("ALTER TABLE cpe_cache ADD COLUMN bank1_version TEXT DEFAULT NULL")
    if "bank2_version" not in columns:
        db.execute("ALTER TABLE cpe_cache ADD COLUMN bank2_version TEXT DEFAULT NULL")
    if "active_bank" not in columns:
        db.execute("ALTER TABLE cpe_cache ADD COLUMN active_bank INTEGER DEFAULT NULL")
    if "bank_last_fetched" not in columns:
        db.execute("ALTER TABLE cpe_cache ADD COLUMN bank_last_fetched TEXT DEFAULT NULL")

    # Add firmware_file_tns100 column to rollouts
    rollout_columns = [row[1] for row in db.execute("PRAGMA table_info(rollouts)").fetchall()]
    if "firmware_file_tns100" not in rollout_columns:
        db.execute("ALTER TABLE rollouts ADD COLUMN firmware_file_tns100 TEXT DEFAULT NULL")
    if "target_version_303l" not in rollout_columns:
        db.execute("ALTER TABLE rollouts ADD COLUMN target_version_303l TEXT DEFAULT NULL")
    if "target_version_tns100" not in rollout_columns:
        db.execute("ALTER TABLE rollouts ADD COLUMN target_version_tns100 TEXT DEFAULT NULL")

    # Backfill firmware_registry for existing firmware files
    firmware_dir = Path(__file__).parent.parent / "firmware"
    if firmware_dir.exists():
        existing_registered = {
            row[0] for row in db.execute("SELECT filename FROM firmware_registry").fetchall()
        }
        for f in firmware_dir.iterdir():
            if f.is_file() and f.suffix in ('.bin', '.img', '.npk', '.tar', '.gz') and f.name not in existing_registered:
                db.execute(
                    "INSERT OR IGNORE INTO firmware_registry (filename, added_at, source) VALUES (?, ?, ?)",
                    (f.name, "2020-01-01T00:00:00", "legacy")
                )

    # Encrypt any plaintext device passwords
    _migrate_encrypt_passwords(db)


def _migrate_encrypt_passwords(db):
    """One-time migration: encrypt any plaintext device passwords in-place."""
    migrated = 0
    for table in ("access_points", "switches"):
        rows = db.execute(f"SELECT ip, password FROM {table}").fetchall()
        for row in rows:
            pw = row[1]
            if pw and not is_encrypted(pw):
                db.execute(
                    f"UPDATE {table} SET password = ? WHERE ip = ?",
                    (encrypt_password(pw), row[0]),
                )
                migrated += 1
    if migrated:
        logger.info(f"Encrypted {migrated} plaintext device password(s)")


def init_db():
    """Initialize the database schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Integrity check and vacuum on existing databases
    if DB_PATH.exists():
        check_conn = None
        try:
            check_conn = sqlite3.connect(str(DB_PATH), timeout=10)
            result = check_conn.execute("PRAGMA integrity_check").fetchone()
            if result[0] != "ok":
                logger.error(f"Database integrity check failed: {result[0]}")
            check_conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            check_conn.execute("PRAGMA incremental_vacuum(100)")
            check_conn.commit()
        except Exception as e:
            logger.error(f"Database integrity check error: {e}")
        finally:
            if check_conn:
                check_conn.close()

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
                last_firmware_update TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tower_site_id) REFERENCES tower_sites(id)
            );

            CREATE TABLE IF NOT EXISTS switches (
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
                bank1_version TEXT,
                bank2_version TEXT,
                active_bank INTEGER,
                last_firmware_update TEXT,
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
            CREATE INDEX IF NOT EXISTS idx_cpe_ip ON cpe_cache(ip);

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

            CREATE TABLE IF NOT EXISTS job_history (
                job_id TEXT PRIMARY KEY,
                started_at TEXT,
                completed_at TEXT,
                duration REAL,
                bank_mode TEXT,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                cancelled_count INTEGER DEFAULT 0,
                devices_json TEXT,
                ap_cpe_map_json TEXT,
                device_roles_json TEXT,
                timezone TEXT
            );

            CREATE TABLE IF NOT EXISTS schedule_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                event TEXT NOT NULL,
                details TEXT,
                job_id TEXT
            );

            CREATE TABLE IF NOT EXISTS rollouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                firmware_file TEXT NOT NULL,
                firmware_file_303l TEXT,
                target_version TEXT,
                target_version_303l TEXT,
                target_version_tns100 TEXT,
                phase TEXT NOT NULL DEFAULT 'canary',
                status TEXT NOT NULL DEFAULT 'active',
                pause_reason TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_phase_completed_at TEXT,
                last_job_id TEXT
            );

            CREATE TABLE IF NOT EXISTS rollout_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rollout_id INTEGER NOT NULL,
                ip TEXT NOT NULL,
                device_type TEXT NOT NULL DEFAULT 'ap',
                phase_assigned TEXT,
                status TEXT DEFAULT 'pending',
                updated_at TEXT,
                FOREIGN KEY (rollout_id) REFERENCES rollouts(id),
                UNIQUE(rollout_id, ip)
            );

            CREATE TABLE IF NOT EXISTS device_durations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                ip TEXT NOT NULL,
                role TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                bank_mode TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS firmware_registry (
                filename TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                source TEXT DEFAULT 'manual'
            );

            CREATE TABLE IF NOT EXISTS device_update_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                ip TEXT NOT NULL,
                role TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'firmware_update',
                pass_number INTEGER DEFAULT 1,
                status TEXT NOT NULL,
                old_version TEXT,
                new_version TEXT,
                model TEXT,
                error TEXT,
                failed_stage TEXT,
                stages_json TEXT,
                duration_seconds REAL,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_device_history_ip ON device_update_history(ip);
            CREATE INDEX IF NOT EXISTS idx_device_history_job ON device_update_history(job_id);
            CREATE INDEX IF NOT EXISTS idx_device_history_action ON device_update_history(action);
            CREATE INDEX IF NOT EXISTS idx_device_history_completed ON device_update_history(completed_at DESC);

            CREATE TABLE IF NOT EXISTS device_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                model TEXT,
                hardware_id TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_device_configs_ip ON device_configs(ip);
            CREATE INDEX IF NOT EXISTS idx_device_configs_hash ON device_configs(ip, config_hash);
            CREATE INDEX IF NOT EXISTS idx_device_configs_fetched ON device_configs(ip, fetched_at DESC);

            CREATE TABLE IF NOT EXISTS config_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                config_fragment TEXT NOT NULL,
                form_data TEXT,
                description TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS radius_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password TEXT NOT NULL,
                description TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                auth_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_auth_at TEXT
            );

            CREATE TABLE IF NOT EXISTS radius_auth_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                client_ip TEXT,
                client_name TEXT,
                client_model TEXT,
                outcome TEXT NOT NULL,
                reason TEXT,
                occurred_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS radius_client_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_spec TEXT NOT NULL UNIQUE COLLATE NOCASE,
                shortname TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS radius_rollouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_template_id INTEGER,
                phase TEXT NOT NULL DEFAULT 'canary',
                status TEXT NOT NULL DEFAULT 'active',
                pause_reason TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_phase_completed_at TEXT,
                completed_at TEXT,
                service_username TEXT
            );
            CREATE TABLE IF NOT EXISTS radius_rollout_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rollout_id INTEGER NOT NULL,
                ip TEXT NOT NULL,
                device_type TEXT NOT NULL,
                phase_assigned TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                updated_at TEXT,
                FOREIGN KEY (rollout_id) REFERENCES radius_rollouts(id),
                UNIQUE(rollout_id, ip)
            );
            CREATE INDEX IF NOT EXISTS idx_radius_auth_occurred ON radius_auth_log(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_radius_auth_client_ip ON radius_auth_log(client_ip);
            CREATE INDEX IF NOT EXISTS idx_radius_auth_username ON radius_auth_log(username);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_radius_auth_unique ON radius_auth_log(occurred_at, username, client_ip, outcome);

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT,
                role TEXT NOT NULL DEFAULT 'viewer',
                auth_method TEXT NOT NULL DEFAULT 'local',
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Migrations: add columns if missing
        _migrate(db)

        # Insert default settings if not exists
        defaults = {
            "schedule_enabled": "false",
            "schedule_days": "tue,wed,thu",
            "schedule_start_hour": "3",
            "schedule_end_hour": "4",
            "parallel_updates": "2",
            "bank_mode": "one",
            "allow_downgrade": "false",
            "timezone": "auto",
            "zip_code": "",
            "weather_check_enabled": "true",
            "min_temperature_c": "-10",
            "temperature_unit": "auto",  # "auto", "c", or "f"
            "schedule_scope": "all",
            "schedule_scope_data": "",
            "firmware_beta_enabled": "false",
            "firmware_last_check": "",
            "firmware_last_check_error": "",
            "firmware_auto_fetched_files": "",
            "setup_completed": "false",
            "admin_password_hash": "",
            "firmware_quarantine_days": "7",
            "slack_webhook_url": "",
            # RADIUS configuration for web authentication
            "radius_enabled": "",  # Empty = use env vars, "true"/"false" = explicit
            "radius_server": "",
            "radius_secret": "",
            "radius_port": "1812",
            "radius_timeout": "5",
            # Built-in RADIUS server for device admin auth
            "builtin_radius_enabled": "true",
            "builtin_radius_host": "",
            "builtin_radius_port": "1812",
            "builtin_radius_secret": "",
            "builtin_radius_secret_updated_at": "",
            "builtin_radius_secret_review_acknowledged_at": "",
            "builtin_radius_mgmt_password": "",
            "rollout_canary_aps": "",
            "rollout_canary_switches": "",
            # Global default device credentials
            "device_default_auth_enabled": "false",
            "device_default_username": "",
            "device_default_password": "",
            # SSL/HTTPS configuration
            "ssl_enabled": "false",
            "ssl_domain": "",
            "ssl_email": "",
            "ssl_cert_expires": "",
            # SFTP backup configuration
            "backup_enabled": "false",
            "backup_sftp_host": "",
            "backup_sftp_port": "22",
            "backup_sftp_path": "/backups/tachyon",
            "backup_sftp_username": "",
            "backup_sftp_password": "",
            "backup_sftp_auth_method": "password",
            "backup_retention_count": "30",
            "backup_last_run": "",
            "backup_last_status": "",
            # Setup wizard tracking
            "setup_wizard_completed": "false",
            # Auto-update configuration
            "release_channel": "stable",  # "stable" or "dev"
            "autoupdate_enabled": "true",
            "autoupdate_last_check": "",
            "autoupdate_available_version": "",
            "autoupdate_release_url": "",
            "autoupdate_release_notes": "",
            # Pre-update reboot
            "pre_update_reboot": "true",
            # Config polling
            "config_poll_enabled": "true",
            "config_poll_interval_hours": "24",
            # Vendor feature flags
            "mikrotik_enabled": "false",
            # License configuration
            "license_key": "",
            "license_status": "free",
            "license_customer_name": "",
            "license_expires_at": "",
            "license_last_validated": "",
            "license_grace_until": "",
            "license_device_limit": "0",
            "license_error": "",
            # Built-in RADIUS server
            "radius_server_enabled": "false",
            "radius_server_port": "1812",
            "radius_server_secret": "",
            "radius_server_auth_mode": "local",
            "radius_server_advertised_address": "",
            "radius_server_ldap_url": "",
            "radius_server_ldap_bind_dn": "",
            "radius_server_ldap_bind_password": "",
            "radius_server_ldap_base_dn": "",
            "radius_server_ldap_user_filter": "(&(objectClass=user)(sAMAccountName={username}))",
        }
        for key, value in defaults.items():
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )


@contextmanager
def get_db():
    """Get database connection context manager."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def checkpoint_db():
    """Flush WAL to main database file for clean shutdown."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        logger.info("Database WAL checkpoint completed")
    except Exception as e:
        logger.error(f"Database WAL checkpoint failed: {e}")


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


def get_tower_site_by_name(name: str) -> Optional[dict]:
    """Get a tower site by name (case-insensitive)."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM tower_sites WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone()
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
    """Delete a tower site (APs and switches will have tower_site_id set to NULL)."""
    with get_db() as db:
        db.execute("UPDATE access_points SET tower_site_id = NULL WHERE tower_site_id = ?", (site_id,))
        db.execute("UPDATE switches SET tower_site_id = NULL WHERE tower_site_id = ?", (site_id,))
        db.execute("DELETE FROM tower_sites WHERE id = ?", (site_id,))


def get_all_device_ips() -> set:
    """Get all device IPs (APs + switches) without decrypting passwords."""
    with get_db() as db:
        ap_rows = db.execute("SELECT ip FROM access_points").fetchall()
        sw_rows = db.execute("SELECT ip FROM switches").fetchall()
        return {row["ip"] for row in ap_rows} | {row["ip"] for row in sw_rows}


def _decrypt_device_row(row_dict: dict) -> dict:
    """Decrypt the password field in a device row if it's encrypted."""
    if row_dict and "password" in row_dict and row_dict["password"]:
        if is_encrypted(row_dict["password"]):
            row_dict["password"] = decrypt_password(row_dict["password"])
    return row_dict


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
        return [_decrypt_device_row(dict(row)) for row in rows]


def get_access_point(ip: str) -> Optional[dict]:
    """Get an access point by IP."""
    with get_db() as db:
        row = db.execute("SELECT * FROM access_points WHERE ip = ?", (ip,)).fetchone()
        return _decrypt_device_row(dict(row)) if row else None


def upsert_access_point(ip: str, username: str, password: str, tower_site_id: int = None, **kwargs) -> int:
    """Create or update an access point."""
    enc_password = encrypt_password(password) if not is_encrypted(password) else password
    with get_db() as db:
        existing = db.execute("SELECT id FROM access_points WHERE ip = ?", (ip,)).fetchone()

        if existing:
            # Update
            updates = {"username": username, "password": enc_password, "tower_site_id": tower_site_id}
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
                (ip, username, enc_password, tower_site_id,
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

        allowed = {"system_name", "model", "mac", "firmware_version", "location",
                   "bank1_version", "bank2_version", "active_bank"}
        updates.update({k: v for k, v in kwargs.items() if k in allowed})

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            db.execute(f"UPDATE access_points SET {set_clause} WHERE ip = ?", (*updates.values(), ip))


def delete_access_point(ip: str):
    """Delete an access point and its cached CPEs."""
    with get_db() as db:
        db.execute("DELETE FROM cpe_cache WHERE ap_ip = ?", (ip,))
        db.execute("DELETE FROM access_points WHERE ip = ?", (ip,))


# Switch operations
def get_switches(tower_site_id: int = None, enabled_only: bool = True) -> list[dict]:
    """Get switches, optionally filtered by tower site."""
    with get_db() as db:
        query = "SELECT * FROM switches WHERE 1=1"
        params = []

        if tower_site_id is not None:
            query += " AND tower_site_id = ?"
            params.append(tower_site_id)

        if enabled_only:
            query += " AND enabled = 1"

        query += " ORDER BY ip"
        rows = db.execute(query, params).fetchall()
        return [_decrypt_device_row(dict(row)) for row in rows]


def get_switch(ip: str) -> Optional[dict]:
    """Get a switch by IP."""
    with get_db() as db:
        row = db.execute("SELECT * FROM switches WHERE ip = ?", (ip,)).fetchone()
        return _decrypt_device_row(dict(row)) if row else None


def upsert_switch(ip: str, username: str, password: str, tower_site_id: int = None, **kwargs) -> int:
    """Create or update a switch."""
    enc_password = encrypt_password(password) if not is_encrypted(password) else password
    with get_db() as db:
        existing = db.execute("SELECT id FROM switches WHERE ip = ?", (ip,)).fetchone()

        if existing:
            updates = {"username": username, "password": enc_password, "tower_site_id": tower_site_id}
            allowed = {"system_name", "model", "mac", "firmware_version", "location", "last_seen", "last_error", "enabled"}
            updates.update({k: v for k, v in kwargs.items() if k in allowed})

            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            db.execute(f"UPDATE switches SET {set_clause} WHERE ip = ?", (*updates.values(), ip))
            return existing["id"]
        else:
            db.execute(
                """INSERT INTO switches (ip, username, password, tower_site_id, system_name, model, mac, firmware_version, location)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ip, username, enc_password, tower_site_id,
                 kwargs.get("system_name"), kwargs.get("model"), kwargs.get("mac"),
                 kwargs.get("firmware_version"), kwargs.get("location"))
            )
            return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_switch_status(ip: str, last_seen: str = None, last_error: str = _UNSET, **kwargs):
    """Update switch status after a poll."""
    with get_db() as db:
        updates = {}
        if last_seen:
            updates["last_seen"] = last_seen
        if last_error is not _UNSET:
            updates["last_error"] = last_error

        allowed = {"system_name", "model", "mac", "firmware_version", "location",
                   "bank1_version", "bank2_version", "active_bank"}
        updates.update({k: v for k, v in kwargs.items() if k in allowed})

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            db.execute(f"UPDATE switches SET {set_clause} WHERE ip = ?", (*updates.values(), ip))


def delete_switch(ip: str):
    """Delete a switch."""
    with get_db() as db:
        db.execute("DELETE FROM switches WHERE ip = ?", (ip,))


def update_device_credentials(device_type: str, ip: str, username: str, password: str):
    """Update stored credentials for an AP or switch."""
    enc_password = encrypt_password(password) if not is_encrypted(password) else password
    table = "access_points" if device_type == "ap" else "switches"
    with get_db() as db:
        db.execute(
            f"UPDATE {table} SET username = ?, password = ? WHERE ip = ?",
            (username, enc_password, ip),
        )


# CPE Cache operations
def upsert_cpe(ap_ip: str, cpe_data: dict):
    """Update or insert a CPE record."""
    with get_db() as db:
        db.execute("""
            INSERT INTO cpe_cache (ap_ip, ip, mac, system_name, model, firmware_version,
                                   link_distance, rx_power, combined_signal, last_local_rssi,
                                   tx_rate, rx_rate, mcs, link_uptime, signal_health,
                                   auth_status, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                auth_status = excluded.auth_status,
                last_updated = excluded.last_updated
        """, (
            ap_ip, cpe_data.get("ip"), cpe_data.get("mac"), cpe_data.get("system_name"),
            cpe_data.get("model"), cpe_data.get("firmware_version"),
            cpe_data.get("link_distance"), cpe_data.get("rx_power"),
            cpe_data.get("combined_signal"), cpe_data.get("last_local_rssi"),
            cpe_data.get("tx_rate"), cpe_data.get("rx_rate"),
            cpe_data.get("mcs"), cpe_data.get("link_uptime"),
            cpe_data.get("signal_health"), cpe_data.get("auth_status"),
            datetime.now().isoformat()
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


def get_cpe_by_ip(ip: str) -> Optional[dict]:
    """Get a CPE by its IP address."""
    with get_db() as db:
        row = db.execute("SELECT * FROM cpe_cache WHERE ip = ?", (ip,)).fetchone()
        return dict(row) if row else None


def update_cpe_auth_status(ap_ip: str, cpe_ip: str, auth_status: str):
    """Update auth_status for a specific CPE."""
    with get_db() as db:
        db.execute(
            "UPDATE cpe_cache SET auth_status = ? WHERE ap_ip = ? AND ip = ?",
            (auth_status, ap_ip, cpe_ip)
        )


def update_cpe_bank_info(ap_ip: str, cpe_ip: str, bank1: str, bank2: str, active: int):
    """Update bank info for a specific CPE."""
    with get_db() as db:
        db.execute(
            """UPDATE cpe_cache SET bank1_version = ?, bank2_version = ?, active_bank = ?,
               bank_last_fetched = ? WHERE ap_ip = ? AND ip = ?""",
            (bank1, bank2, active, datetime.now().isoformat(), ap_ip, cpe_ip)
        )


def get_cpe_bank_last_fetched(ap_ip: str, cpe_ip: str) -> Optional[str]:
    """Get when bank info was last fetched for a CPE."""
    with get_db() as db:
        row = db.execute(
            "SELECT bank_last_fetched FROM cpe_cache WHERE ap_ip = ? AND ip = ?",
            (ap_ip, cpe_ip)
        ).fetchone()
        return row["bank_last_fetched"] if row else None


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
def _invalidate_settings_cache():
    """Invalidate the in-memory settings cache."""
    global _settings_cache, _settings_cache_time
    _settings_cache = None
    _settings_cache_time = 0


def _get_cached_settings() -> dict:
    """Get settings from cache or DB. Returns full settings dict."""
    global _settings_cache, _settings_cache_time
    now = time.monotonic()
    if _settings_cache is not None and (now - _settings_cache_time) < _SETTINGS_CACHE_TTL:
        return _settings_cache
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
        _settings_cache = {row["key"]: row["value"] for row in rows}
        _settings_cache_time = now
        return _settings_cache


def get_setting(key: str, default: str = None) -> Optional[str]:
    """Get a setting value (uses cache)."""
    return _get_cached_settings().get(key, default)


def set_setting(key: str, value: str):
    """Set a setting value."""
    _invalidate_settings_cache()
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.now().isoformat())
        )


def delete_setting(key: str):
    """Delete a setting by key."""
    _invalidate_settings_cache()
    with get_db() as db:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))


def delete_expired_oidc_states(cutoff_iso: str) -> int:
    """Delete OIDC state entries older than cutoff. Returns count deleted."""
    _invalidate_settings_cache()
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'oidc_state_%'"
        ).fetchall()
        deleted = 0
        for row in rows:
            value = row["value"]
            if not value:
                db.execute("DELETE FROM settings WHERE key = ?", (row["key"],))
                deleted += 1
                continue
            try:
                import json
                data = json.loads(value)
                if data.get("created_at", "") < cutoff_iso:
                    db.execute("DELETE FROM settings WHERE key = ?", (row["key"],))
                    deleted += 1
            except (json.JSONDecodeError, TypeError):
                db.execute("DELETE FROM settings WHERE key = ?", (row["key"],))
                deleted += 1
        return deleted


def get_all_settings() -> dict:
    """Get all settings as a dictionary (uses cache)."""
    return dict(_get_cached_settings())


def set_settings(settings: dict):
    """Set multiple settings at once."""
    _invalidate_settings_cache()
    with get_db() as db:
        for key, value in settings.items():
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, str(value), datetime.now().isoformat())
            )


# Firmware registry operations
def register_firmware(filename: str, source: str = "manual"):
    """Register a firmware file with its addition timestamp. No-op if already registered."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO firmware_registry (filename, added_at, source) VALUES (?, ?, ?)",
            (filename, datetime.now().isoformat(), source)
        )


def get_firmware_registry() -> list[dict]:
    """Get all registered firmware entries."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM firmware_registry ORDER BY added_at DESC").fetchall()
        return [dict(row) for row in rows]


def get_firmware_added_at(filename: str) -> Optional[str]:
    """Get the added_at timestamp for a firmware file."""
    with get_db() as conn:
        row = conn.execute("SELECT added_at FROM firmware_registry WHERE filename = ?", (filename,)).fetchone()
        return row["added_at"] if row else None


def unregister_firmware(filename: str):
    """Remove a firmware file from the registry."""
    with get_db() as conn:
        conn.execute("DELETE FROM firmware_registry WHERE filename = ?", (filename,))


def _extract_release_date_from_filename(filename: str) -> Optional[datetime]:
    """Extract release date from firmware filename (e.g., tna-30x-1.12.2-r54944-20250828-...).

    Returns the release date as datetime, or None if not found.
    """
    import re
    # Look for YYYYMMDD pattern in filename
    match = re.search(r'-(\d{4})(\d{2})(\d{2})-', filename)
    if match:
        try:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            return datetime(year, month, day)
        except ValueError:
            pass
    return None


def is_firmware_hold_cleared(filename: str, hold_days: int = 7) -> bool:
    """Check if firmware has cleared the auto-download hold period.

    Uses the release date from the filename (preferred) or falls back to download date.
    """
    # Try to get release date from filename first
    release_date = _extract_release_date_from_filename(filename)
    if release_date:
        return datetime.now() >= release_date + timedelta(days=hold_days)

    # Fall back to download date
    added_at = get_firmware_added_at(filename)
    if added_at is None:
        return True  # Not registered — treat as cleared (legacy file)
    added_dt = datetime.fromisoformat(added_at)
    return datetime.now() >= added_dt + timedelta(days=hold_days)


# Alias for backwards compatibility
def is_firmware_quarantine_cleared(filename: str, quarantine_days: int = 7) -> bool:
    """Alias for is_firmware_hold_cleared (backwards compatibility)."""
    return is_firmware_hold_cleared(filename, quarantine_days)


def get_firmware_hold_info(filename: str, hold_days: int = 7) -> dict:
    """Get hold period status info for a firmware file.

    Uses release date from filename when available.
    """
    # Try to get release date from filename first
    release_date = _extract_release_date_from_filename(filename)

    if release_date:
        reference_dt = release_date
        reference_type = "release_date"
    else:
        added_at = get_firmware_added_at(filename)
        if added_at is None:
            return {"cleared": True, "reference_date": None, "reference_type": None,
                    "clears_at": None, "remaining_days": 0}
        reference_dt = datetime.fromisoformat(added_at)
        reference_type = "download_date"

    clears_at = reference_dt + timedelta(days=hold_days)
    now = datetime.now()
    cleared = now >= clears_at
    remaining_seconds = max(0, (clears_at - now).total_seconds()) if not cleared else 0
    remaining_days = remaining_seconds / 86400

    return {
        "cleared": cleared,
        "reference_date": reference_dt.isoformat(),
        "reference_type": reference_type,
        "clears_at": clears_at.isoformat(),
        "remaining_days": round(remaining_days, 1),
    }


# Alias for backwards compatibility
def get_firmware_quarantine_info(filename: str, quarantine_days: int = 7) -> dict:
    """Alias for get_firmware_hold_info (backwards compatibility)."""
    info = get_firmware_hold_info(filename, quarantine_days)
    # Map new fields to old field names for compatibility
    return {
        "cleared": info["cleared"],
        "added_at": info.get("reference_date"),
        "clears_at": info.get("clears_at"),
        "remaining_hours": round(info.get("remaining_days", 0) * 24, 1),
    }


# Job History operations
def save_job_history(job_id: str, started_at: str, completed_at: str, duration: float,
                     bank_mode: str, success_count: int, failed_count: int,
                     skipped_count: int, cancelled_count: int,
                     devices: dict, ap_cpe_map: dict, device_roles: dict,
                     timezone: str = None):
    """Save a completed job to history."""
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO job_history
               (job_id, started_at, completed_at, duration, bank_mode,
                success_count, failed_count, skipped_count, cancelled_count,
                devices_json, ap_cpe_map_json, device_roles_json, timezone)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, started_at, completed_at, duration, bank_mode,
             success_count, failed_count, skipped_count, cancelled_count,
             json.dumps(devices), json.dumps(ap_cpe_map), json.dumps(device_roles),
             timezone)
        )


def get_job_history(limit: int = 20) -> list[dict]:
    """Get recent job history, newest first."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM job_history ORDER BY completed_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["devices"] = json.loads(d.pop("devices_json"))
            d["ap_cpe_map"] = json.loads(d.pop("ap_cpe_map_json"))
            d["device_roles"] = json.loads(d.pop("device_roles_json"))
            results.append(d)
        return results


# Schedule Log operations
def log_schedule_event(event: str, details: str = None, job_id: str = None):
    """Log a scheduler event."""
    with get_db() as db:
        db.execute(
            "INSERT INTO schedule_log (timestamp, event, details, job_id) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), event, details, job_id)
        )


def get_schedule_log(limit: int = 50) -> list[dict]:
    """Get recent schedule log entries."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM schedule_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


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


def delete_all_sessions():
    """Delete all sessions."""
    with get_db() as db:
        db.execute("DELETE FROM sessions")


def cleanup_expired_sessions():
    """Remove all expired sessions."""
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (datetime.now().isoformat(),))


def cleanup_old_job_history(max_age_days: int = 90):
    """Remove job history entries older than max_age_days."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with get_db() as db:
        db.execute("DELETE FROM job_history WHERE completed_at < ?", (cutoff,))


def cleanup_old_schedule_log(max_age_days: int = 90):
    """Remove schedule log entries older than max_age_days."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with get_db() as db:
        db.execute("DELETE FROM schedule_log WHERE timestamp < ?", (cutoff,))


def cleanup_old_rollouts(max_age_days: int = 180):
    """Remove completed/cancelled rollouts and their devices older than max_age_days."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with get_db() as db:
        db.execute(
            """DELETE FROM rollout_devices WHERE rollout_id IN (
                SELECT id FROM rollouts WHERE status IN ('completed', 'cancelled') AND updated_at < ?
            )""",
            (cutoff,)
        )
        db.execute(
            "DELETE FROM rollouts WHERE status IN ('completed', 'cancelled') AND updated_at < ?",
            (cutoff,)
        )


def cleanup_old_device_durations(max_age_days: int = 180):
    """Remove device duration records older than max_age_days."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with get_db() as db:
        db.execute("DELETE FROM device_durations WHERE created_at < ?", (cutoff,))


def cleanup_old_radius_auth_log(max_age_days: int = 90):
    """Remove RADIUS auth log entries older than max_age_days."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with get_db() as db:
        db.execute("DELETE FROM radius_auth_log WHERE occurred_at < ?", (cutoff,))


# Rollout operations
def get_active_rollout() -> Optional[dict]:
    """Get the current active or paused rollout."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM rollouts WHERE status IN ('active', 'paused') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_current_rollout() -> Optional[dict]:
    """Get the most recent rollout (active, paused, or completed) for UI display."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM rollouts WHERE status IN ('active', 'paused', 'completed') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_rollout(rollout_id: int) -> Optional[dict]:
    """Get a rollout by ID."""
    with get_db() as db:
        row = db.execute("SELECT * FROM rollouts WHERE id = ?", (rollout_id,)).fetchone()
        return dict(row) if row else None


def create_rollout(firmware_file: str, firmware_file_303l: str = None, firmware_file_tns100: str = None) -> int:
    """Create a new rollout. Returns the rollout ID."""
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO rollouts (firmware_file, firmware_file_303l, firmware_file_tns100) VALUES (?, ?, ?)",
            (firmware_file, firmware_file_303l, firmware_file_tns100)
        )
        return cursor.lastrowid


def get_last_rollout_for_firmware(firmware_file: str) -> Optional[dict]:
    """Get the most recent rollout for a given firmware file."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM rollouts WHERE firmware_file = ? ORDER BY id DESC LIMIT 1",
            (firmware_file,)
        ).fetchone()
        return dict(row) if row else None


def get_last_rollout_for_firmware_set(
    firmware_file: str,
    firmware_file_303l: str = None,
    firmware_file_tns100: str = None,
) -> Optional[dict]:
    """Get the most recent rollout matching all three firmware files."""
    with get_db() as db:
        row = db.execute(
            """SELECT * FROM rollouts
               WHERE firmware_file = ?
                 AND COALESCE(firmware_file_303l, '') = ?
                 AND COALESCE(firmware_file_tns100, '') = ?
               ORDER BY id DESC LIMIT 1""",
            (firmware_file, firmware_file_303l or "", firmware_file_tns100 or ""),
        ).fetchone()
        return dict(row) if row else None


PHASE_ORDER = ["canary", "pct10", "pct50", "pct100"]


def advance_rollout_phase(rollout_id: int):
    """Advance rollout to the next phase. If already at pct100, mark completed."""
    with get_db() as db:
        row = db.execute("SELECT phase FROM rollouts WHERE id = ?", (rollout_id,)).fetchone()
        if not row:
            return
        current = row["phase"]
        now = datetime.now().isoformat()
        if current in PHASE_ORDER:
            idx = PHASE_ORDER.index(current)
            if idx + 1 < len(PHASE_ORDER):
                next_phase = PHASE_ORDER[idx + 1]
                db.execute(
                    "UPDATE rollouts SET phase = ?, updated_at = ? WHERE id = ?",
                    (next_phase, now, rollout_id)
                )
            else:
                db.execute(
                    "UPDATE rollouts SET status = 'completed', updated_at = ? WHERE id = ?",
                    (now, rollout_id)
                )


def complete_rollout_phase(rollout_id: int):
    """Mark current phase as completed and advance to next."""
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "UPDATE rollouts SET last_phase_completed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, rollout_id)
        )
    advance_rollout_phase(rollout_id)


def pause_rollout(rollout_id: int, reason: str):
    """Pause a rollout with a reason."""
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "UPDATE rollouts SET status = 'paused', pause_reason = ?, updated_at = ? WHERE id = ?",
            (reason, now, rollout_id)
        )


def resume_rollout(rollout_id: int):
    """Resume a paused rollout."""
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "UPDATE rollouts SET status = 'active', pause_reason = NULL, updated_at = ? WHERE id = ?",
            (now, rollout_id)
        )


def cancel_rollout(rollout_id: int):
    """Cancel a rollout."""
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "UPDATE rollouts SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (now, rollout_id)
        )


def set_rollout_target_versions(rollout_id: int, versions: dict):
    """Set learned target versions on a rollout."""
    now = datetime.now().isoformat()
    target_30x = versions.get("tna-30x")
    target_303l = versions.get("tna-303l")
    target_tns100 = versions.get("tns-100")
    with get_db() as db:
        db.execute(
            """UPDATE rollouts
               SET target_version = COALESCE(?, target_version),
                   target_version_303l = COALESCE(?, target_version_303l),
                   target_version_tns100 = COALESCE(?, target_version_tns100),
                   updated_at = ?
               WHERE id = ?""",
            (target_30x, target_303l, target_tns100, now, rollout_id)
        )


def set_rollout_job_id(rollout_id: int, job_id: str):
    """Set the current job ID on a rollout."""
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "UPDATE rollouts SET last_job_id = ?, updated_at = ? WHERE id = ?",
            (job_id, now, rollout_id)
        )


def assign_device_to_rollout(rollout_id: int, ip: str, device_type: str, phase: str):
    """Assign a device to a rollout phase."""
    with get_db() as db:
        db.execute(
            """INSERT OR IGNORE INTO rollout_devices (rollout_id, ip, device_type, phase_assigned, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (rollout_id, ip, device_type, phase)
        )


def mark_rollout_device(rollout_id: int, ip: str, status: str):
    """Mark a single rollout device status."""
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "UPDATE rollout_devices SET status = ?, updated_at = ? WHERE rollout_id = ? AND ip = ?",
            (status, now, rollout_id, ip)
        )
        # If successfully updated, record the timestamp on the device itself
        if status == "updated":
            # Try access_points first, then switches
            db.execute(
                "UPDATE access_points SET last_firmware_update = ? WHERE ip = ?",
                (now, ip)
            )
            db.execute(
                "UPDATE switches SET last_firmware_update = ? WHERE ip = ?",
                (now, ip)
            )


def mark_rollout_phase_devices(rollout_id: int, phase: str, status: str):
    """Bulk mark all devices in a rollout phase."""
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "UPDATE rollout_devices SET status = ?, updated_at = ? WHERE rollout_id = ? AND phase_assigned = ?",
            (status, now, rollout_id, phase)
        )
        # If successfully updated, record the timestamp on the devices themselves
        if status == "updated":
            # Get the IPs for this phase
            rows = db.execute(
                "SELECT ip FROM rollout_devices WHERE rollout_id = ? AND phase_assigned = ?",
                (rollout_id, phase)
            ).fetchall()
            for row in rows:
                ip = row[0]
                db.execute(
                    "UPDATE access_points SET last_firmware_update = ? WHERE ip = ?",
                    (now, ip)
                )
                db.execute(
                    "UPDATE switches SET last_firmware_update = ? WHERE ip = ?",
                    (now, ip)
                )


def get_rollout_devices(rollout_id: int, phase: str = None) -> list[dict]:
    """Get devices for a rollout, optionally filtered by phase."""
    with get_db() as db:
        if phase:
            rows = db.execute(
                "SELECT * FROM rollout_devices WHERE rollout_id = ? AND phase_assigned = ?",
                (rollout_id, phase)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM rollout_devices WHERE rollout_id = ?",
                (rollout_id,)
            ).fetchall()
        return [dict(row) for row in rows]


def get_rollout_progress(rollout_id: int) -> dict:
    """Get rollout progress counts by status."""
    with get_db() as db:
        rows = db.execute(
            "SELECT status, COUNT(*) as count FROM rollout_devices WHERE rollout_id = ? GROUP BY status",
            (rollout_id,)
        ).fetchall()
        result = {"total": 0, "pending": 0, "updated": 0, "failed": 0, "skipped": 0}
        for row in rows:
            s = row["status"]
            c = row["count"]
            if s in result:
                result[s] = c
            result["total"] += c
        return result


def get_rollout_devices_by_status(rollout_id: int, status: str) -> list[dict]:
    """Get rollout devices filtered by status."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ip, device_type, phase_assigned, status, updated_at FROM rollout_devices WHERE rollout_id = ? AND status = ?",
            (rollout_id, status),
        ).fetchall()
        return [dict(row) for row in rows]


# Device duration tracking
DEFAULT_DURATIONS = {"ap": 180.0, "cpe": 120.0, "switch": 600.0}


def save_device_duration(job_id: str, ip: str, role: str, duration_seconds: float, bank_mode: str = None):
    """Save a successful device update duration."""
    with get_db() as db:
        db.execute(
            "INSERT INTO device_durations (job_id, ip, role, duration_seconds, bank_mode) VALUES (?, ?, ?, ?, ?)",
            (job_id, ip, role, duration_seconds, bank_mode)
        )


def get_avg_durations() -> dict:
    """Get average update duration per device role from recent history.

    Returns dict like {"ap": 180.0, "cpe": 120.0, "switch": 600.0} in seconds.
    Falls back to defaults if no history for a role.
    """
    result = dict(DEFAULT_DURATIONS)
    with get_db() as db:
        for role in ("ap", "cpe", "switch"):
            row = db.execute(
                """SELECT AVG(duration_seconds) as avg_dur FROM (
                       SELECT duration_seconds FROM device_durations
                       WHERE role = ? ORDER BY created_at DESC LIMIT 50
                   )""",
                (role,)
            ).fetchone()
            if row and row["avg_dur"] is not None:
                result[role] = round(row["avg_dur"], 1)
    return result


# Device Update History operations

def save_device_update_history(job_id: Optional[str], ip: str, role: str, pass_number: int,
                               status: str, old_version: Optional[str], new_version: Optional[str],
                               model: Optional[str], error: Optional[str], failed_stage: Optional[str],
                               stages: list, duration_seconds: float,
                               started_at: str, completed_at: str,
                               action: str = "firmware_update"):
    """Save a per-device update/config history record."""
    with get_db() as db:
        db.execute(
            """INSERT INTO device_update_history
               (job_id, ip, role, action, pass_number, status, old_version, new_version,
                model, error, failed_stage, stages_json, duration_seconds,
                started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, ip, role, action, pass_number, status, old_version, new_version,
             model, error, failed_stage, json.dumps(stages), duration_seconds,
             started_at, completed_at)
        )


def get_device_update_history(ip: Optional[str] = None, action: Optional[str] = None,
                              status: Optional[str] = None,
                              limit: int = 100, offset: int = 0) -> list[dict]:
    """Get device update history with optional filters, newest first."""
    with get_db() as db:
        query = "SELECT * FROM device_update_history WHERE 1=1"
        params = []
        if ip:
            query += " AND ip = ?"
            params.append(ip)
        if action:
            query += " AND action = ?"
            params.append(action)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY completed_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = db.execute(query, params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["stages"] = json.loads(d.pop("stages_json")) if d.get("stages_json") else []
            results.append(d)
        return results


def get_device_update_history_by_job(job_id: str) -> list[dict]:
    """Get all device history records for a specific job."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM device_update_history WHERE job_id = ? ORDER BY ip, pass_number",
            (job_id,)
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["stages"] = json.loads(d.pop("stages_json")) if d.get("stages_json") else []
            results.append(d)
        return results


def cleanup_old_device_update_history(max_age_days: int = 180):
    """Remove device update history records older than max_age_days."""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with get_db() as db:
        db.execute("DELETE FROM device_update_history WHERE completed_at < ?", (cutoff,))


# Device Config operations

def save_device_config(ip: str, config_json: str, config_hash: str,
                       model: str = None, hardware_id: str = None):
    """Save a device config snapshot."""
    with get_db() as db:
        db.execute(
            """INSERT INTO device_configs (ip, config_json, config_hash, model, hardware_id, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ip, config_json, config_hash, model, hardware_id, datetime.now().isoformat())
        )


def get_latest_device_config(ip: str) -> Optional[dict]:
    """Get the most recent config snapshot for a device."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM device_configs WHERE ip = ? ORDER BY fetched_at DESC LIMIT 1",
            (ip,)
        ).fetchone()
        return dict(row) if row else None


def get_device_config_history(ip: str, limit: int = 20) -> list[dict]:
    """Get config snapshots for a device, newest first."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, ip, config_hash, model, hardware_id, fetched_at FROM device_configs WHERE ip = ? ORDER BY fetched_at DESC LIMIT ?",
            (ip, limit)
        ).fetchall()
        return [dict(row) for row in rows]


def get_device_config_by_id(config_id: int) -> Optional[dict]:
    """Get a specific config snapshot by ID."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM device_configs WHERE id = ?", (config_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_latest_configs() -> dict:
    """Get the latest config for each device. Returns {ip: {id, config_json, config_hash, fetched_at, ...}}."""
    with get_db() as db:
        rows = db.execute("""
            SELECT dc.* FROM device_configs dc
            INNER JOIN (
                SELECT ip, MAX(fetched_at) as max_fetched
                FROM device_configs GROUP BY ip
            ) latest ON dc.ip = latest.ip AND dc.fetched_at = latest.max_fetched
        """).fetchall()
        return {row["ip"]: dict(row) for row in rows}


def get_latest_config_hash(ip: str) -> Optional[str]:
    """Get the hash of the most recent config for a device."""
    with get_db() as db:
        row = db.execute(
            "SELECT config_hash FROM device_configs WHERE ip = ? ORDER BY fetched_at DESC LIMIT 1",
            (ip,)
        ).fetchone()
        return row["config_hash"] if row else None


def cleanup_old_device_configs(max_per_device: int = 50):
    """Keep only the most recent N config snapshots per device."""
    with get_db() as db:
        ips = db.execute("SELECT DISTINCT ip FROM device_configs").fetchall()
        for row in ips:
            ip = row["ip"]
            db.execute("""
                DELETE FROM device_configs WHERE ip = ? AND id NOT IN (
                    SELECT id FROM device_configs WHERE ip = ?
                    ORDER BY fetched_at DESC LIMIT ?
                )
            """, (ip, ip, max_per_device))


# Config Template operations

def save_config_template(name: str, category: str, config_fragment: str,
                         form_data: str = None, description: str = None) -> int:
    """Create a new config template. Returns the template ID."""
    with get_db() as db:
        cursor = db.execute(
            """INSERT INTO config_templates (name, category, config_fragment, form_data, description)
               VALUES (?, ?, ?, ?, ?)""",
            (name, category, config_fragment, form_data, description)
        )
        return cursor.lastrowid


def get_config_templates(enabled_only: bool = False) -> list[dict]:
    """Get all config templates."""
    with get_db() as db:
        query = "SELECT * FROM config_templates"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY category, name"
        rows = db.execute(query).fetchall()
        return [dict(row) for row in rows]


def get_config_template(template_id: int) -> Optional[dict]:
    """Get a config template by ID."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM config_templates WHERE id = ?", (template_id,)
        ).fetchone()
        return dict(row) if row else None


def get_config_template_by_category(category: str) -> Optional[dict]:
    """Get a config template by category (returns first match)."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM config_templates WHERE category = ? ORDER BY id LIMIT 1",
            (category,)
        ).fetchone()
        return dict(row) if row else None


def update_config_template(template_id: int, **kwargs):
    """Update a config template."""
    allowed = {"name", "category", "config_fragment", "form_data", "description", "enabled"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    with get_db() as db:
        db.execute(
            f"UPDATE config_templates SET {set_clause} WHERE id = ?",
            (*updates.values(), template_id)
        )


def delete_config_template(template_id: int):
    """Delete a config template."""
    with get_db() as db:
        db.execute("DELETE FROM config_templates WHERE id = ?", (template_id,))


# ---------------------------------------------------------------------------
# User management (RBAC)
# ---------------------------------------------------------------------------

VALID_ROLES = ("admin", "operator", "viewer")


def create_user(username: str, password_hash: Optional[str], role: str = "viewer",
                auth_method: str = "local") -> int:
    """Create a user. Returns the new user ID."""
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role: {role}")
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO users (username, password_hash, role, auth_method) VALUES (?, ?, ?, ?)",
            (username, password_hash, role, auth_method),
        )
        return cursor.lastrowid


def get_user(username: str) -> Optional[dict]:
    """Get a user by username (case-insensitive). Returns dict or None."""
    with get_db() as db:
        row = db.execute(
            "SELECT id, username, password_hash, role, auth_method, enabled, "
            "created_at, updated_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    """Get a user by ID. Returns dict or None."""
    with get_db() as db:
        row = db.execute(
            "SELECT id, username, password_hash, role, auth_method, enabled, "
            "created_at, updated_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    """List all users (without password hashes)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, username, role, auth_method, enabled, created_at, updated_at "
            "FROM users ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]


def update_user(user_id: int, **kwargs) -> bool:
    """Update a user. Returns True if found and updated."""
    allowed = {"role", "password_hash", "enabled"}
    updates = {}
    for key, value in kwargs.items():
        if key not in allowed:
            continue
        if key == "role" and value not in VALID_ROLES:
            raise ValueError(f"Invalid role: {value}")
        if key == "enabled":
            value = 1 if value else 0
        updates[key] = value
    if not updates:
        return False
    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]
    with get_db() as db:
        cursor = db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        return cursor.rowcount > 0


def delete_user(user_id: int) -> bool:
    """Delete a user. Returns True if found and deleted."""
    with get_db() as db:
        cursor = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0


def count_admin_users() -> int:
    """Count enabled admin users."""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND enabled = 1"
        ).fetchone()
        return row[0]


def delete_sessions_for_user(username: str):
    """Delete all sessions for a given username."""
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE username = ?", (username,))


# Initialize on import
init_db()
