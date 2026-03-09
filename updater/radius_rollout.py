"""RADIUS rollout workflow — phased device migration to RADIUS authentication.

Extracted from builtin_radius.py. Pure database CRUD operations for managing
the rollout lifecycle (canary → 10% → 50% → 100%).
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from . import database as db
from .crypto import decrypt_password, encrypt_password, is_encrypted

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANAGEMENT_SERVICE_USERNAME = "sixtyops-radius-mgmt"
RESERVED_USERNAMES = {"admin", "root", MANAGEMENT_SERVICE_USERNAME}
ROLLOUT_PHASE_ORDER = ["canary", "pct10", "pct50", "pct100"]

SETTING_RADIUS_SECRET = "builtin_radius_secret"
SETTING_RADIUS_SECRET_UPDATED_AT = "builtin_radius_secret_updated_at"
SETTING_RADIUS_SECRET_REVIEW_ACKNOWLEDGED_AT = "builtin_radius_secret_review_acknowledged_at"
SETTING_RADIUS_MGMT_PASSWORD = "builtin_radius_mgmt_password"

ROTATION_RECOMMEND_AFTER_DAYS = 365


# ---------------------------------------------------------------------------
# Management service account
# ---------------------------------------------------------------------------

def get_management_service_credentials(create_if_missing: bool = True) -> tuple[str, str]:
    """Return the hidden RADIUS account used for automated device verification.

    The password is stored with Fernet encryption (reversible) because it must
    be sent as plaintext to devices during rollout verification.
    """
    stored = db.get_setting(SETTING_RADIUS_MGMT_PASSWORD, "")
    if stored:
        password = decrypt_password(stored) if is_encrypted(stored) else stored
        return MANAGEMENT_SERVICE_USERNAME, password

    if not create_if_missing:
        return MANAGEMENT_SERVICE_USERNAME, ""

    password = secrets.token_urlsafe(24)
    db.set_setting(SETTING_RADIUS_MGMT_PASSWORD, encrypt_password(password))
    return MANAGEMENT_SERVICE_USERNAME, password


# ---------------------------------------------------------------------------
# Secret rotation tracking
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Rollout CRUD
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Client override CRUD
# ---------------------------------------------------------------------------

def validate_client_spec(client_spec: str) -> str:
    """Validate an IP or CIDR allowed to query the RADIUS server."""
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


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def get_auth_stats(limit: int = 10) -> dict:
    """Return aggregate auth stats and recent auth attempts."""
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
        override_count = conn.execute(
            "SELECT COUNT(*) FROM radius_client_overrides WHERE enabled = 1"
        ).fetchone()[0]
        device_count = 0
        for table in ("access_points", "switches"):
            device_count += conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE enabled = 1"
            ).fetchone()[0]
        known_clients = device_count + override_count

    total = totals["total"] or 0
    accepts = totals["accepts"] or 0
    success_rate = round((accepts / total) * 100, 1) if total else 100.0

    return {
        "admin_accounts": admin_accounts,
        "known_clients": known_clients,
        "active_devices_24h": active_devices,
        "auth_success_rate": success_rate,
        "logins_today": logins_today,
        "recent_logins": [dict(row) for row in recent_rows],
    }


def get_auth_log(limit: int = 50, offset: int = 0) -> list[dict]:
    """Return recent RADIUS authentication log entries."""
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT username, client_ip, client_name, client_model, outcome, reason, occurred_at
            FROM radius_auth_log
            ORDER BY occurred_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]
