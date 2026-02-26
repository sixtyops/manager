"""Authentication module: local + OIDC SSO, session management."""

import hmac
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional

import bcrypt as _bcrypt

from fastapi import Request, WebSocket, HTTPException

from . import database as db

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "session_id"
SESSION_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Local authentication
# ---------------------------------------------------------------------------

def authenticate_local(username: str, password: str) -> bool:
    """Authenticate against DB-stored hash (preferred) or env var fallback."""
    admin_user = os.environ.get("ADMIN_USERNAME")
    if not admin_user or username != admin_user:
        return False

    # Prefer DB-stored bcrypt hash (set during initial setup)
    db_hash = db.get_setting("admin_password_hash", "")
    if db_hash:
        try:
            return _bcrypt.checkpw(password.encode(), db_hash.encode())
        except Exception:
            return False

    # Fall back to env var (bootstrap password)
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if not admin_pass:
        return False

    if admin_pass.startswith("$2b$") or admin_pass.startswith("$2a$"):
        return _bcrypt.checkpw(password.encode(), admin_pass.encode())

    return hmac.compare_digest(password, admin_pass)


def is_setup_required() -> bool:
    """Check if the admin needs to set or change the default password.

    Includes lockout recovery: if setup is marked complete but no password
    exists anywhere, auto-reset to allow re-setup.
    """
    setup_done = db.get_setting("setup_completed", "false") == "true"
    if setup_done and is_first_run():
        # Lockout state: setup_completed=true but no password configured.
        # This can happen if the DB is wiped after setup or env var removed.
        # Auto-recover by resetting setup_completed so the setup page is accessible.
        logger.warning("Lockout recovery: setup_completed=true but no password configured, resetting")
        db.set_setting("setup_completed", "false")
        return True
    return not setup_done


def is_first_run() -> bool:
    """Check if this is a fresh install with no password configured yet.

    Returns True if no password hash in DB and no ADMIN_PASSWORD env var.
    In this state, the setup page should be accessible without authentication.
    """
    has_db_hash = bool(db.get_setting("admin_password_hash", ""))
    has_env_password = bool(os.environ.get("ADMIN_PASSWORD"))
    return not has_db_hash and not has_env_password


_setup_lock = threading.Lock()


def complete_setup(new_password: str) -> bool:
    """Hash and store a new admin password, marking setup as complete.

    Returns True if setup was performed, False if already completed (race guard).
    Uses a lock to prevent TOCTOU race between concurrent /setup requests.
    """
    # Hash outside the lock (bcrypt is slow, ~100ms)
    hashed = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()

    with _setup_lock:
        # Check inside lock to prevent race condition
        if db.get_setting("setup_completed", "false") == "true":
            return False
        db.set_settings({
            "admin_password_hash": hashed,
            "setup_completed": "true",
            "schedule_enabled": "true",
            "autoupdate_enabled": "true",
        })
        return True


# ---------------------------------------------------------------------------
# Unified authenticate
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str) -> Optional[str]:
    """Authenticate via local credentials. Returns username on success, None on failure."""
    if authenticate_local(username, password):
        return username
    return None


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


async def require_auth(request: Request) -> dict:
    """Dependency that enforces authentication on every route.

    - Page requests (Accept: text/html) → redirect to /login
    - API requests → 401
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        session = db.get_session(session_id)
        if session:
            return session

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        raise HTTPException(status_code=303, detail="Not authenticated",
                            headers={"Location": "/login"})
    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_auth_ws(websocket: WebSocket) -> Optional[dict]:
    """Validate session for WebSocket before accept(). Returns session or None."""
    session_id = websocket.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        session = db.get_session(session_id)
        if session:
            return session
    return None
