"""Built-in RADIUS server for device admin authentication."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import struct
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import database as db
from .crypto import decrypt_password, encrypt_password, is_encrypted

logger = logging.getLogger(__name__)

ACCESS_REQUEST = 1
ACCESS_ACCEPT = 2
ACCESS_REJECT = 3

ATTR_USER_NAME = 1
ATTR_USER_PASSWORD = 2
ATTR_REPLY_MESSAGE = 18

SETTING_RADIUS_ENABLED = "builtin_radius_enabled"
SETTING_RADIUS_HOST = "builtin_radius_host"
SETTING_RADIUS_PORT = "builtin_radius_port"
SETTING_RADIUS_SECRET = "builtin_radius_secret"
SETTING_RADIUS_SECRET_UPDATED_AT = "builtin_radius_secret_updated_at"
SETTING_RADIUS_SECRET_REVIEW_ACKNOWLEDGED_AT = "builtin_radius_secret_review_acknowledged_at"
SETTING_RADIUS_MGMT_PASSWORD = "builtin_radius_mgmt_password"

ROTATION_RECOMMEND_AFTER_DAYS = 365

MANAGEMENT_SERVICE_USERNAME = "sixtyops-radius-mgmt"
RESERVED_USERNAMES = {"admin", "root", MANAGEMENT_SERVICE_USERNAME}
ROLLOUT_PHASE_ORDER = ["canary", "pct10", "pct50", "pct100"]

DATA_DIR = Path(__file__).parent.parent / "data"
RADIUS_CONFIG_DIR = DATA_DIR / "radius"
RADIUS_CLIENTS_FILE = RADIUS_CONFIG_DIR / "clients.conf"
RADIUS_USERS_FILE = RADIUS_CONFIG_DIR / "mods-config" / "files" / "authorize"
RADIUS_SERVICE_NAME = "radius"
RADIUS_CONTAINER_NAME = "tachyon-radius"

LOG_OK_RE = re.compile(
    r"Login OK: \[(?P<username>[^\]/]+).*?\(from client (?P<client_name>\S+).*?cli (?P<client_ip>[^\s)]+)\)"
)
LOG_FAIL_RE = re.compile(
    r"Login incorrect.*?: \[(?P<username>[^\]/]+).*?\(from client (?P<client_name>\S+).*?cli (?P<client_ip>[^\s)]+)\)"
)
LOG_INVALID_RE = re.compile(
    r"Invalid user(?: .*?)?: \[(?P<username>[^\]/]+).*?\(from client (?P<client_name>\S+).*?cli (?P<client_ip>[^\s)]+)\)"
)


@dataclass
class BuiltinRadiusConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1812
    secret: str = ""


class RadiusPacketError(ValueError):
    """Raised for malformed RADIUS packets."""


def _coerce_port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 1812
    return port if 1 <= port <= 65535 else 1812


def get_config() -> BuiltinRadiusConfig:
    """Return the current built-in RADIUS server configuration."""
    return BuiltinRadiusConfig(
        enabled=db.get_setting(SETTING_RADIUS_ENABLED, "false").lower() == "true",
        host=(db.get_setting(SETTING_RADIUS_HOST, "") or "").strip(),
        port=_coerce_port(db.get_setting(SETTING_RADIUS_PORT, "1812")),
        secret=db.get_setting(SETTING_RADIUS_SECRET, ""),
    )


def set_config(config: BuiltinRadiusConfig):
    """Persist built-in RADIUS server settings."""
    previous = get_config()
    settings = {
        SETTING_RADIUS_ENABLED: str(config.enabled).lower(),
        SETTING_RADIUS_HOST: (config.host or "").strip(),
        SETTING_RADIUS_PORT: str(_coerce_port(config.port)),
        SETTING_RADIUS_SECRET: config.secret,
    }
    if config.secret != previous.secret:
        if config.secret:
            now = datetime.now().isoformat()
            settings[SETTING_RADIUS_SECRET_UPDATED_AT] = now
            settings[SETTING_RADIUS_SECRET_REVIEW_ACKNOWLEDGED_AT] = now
        else:
            settings[SETTING_RADIUS_SECRET_UPDATED_AT] = ""
            settings[SETTING_RADIUS_SECRET_REVIEW_ACKNOWLEDGED_AT] = ""
    db.set_settings(settings)


def _parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_secret_rotation_summary() -> dict:
    """Return derived metadata for manual secret-rotation reminders."""
    secret = db.get_setting(SETTING_RADIUS_SECRET, "")
    if not secret:
        return {
            "secret_last_rotated_at": None,
            "secret_age_days": None,
            "rotation_recommended": False,
            "rotation_status": "missing",
            "rotation_recommend_after_days": ROTATION_RECOMMEND_AFTER_DAYS,
        }

    updated_at = db.get_setting(SETTING_RADIUS_SECRET_UPDATED_AT, "")
    parsed_updated = _parse_datetime(updated_at)
    if not parsed_updated:
        return {
            "secret_last_rotated_at": None,
            "secret_age_days": None,
            "rotation_recommended": False,
            "rotation_status": "unknown",
            "rotation_recommend_after_days": ROTATION_RECOMMEND_AFTER_DAYS,
        }

    age_days = max(0, (datetime.now(parsed_updated.tzinfo) - parsed_updated).days)
    is_due = age_days >= ROTATION_RECOMMEND_AFTER_DAYS
    return {
        "secret_last_rotated_at": updated_at,
        "secret_age_days": age_days,
        "rotation_recommended": is_due,
        "rotation_status": "due" if is_due else "healthy",
        "rotation_recommend_after_days": ROTATION_RECOMMEND_AFTER_DAYS,
    }


def mark_secret_reviewed() -> dict:
    """Start tracking a legacy secret from today without changing the secret value."""
    if not db.get_setting(SETTING_RADIUS_SECRET, ""):
        raise ValueError("Shared secret is not configured")

    summary = get_secret_rotation_summary()
    if summary["rotation_status"] != "unknown":
        raise ValueError("Secret review mark is only available for legacy untracked secrets")

    now = datetime.now().isoformat()
    db.set_settings({
        SETTING_RADIUS_SECRET_UPDATED_AT: now,
        SETTING_RADIUS_SECRET_REVIEW_ACKNOWLEDGED_AT: now,
    })
    return get_secret_rotation_summary()


def get_management_service_credentials(create_if_missing: bool = True) -> tuple[str, str]:
    """Return the hidden Radius account used for automated device verification."""
    stored = db.get_setting(SETTING_RADIUS_MGMT_PASSWORD, "")
    if stored:
        return MANAGEMENT_SERVICE_USERNAME, _decrypt_user_password(stored)

    if not create_if_missing:
        return MANAGEMENT_SERVICE_USERNAME, ""

    password = secrets.token_urlsafe(24)
    db.set_setting(SETTING_RADIUS_MGMT_PASSWORD, encrypt_password(password))
    return MANAGEMENT_SERVICE_USERNAME, password


def get_active_rollout() -> Optional[dict]:
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM radius_rollouts WHERE status IN ('active', 'paused') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_current_rollout() -> Optional[dict]:
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM radius_rollouts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_rollout(rollout_id: int) -> Optional[dict]:
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM radius_rollouts WHERE id = ?", (rollout_id,)).fetchone()
        return dict(row) if row else None


def create_rollout(config_template_id: int, service_username: str) -> int:
    now = datetime.now().isoformat()
    with db.get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO radius_rollouts (config_template_id, phase, status, created_at, updated_at, service_username)
            VALUES (?, 'canary', 'active', ?, ?, ?)
            """,
            (config_template_id, now, now, service_username),
        )
        return cursor.lastrowid


