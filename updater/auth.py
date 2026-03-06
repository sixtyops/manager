"""Authentication module: local + OIDC SSO, session management, RBAC."""

import hmac
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional

import bcrypt as _bcrypt

from fastapi import Depends, Request, WebSocket, HTTPException

from . import database as db

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "session_id"
SESSION_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Local authentication
# ---------------------------------------------------------------------------

def authenticate_local(username: str, password: str) -> Optional[dict]:
    """Authenticate against users table, with env-var fallback for bootstrap.

    Returns user dict on success, None on failure.
    """
    user = db.get_user(username)

    if user and user["enabled"]:
        # User exists with a password hash — check it
        if user["password_hash"]:
            try:
                if _bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
                    return user
            except Exception:
                pass
            return None

        # User exists but no password_hash (bootstrapped from env var).
        # Try env-var fallback and return the real user record if it passes.
        env_result = _authenticate_env_fallback(username, password)
        if env_result:
            return user
        return None

    # No user in DB at all — try env-var fallback (pre-migration state)
    if user is None and db.count_admin_users() == 0:
        return _authenticate_env_fallback(username, password)

    return None


def _authenticate_env_fallback(username: str, password: str) -> Optional[dict]:
    """Authenticate against ADMIN_USERNAME/ADMIN_PASSWORD env vars.

    Returns a synthetic user dict for backward compatibility during bootstrap.
    """
    admin_user = os.environ.get("ADMIN_USERNAME")
    if not admin_user or username != admin_user:
        return None

    # Prefer DB-stored bcrypt hash (set during initial setup)
    db_hash = db.get_setting("admin_password_hash", "")
    if db_hash:
        try:
            if _bcrypt.checkpw(password.encode(), db_hash.encode()):
                return {"id": 0, "username": admin_user, "role": "admin",
                        "auth_method": "local", "enabled": 1}
        except Exception:
            pass
        return None

    # Fall back to env var (bootstrap password)
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if not admin_pass:
        return None

    ok = False
    if admin_pass.startswith("$2b$") or admin_pass.startswith("$2a$"):
        ok = _bcrypt.checkpw(password.encode(), admin_pass.encode())
    else:
        ok = hmac.compare_digest(password, admin_pass)

    if ok:
        return {"id": 0, "username": admin_user, "role": "admin",
                "auth_method": "local", "enabled": 1}
    return None


def is_setup_required() -> bool:
    """Check if the admin needs to set or change the default password.

    Includes lockout recovery: if setup is marked complete but no password
    exists anywhere, auto-reset to allow re-setup.
    """
    setup_done = db.get_setting("setup_completed", "false") == "true"
    if setup_done and is_first_run():
        logger.warning("Lockout recovery: setup_completed=true but no password configured, resetting")
        db.set_setting("setup_completed", "false")
        return True
    return not setup_done


def is_first_run() -> bool:
    """Check if this is a fresh install with no password configured yet."""
    has_db_hash = bool(db.get_setting("admin_password_hash", ""))
    has_env_password = bool(os.environ.get("ADMIN_PASSWORD"))
    return not has_db_hash and not has_env_password


_setup_lock = threading.Lock()


def complete_setup(new_password: str) -> bool:
    """Hash and store a new admin password, marking setup as complete.

    Returns True if setup was performed, False if already completed (race guard).
    """
    hashed = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()

    with _setup_lock:
        if db.get_setting("setup_completed", "false") == "true":
            return False
        db.set_settings({
            "admin_password_hash": hashed,
            "setup_completed": "true",
            "schedule_enabled": "true",
            "autoupdate_enabled": "true",
        })
        # Also update or create the admin user in users table
        admin_username = os.environ.get("ADMIN_USERNAME", "admin")
        user = db.get_user(admin_username)
        if user:
            db.update_user(user["id"], password_hash=hashed)
        else:
            db.create_user(admin_username, hashed, "admin", "local")
        return True


