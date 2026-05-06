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
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL UNIQUE,
            vendor TEXT NOT NULL DEFAULT 'tachyon',
            role TEXT NOT NULL DEFAULT 'ap',
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
            last_config_poll_at TEXT,
            last_config_poll_status TEXT,
            last_config_poll_error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (tower_site_id) REFERENCES tower_sites(id)
        );
        CREATE INDEX idx_devices_vendor ON devices(vendor);
        CREATE INDEX idx_devices_role ON devices(role);
        CREATE INDEX idx_devices_site ON devices(tower_site_id);

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
            source TEXT DEFAULT 'manual',
            sha256 TEXT DEFAULT NULL
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
        CREATE INDEX idx_schedule_log_timestamp ON schedule_log(timestamp DESC);
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
        CREATE INDEX idx_device_durations_job ON device_durations(job_id);
        CREATE INDEX idx_device_durations_created ON device_durations(created_at);
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
            mac TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            deleted_at TEXT DEFAULT NULL,
            device_label TEXT DEFAULT NULL
        );
        CREATE INDEX idx_device_configs_ip ON device_configs(ip);
        CREATE INDEX idx_device_configs_hash ON device_configs(ip, config_hash);
        CREATE INDEX idx_device_configs_deleted ON device_configs(deleted_at);
        CREATE INDEX idx_device_configs_mac ON device_configs(mac);

        CREATE TABLE config_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            config_fragment TEXT NOT NULL,
            form_data TEXT,
            description TEXT,
            enabled INTEGER DEFAULT 1,
            scope TEXT DEFAULT 'global',
            site_id INTEGER REFERENCES tower_sites(id),
            device_types TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE config_enforce_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            device_type TEXT,
            phase TEXT,
            status TEXT NOT NULL,
            error TEXT,
            template_ids TEXT,
            enforced_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_config_enforce_ip ON config_enforce_log(ip, enforced_at DESC);

        CREATE TABLE config_push_rollouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_ids TEXT NOT NULL,
            template_names TEXT,
            templates_snapshot TEXT,
            phase TEXT NOT NULL DEFAULT 'canary',
            status TEXT NOT NULL DEFAULT 'active',
            pause_reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_phase_completed_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE config_push_rollout_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rollout_id INTEGER NOT NULL,
            ip TEXT NOT NULL,
            device_type TEXT NOT NULL,
            phase_assigned TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            updated_at TEXT,
            FOREIGN KEY (rollout_id) REFERENCES config_push_rollouts(id),
            UNIQUE(rollout_id, ip)
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
            service_username TEXT,
            target_ips_json TEXT
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

        CREATE TABLE active_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT,
            device_ips_json TEXT,
            firmware_name TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE device_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            filter_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

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

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            details TEXT,
            ip_address TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_audit_log_created ON audit_log(created_at DESC);
        CREATE INDEX idx_audit_log_user ON audit_log(username, created_at DESC);
        CREATE INDEX idx_audit_log_action ON audit_log(action);

        CREATE TABLE api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            scopes TEXT DEFAULT 'read',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT,
            expires_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX idx_api_tokens_hash ON api_tokens(token_hash);
        CREATE INDEX idx_api_tokens_user ON api_tokens(user_id);

        CREATE TABLE freeze_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            reason TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Sync triggers: legacy tables → devices
        CREATE TRIGGER trg_ap_to_devices_insert AFTER INSERT ON access_points
        BEGIN
            INSERT OR REPLACE INTO devices (ip, vendor, role, tower_site_id, username, password,
                system_name, model, mac, firmware_version, location, last_seen, last_error,
                enabled, bank1_version, bank2_version, active_bank, last_firmware_update, notes, created_at)
            VALUES (NEW.ip, 'tachyon', 'ap', NEW.tower_site_id, NEW.username, NEW.password,
                NEW.system_name, NEW.model, NEW.mac, NEW.firmware_version, NEW.location,
                NEW.last_seen, NEW.last_error, NEW.enabled, NEW.bank1_version, NEW.bank2_version,
                NEW.active_bank, NEW.last_firmware_update, NEW.notes, NEW.created_at);
        END;
        CREATE TRIGGER trg_ap_to_devices_update AFTER UPDATE ON access_points
        BEGIN
            UPDATE devices SET tower_site_id=NEW.tower_site_id, username=NEW.username,
                password=NEW.password, system_name=NEW.system_name, model=NEW.model,
                mac=NEW.mac, firmware_version=NEW.firmware_version, location=NEW.location,
                last_seen=NEW.last_seen, last_error=NEW.last_error, enabled=NEW.enabled,
                bank1_version=NEW.bank1_version, bank2_version=NEW.bank2_version,
                active_bank=NEW.active_bank, last_firmware_update=NEW.last_firmware_update, notes=NEW.notes
            WHERE ip = NEW.ip;
        END;
        CREATE TRIGGER trg_ap_to_devices_delete AFTER DELETE ON access_points
        BEGIN DELETE FROM devices WHERE ip = OLD.ip; END;

        CREATE TRIGGER trg_sw_to_devices_insert AFTER INSERT ON switches
        BEGIN
            INSERT OR REPLACE INTO devices (ip, vendor, role, tower_site_id, username, password,
                system_name, model, mac, firmware_version, location, last_seen, last_error,
                enabled, bank1_version, bank2_version, active_bank, last_firmware_update, notes, created_at)
            VALUES (NEW.ip, 'tachyon', 'switch', NEW.tower_site_id, NEW.username, NEW.password,
                NEW.system_name, NEW.model, NEW.mac, NEW.firmware_version, NEW.location,
                NEW.last_seen, NEW.last_error, NEW.enabled, NEW.bank1_version, NEW.bank2_version,
                NEW.active_bank, NEW.last_firmware_update, NEW.notes, NEW.created_at);
        END;
        CREATE TRIGGER trg_sw_to_devices_update AFTER UPDATE ON switches
        BEGIN
            UPDATE devices SET tower_site_id=NEW.tower_site_id, username=NEW.username,
                password=NEW.password, system_name=NEW.system_name, model=NEW.model,
                mac=NEW.mac, firmware_version=NEW.firmware_version, location=NEW.location,
                last_seen=NEW.last_seen, last_error=NEW.last_error, enabled=NEW.enabled,
                bank1_version=NEW.bank1_version, bank2_version=NEW.bank2_version,
                active_bank=NEW.active_bank, last_firmware_update=NEW.last_firmware_update, notes=NEW.notes
            WHERE ip = NEW.ip;
        END;
        CREATE TRIGGER trg_sw_to_devices_delete AFTER DELETE ON switches
        BEGIN DELETE FROM devices WHERE ip = OLD.ip; END;

        -- Reverse sync: devices → legacy tables
        CREATE TRIGGER trg_devices_to_legacy_insert AFTER INSERT ON devices WHEN NEW.vendor = 'tachyon'
        BEGIN
            INSERT OR IGNORE INTO access_points (ip, tower_site_id, username, password,
                system_name, model, mac, firmware_version, location, last_seen, last_error,
                enabled, last_firmware_update, created_at)
            SELECT NEW.ip, NEW.tower_site_id, NEW.username, NEW.password,
                NEW.system_name, NEW.model, NEW.mac, NEW.firmware_version, NEW.location,
                NEW.last_seen, NEW.last_error, NEW.enabled, NEW.last_firmware_update, NEW.created_at
            WHERE NEW.role = 'ap';
            INSERT OR IGNORE INTO switches (ip, tower_site_id, username, password,
                system_name, model, mac, firmware_version, location, last_seen, last_error,
                enabled, bank1_version, bank2_version, active_bank, last_firmware_update, created_at)
            SELECT NEW.ip, NEW.tower_site_id, NEW.username, NEW.password,
                NEW.system_name, NEW.model, NEW.mac, NEW.firmware_version, NEW.location,
                NEW.last_seen, NEW.last_error, NEW.enabled, NEW.bank1_version, NEW.bank2_version,
                NEW.active_bank, NEW.last_firmware_update, NEW.created_at
            WHERE NEW.role = 'switch';
        END;
        CREATE TRIGGER trg_devices_to_legacy_update AFTER UPDATE ON devices WHEN NEW.vendor = 'tachyon'
        BEGIN
            UPDATE access_points SET tower_site_id=NEW.tower_site_id, username=NEW.username,
                password=NEW.password, system_name=NEW.system_name, model=NEW.model,
                mac=NEW.mac, firmware_version=NEW.firmware_version, location=NEW.location,
                last_seen=NEW.last_seen, last_error=NEW.last_error, enabled=NEW.enabled,
                last_firmware_update=NEW.last_firmware_update, notes=NEW.notes
            WHERE ip = NEW.ip AND NEW.role = 'ap';
            UPDATE switches SET tower_site_id=NEW.tower_site_id, username=NEW.username,
                password=NEW.password, system_name=NEW.system_name, model=NEW.model,
                mac=NEW.mac, firmware_version=NEW.firmware_version, location=NEW.location,
                last_seen=NEW.last_seen, last_error=NEW.last_error, enabled=NEW.enabled,
                bank1_version=NEW.bank1_version, bank2_version=NEW.bank2_version,
                active_bank=NEW.active_bank, last_firmware_update=NEW.last_firmware_update, notes=NEW.notes
            WHERE ip = NEW.ip AND NEW.role = 'switch';
        END;
        CREATE TRIGGER trg_devices_to_legacy_delete AFTER DELETE ON devices
        BEGIN
            DELETE FROM access_points WHERE ip = OLD.ip;
            DELETE FROM switches WHERE ip = OLD.ip;
        END;
    """)
    # Insert default settings
    defaults = {
        "schedule_enabled": "true",
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
        "notification_consecutive_failures": "0",
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
        # Poller concurrency
        "poller_concurrency": "10",
        # Device alerts
        "alert_device_offline_enabled": "true",
        "alert_device_offline_cooldown_minutes": "60",
        # Generic webhooks
        "webhook_enabled": "false",
        "webhook_url": "",
        "webhook_method": "POST",
        "webhook_headers": "{}",
        "webhook_secret": "",
        "webhook_events": "job_completed,job_failed,device_offline,device_recovered",
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

    mock_scheduler = MagicMock()
    mock_scheduler.start = AsyncMock()
    mock_scheduler.stop = AsyncMock()
    mock_radius_svc = MagicMock()
    mock_radius_svc.run_forever = AsyncMock()
    # FreeRADIUS runtime removed — pyrad runs in-process

    async def _noop_supervised_task(name, coro_func, *args, **kwargs):
        """No-op replacement so supervised background loops don't spin."""
        pass

    with patch("updater.app.init_poller", return_value=mock_poller), \
         patch("updater.app.get_poller", return_value=mock_poller), \
         patch("updater.app.init_fetcher", return_value=mock_fetcher), \
         patch("updater.app.get_fetcher", return_value=mock_fetcher), \
         patch("updater.app.init_checker", return_value=mock_checker), \
         patch("updater.app.get_checker", return_value=mock_checker), \
         patch("updater.app.init_scheduler", return_value=mock_scheduler), \
         patch("updater.app.init_radius_service", return_value=mock_radius_svc), \
         patch("updater.app.ensure_admin_user"), \
         patch("updater.app._recover_crashed_device_jobs"), \
         patch("updater.app._supervised_task", side_effect=_noop_supervised_task), \
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
    """No-op — all features are always unlocked. Kept for backward compat."""
    yield


@pytest.fixture
def free_license(mock_db):
    """No-op — all features are always unlocked. Kept for backward compat."""
    yield
