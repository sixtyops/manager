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
            "site_name": sites.get(ap.get("tower_site_id"), ""),
            "system_name": ap.get("system_name") or "",
            "model": ap.get("model") or "",
            "mac": ap.get("mac") or "",
            "firmware_version": ap.get("firmware_version") or "",
            "location": ap.get("location") or "",
            "enabled": "1" if ap.get("enabled", 1) else "0",
        })

    for sw in switches:
        writer.writerow({
            "type": "switch",
            "ip": sw["ip"],
            "username": sw["username"],
            "password": fernet.encrypt(sw["password"].encode()).decode(),
            "site_name": sites.get(sw.get("tower_site_id"), ""),
            "system_name": sw.get("system_name") or "",
            "model": sw.get("model") or "",
            "mac": sw.get("mac") or "",
            "firmware_version": sw.get("firmware_version") or "",
            "location": sw.get("location") or "",
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
    }

    lines = csv_content.splitlines(keepends=True)

    # Extract salt from comment header; split into device and RADIUS sections
    salt_b64 = None
    csv_lines = []
    radius_lines = []
    in_radius_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# salt="):
            salt_b64 = stripped.split("=", 1)[1]
        elif stripped == "# section=radius_users":
            in_radius_section = True
        elif not stripped.startswith("#"):
            if in_radius_section:
                radius_lines.append(line)
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

    return results