def update_rollout_status(rollout_id: int, status: str, pause_reason: str = ""):
    now = datetime.now().isoformat()
    completed_at = now if status == "completed" else None
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE radius_rollouts
            SET status = ?, pause_reason = ?, updated_at = ?, completed_at = COALESCE(?, completed_at)
            WHERE id = ?
            """,
            (status, pause_reason or None, now, completed_at, rollout_id),
        )


def complete_rollout_phase(rollout_id: int):
    rollout = get_rollout(rollout_id)
    if not rollout:
        return
    now = datetime.now().isoformat()
    current = rollout["phase"]
    if current in ROLLOUT_PHASE_ORDER:
        idx = ROLLOUT_PHASE_ORDER.index(current)
        if idx + 1 < len(ROLLOUT_PHASE_ORDER):
            next_phase = ROLLOUT_PHASE_ORDER[idx + 1]
            with db.get_db() as conn:
                conn.execute(
                    """
                    UPDATE radius_rollouts
                    SET phase = ?, last_phase_completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_phase, now, now, rollout_id),
                )
            return
    update_rollout_status(rollout_id, "completed")
    with db.get_db() as conn:
        conn.execute(
            "UPDATE radius_rollouts SET last_phase_completed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, rollout_id),
        )


def assign_device_to_rollout(rollout_id: int, ip: str, device_type: str, phase: str):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO radius_rollout_devices (rollout_id, ip, device_type, phase_assigned, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (rollout_id, ip, device_type, phase),
        )


