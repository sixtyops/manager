"""RADIUS user management — CRUD operations for the local user database."""

import logging
import re
from datetime import datetime
from typing import Optional

import bcrypt

from . import database as db
from .crypto import decrypt_password, is_encrypted

logger = logging.getLogger(__name__)

# Allowed characters for RADIUS usernames
_USERNAME_RE = re.compile(r'^[a-zA-Z0-9._@-]{1,128}$')


def validate_username(username: str) -> str:
    """Validate and return a cleaned username. Raises ValueError if invalid."""
    username = username.strip()
    if not username:
        raise ValueError("Username is required")
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "Username must be 1-128 characters using only letters, "
            "digits, '.', '_', '@', or '-'"
        )
    return username


def _hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def create_radius_user(
    username: str, password: str, description: str = ""
) -> int:
    """Create a RADIUS user. Returns the new user ID."""
    username = validate_username(username)
    if not password:
        raise ValueError("Password is required")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    hashed = _hash_password(password)
    with db.get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO radius_users (username, password, description) "
            "VALUES (?, ?, ?)",
            (username, hashed, description),
        )
        return cursor.lastrowid


def get_radius_users() -> list[dict]:
    """Get all RADIUS users (without password hashes)."""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, description, enabled, last_auth_at, "
            "auth_count, created_at, updated_at FROM radius_users "
            "ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]


def get_radius_users_for_backup() -> list[dict]:
    """Get all RADIUS users with password hashes (for backup export)."""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT username, password, enabled FROM radius_users "
            "ORDER BY username COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]


def get_radius_user(user_id: int) -> Optional[dict]:
    """Get a single RADIUS user by ID (without password)."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id, username, description, enabled, last_auth_at, "
            "auth_count, created_at, updated_at FROM radius_users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def get_radius_user_by_name(username: str) -> Optional[dict]:
    """Get a RADIUS user by username (includes password hash for auth)."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM radius_users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None


def update_radius_user(user_id: int, **kwargs) -> bool:
    """Update a RADIUS user. Returns True if found and updated."""
    allowed = {"username", "password", "description", "enabled"}
    updates = {}
    for key, value in kwargs.items():
        if key not in allowed:
            continue
        if key == "username":
            value = validate_username(value)
            updates["username"] = value
        elif key == "password":
            if value:
                if len(value) < 8:
                    raise ValueError("Password must be at least 8 characters")
                updates["password"] = _hash_password(value)
        elif key == "enabled":
            updates["enabled"] = 1 if value else 0
        else:
            updates[key] = value

    if not updates:
        return False

    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]

    with db.get_db() as conn:
        cursor = conn.execute(
            f"UPDATE radius_users SET {set_clause} WHERE id = ?",
            values,
        )
        return cursor.rowcount > 0


def delete_radius_user(user_id: int) -> bool:
    """Delete a RADIUS user. Returns True if found and deleted."""
    with db.get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM radius_users WHERE id = ?", (user_id,)
        )
        return cursor.rowcount > 0


def _is_bcrypt_hash(value: str) -> bool:
    """Check if a string looks like a bcrypt hash."""
    return value.startswith("$2b$") or value.startswith("$2a$")


def verify_radius_user(username: str, password: str) -> bool:
    """Verify credentials for a RADIUS user.

    Returns True if valid. Returns False for wrong password, unknown user,
    or disabled user — callers should not distinguish between these cases.
    Updates last_auth_at and auth_count on success.

    Handles transparent migration from Fernet-encrypted passwords (legacy
    builtin_radius format) to bcrypt hashes.
    """
    user = get_radius_user_by_name(username)
    if not user:
        return False
    if not user["enabled"]:
        return False

    stored = user["password"]

    if _is_bcrypt_hash(stored):
        # Standard bcrypt verification
        if not bcrypt.checkpw(password.encode(), stored.encode()):
            return False
    elif is_encrypted(stored):
        # Legacy Fernet-encrypted password — decrypt and compare
        try:
            decrypted = decrypt_password(stored)
        except Exception:
            return False
        if password != decrypted:
            return False
        # Migrate to bcrypt on successful verification
        try:
            new_hash = _hash_password(password)
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE radius_users SET password = ? WHERE id = ?",
                    (new_hash, user["id"]),
                )
            logger.info("Migrated RADIUS user %s from Fernet to bcrypt", username)
        except Exception:
            logger.warning("Failed to migrate password for user %s", username)
    else:
        # Plaintext fallback (e.g., dev mode seeded data)
        if password != stored:
            return False
        # Migrate to bcrypt
        try:
            new_hash = _hash_password(password)
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE radius_users SET password = ? WHERE id = ?",
                    (new_hash, user["id"]),
                )
            logger.info("Migrated RADIUS user %s from plaintext to bcrypt", username)
        except Exception:
            logger.warning("Failed to migrate password for user %s", username)

    # Update auth stats on success
    with db.get_db() as conn:
        conn.execute(
            "UPDATE radius_users SET last_auth_at = ?, auth_count = auth_count + 1 "
            "WHERE id = ?",
            (datetime.now().isoformat(), user["id"]),
        )
    return True
