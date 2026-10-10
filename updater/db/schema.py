"""Shared SQLite schema for application startup and test databases.

Call build_schema(connection), then database._migrate(connection) to apply
legacy migrations. The caller owns the connection and seeds default data.
"""

import sqlite3


def build_schema(db: sqlite3.Connection) -> None:
    """Create the base tables, indexes, and triggers without seeding data."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tower_sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            location TEXT,
            latitude REAL,
            longitude REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS devices (
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
        CREATE INDEX IF NOT EXISTS idx_devices_vendor ON devices(vendor);
        CREATE INDEX IF NOT EXISTS idx_devices_role ON devices(role);
        CREATE INDEX IF NOT EXISTS idx_devices_site ON devices(tower_site_id);

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
            -- Radio params for the link-budget UI
            channel INTEGER,
            bandwidth_mhz INTEGER,
            frequency_mhz INTEGER,
            antenna_kit TEXT,
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

        CREATE TABLE IF NOT EXISTS switch_bridge_entries (
            switch_ip TEXT NOT NULL,
            mac TEXT NOT NULL,
            port TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (switch_ip, mac, port)
        );

        CREATE INDEX IF NOT EXISTS idx_switch_bridge_entries_mac
            ON switch_bridge_entries(mac);

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
            last_config_poll_at TEXT,
            last_config_poll_status TEXT,
            last_config_poll_error TEXT,
            -- Link-budget telemetry (signal-health UI)
            target_rssi_dbm REAL,
            snr_db REAL,
            sector_tx INTEGER,
            sector_rx INTEGER,
            antenna_kit TEXT,
            max_rain_mm_hr REAL,
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
            last_job_id TEXT,
            last_phase_window TEXT,
            canary_completed_at TEXT
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

        CREATE INDEX IF NOT EXISTS idx_schedule_log_timestamp ON schedule_log(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_device_durations_job ON device_durations(job_id);
        CREATE INDEX IF NOT EXISTS idx_device_durations_created ON device_durations(created_at);

        CREATE TABLE IF NOT EXISTS firmware_registry (
            filename TEXT PRIMARY KEY,
            added_at TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            sha256 TEXT DEFAULT NULL
        );

        -- One immutable row per distinct firmware file (identified by sha256).
        -- A file name can hold different bytes over time; each set of bytes
        -- gets its own row. verified_at is set only when the vendor checksum
        -- matched or an admin uploaded the file and uploaded_by records who.
        -- Devices do not check firmware signatures, so this row is the guard.
        -- path is the file name relative to the firmware directory.
        CREATE TABLE IF NOT EXISTS firmware_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family TEXT NOT NULL,
            version TEXT,
            sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
            vendor_checksum TEXT,
            vendor_checksum_source TEXT,
            source TEXT NOT NULL CHECK (source IN ('fetched', 'uploaded')),
            uploaded_by TEXT,
            release_date TEXT,
            path TEXT NOT NULL,
            verified_at TEXT,
            CHECK (
                verified_at IS NULL
                OR vendor_checksum IS NOT NULL
                OR (source = 'uploaded' AND uploaded_by IS NOT NULL)
            )
        );

        -- The identity columns of an artifact never change.
        CREATE TRIGGER IF NOT EXISTS firmware_artifacts_immutable
        BEFORE UPDATE OF family, version, sha256, source, path, release_date
        ON firmware_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'firmware_artifacts identity columns are immutable');
        END;

        -- A device is "confirmed working" on a firmware version when it was
        -- updated to that version and passed its post-update smoke tests.
        -- This is the operator's manual canary: confirming one device of a
        -- model family clears that family's Firmware Hold early (see the
        -- scheduler's _confirmed_family_signal). Live health (on-version, no
        -- last_error, seen recently) is re-checked at read time, so a stale
        -- row for an old version is simply ignored.
        CREATE TABLE IF NOT EXISTS firmware_confirmations (
            ip TEXT NOT NULL,
            version TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            PRIMARY KEY (ip, version)
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
            mac TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            deleted_at TEXT DEFAULT NULL,
            device_label TEXT DEFAULT NULL,
            kind TEXT NOT NULL DEFAULT 'poll'
                CHECK (kind IN ('poll', 'pre_push', 'post_push'))
        );
        CREATE INDEX IF NOT EXISTS idx_device_configs_ip ON device_configs(ip);
        CREATE INDEX IF NOT EXISTS idx_device_configs_hash ON device_configs(ip, config_hash);
        CREATE INDEX IF NOT EXISTS idx_device_configs_fetched ON device_configs(ip, fetched_at DESC);
        -- idx_device_configs_deleted, idx_device_configs_mac are created in
        -- _migrate after the columns are guaranteed to exist on legacy DBs.

        CREATE TABLE IF NOT EXISTS config_templates (
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

        CREATE TABLE IF NOT EXISTS config_enforce_log (
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
            service_username TEXT,
            target_ips_json TEXT
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
        CREATE TABLE IF NOT EXISTS config_push_rollouts (
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
        CREATE TABLE IF NOT EXISTS config_push_rollout_devices (
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

        CREATE TABLE IF NOT EXISTS device_uptime_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            device_type TEXT NOT NULL DEFAULT 'ap',
            event TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            details TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_uptime_ip ON device_uptime_events(ip);
        CREATE INDEX IF NOT EXISTS idx_uptime_occurred ON device_uptime_events(occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_uptime_ip_occurred ON device_uptime_events(ip, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_uptime_device_type ON device_uptime_events(device_type, occurred_at DESC);

        CREATE TABLE IF NOT EXISTS active_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT,
            device_ips_json TEXT,
            firmware_name TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

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

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            details TEXT,
            ip_address TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(username, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);

        CREATE TABLE IF NOT EXISTS api_tokens (
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
        CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);
        CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);

        CREATE TABLE IF NOT EXISTS freeze_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            reason TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS device_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            filter_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Sync triggers: legacy tables → devices (bidirectional sync during transition)
        CREATE TRIGGER IF NOT EXISTS trg_ap_to_devices_insert AFTER INSERT ON access_points
        BEGIN
            INSERT OR REPLACE INTO devices (ip, vendor, role, tower_site_id, username, password,
                system_name, model, mac, firmware_version, location, last_seen, last_error,
                enabled, bank1_version, bank2_version, active_bank, last_firmware_update, notes, created_at)
            VALUES (NEW.ip, 'tachyon', 'ap', NEW.tower_site_id, NEW.username, NEW.password,
                NEW.system_name, NEW.model, NEW.mac, NEW.firmware_version, NEW.location,
                NEW.last_seen, NEW.last_error, NEW.enabled, NEW.bank1_version, NEW.bank2_version,
                NEW.active_bank, NEW.last_firmware_update, NEW.notes, NEW.created_at);
        END;
        CREATE TRIGGER IF NOT EXISTS trg_ap_to_devices_update AFTER UPDATE ON access_points
        BEGIN
            UPDATE devices SET tower_site_id=NEW.tower_site_id, username=NEW.username,
                password=NEW.password, system_name=NEW.system_name, model=NEW.model,
                mac=NEW.mac, firmware_version=NEW.firmware_version, location=NEW.location,
                last_seen=NEW.last_seen, last_error=NEW.last_error, enabled=NEW.enabled,
                bank1_version=NEW.bank1_version, bank2_version=NEW.bank2_version,
                active_bank=NEW.active_bank, last_firmware_update=NEW.last_firmware_update, notes=NEW.notes
            WHERE ip = NEW.ip;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_ap_to_devices_delete AFTER DELETE ON access_points
        BEGIN DELETE FROM devices WHERE ip = OLD.ip; END;

        CREATE TRIGGER IF NOT EXISTS trg_sw_to_devices_insert AFTER INSERT ON switches
        BEGIN
            INSERT OR REPLACE INTO devices (ip, vendor, role, tower_site_id, username, password,
                system_name, model, mac, firmware_version, location, last_seen, last_error,
                enabled, bank1_version, bank2_version, active_bank, last_firmware_update, notes, created_at)
            VALUES (NEW.ip, 'tachyon', 'switch', NEW.tower_site_id, NEW.username, NEW.password,
                NEW.system_name, NEW.model, NEW.mac, NEW.firmware_version, NEW.location,
                NEW.last_seen, NEW.last_error, NEW.enabled, NEW.bank1_version, NEW.bank2_version,
                NEW.active_bank, NEW.last_firmware_update, NEW.notes, NEW.created_at);
        END;
        CREATE TRIGGER IF NOT EXISTS trg_sw_to_devices_update AFTER UPDATE ON switches
        BEGIN
            UPDATE devices SET tower_site_id=NEW.tower_site_id, username=NEW.username,
                password=NEW.password, system_name=NEW.system_name, model=NEW.model,
                mac=NEW.mac, firmware_version=NEW.firmware_version, location=NEW.location,
                last_seen=NEW.last_seen, last_error=NEW.last_error, enabled=NEW.enabled,
                bank1_version=NEW.bank1_version, bank2_version=NEW.bank2_version,
                active_bank=NEW.active_bank, last_firmware_update=NEW.last_firmware_update, notes=NEW.notes
            WHERE ip = NEW.ip;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_sw_to_devices_delete AFTER DELETE ON switches
        BEGIN DELETE FROM devices WHERE ip = OLD.ip; END;

        -- Reverse sync: devices → legacy tables (for Tachyon vendor only)
        CREATE TRIGGER IF NOT EXISTS trg_devices_to_legacy_insert AFTER INSERT ON devices
        WHEN NEW.vendor = 'tachyon'
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
        CREATE TRIGGER IF NOT EXISTS trg_devices_to_legacy_update AFTER UPDATE ON devices
        WHEN NEW.vendor = 'tachyon'
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
                active_bank=NEW.active_bank, last_firmware_update=NEW.last_firmware_update
            WHERE ip = NEW.ip AND NEW.role = 'switch';
        END;
        CREATE TRIGGER IF NOT EXISTS trg_devices_to_legacy_delete AFTER DELETE ON devices
        BEGIN
            DELETE FROM access_points WHERE ip = OLD.ip;
            DELETE FROM switches WHERE ip = OLD.ip;
        END;
    """)