def get_rollout_devices(rollout_id: int, phase: Optional[str] = None) -> list[dict]:
    with db.get_db() as conn:
        if phase:
            rows = conn.execute(
                "SELECT * FROM radius_rollout_devices WHERE rollout_id = ? AND phase_assigned = ? ORDER BY ip",
                (rollout_id, phase),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM radius_rollout_devices WHERE rollout_id = ? ORDER BY ip",
                (rollout_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def mark_rollout_device(rollout_id: int, ip: str, status: str, error: str = ""):
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE radius_rollout_devices
            SET status = ?, error = ?, updated_at = ?
            WHERE rollout_id = ? AND ip = ?
            """,
            (status, error or None, datetime.now().isoformat(), rollout_id, ip),
        )


def get_rollout_progress(rollout_id: int) -> dict:
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM radius_rollout_devices WHERE rollout_id = ? GROUP BY status",
            (rollout_id,),
        ).fetchall()
    result = {"total": 0, "pending": 0, "updated": 0, "failed": 0, "skipped": 0}
    for row in rows:
        status = row["status"] or "pending"
        count = row["count"] or 0
        result["total"] += count
        result[status] = count
    return result


def validate_username(username: str) -> str:
    """Validate a RADIUS username and normalize surrounding whitespace."""
    normalized = (username or "").strip()
    if not normalized:
        raise ValueError("Username is required")
    if normalized.lower() in RESERVED_USERNAMES:
        raise ValueError("Reserved usernames are not allowed")
    if len(normalized) > 64:
        raise ValueError("Username is too long")
    return normalized


def validate_client_spec(client_spec: str) -> str:
    """Validate an IP or CIDR allowed to query the built-in RADIUS server."""
    normalized = (client_spec or "").strip()
    if not normalized:
        raise ValueError("Client IP or CIDR is required")

    try:
        if "/" in normalized:
            return str(ipaddress.ip_network(normalized, strict=False))
        return str(ipaddress.ip_address(normalized))
    except ValueError as exc:
        raise ValueError("Client must be a valid IP address or CIDR") from exc


def list_client_overrides() -> list[dict]:
    """Return manually configured RADIUS clients."""
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, client_spec, shortname, enabled, created_at, updated_at
            FROM radius_client_overrides
            ORDER BY client_spec COLLATE NOCASE
            """
        ).fetchall()
        return [dict(row) for row in rows]


def create_client_override(client_spec: str, shortname: str = "", enabled: bool = True) -> dict:
    """Create a manual RADIUS client override."""
    normalized = validate_client_spec(client_spec)
    shortname = (shortname or "").strip()
    now = datetime.now().isoformat()
    try:
        with db.get_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO radius_client_overrides (client_spec, shortname, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (normalized, shortname, 1 if enabled else 0, now, now),
            )
            override_id = cursor.lastrowid
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise ValueError("Client override already exists") from exc
        raise

    return get_client_override(override_id)


def get_client_override(override_id: int) -> dict:
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT id, client_spec, shortname, enabled, created_at, updated_at
            FROM radius_client_overrides
            WHERE id = ?
            """,
            (override_id,),
        ).fetchone()
        if not row:
            raise ValueError("Client override not found")
        return dict(row)


def update_client_override(override_id: int, client_spec: str, shortname: str = "", enabled: bool = True) -> dict:
    """Update a manual RADIUS client override."""
    normalized = validate_client_spec(client_spec)
    shortname = (shortname or "").strip()
    try:
        with db.get_db() as conn:
            cursor = conn.execute(
                """
                UPDATE radius_client_overrides
                SET client_spec = ?, shortname = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized, shortname, 1 if enabled else 0, datetime.now().isoformat(), override_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("Client override not found")
    except ValueError:
        raise
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise ValueError("Client override already exists") from exc
        raise

    return get_client_override(override_id)


def delete_client_override(override_id: int) -> bool:
    """Delete a manual RADIUS client override."""
    with db.get_db() as conn:
        cursor = conn.execute("DELETE FROM radius_client_overrides WHERE id = ?", (override_id,))
        return cursor.rowcount > 0


def list_users() -> list[dict]:
    """Return all configured RADIUS users without secrets."""
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, username, enabled, created_at, updated_at, last_auth_at
            FROM radius_users
            ORDER BY username COLLATE NOCASE
            """
        ).fetchall()
        return [dict(row) for row in rows]


def list_users_for_backup() -> list[dict]:
    """Return RADIUS users with decrypted passwords for backup export."""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT username, password, enabled FROM radius_users ORDER BY username COLLATE NOCASE"
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "username": row["username"],
                "password": _decrypt_user_password(row["password"]),
                "enabled": row["enabled"],
            })
        return result


def get_user(user_id: int) -> Optional[dict]:
    """Return a single RADIUS user row including the encrypted password."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM radius_users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def create_user(username: str, password: str, enabled: bool = True, *, _skip_length_check: bool = False) -> dict:
    """Create a new RADIUS user."""
    normalized = validate_username(username)
    if not password:
        raise ValueError("Password is required")
    if not _skip_length_check and len(password) < 12:
        raise ValueError("Password must be at least 12 characters")

    encrypted = encrypt_password(password)
    now = datetime.now().isoformat()
    try:
        with db.get_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO radius_users (username, password, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (normalized, encrypted, 1 if enabled else 0, now, now),
            )
            user_id = cursor.lastrowid
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise ValueError("Username already exists") from exc
        raise

    return get_user_summary(user_id)


def update_user(user_id: int, username: str, password: str = "", enabled: bool = True, *, _skip_length_check: bool = False) -> dict:
    """Update an existing RADIUS user."""
    existing = get_user(user_id)
    if not existing:
        raise ValueError("User not found")

    normalized = validate_username(username)
    stored_password = existing["password"]
    if password:
        if not _skip_length_check and len(password) < 12:
            raise ValueError("Password must be at least 12 characters")
        stored_password = encrypt_password(password)

    try:
        with db.get_db() as conn:
            conn.execute(
                """
                UPDATE radius_users
                SET username = ?, password = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized, stored_password, 1 if enabled else 0, datetime.now().isoformat(), user_id),
            )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise ValueError("Username already exists") from exc
        raise

    return get_user_summary(user_id)


def delete_user(user_id: int) -> bool:
    """Delete a RADIUS user. Returns True if removed."""
    with db.get_db() as conn:
        cursor = conn.execute("DELETE FROM radius_users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0


def get_user_summary(user_id: int) -> dict:
    """Return a public user summary by id."""
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT id, username, enabled, created_at, updated_at, last_auth_at
            FROM radius_users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            raise ValueError("User not found")
        return dict(row)


def _get_user_for_auth(username: str) -> Optional[dict]:
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM radius_users
            WHERE username = ? COLLATE NOCASE AND enabled = 1
            """,
            ((username or "").strip(),),
        ).fetchone()
        return dict(row) if row else None


def _resolve_client_info(client_ip: str) -> dict:
    with db.get_db() as conn:
        ap = conn.execute(
            "SELECT system_name, model FROM access_points WHERE ip = ? AND enabled = 1",
            (client_ip,),
        ).fetchone()
        if ap:
            return {
                "allowed": True,
                "name": ap["system_name"] or client_ip,
                "model": ap["model"] or "",
            }

        switch = conn.execute(
            "SELECT system_name, model FROM switches WHERE ip = ? AND enabled = 1",
            (client_ip,),
        ).fetchone()
        if switch:
            return {
                "allowed": True,
                "name": switch["system_name"] or client_ip,
                "model": switch["model"] or "",
            }

        cpe = conn.execute(
            "SELECT system_name, model FROM cpe_cache WHERE ip = ?",
            (client_ip,),
        ).fetchone()
        if cpe:
            return {
                "allowed": True,
                "name": cpe["system_name"] or client_ip,
                "model": cpe["model"] or "",
            }

        override_rows = conn.execute(
            """
            SELECT client_spec, shortname
            FROM radius_client_overrides
            WHERE enabled = 1
            ORDER BY client_spec COLLATE NOCASE
            """
        ).fetchall()

    try:
        parsed_ip = ipaddress.ip_address(client_ip)
    except ValueError:
        parsed_ip = None

    if parsed_ip:
        for row in override_rows:
            spec = row["client_spec"]
            if not spec:
                continue
            try:
                if "/" in spec:
                    if parsed_ip in ipaddress.ip_network(spec, strict=False):
                        return {
                            "allowed": True,
                            "name": row["shortname"] or spec,
                            "model": "",
                        }
                elif parsed_ip == ipaddress.ip_address(spec):
                    return {
                        "allowed": True,
                        "name": row["shortname"] or spec,
                        "model": "",
                    }
            except ValueError:
                continue

    return {"allowed": False, "name": client_ip, "model": ""}


def _known_client_count() -> int:
    return len(_iter_radius_clients())


def _decrypt_user_password(encrypted: str) -> str:
    if encrypted and is_encrypted(encrypted):
        return decrypt_password(encrypted)
    return encrypted


def _mark_auth_success(user_id: int):
    with db.get_db() as conn:
        conn.execute(
            "UPDATE radius_users SET last_auth_at = ?, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), datetime.now().isoformat(), user_id),
        )


def record_auth_attempt(username: str, client_ip: str, outcome: str, reason: str,
                        client_name: str = "", client_model: str = "",
                        occurred_at: Optional[str] = None):
    """Persist a RADIUS auth attempt and keep the log bounded."""
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO radius_auth_log (username, client_ip, client_name, client_model, outcome, reason, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (username or "").strip(),
                client_ip,
                client_name,
                client_model,
                outcome,
                reason,
                occurred_at or datetime.now().isoformat(),
            ),
        )
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        conn.execute("DELETE FROM radius_auth_log WHERE occurred_at < ?", (cutoff,))


