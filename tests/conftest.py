"""Shared test fixtures."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

# Set test env vars before any app imports
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "testpass123"


@pytest.fixture
def memory_db():
    """In-memory SQLite database with full schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tower_sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            location TEXT,
            latitude REAL,
            longitude REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE access_points (
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
        CREATE TABLE switches (
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
        CREATE TABLE cpe_cache (
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
            auth_status TEXT DEFAULT NULL,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ap_ip, ip)
        );
        CREATE INDEX idx_cpe_ap ON cpe_cache(ap_ip);
        CREATE TABLE rollouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firmware_file TEXT NOT NULL,
            firmware_file_303l TEXT,
            firmware_file_tns100 TEXT DEFAULT NULL,
            target_version TEXT,
            phase TEXT NOT NULL DEFAULT 'canary',
            status TEXT NOT NULL DEFAULT 'active',
            pause_reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_phase_completed_at TEXT,
            last_job_id TEXT
        );
        CREATE TABLE firmware_registry (
            filename TEXT PRIMARY KEY,
            added_at TEXT NOT NULL,
            source TEXT DEFAULT 'manual'
        );
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            ip_address TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE job_history (
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
        CREATE TABLE schedule_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            event TEXT NOT NULL,
            details TEXT,
            job_id TEXT
        );
        CREATE TABLE rollout_devices (
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
        CREATE TABLE device_durations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            ip TEXT NOT NULL,
            role TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            bank_mode TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE device_update_history (
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
        CREATE INDEX idx_device_history_ip ON device_update_history(ip);
        CREATE INDEX idx_device_history_job ON device_update_history(job_id);
        CREATE INDEX idx_device_history_action ON device_update_history(action);
    """)
    # Insert default settings
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
        "setup_completed": "true",
    }
    for key, value in defaults.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(memory_db):
    """Monkeypatch database.get_db to use in-memory DB."""
    @contextmanager
    def _get_db():
        try:
            yield memory_db
            memory_db.commit()
        except Exception:
            memory_db.rollback()
            raise

    with patch("updater.database.get_db", _get_db):
        yield memory_db


@pytest.fixture
def client(mock_db):
    """FastAPI TestClient with mocked DB and poller."""
    mock_poller = MagicMock()
    mock_poller.start = AsyncMock()
    mock_poller.stop = AsyncMock()
    mock_poller.get_topology = MagicMock(return_value={
        "sites": [], "total_aps": 0, "total_cpes": 0,
        "overall_health": {"green": 0, "yellow": 0, "red": 0},
    })
    mock_poller.poll_ap_now = AsyncMock(return_value=True)

    with patch("updater.app.init_poller", return_value=mock_poller), \
         patch("updater.app.get_poller", return_value=mock_poller), \
         patch("updater.database.cleanup_expired_sessions"):
        from updater.app import app
        with TestClient(app) as tc:
            yield tc


@pytest.fixture
def authed_client(client, mock_db):
    """TestClient with a valid session cookie."""
    from updater import database as db
    session_id = "test-session-id-1234"
    expires = (datetime.now() + timedelta(hours=24)).isoformat()
    mock_db.execute(
        "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, "admin", "127.0.0.1", expires)
    )
    mock_db.commit()
    client.cookies.set("session_id", session_id)
    return client