def ensure_admin_user():
    """Auto-create the bootstrap admin user if users table is empty.

    Called on startup after init_db(). No-op if users already exist.
    """
    try:
        existing = db.list_users()
        if existing:
            return

        admin_username = os.environ.get("ADMIN_USERNAME", "admin")
        pw_hash = db.get_setting("admin_password_hash", "")
        if not pw_hash:
            env_pw = os.environ.get("ADMIN_PASSWORD", "")
            if env_pw.startswith("$2b$") or env_pw.startswith("$2a$"):
                pw_hash = env_pw
            elif env_pw:
                pw_hash = _bcrypt.hashpw(env_pw.encode(), _bcrypt.gensalt()).decode()

        db.create_user(admin_username, pw_hash or None, "admin", "local")
        logger.info(f"Migrated bootstrap admin user '{admin_username}' to users table")
    except Exception as e:
        logger.error(f"Failed to ensure admin user: {e}")


# ---------------------------------------------------------------------------
# Unified authenticate
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str) -> Optional[dict]:
    """Authenticate via local credentials. Returns user dict on success, None on failure."""
    return authenticate_local(username, password)


# ---------------------------------------------------------------------------
# OIDC authentication (callback validation)
# ---------------------------------------------------------------------------

def authenticate_oidc_user(email: str, groups: list[str]) -> Optional[str]:
    """Validate an OIDC-authenticated user against the allowed group.

    Returns the email as session username if authorized, None otherwise.
    """
    from . import oidc_config

    config = oidc_config.get_oidc_config()
    if not config.enabled or not config.allowed_group:
        return None

    if config.allowed_group in groups:
        logger.info(f"OIDC auth successful for {email} (group: {config.allowed_group})")
        return email

    logger.warning(f"OIDC auth denied for {email}: not in group '{config.allowed_group}'")
    return None


def ensure_oidc_user(email: str) -> dict:
    """Ensure an OIDC user exists in the users table. Returns user dict."""
    user = db.get_user(email)
    if user:
        return user
    default_role = db.get_setting("oidc_default_role", "viewer")
    if default_role not in db.VALID_ROLES:
        default_role = "viewer"
    user_id = db.create_user(email, None, default_role, "oidc")
    return db.get_user_by_id(user_id)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def create_session(username: str, ip_address: str) -> str:
    """Create a new session in the DB and return the session_id."""
    session_id = str(uuid.uuid4())
    expires_at = (datetime.now() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    db.create_session(session_id, username, ip_address, expires_at)
    return session_id


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def is_request_secure(request: Request) -> bool:
    """Return True if the request arrived over HTTPS (directly or via proxy)."""
    if request.headers.get("x-forwarded-proto") == "https":
        return True
    return request.url.scheme == "https"


def _enrich_session_with_role(session: dict) -> dict:
    """Look up the user's current role and attach it to the session dict."""
    user = db.get_user(session["username"])
    if user:
        session["role"] = user["role"]
        session["user_id"] = user["id"]
    else:
        session["role"] = "viewer"
        session["user_id"] = 0
    return session


async def require_auth(request: Request) -> dict:
    """Dependency that enforces authentication on every route.

    - Page requests (Accept: text/html) -> redirect to /login
    - API requests -> 401
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        session = db.get_session(session_id)
        if session:
            return _enrich_session_with_role(session)

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        raise HTTPException(status_code=303, detail="Not authenticated",
                            headers={"Location": "/login"})
    raise HTTPException(status_code=401, detail="Not authenticated")


def require_role(*allowed_roles: str):
    """FastAPI dependency factory that enforces role-based access.

    Usage: Depends(require_role("admin", "operator"))
    """
    async def _check(session: dict = Depends(require_auth)) -> dict:
        role = session.get("role", "viewer")
        if role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return session
    return _check


async def require_auth_ws(websocket: WebSocket) -> Optional[dict]:
    """Validate session for WebSocket before accept(). Returns session with role or None."""
    session_id = websocket.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        session = db.get_session(session_id)
        if session:
            return _enrich_session_with_role(session)
    return None