def authenticate_request(username: str, password: str, client_ip: str) -> tuple[bool, str, dict]:
    """Authenticate a RADIUS login against the built-in user database."""
    normalized = (username or "").strip()
    client = _resolve_client_info(client_ip)

    if not client["allowed"]:
        return False, "Unknown client device", client

    if normalized.lower() in RESERVED_USERNAMES:
        return False, "Reserved usernames are blocked", client

    user = _get_user_for_auth(normalized)
    if not user:
        return False, "Unknown or disabled user", client

    stored_password = _decrypt_user_password(user["password"])
    if not hmac.compare_digest(password or "", stored_password):
        return False, "Invalid credentials", client

    _mark_auth_success(user["id"])
    return True, "Authenticated", client


def get_stats(limit: int = 10) -> dict:
    """Return server status, aggregate stats, and recent auth attempts."""
    config = get_config()
    runtime = get_runtime()
    state = runtime.get_container_state()
    log_error = _sync_freeradius_logs()
    today = datetime.now().date().isoformat()
    last_24h = (datetime.now() - timedelta(hours=24)).isoformat()

    with db.get_db() as conn:
        admin_accounts = conn.execute(
            "SELECT COUNT(*) FROM radius_users WHERE enabled = 1"
        ).fetchone()[0]
        totals = conn.execute(
            """
            SELECT
                SUM(CASE WHEN outcome = 'accept' THEN 1 ELSE 0 END) AS accepts,
                COUNT(*) AS total
            FROM radius_auth_log
            WHERE occurred_at >= ?
            """,
            (last_24h,),
        ).fetchone()
        recent_rows = conn.execute(
            """
            SELECT username, client_ip, client_name, client_model, outcome, reason, occurred_at
            FROM radius_auth_log
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        logins_today = conn.execute(
            "SELECT COUNT(*) FROM radius_auth_log WHERE substr(occurred_at, 1, 10) = ?",
            (today,),
        ).fetchone()[0]
        active_devices = conn.execute(
            """
            SELECT COUNT(DISTINCT client_ip)
            FROM radius_auth_log
            WHERE occurred_at >= ?
            """,
            (last_24h,),
        ).fetchone()[0]

    total = totals["total"] or 0
    accepts = totals["accepts"] or 0
    success_rate = round((accepts / total) * 100, 1) if total else 100.0

    return {
        "enabled": config.enabled,
        "configured": bool(config.secret),
        "running": state["running"],
        "healthy": runtime.is_healthy(state),
        "container_status": state["status"],
        "health_status": state["health"],
        "port": config.port,
        "secret_set": bool(config.secret),
        "last_error": runtime.last_error or log_error,
        "admin_accounts": admin_accounts,
        "known_clients": _known_client_count(),
        "active_devices_24h": active_devices,
        "auth_success_rate": success_rate,
        "logins_today": logins_today,
        "recent_logins": [dict(row) for row in recent_rows],
        **get_secret_rotation_summary(),
    }


def get_public_config_summary() -> dict:
    """Return a safe summary of the built-in RADIUS server config for the UI."""
    config = get_config()
    runtime = get_runtime()
    state = runtime.get_container_state()
    return {
        "enabled": config.enabled,
        "host": config.host,
        "port": config.port,
        "secret_set": bool(config.secret),
        "configured": bool(config.secret),
        "running": state["running"],
        "healthy": runtime.is_healthy(state),
        "container_status": state["status"],
        "health_status": state["health"],
        "last_error": runtime.last_error,
        **get_secret_rotation_summary(),
    }


def _docker_socket_available() -> bool:
    return not os.environ.get("PYTEST_CURRENT_TEST") and Path("/var/run/docker.sock").exists()


def _get_compose_dir() -> Optional[Path]:
    candidates = [
        Path("/app/repo"),
        Path("/app"),
        Path.cwd(),
        Path(__file__).parent.parent,
    ]
    for path in candidates:
        if (path / "docker-compose.yml").exists():
            return path
    return None


def _get_compose_cmd(compose_dir: Path) -> list[str]:
    cmd = ["docker", "compose", "-f", str(compose_dir / "docker-compose.yml")]
    standalone = compose_dir / "docker-compose.standalone.yml"
    if standalone.exists():
        cmd.extend(["-f", str(standalone)])
    return cmd


def _run_compose(args: list[str]) -> subprocess.CompletedProcess | None:
    if not _docker_socket_available():
        return None

    compose_dir = _get_compose_dir()
    if not compose_dir:
        logger.warning("Cannot manage FreeRADIUS container: docker-compose.yml not found")
        return None

    cmd = _get_compose_cmd(compose_dir) + args
    return subprocess.run(cmd, capture_output=True, text=True, cwd=compose_dir, timeout=30)


def _run_docker(args: list[str]) -> subprocess.CompletedProcess | None:
    if not _docker_socket_available():
        return None
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def _escape_radius_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _iter_radius_clients() -> list[dict]:
    clients = []
    seen = set()

    with db.get_db() as conn:
        override_rows = conn.execute(
            """
            SELECT client_spec, shortname
            FROM radius_client_overrides
            WHERE enabled = 1
            ORDER BY client_spec COLLATE NOCASE
            """
        ).fetchall()

    for row in override_rows:
        spec = row["client_spec"]
        if not spec or spec in seen:
            continue
        seen.add(spec)
        shortname = row["shortname"] or spec
        safe_shortname = re.sub(r"[^A-Za-z0-9_.-]", "-", shortname)[:32]
        clients.append({"ip": spec, "shortname": safe_shortname or "manual-client"})

    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT ip, system_name, model FROM access_points WHERE enabled = 1
            UNION
            SELECT ip, system_name, model FROM switches WHERE enabled = 1
            UNION
            SELECT ip, system_name, model FROM cpe_cache
            """
        ).fetchall()

    for row in rows:
        ip = row["ip"]
        if not ip or ip in seen:
            continue
        seen.add(ip)
        shortname = row["system_name"] or row["model"] or ip
        safe_shortname = re.sub(r"[^A-Za-z0-9_.-]", "-", shortname)[:32]
        clients.append({"ip": ip, "shortname": safe_shortname or ip.replace(".", "_")})
    return clients


