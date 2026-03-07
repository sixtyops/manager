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
# Enable all features by default in tests (license gating tests override this)
os.environ["TACHYON_FORCE_PRO"] = "1"


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
            bank1_version TEXT,
            bank2_version TEXT,
            active_bank INTEGER,
            last_firmware_update TEXT,
            notes TEXT,
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
            notes TEXT,
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

        CREATE TABLE device_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            model TEXT,
            hardware_id TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_device_configs_ip ON device_configs(ip);
        CREATE INDEX idx_device_configs_hash ON device_configs(ip, config_hash);

        CREATE TABLE config_templates (
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

        CREATE TABLE radius_users (
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

        CREATE TABLE radius_auth_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            client_ip TEXT,
            client_name TEXT,
            client_model TEXT,
            outcome TEXT NOT NULL,
            reason TEXT,
            occurred_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX idx_radius_auth_unique ON radius_auth_log(occurred_at, username, client_ip, outcome);

        CREATE TABLE radius_client_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_spec TEXT NOT NULL UNIQUE COLLATE NOCASE,
            shortname TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE radius_rollouts (
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
        CREATE TABLE radius_rollout_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rollout_id INTEGER NOT NULL,
            ip TEXT NOT NULL,
            device_type TEXT NOT NULL,
            phase_assigned TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            updated_at TEXT,
            UNIQUE(rollout_id, ip)
        );

        CREATE TABLE device_uptime_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            device_type TEXT NOT NULL,
            event TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            details TEXT
        );
        CREATE INDEX idx_uptime_ip ON device_uptime_events(ip);
        CREATE INDEX idx_uptime_occurred ON device_uptime_events(occurred_at);
        CREATE INDEX idx_uptime_device_type ON device_uptime_events(device_type, occurred_at DESC);

        CREATE TABLE users (
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
        "config_poll_enabled": "true",
        "config_poll_interval_hours": "24",
        "builtin_radius_enabled": "true",
        "builtin_radius_host": "",
        "builtin_radius_port": "1812",
        "builtin_radius_secret": "",
        "builtin_radius_secret_updated_at": "",
        "builtin_radius_secret_review_acknowledged_at": "",
        "builtin_radius_mgmt_password": "",
        "rollout_canary_aps": "",
        "rollout_canary_switches": "",
        # License defaults
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
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    # Seed admin user for RBAC
    conn.execute(
        "INSERT INTO users (username, role, auth_method) VALUES (?, ?, ?)",
        ("admin", "admin", "local"),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(memory_db):
    """Monkeypatch database.get_db to use in-memory DB."""
    import updater.database as db_mod

    @contextmanager
    def _get_db():
        # Invalidate settings cache on every DB access so tests
        # that write settings directly via mock_db.execute() see fresh data
        db_mod._invalidate_settings_cache()
        try:
            yield memory_db
            memory_db.commit()
        except Exception:
            memory_db.rollback()
            raise

    db_mod._invalidate_settings_cache()
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
    mock_fetcher = MagicMock()
    mock_fetcher.start = AsyncMock()
    mock_fetcher.stop = AsyncMock()
    mock_fetcher.reselect = MagicMock()
    mock_fetcher.check_and_download = AsyncMock(return_value={"downloaded": [], "errors": []})
    mock_checker = MagicMock()
    mock_checker.start = AsyncMock()
    mock_checker.stop = AsyncMock()
    mock_checker.get_update_status = MagicMock(return_value={
        "current_version": "test",
        "enabled": False,
        "last_check": "",
        "available_version": "",
        "release_url": "",
        "release_notes": "",
        "update_available": False,
        "docker_socket_available": False,
        "can_update": True,
        "blocked_reason": "",
    })
    mock_checker.check_for_updates = AsyncMock(return_value={
        "current_version": "test",
        "latest_version": "test",
        "update_available": False,
        "release_url": "",
        "release_notes": "",
        "error": None,
    })

    with patch("updater.app.init_poller", return_value=mock_poller), \
         patch("updater.app.get_poller", return_value=mock_poller), \
         patch("updater.app.init_fetcher", return_value=mock_fetcher), \
         patch("updater.app.get_fetcher", return_value=mock_fetcher), \
         patch("updater.app.init_checker", return_value=mock_checker), \
         patch("updater.app.get_checker", return_value=mock_checker), \
         patch("updater.app.verify_update_on_startup", new=AsyncMock()), \
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


@pytest.fixture
def operator_client(client, mock_db):
    """TestClient with an operator role session."""
    mock_db.execute(
        "INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
        ("operator1", "operator", "local"),
    )
    session_id = "test-session-operator"
    expires = (datetime.now() + timedelta(hours=24)).isoformat()
    mock_db.execute(
        "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, "operator1", "127.0.0.1", expires),
    )
    mock_db.commit()
    client.cookies.set("session_id", session_id)
    return client


@pytest.fixture
def viewer_client(client, mock_db):
    """TestClient with a viewer role session."""
    mock_db.execute(
        "INSERT OR IGNORE INTO users (username, role, auth_method) VALUES (?, ?, ?)",
        ("viewer1", "viewer", "local"),
    )
    session_id = "test-session-viewer"
    expires = (datetime.now() + timedelta(hours=24)).isoformat()
    mock_db.execute(
        "INSERT INTO sessions (session_id, username, ip_address, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, "viewer1", "127.0.0.1", expires),
    )
    mock_db.commit()
    client.cookies.set("session_id", session_id)
    return client


@pytest.fixture
def pro_license(mock_db):
    """Set up a PRO license in the test DB and enable it in the license module."""
    import updater.license as lic
    mock_db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("license_status", "active"))
    mock_db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("license_key", "TEST-PRO-KEY"))
    mock_db.commit()
    lic._license_state = None
    # Temporarily disable FORCE_PRO so license state is read from DB
    old_force = lic._FORCE_PRO
    lic._FORCE_PRO = False
    yield
    lic._FORCE_PRO = old_force
    lic._license_state = None


@pytest.fixture
def free_license(mock_db):
    """Ensure free tier with no license key, and disable FORCE_PRO."""
    import updater.license as lic
    lic._license_state = None
    old_force = lic._FORCE_PRO
    lic._FORCE_PRO = False
    yield
    lic._FORCE_PRO = old_force
    lic._license_state = None
