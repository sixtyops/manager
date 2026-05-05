"""Device backup: export and import as CSV with encrypted passwords."""

import base64
import csv
import io
import ipaddress
import logging
import os
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import database as db

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 480_000

# Export includes extra columns as a reference snapshot; import only needs ip/username/password
EXPORT_COLUMNS = [
    "ip", "username", "password", "type", "site_name",
    "system_name", "model", "mac", "firmware_version", "location", "enabled",
]

RADIUS_EXPORT_COLUMNS = ["username", "password", "enabled"]

# Snapshot rows are written under `# section=device_configs`.
# config_json is Fernet-encrypted with the same key as device passwords so
# RADIUS shared secrets / WPA PSKs / SNMP communities never sit in the
# exported file in plaintext. `mac` is the per-unit identifier carried
# across DR so auto-rebind keeps working for restored history.
CONFIG_EXPORT_COLUMNS = [
    "ip", "fetched_at", "config_hash", "model", "hardware_id", "mac",
    "deleted_at", "device_label", "config_json",
]

# Characters that turn a CSV cell into an executable formula in
# Excel/Sheets/LibreOffice when the cell starts with one of them.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: str) -> str:
    """Defang a CSV cell against formula injection.

    Spreadsheets evaluate cells starting with `=`, `+`, `-`, `@`, tab,
    or CR. Prepend a single quote so they're treated as literal text.
    `_unsafe_cell` reverses this on import.
    """
    if value and value[0] in _CSV_INJECTION_PREFIXES:
        return "'" + value
    return value


def _unsafe_cell(value: str) -> str:
    """Reverse `_safe_cell`: strip a leading single quote that we added on export."""
    if value and value.startswith("'") and len(value) > 1 and value[1] in _CSV_INJECTION_PREFIXES:
        return value[1:]
    return value


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a Fernet key from a passphrase using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def build_csv_export(passphrase: str) -> tuple[str, str]:
    """Build a CSV string of all devices. Passwords are Fernet-encrypted.

    Returns (csv_content, salt_b64) — the salt is embedded in a comment header
    so the same passphrase can decrypt on import.
    """
    salt = os.urandom(16)
    key = _derive_key(passphrase, salt)
    fernet = Fernet(key)
    salt_b64 = base64.b64encode(salt).decode()

    sites = {s["id"]: s["name"] for s in db.get_tower_sites()}
    aps = db.get_access_points(enabled_only=False)
    switches = db.get_switches(enabled_only=False)

    buf = io.StringIO()
    # Write salt as a comment header so import can find it
    buf.write(f"# salt={salt_b64}\n")

    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()

    for ap in aps:
        writer.writerow({
            "type": "ap",
            "ip": ap["ip"],
            "username": ap["username"],
            "password": fernet.encrypt(ap["password"].encode()).decode(),
            "site_name": _safe_cell(sites.get(ap.get("tower_site_id"), "")),
            "system_name": _safe_cell(ap.get("system_name") or ""),
            "model": ap.get("model") or "",
            "mac": ap.get("mac") or "",
            "firmware_version": ap.get("firmware_version") or "",
            "location": _safe_cell(ap.get("location") or ""),
            "enabled": "1" if ap.get("enabled", 1) else "0",
        })

    for sw in switches:
        writer.writerow({
            "type": "switch",
            "ip": sw["ip"],
            "username": sw["username"],
            "password": fernet.encrypt(sw["password"].encode()).decode(),
            "site_name": _safe_cell(sites.get(sw.get("tower_site_id"), "")),
            "system_name": _safe_cell(sw.get("system_name") or ""),
            "model": sw.get("model") or "",
            "mac": sw.get("mac") or "",
            "firmware_version": sw.get("firmware_version") or "",
            "location": _safe_cell(sw.get("location") or ""),
            "enabled": "1" if sw.get("enabled", 1) else "0",
        })

    # RADIUS users section
    try:
        from . import radius_users as ru
        all_users = ru.get_radius_users_for_backup()
        if all_users:
            buf.write("# section=radius_users\n")
            radius_writer = csv.DictWriter(buf, fieldnames=RADIUS_EXPORT_COLUMNS)
            radius_writer.writeheader()
            for user in all_users:
                # Export the password (bcrypt hash or legacy) Fernet-wrapped for transport
                radius_writer.writerow({
                    "username": user["username"],
                    "password": fernet.encrypt(user["password"].encode()).decode(),
                    "enabled": "1" if user.get("enabled", 1) else "0",
                })
    except Exception:
        logger.debug("Could not export RADIUS users", exc_info=True)

    # Device config snapshots (live + recycle-bin) section
    try:
        with db.get_db() as conn:
            rows = conn.execute(
                """SELECT ip, config_json, config_hash, model, hardware_id, mac,
                          fetched_at, deleted_at, device_label
                     FROM device_configs
                    ORDER BY ip, fetched_at"""
            ).fetchall()
        if rows:
            buf.write("# section=device_configs\n")
            cfg_writer = csv.DictWriter(buf, fieldnames=CONFIG_EXPORT_COLUMNS)
            cfg_writer.writeheader()
            for r in rows:
                cfg_writer.writerow({
                    "ip": r["ip"],
                    "fetched_at": r["fetched_at"] or "",
                    "config_hash": r["config_hash"] or "",
                    "model": r["model"] or "",
                    "hardware_id": r["hardware_id"] or "",
                    "mac": r["mac"] or "",
                    "deleted_at": r["deleted_at"] or "",
                    "device_label": _safe_cell(r["device_label"] or ""),
                    "config_json": fernet.encrypt((r["config_json"] or "").encode()).decode(),
                })
    except Exception:
        logger.debug("Could not export device_configs", exc_info=True)

    return buf.getvalue(), salt_b64