def _write_freeradius_files():
    config = get_config()
    clients = _iter_radius_clients()
    users = []
    service_username, service_password = get_management_service_credentials(create_if_missing=bool(config.secret))

    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT username, password, enabled FROM radius_users WHERE enabled = 1 ORDER BY username COLLATE NOCASE"
        ).fetchall()
        users = [dict(row) for row in rows]

    RADIUS_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)

    client_blocks = [
        "client localhost {",
        "    ipaddr = 127.0.0.1",
        '    secret = "local-testing-only"',
        "    shortname = localhost",
        "}",
        "",
    ]
    if config.secret:
        for idx, client in enumerate(clients, start=1):
            client_blocks.extend([
                f"client sixtyops_{idx} {{",
                f"    ipaddr = {client['ip']}",
                f'    secret = "{_escape_radius_value(config.secret)}"',
                f"    shortname = {client['shortname']}",
                "    nastype = other",
                "}",
                "",
            ])

    user_lines = [
        "# Generated by SixtyOps. Do not edit by hand.",
        "",
    ]
    if service_password:
        user_lines.append(f'{service_username} Cleartext-Password := "{_escape_radius_value(service_password)}"')
        user_lines.append("")
    for user in users:
        password = _escape_radius_value(_decrypt_user_password(user["password"]))
        user_lines.append(f'{user["username"]} Cleartext-Password := "{password}"')
        user_lines.append("")

    if len(user_lines) == 2:
        user_lines.append("# No active RADIUS users configured")

    RADIUS_CLIENTS_FILE.write_text("\n".join(client_blocks) + "\n")
    RADIUS_USERS_FILE.write_text("\n".join(user_lines) + "\n")


