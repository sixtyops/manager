"""Authentication module: RADIUS + local fallback, session management."""

import logging
import os
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
# RADIUS authentication
# ---------------------------------------------------------------------------

def _radius_configured() -> bool:
    """Check if RADIUS env vars are set."""
    return bool(os.environ.get("RADIUS_SERVER") and os.environ.get("RADIUS_SECRET"))


def authenticate_radius(username: str, password: str) -> bool:
    """Authenticate via RADIUS. Returns False if unconfigured or rejected."""
    if not _radius_configured():
        return False

    try:
        from pyrad.client import Client
        from pyrad.dictionary import Dictionary
        import pyrad.packet

        server = os.environ["RADIUS_SERVER"]
        secret = os.environ["RADIUS_SECRET"].encode()
        port = int(os.environ.get("RADIUS_PORT", "1812"))

        # pyrad requires a dictionary file; use a minimal inline one
        import tempfile
        dict_content = (
            "ATTRIBUTE\tUser-Name\t1\tstring\n"
            "ATTRIBUTE\tUser-Password\t2\tstring\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dict", delete=False) as f:
            f.write(dict_content)
            dict_path = f.name

        try:
            client = Client(
                server=server,
                secret=secret,
                authport=port,
                dict=Dictionary(dict_path),
            )
            client.timeout = 5
            client.retries = 1

            req = client.CreateAuthPacket(code=pyrad.packet.AccessRequest)
            req["User-Name"] = username
            req["User-Password"] = req.PwCrypt(password)

            reply = client.SendPacket(req)
            return reply.code == pyrad.packet.AccessAccept
        finally:
            os.unlink(dict_path)

    except Exception as e:
        logger.error(f"RADIUS authentication error: {e}")
        return False


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
    """Check if the admin needs to change the default password."""
    return db.get_setting("setup_completed", "false") != "true"


def complete_setup(new_password: str):
    """Hash and store a new admin password, marking setup as complete."""
    hashed = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()
    db.set_setting("admin_password_hash", hashed)
    db.set_setting("setup_completed", "true")


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