def process_csv_import(csv_content: str, passphrase: str, conflict_mode: str = "skip") -> dict:
    """Import devices from a CSV. Only ip, username, password are used.

    Devices are inserted as APs initially; the poller auto-classifies them
    (AP vs switch) and discovers site/model/firmware on the next poll cycle.

    conflict_mode: "skip" keeps existing devices, "update" overwrites credentials.
    """
    results = {
        "devices": {"added": 0, "updated": 0, "skipped": 0, "failed": 0, "errors": []},
        "radius_users": {"added": 0, "updated": 0, "skipped": 0, "failed": 0, "errors": []},
        "device_configs": {"added": 0, "skipped": 0, "failed": 0, "errors": []},
    }

    lines = csv_content.splitlines(keepends=True)

    # Extract salt from comment header; split into device, RADIUS, and config sections
    salt_b64 = None
    csv_lines = []
    radius_lines = []
    config_lines = []
    section = "devices"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# salt="):
            salt_b64 = stripped.split("=", 1)[1]
        elif stripped == "# section=radius_users":
            section = "radius_users"
        elif stripped == "# section=device_configs":
            section = "device_configs"
        elif not stripped.startswith("#"):
            if section == "radius_users":
                radius_lines.append(line)
            elif section == "device_configs":
                config_lines.append(line)
            else:
                csv_lines.append(line)

    if not salt_b64:
        raise ValueError("CSV is missing the salt header — this file may not have been exported from this system")

    salt = base64.b64decode(salt_b64)
    key = _derive_key(passphrase, salt)
    fernet = Fernet(key)

    reader = csv.DictReader(io.StringIO("".join(csv_lines)))

    required = {"ip", "username", "password"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        missing = required - set(reader.fieldnames or [])
        raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

    for row in reader:
        ip = row.get("ip", "").strip()
        if not ip:
            continue
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            results["devices"]["failed"] += 1
            results["devices"]["errors"].append(f"{ip}: invalid IP address")
            continue

        # Decrypt password
        try:
            password = fernet.decrypt(row["password"].encode()).decode()
        except InvalidToken:
            results["devices"]["failed"] += 1
            results["devices"]["errors"].append(f"{ip}: wrong passphrase or corrupted password")
            continue
        except Exception as e:
            results["devices"]["failed"] += 1
            results["devices"]["errors"].append(f"{ip}: import error — {e}")
            continue

        username = row.get("username", "").strip()

        try:
            # Check if device already exists (in either table)
            existing_ap = db.get_access_point(ip)
            existing_sw = db.get_switch(ip)
            existing = existing_ap or existing_sw

            if existing and conflict_mode == "skip":
                results["devices"]["skipped"] += 1
                continue

            # Insert as AP; the poller will auto-classify and move to
            # the switches table if it's a TNS model
            if existing_sw:
                db.upsert_switch(ip, username, password)
            else:
                db.upsert_access_point(ip, username, password)

            if existing:
                results["devices"]["updated"] += 1
            else:
                results["devices"]["added"] += 1
        except Exception as e:
            results["devices"]["failed"] += 1
            results["devices"]["errors"].append(f"{ip}: {e}")

    # Import RADIUS users if present
    if radius_lines:
        try:
            from . import radius_users as ru
            radius_reader = csv.DictReader(io.StringIO("".join(radius_lines)))
            for row in radius_reader:
                username = (row.get("username") or "").strip()
                if not username:
                    continue
                try:
                    password = fernet.decrypt(row["password"].encode()).decode()
                except (InvalidToken, Exception) as e:
                    results["radius_users"]["failed"] += 1
                    results["radius_users"]["errors"].append(f"{username}: wrong passphrase or corrupted")
                    continue

                enabled = row.get("enabled", "1") == "1"
                existing = ru.get_radius_user_by_name(username)
                try:
                    if existing and conflict_mode == "skip":
                        results["radius_users"]["skipped"] += 1
                    elif existing:
                        # Password may be a bcrypt hash (from backup) or plaintext (legacy)
                        update_kwargs = {"enabled": enabled}
                        if not ru._is_bcrypt_hash(password):
                            update_kwargs["password"] = password
                        else:
                            # Direct hash import: write bcrypt hash directly
                            with db.get_db() as conn:
                                conn.execute(
                                    "UPDATE radius_users SET password = ?, enabled = ?, updated_at = ? WHERE id = ?",
                                    (password, enabled, datetime.now().isoformat(), existing["id"]),
                                )
                        results["radius_users"]["updated"] += 1
                    else:
                        if ru._is_bcrypt_hash(password):
                            # Direct hash import
                            with db.get_db() as conn:
                                conn.execute(
                                    "INSERT INTO radius_users (username, password, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                                    (username, password, enabled, datetime.now().isoformat(), datetime.now().isoformat()),
                                )
                        else:
                            ru.create_radius_user(username, password)
                            if not enabled:
                                user = ru.get_radius_user_by_name(username)
                                if user:
                                    ru.update_radius_user(user["id"], enabled=False)
                        results["radius_users"]["added"] += 1
                except Exception as e:
                    results["radius_users"]["failed"] += 1
                    results["radius_users"]["errors"].append(f"{username}: {e}")
        except Exception:
            logger.debug("Could not import RADIUS users", exc_info=True)

    # Import device config snapshots if present. Idempotent on (ip, fetched_at):
    # if a row with the same ip+fetched_at already exists we skip rather than
    # duplicate, so re-importing the same backup is safe.
    if config_lines:
        try:
            cfg_reader = csv.DictReader(io.StringIO("".join(config_lines)))
            with db.get_db() as conn:
                existing_keys = {
                    (r["ip"], r["fetched_at"])
                    for r in conn.execute(
                        "SELECT ip, fetched_at FROM device_configs"
                    ).fetchall()
                }
                for row in cfg_reader:
                    ip = (row.get("ip") or "").strip()
                    fetched_at = (row.get("fetched_at") or "").strip()
                    if not ip or not fetched_at:
                        results["device_configs"]["failed"] += 1
                        continue
                    if (ip, fetched_at) in existing_keys:
                        results["device_configs"]["skipped"] += 1
                        continue
                    try:
                        config_json = fernet.decrypt(row["config_json"].encode()).decode()
                    except InvalidToken:
                        results["device_configs"]["failed"] += 1
                        results["device_configs"]["errors"].append(
                            f"{ip}@{fetched_at}: wrong passphrase or corrupted"
                        )
                        continue
                    except Exception as e:
                        results["device_configs"]["failed"] += 1
                        results["device_configs"]["errors"].append(
                            f"{ip}@{fetched_at}: decrypt error — {e}"
                        )
                        continue
                    mac_value = (row.get("mac") or "").strip().upper() or None
                    try:
                        conn.execute(
                            """INSERT INTO device_configs
                                   (ip, config_json, config_hash, model, hardware_id, mac,
                                    fetched_at, deleted_at, device_label)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                ip,
                                config_json,
                                row.get("config_hash") or "",
                                row.get("model") or None,
                                row.get("hardware_id") or None,
                                mac_value,
                                fetched_at,
                                row.get("deleted_at") or None,
                                _unsafe_cell(row.get("device_label") or "") or None,
                            ),
                        )
                        existing_keys.add((ip, fetched_at))
                        results["device_configs"]["added"] += 1
                    except Exception as e:
                        results["device_configs"]["failed"] += 1
                        results["device_configs"]["errors"].append(f"{ip}@{fetched_at}: {e}")
        except Exception:
            logger.debug("Could not import device_configs", exc_info=True)

    return results