def _parse_timestamp(value: str) -> str:
    value = value.strip()
    if value.endswith("Z"):
        return value
    if "+" in value[10:] or value.count("-") > 2:
        return value
    return value + "Z"


def _parse_freeradius_logs(hours: int = 24, limit: int = 10) -> dict:
    result = _run_docker(["logs", "--timestamps", "--since", f"{hours}h", RADIUS_CONTAINER_NAME])
    if result is None or result.returncode != 0:
        return {
            "logins_today": 0,
            "recent_logins": [],
            "active_devices_24h": 0,
            "auth_success_rate": 100.0,
            "last_error": (result.stderr.strip() if result and result.stderr else ""),
        }

    entries = []
    seen_devices = set()
    accepts = 0
    total = 0
    today = datetime.now().date().isoformat()

    for raw_line in result.stderr.splitlines() + result.stdout.splitlines():
        if "Login OK:" in raw_line:
            outcome = "accept"
            match = LOG_OK_RE.search(raw_line)
        elif "Login incorrect" in raw_line:
            outcome = "reject"
            match = LOG_FAIL_RE.search(raw_line)
        elif "Invalid user" in raw_line:
            outcome = "reject"
            match = LOG_INVALID_RE.search(raw_line)
        else:
            continue

        if not match:
            continue

        timestamp, _, _ = raw_line.partition(" ")
        client_ip = match.group("client_ip")
        client = _resolve_client_info(client_ip)
        reason = "Authenticated" if outcome == "accept" else "Invalid credentials"

        entry = {
            "username": match.group("username"),
            "client_ip": client_ip,
            "client_name": client["name"] if client["allowed"] else match.group("client_name"),
            "client_model": client["model"],
            "outcome": outcome,
            "reason": reason,
            "occurred_at": _parse_timestamp(timestamp),
        }
        entries.append(entry)
        seen_devices.add(client_ip)
        total += 1
        if outcome == "accept":
            accepts += 1

    entries.sort(key=lambda item: item["occurred_at"], reverse=True)
    success_rate = round((accepts / total) * 100, 1) if total else 100.0
    logins_today = sum(1 for entry in entries if entry["occurred_at"][:10] == today)
    return {
        "logins_today": logins_today,
        "recent_logins": entries[:limit],
        "active_devices_24h": len(seen_devices),
        "auth_success_rate": success_rate,
        "last_error": "",
    }


def _sync_freeradius_logs(hours: int = 24) -> str:
    parsed = _parse_freeradius_logs(hours=hours, limit=500)
    if parsed["last_error"]:
        return parsed["last_error"]

    for entry in parsed["recent_logins"]:
        record_auth_attempt(
            username=entry["username"],
            client_ip=entry["client_ip"],
            outcome=entry["outcome"],
            reason=entry["reason"],
            client_name=entry["client_name"],
            client_model=entry["client_model"],
            occurred_at=entry["occurred_at"],
        )

    return ""


def sync_auth_history(hours: int = 24) -> str:
    """Public entry point for background auth-history ingestion."""
    return _sync_freeradius_logs(hours=hours)


