"""Dev mode: seed database with dummy data and provide a no-op poller.

Activated by setting SIXTYOPS_DEV_MODE=1.  Never imported in production.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from . import database as db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seed data constants
# ---------------------------------------------------------------------------

_SITES = [
    {"name": "Hilltop Tower", "location": "Rural hilltop site", "latitude": 38.9072, "longitude": -77.0369},
    {"name": "Downtown Rooftop", "location": "City center rooftop", "latitude": 38.8951, "longitude": -77.0364},
    {"name": "Valley Ridge", "location": "Valley overlook", "latitude": 38.8816, "longitude": -77.0910},
]

_APS = [
    # (ip, username, password, site_index, system_name, model, mac, firmware, bank1, bank2, active_bank, last_error)
    ("10.0.1.1", "root", "tachyon123", 0, "HT-AP1", "TNA-301", "00:1A:2B:01:01:01", "1.12.3.7782", "1.12.3.7782", "1.12.2.54970", 1, None),
    ("10.0.1.2", "root", "tachyon123", 0, "HT-AP2", "TNA-302", "00:1A:2B:01:01:02", "1.12.2.54970", "1.12.2.54970", "1.12.1.43000", 1, None),
    ("10.0.2.1", "root", "tachyon123", 1, "DT-AP1", "TNA-301", "00:1A:2B:02:01:01", "1.12.3.7782", "1.12.3.7782", "1.12.2.54970", 1, None),
    ("10.0.2.2", "root", "tachyon123", 1, "DT-AP2", "TNA-302", "00:1A:2B:02:01:02", "1.12.3.7782", "1.12.3.7782", "1.12.2.54970", 1, None),
    ("10.0.3.1", "root", "tachyon123", 2, "VR-AP1", "TNA-301", "00:1A:2B:03:01:01", "1.12.2.54970", "1.12.2.54970", "1.12.1.43000", 1, None),
    ("10.0.3.2", "root", "tachyon123", 2, "VR-AP2", "TNA-302", "00:1A:2B:03:01:02", "1.12.2.54970", "1.12.2.54970", "1.12.1.43000", 1, "Timeout connecting to device"),
]

_SWITCHES = [
    ("10.1.1.1", "admin", "switchpass", 0, "HT-SW1", "TNS-100", "00:1A:2B:04:01:01", "2.0.1.100", "2.0.1.100", "2.0.0.90", 1),
    ("10.1.2.1", "admin", "switchpass", 1, "DT-SW1", "TNS-100", "00:1A:2B:04:02:01", "2.0.1.100", "2.0.1.100", "2.0.0.90", 1),
]

# (ap_ip, ip, mac, system_name, model, firmware, link_distance, rx_power, combined_signal, last_local_rssi, tx_rate, rx_rate, mcs, link_uptime, signal_health, auth_status)
_CPES = [
    ("10.0.1.1", "10.0.1.10", "00:1A:2B:C1:01:10", "Smith-Farm", "TNA-303L-65", "1.12.3.7782", 2500.0, -58.0, -56.0, -55.0, 180.0, 160.0, 9, 864000, "green", "ok"),
    ("10.0.1.1", "10.0.1.11", "00:1A:2B:C1:01:11", "Johnson-Barn", "TNA-303L-65", "1.12.2.54970", 4200.0, -65.0, -63.0, -62.0, 130.0, 110.0, 7, 432000, "green", "ok"),
    ("10.0.1.1", "10.0.1.12", "00:1A:2B:C1:01:12", "Wilson-House", "TNA-301", "1.12.2.54970", 6800.0, -74.0, -72.0, -71.0, 65.0, 50.0, 4, 172800, "yellow", "ok"),
    ("10.0.1.2", "10.0.1.20", "00:1A:2B:C1:02:20", "Davis-Office", "TNA-303L-65", "1.12.3.7782", 1800.0, -55.0, -53.0, -52.0, 200.0, 180.0, 10, 950400, "green", "ok"),
    ("10.0.1.2", "10.0.1.21", "00:1A:2B:C1:02:21", "Brown-Shop", "TNA-303L-65", "1.12.2.54970", 5100.0, -69.0, -67.0, -66.0, 100.0, 85.0, 6, 259200, "green", "ok"),
    ("10.0.1.2", "10.0.1.22", "00:1A:2B:C1:02:22", "Miller-Ranch", "TNA-301", "1.12.1.43000", 8200.0, -79.0, -77.0, -76.0, 40.0, 30.0, 2, 86400, "red", "failed"),
    ("10.0.2.1", "10.0.2.10", "00:1A:2B:C2:01:10", "Apt-201", "TNA-303L-65", "1.12.3.7782", 500.0, -45.0, -43.0, -42.0, 260.0, 240.0, 11, 1209600, "green", "ok"),
    ("10.0.2.1", "10.0.2.11", "00:1A:2B:C2:01:11", "Office-3B", "TNA-303L-65", "1.12.3.7782", 800.0, -52.0, -50.0, -49.0, 220.0, 200.0, 10, 604800, "green", "ok"),
    ("10.0.2.1", "10.0.2.12", "00:1A:2B:C2:01:12", "Cafe-Main", "TNA-301", "1.12.2.54970", 1200.0, -60.0, -58.0, -57.0, 160.0, 140.0, 8, 345600, "green", "ok"),
    ("10.0.2.2", "10.0.2.20", "00:1A:2B:C2:02:20", "Studio-5A", "TNA-303L-65", "1.12.3.7782", 600.0, -48.0, -46.0, -45.0, 240.0, 220.0, 11, 518400, "green", "ok"),
    ("10.0.2.2", "10.0.2.21", "00:1A:2B:C2:02:21", "Library-2F", "TNA-303L-65", "1.12.2.54970", 950.0, -56.0, -54.0, -53.0, 190.0, 170.0, 9, 691200, "green", "ok"),
    ("10.0.2.2", "10.0.2.22", "00:1A:2B:C2:02:22", "Garage-West", "TNA-301", "1.12.2.54970", 1500.0, -63.0, -61.0, -60.0, 140.0, 120.0, 7, 259200, "green", "unreachable"),
    ("10.0.3.1", "10.0.3.10", "00:1A:2B:C3:01:10", "Ridge-Cabin", "TNA-303L-65", "1.12.2.54970", 3200.0, -62.0, -60.0, -59.0, 150.0, 130.0, 8, 777600, "green", "ok"),
    ("10.0.3.1", "10.0.3.11", "00:1A:2B:C3:01:11", "Lookout-Post", "TNA-303L-65", "1.12.2.54970", 5500.0, -70.0, -68.0, -67.0, 90.0, 75.0, 5, 432000, "yellow", "ok"),
    ("10.0.3.1", "10.0.3.12", "00:1A:2B:C3:01:12", "Valley-Store", "TNA-301", "1.12.1.43000", 7200.0, -76.0, -74.0, -73.0, 55.0, 40.0, 3, 172800, "yellow", "ok"),
    ("10.0.3.2", "10.0.3.20", "00:1A:2B:C3:02:20", "Hilltop-Inn", "TNA-303L-65", "1.12.2.54970", 2800.0, -60.0, -58.0, -57.0, 160.0, 140.0, 8, 604800, "green", "ok"),
    ("10.0.3.2", "10.0.3.21", "00:1A:2B:C3:02:21", "Trail-Office", "TNA-303L-65", "1.12.1.43000", 6000.0, -72.0, -70.0, -69.0, 75.0, 60.0, 4, 259200, "yellow", "ok"),
    ("10.0.3.2", "10.0.3.22", "00:1A:2B:C3:02:22", "Creek-House", "TNA-301", "1.12.1.43000", 7800.0, -78.0, -76.0, -75.0, 45.0, 35.0, 2, 86400, "red", "ok"),
]


def _now_str(offset_hours: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def seed_database() -> None:
    """Populate the database with realistic dummy data.

    Skips seeding if data already exists (check access_points count).
    Delete data/sixtyops.db to re-seed.
    """
    with db.get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        if count > 0:
            logger.info("Dev mode: database already seeded, skipping")
            return

    logger.info("Dev mode: seeding database with dummy data...")

    # Ensure setup_completed is true so we skip the setup wizard
    db.set_setting("setup_completed", "true")

    # --- Tower sites ---
    site_ids = []
    for s in _SITES:
        sid = db.create_tower_site(s["name"], s["location"], s["latitude"], s["longitude"])
        site_ids.append(sid)

    # --- Access points ---
    now = _now_str()
    for ap in _APS:
        ip, username, password, site_idx, system_name, model, mac, fw, b1, b2, ab, last_err = ap
        db.upsert_access_point(
            ip, username, password,
            tower_site_id=site_ids[site_idx],
            system_name=system_name, model=model, mac=mac,
            firmware_version=fw,
        )
        db.update_ap_status(
            ip, last_seen=now, last_error=last_err,
            bank1_version=b1, bank2_version=b2, active_bank=ab,
        )

    # --- Switches ---
    for sw in _SWITCHES:
        ip, username, password, site_idx, system_name, model, mac, fw, b1, b2, ab = sw
        db.upsert_switch(
            ip, username, password,
            tower_site_id=site_ids[site_idx],
            system_name=system_name, model=model, mac=mac,
            firmware_version=fw,
        )
        db.update_switch_status(
            ip, last_seen=now,
            bank1_version=b1, bank2_version=b2, active_bank=ab,
        )

    # --- CPEs ---
    for cpe in _CPES:
        ap_ip, ip, mac, sys_name, model, fw, dist, rx, combined, local_rssi, tx_rate, rx_rate, mcs, uptime, health, auth = cpe
        db.upsert_cpe(ap_ip, {
            "ip": ip, "mac": mac, "system_name": sys_name, "model": model,
            "firmware_version": fw, "link_distance": dist, "rx_power": rx,
            "combined_signal": combined, "last_local_rssi": local_rssi,
            "tx_rate": tx_rate, "rx_rate": rx_rate, "mcs": mcs,
            "link_uptime": uptime, "signal_health": health, "auth_status": auth,
        })

    # --- Firmware registry ---
    for fname, source in [
        ("tna-30x-1.12.3-r7782.bin", "freshdesk"),
        ("tna-30x-1.12.2-r54970.bin", "manual"),
        ("tna-303l-1.12.2-r7713.bin", "manual"),
    ]:
        db.register_firmware(fname, source)

    # --- Completed rollout ---
    _seed_completed_rollout()

    # --- Active rollout in canary phase ---
    _seed_active_rollout()

    # --- Job history ---
    _seed_job_history()

    # --- Device configs ---
    _seed_device_configs()

    # --- RADIUS users ---
    _seed_radius_users()

    # --- Config templates ---
    _seed_config_templates()

    logger.info("Dev mode: seeding complete")


def _seed_completed_rollout() -> None:
    rollout_id = db.create_rollout("tna-30x-1.12.3-r7782.bin", "tna-303l-1.12.2-r7713.bin")
    db.set_rollout_target_versions(rollout_id, {
        "tna-30x": "1.12.3.7782",
        "tna-303l": "1.12.2.7713",
    })

    # Assign devices and mark as updated through all phases
    phase_devices = {
        "canary": [("10.0.2.1", "ap")],
        "pct10": [("10.0.1.1", "ap")],
        "pct50": [("10.0.2.2", "ap"), ("10.0.1.2", "ap")],
        "pct100": [("10.0.3.1", "ap"), ("10.0.3.2", "ap")],
    }
    for phase, devices in phase_devices.items():
        for ip, dtype in devices:
            db.assign_device_to_rollout(rollout_id, ip, dtype, phase)
            db.mark_rollout_device(rollout_id, ip, "updated")

    # Advance through all phases
    for _ in range(3):  # canary -> pct10 -> pct50 -> pct100
        db.advance_rollout_phase(rollout_id)

    # Complete the rollout
    with db.get_db() as conn:
        conn.execute(
            "UPDATE rollouts SET status='completed', updated_at=? WHERE id=?",
            (_now_str(-48), rollout_id),
        )

    # Save device update history for this rollout
    job_id = f"dev-job-{uuid.uuid4().hex[:8]}"
    db.set_rollout_job_id(rollout_id, job_id)
    for ip in ["10.0.2.1", "10.0.1.1", "10.0.2.2", "10.0.1.2", "10.0.3.1"]:
        db.save_device_update_history(
            job_id=job_id, ip=ip, role="ap", pass_number=1,
            status="success", old_version="1.12.2.54970", new_version="1.12.3.7782",
            model="TNA-301", error=None, failed_stage=None,
            stages=[
                {"name": "upload", "duration": 45.2},
                {"name": "install", "duration": 30.1},
                {"name": "reboot", "duration": 60.0},
                {"name": "verify", "duration": 15.5},
            ],
            duration_seconds=150.8,
            started_at=_now_str(-50), completed_at=_now_str(-49),
        )

    # One failure
    db.save_device_update_history(
        job_id=job_id, ip="10.0.3.2", role="ap", pass_number=1,
        status="failed", old_version="1.12.2.54970", new_version="1.12.3.7782",
        model="TNA-302", error="Device unreachable after reboot", failed_stage="verify",
        stages=[
            {"name": "upload", "duration": 48.0},
            {"name": "install", "duration": 32.0},
            {"name": "reboot", "duration": 120.0},
            {"name": "verify", "duration": 0, "error": "timeout"},
        ],
        duration_seconds=200.0,
        started_at=_now_str(-50), completed_at=_now_str(-49),
    )


def _seed_active_rollout() -> None:
    rollout_id = db.create_rollout("tna-30x-1.12.3-r7782.bin")
    db.set_rollout_target_versions(rollout_id, {"tna-30x": "1.12.3.7782"})
    # Canary device assigned but pending
    db.assign_device_to_rollout(rollout_id, "10.0.3.1", "ap", "canary")
    db.assign_device_to_rollout(rollout_id, "10.1.1.1", "switch", "canary")


def _seed_job_history() -> None:
    # A couple of past jobs
    job1_id = f"dev-hist-{uuid.uuid4().hex[:8]}"
    db.save_job_history(
        job_id=job1_id,
        started_at=_now_str(-72), completed_at=_now_str(-71),
        duration=3600.0, bank_mode="dual",
        success_count=4, failed_count=1, skipped_count=0, cancelled_count=0,
        devices=json.dumps([
            {"ip": "10.0.1.1", "role": "ap", "status": "success"},
            {"ip": "10.0.2.1", "role": "ap", "status": "success"},
            {"ip": "10.0.2.2", "role": "ap", "status": "success"},
            {"ip": "10.0.3.1", "role": "ap", "status": "success"},
            {"ip": "10.0.3.2", "role": "ap", "status": "failed"},
        ]),
        ap_cpe_map=json.dumps({}),
        device_roles=json.dumps({"10.0.1.1": "ap", "10.0.2.1": "ap", "10.0.2.2": "ap", "10.0.3.1": "ap", "10.0.3.2": "ap"}),
    )

    job2_id = f"dev-hist-{uuid.uuid4().hex[:8]}"
    db.save_job_history(
        job_id=job2_id,
        started_at=_now_str(-168), completed_at=_now_str(-167),
        duration=2400.0, bank_mode="single",
        success_count=6, failed_count=0, skipped_count=0, cancelled_count=0,
        devices=json.dumps([
            {"ip": "10.0.1.1", "role": "ap", "status": "success"},
            {"ip": "10.0.1.2", "role": "ap", "status": "success"},
            {"ip": "10.0.2.1", "role": "ap", "status": "success"},
            {"ip": "10.0.2.2", "role": "ap", "status": "success"},
            {"ip": "10.0.3.1", "role": "ap", "status": "success"},
            {"ip": "10.0.3.2", "role": "ap", "status": "success"},
        ]),
        ap_cpe_map=json.dumps({}),
        device_roles=json.dumps({"10.0.1.1": "ap", "10.0.1.2": "ap", "10.0.2.1": "ap", "10.0.2.2": "ap", "10.0.3.1": "ap", "10.0.3.2": "ap"}),
    )


def _seed_device_configs() -> None:
    configs = {
        "10.0.1.1": {
            "system": {"hostname": "HT-AP1", "timezone": "America/Chicago"},
            "wireless": {"mode": "ap", "channel": 36, "bandwidth": 80, "tx_power": 27},
            "network": {"ip": "10.0.1.1", "netmask": "255.255.255.0", "gateway": "10.0.1.254"},
            "services": {"snmp": {"enabled": True, "community": "public"}, "ntp": {"enabled": True}},
        },
        "10.0.2.1": {
            "system": {"hostname": "DT-AP1", "timezone": "America/New_York"},
            "wireless": {"mode": "ap", "channel": 149, "bandwidth": 40, "tx_power": 24},
            "network": {"ip": "10.0.2.1", "netmask": "255.255.255.0", "gateway": "10.0.2.254"},
            "services": {"snmp": {"enabled": False}, "ntp": {"enabled": True}},
        },
        "10.0.3.1": {
            "system": {"hostname": "VR-AP1", "timezone": "America/Denver"},
            "wireless": {"mode": "ap", "channel": 60, "bandwidth": 80, "tx_power": 30},
            "network": {"ip": "10.0.3.1", "netmask": "255.255.255.0", "gateway": "10.0.3.254"},
            "services": {"snmp": {"enabled": True, "community": "tachyon"}, "ntp": {"enabled": True}},
        },
        "10.0.1.10": {
            "system": {"hostname": "Smith-Farm", "timezone": "America/Chicago"},
            "wireless": {"mode": "cpe", "channel": 36, "bandwidth": 80, "tx_power": 20},
            "network": {"ip": "10.0.1.10", "netmask": "255.255.255.0", "gateway": "10.0.1.254"},
        },
    }
    import hashlib
    for ip, cfg in configs.items():
        cfg_json = json.dumps(cfg, sort_keys=True)
        cfg_hash = hashlib.sha256(cfg_json.encode()).hexdigest()
        model = "TNA-301" if ip.endswith(".1") else "TNA-303L-65"
        db.save_device_config(ip, cfg_json, cfg_hash, model=model, hardware_id=f"hw-{ip.replace('.', '-')}")


def _seed_radius_users() -> None:
    from .radius_users import _hash_password
    with db.get_db() as conn:
        now = _now_str()
        for username, password, enabled in [
            ("subscriber1", "securepass1", 1),
            ("subscriber2", "securepass2", 1),
            ("testuser", "testpass", 0),
        ]:
            hashed = _hash_password(password)
            conn.execute(
                "INSERT OR IGNORE INTO radius_users (username, password, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (username, hashed, enabled, now, now),
            )

        # Auth log entries with distinct timestamps
        log_entries = [
            ("subscriber1", "10.0.1.10", "accept", -48),
            ("subscriber1", "10.0.1.10", "accept", -24),
            ("subscriber2", "10.0.2.10", "accept", -12),
            ("testuser", "10.0.3.10", "reject", -6),
            ("unknown", "10.0.1.22", "reject", -1),
        ]
        for username, client_ip, outcome, hours_ago in log_entries:
            conn.execute(
                "INSERT INTO radius_auth_log (username, client_ip, outcome, occurred_at) VALUES (?, ?, ?, ?)",
                (username, client_ip, outcome, _now_str(hours_ago)),
            )


def _seed_config_templates() -> None:
    templates = [
        ("NTP Standard", "ntp", {"services": {"ntp": {"enabled": True, "server1": "time.google.com", "server2": "time.cloudflare.com"}}}),
        ("SNMP Standard", "snmp", {"services": {"snmp": {"enabled": True, "community_ro": "public"}}}),
        ("Discovery Standard", "discovery", {"services": {"lldp": {"enabled": True}, "cdp": {"enabled": False}}}),
    ]
    for name, category, fragment in templates:
        db.save_config_template(name, category, json.dumps(fragment))


# ---------------------------------------------------------------------------
# DevModePoller — drop-in replacement for NetworkPoller
# ---------------------------------------------------------------------------

class DevModePoller:
    """A no-op poller that builds topology from seeded DB data."""

    def __init__(self, broadcast_func=None):
        self.broadcast_func = broadcast_func
        self._running = False

    async def start(self):
        self._running = True
        logger.info("Dev mode poller started (no network polling)")

    async def stop(self):
        self._running = False
        logger.info("Dev mode poller stopped")

    def get_topology(self) -> dict:
        """Build topology from database — same as NetworkPoller.get_topology()."""
        from .poller import NetworkPoller
        # Create a bare instance and call get_topology which reads entirely from DB
        temp = NetworkPoller.__new__(NetworkPoller)
        return temp.get_topology()

    async def poll_ap_now(self, ip: str) -> bool:
        logger.debug(f"Dev mode: skipping poll for AP {ip}")
        return True

    async def poll_switch_now(self, ip: str) -> bool:
        logger.debug(f"Dev mode: skipping poll for switch {ip}")
        return True

    def invalidate_client(self, ip: str):
        pass

    async def poll_all_configs(self):
        logger.debug("Dev mode: skipping config poll")

    async def poll_configs_for_ips(self, ips: list[str]):
        logger.debug(f"Dev mode: skipping config poll for {ips}")
