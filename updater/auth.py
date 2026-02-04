"""Authentication module: RADIUS + local fallback, session management."""

import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

import bcrypt as _bcrypt

from fastapi import Request, WebSocket, HTTPException

from . import database as db
from . import radius_config

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "session_id"
SESSION_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# RADIUS authentication
# ---------------------------------------------------------------------------

def _radius_configured() -> bool:
    """Check if RADIUS is configured (via database settings or env vars)."""
    return radius_config.is_web_radius_enabled()


def authenticate_radius(username: str, password: str) -> bool:
    """Authenticate via RADIUS. Returns False if unconfigured or rejected."""
    return radius_config.authenticate_via_radius(username, password)


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

    return password == admin_pass


def is_setup_required() -> bool:
    """Check if the admin needs to set or change the default password."""
    return db.get_setting("setup_completed", "false") != "true"


def is_first_run() -> bool:
    """Check if this is a fresh install with no password configured yet.

    Returns True if no password hash in DB and no ADMIN_PASSWORD env var.
    In this state, the setup page should be accessible without authentication.
    """
    has_db_hash = bool(db.get_setting("admin_password_hash", ""))
    has_env_password = bool(os.environ.get("ADMIN_PASSWORD"))
    return not has_db_hash and not has_env_password


def complete_setup(new_password: str):
    """Hash and store a new admin password, marking setup as complete."""
    hashed = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()
    db.set_setting("admin_password_hash", hashed)
    db.set_setting("setup_completed", "true")
    # Enable auto-updates by default on first run
    db.set_setting("schedule_enabled", "true")  # Device firmware auto-update
    db.set_setting("autoupdate_enabled", "true")  # App self-update


# ---------------------------------------------------------------------------
# Unified authenticate
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str) -> Optional[str]:
    """Try RADIUS then local. Returns session_id on success, None on failure."""
    if authenticate_radius(username, password) or authenticate_local(username, password):
        return username
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