def _parse_packet(data: bytes) -> tuple[int, int, bytes, dict[int, list[bytes]]]:
    if len(data) < 20:
        raise RadiusPacketError("packet too short")

    code, identifier, length = struct.unpack("!BBH", data[:4])
    if length != len(data):
        raise RadiusPacketError("invalid packet length")

    authenticator = data[4:20]
    attrs = {}
    cursor = 20
    while cursor < len(data):
        if cursor + 2 > len(data):
            raise RadiusPacketError("truncated attribute header")
        attr_type = data[cursor]
        attr_len = data[cursor + 1]
        if attr_len < 2 or cursor + attr_len > len(data):
            raise RadiusPacketError("invalid attribute length")
        attrs.setdefault(attr_type, []).append(data[cursor + 2: cursor + attr_len])
        cursor += attr_len

    return code, identifier, authenticator, attrs


def _decode_user_password(value: bytes, secret: str, request_authenticator: bytes) -> str:
    if not value or len(value) % 16 != 0:
        raise RadiusPacketError("invalid encrypted password")

    secret_bytes = secret.encode()
    previous = request_authenticator
    plaintext = bytearray()

    for offset in range(0, len(value), 16):
        block = value[offset: offset + 16]
        digest = hashlib.md5(secret_bytes + previous).digest()
        plaintext.extend(b ^ d for b, d in zip(block, digest))
        previous = block

    return bytes(plaintext).rstrip(b"\x00").decode("utf-8", errors="ignore")


def _build_packet(code: int, identifier: int, request_authenticator: bytes,
                  secret: str, attributes: list[tuple[int, bytes]] | None = None) -> bytes:
    attr_bytes = b""
    for attr_type, value in attributes or []:
        value = value or b""
        attr_bytes += struct.pack("!BB", attr_type, len(value) + 2) + value

    length = 20 + len(attr_bytes)
    header = struct.pack("!BBH", code, identifier, length)
    response_authenticator = hashlib.md5(
        header + request_authenticator + attr_bytes + secret.encode()
    ).digest()
    return header + response_authenticator + attr_bytes


def _get_text_attribute(attrs: dict[int, list[bytes]], attr_type: int) -> str:
    values = attrs.get(attr_type) or []
    if not values:
        return ""
    return values[0].decode("utf-8", errors="ignore").strip()


class BuiltinRadiusProtocol(asyncio.DatagramProtocol):
    """Asyncio UDP protocol for Access-Request handling."""

    def __init__(self, runtime: "BuiltinRadiusRuntime"):
        self.runtime = runtime
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        self.runtime.transport = transport

    def datagram_received(self, data: bytes, addr):
        client_ip = addr[0]
        try:
            response = self.runtime.handle_access_request(data, client_ip)
        except Exception:
            logger.exception("Built-in RADIUS request handling failed")
            return
        if response:
            self.transport.sendto(response, addr)

    def connection_lost(self, exc):
        self.runtime.transport = None


class BuiltinRadiusRuntime:
    """Lifecycle manager for the built-in FreeRADIUS service."""

    def __init__(self):
        self.last_error = ""

    def get_container_state(self) -> dict:
        result = _run_docker([
            "inspect",
            "-f",
            "{{.State.Running}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            RADIUS_CONTAINER_NAME,
        ])
        if not result:
            return {"running": False, "status": "unknown", "health": "unknown"}
        if result.returncode != 0:
            stderr = (result.stderr or "").strip().lower()
            if "no such object" in stderr:
                return {"running": False, "status": "missing", "health": "unknown"}
            return {"running": False, "status": "error", "health": "unknown"}

        parts = (result.stdout or "").strip().split("|", 2)
        if len(parts) != 3:
            return {"running": False, "status": "unknown", "health": "unknown"}

        running_raw, status, health = parts
        return {
            "running": running_raw.strip().lower() == "true",
            "status": status.strip() or "unknown",
            "health": health.strip() or "unknown",
        }

    def is_running(self) -> bool:
        return self.get_container_state()["running"]

    def is_healthy(self, state: Optional[dict] = None) -> bool:
        state = state or self.get_container_state()
        return state["running"] and state["health"] in {"healthy", "none"}

    async def start(self):
        try:
            _write_freeradius_files()
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("Failed to write FreeRADIUS config: %s", exc)
            return

        config = get_config()
        if not config.enabled or not config.secret:
            await self.stop()
            return

        if not _docker_socket_available():
            self.last_error = "Docker socket unavailable; cannot start FreeRADIUS service"
            logger.warning(self.last_error)
            return

        result = await asyncio.to_thread(_run_compose, ["up", "-d", RADIUS_SERVICE_NAME])
        if result and result.returncode != 0:
            self.last_error = result.stderr.strip() or result.stdout.strip() or "Failed to start FreeRADIUS service"
            logger.error("Failed to start FreeRADIUS service: %s", self.last_error)
            return

        self.last_error = ""
        logger.info("Built-in FreeRADIUS service refreshed")

    async def stop(self):
        if not _docker_socket_available():
            return
        result = await asyncio.to_thread(_run_compose, ["stop", RADIUS_SERVICE_NAME])
        if result and result.returncode != 0:
            self.last_error = result.stderr.strip() or result.stdout.strip() or "Failed to stop FreeRADIUS service"
            logger.warning("Failed to stop FreeRADIUS service: %s", self.last_error)

    async def reload(self):
        try:
            _write_freeradius_files()
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("Failed to write FreeRADIUS config: %s", exc)
            return

        config = get_config()
        if not config.enabled or not config.secret:
            await self.stop()
            return

        if not _docker_socket_available():
            self.last_error = "Docker socket unavailable; cannot reload FreeRADIUS service"
            logger.warning(self.last_error)
            return

        if self.is_running():
            result = await asyncio.to_thread(_run_docker, ["kill", "-s", "HUP", RADIUS_CONTAINER_NAME])
            if result and result.returncode == 0:
                self.last_error = ""
                logger.info("Sent HUP to FreeRADIUS container")
                return

            logger.warning("FreeRADIUS HUP failed, falling back to restart")
            restart = await asyncio.to_thread(_run_compose, ["restart", RADIUS_SERVICE_NAME])
            if restart and restart.returncode == 0:
                self.last_error = ""
                return

            self.last_error = (
                (restart.stderr.strip() if restart and restart.stderr else "")
                or (result.stderr.strip() if result and result.stderr else "")
                or "Failed to reload FreeRADIUS service"
            )
            logger.error(self.last_error)
            return

        await self.start()

    async def ensure_healthy(self):
        """Restart the service if Docker reports it unhealthy or stopped."""
        config = get_config()
        if not config.enabled or not config.secret or not _docker_socket_available():
            return

        state = self.get_container_state()
        if not state["running"] or state["status"] in {"exited", "dead", "missing"}:
            logger.warning("FreeRADIUS not running (status=%s); attempting recovery", state["status"])
            await self.start()
            return

        if state["health"] == "unhealthy":
            logger.warning("FreeRADIUS unhealthy; restarting container")
            restart = await asyncio.to_thread(_run_compose, ["restart", RADIUS_SERVICE_NAME])
            if restart and restart.returncode == 0:
                self.last_error = ""
                return
            self.last_error = (
                (restart.stderr.strip() if restart and restart.stderr else "")
                or (restart.stdout.strip() if restart and restart.stdout else "")
                or "Failed to restart unhealthy FreeRADIUS service"
            )
            logger.error(self.last_error)

    def handle_access_request(self, data: bytes, client_ip: str) -> Optional[bytes]:
        config = get_config()
        if not config.enabled or not config.secret:
            return None

        try:
            code, identifier, request_authenticator, attrs = _parse_packet(data)
        except RadiusPacketError as exc:
            logger.warning("Ignoring malformed RADIUS packet from %s: %s", client_ip, exc)
            return None

        if code != ACCESS_REQUEST:
            logger.debug("Ignoring non-Access-Request RADIUS code %s from %s", code, client_ip)
            return None

        username = _get_text_attribute(attrs, ATTR_USER_NAME)
        encrypted_passwords = attrs.get(ATTR_USER_PASSWORD) or []
        if not username or not encrypted_passwords:
            record_auth_attempt(username, client_ip, "reject", "Missing username or password")
            return _build_packet(
                ACCESS_REJECT,
                identifier,
                request_authenticator,
                config.secret,
                [(ATTR_REPLY_MESSAGE, b"Missing username or password")],
            )

        try:
            password = _decode_user_password(encrypted_passwords[0], config.secret, request_authenticator)
        except RadiusPacketError:
            record_auth_attempt(username, client_ip, "reject", "Invalid password payload")
            return _build_packet(
                ACCESS_REJECT,
                identifier,
                request_authenticator,
                config.secret,
                [(ATTR_REPLY_MESSAGE, b"Invalid password payload")],
            )

        accepted, reason, client = authenticate_request(username, password, client_ip)
        outcome = "accept" if accepted else "reject"
        record_auth_attempt(username, client_ip, outcome, reason, client["name"], client["model"])
        reply_message = reason.encode("utf-8", errors="ignore")[:120]
        return _build_packet(
            ACCESS_ACCEPT if accepted else ACCESS_REJECT,
            identifier,
            request_authenticator,
            config.secret,
            [(ATTR_REPLY_MESSAGE, reply_message)],
        )


_runtime: Optional[BuiltinRadiusRuntime] = None


def get_runtime() -> BuiltinRadiusRuntime:
    """Return the singleton runtime manager."""
    global _runtime
    if _runtime is None:
        _runtime = BuiltinRadiusRuntime()
    return _runtime


def test_radius_server() -> tuple[bool, str]:
    """Test the built-in RADIUS server by checking config and container health."""
    config = get_config()
    if not config.enabled:
        return False, "RADIUS server is not enabled"

    runtime = get_runtime()
    state = runtime.get_container_state()

    if not state["running"]:
        return False, f"RADIUS container is not running (status: {state['status']})"

    if state["health"] == "unhealthy":
        return False, "RADIUS container is running but unhealthy"

    user_count = len(list_users())
    client_count = len(list_client_overrides())
    return True, f"RADIUS server is running and healthy ({user_count} users, {client_count} client overrides)"
