"""Authentication module: local + OIDC SSO, API tokens, session management, RBAC."""

import hashlib
import hmac
import logging
import os
import secrets
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

def _normalize_oidc_groups(groups: object) -> set[str]:
    """Normalize OIDC group claims to exact-match string membership."""
    if isinstance(groups, str):
        return {groups}
    if isinstance(groups, (list, tuple, set)):
        return {group for group in groups if isinstance(group, str)}
    return set()


def authenticate_oidc_user(email: str, groups: list[str]) -> Optional[str]:
    """Validate an OIDC-authenticated user against the allowed group.

    Returns the email as session username if authorized, None otherwise.
    """
    from . import oidc_config

    config = oidc_config.get_oidc_config()
    if not config.enabled or not config.allowed_group:
        return None

    normalized_groups = _normalize_oidc_groups(groups)
    if config.allowed_group in normalized_groups:
        logger.info(f"OIDC auth successful for {email} (group: {config.allowed_group})")
        return email

    logger.warning(f"OIDC auth denied for {email}: not in group '{config.allowed_group}'")
    return None


def ensure_oidc_user(email: str, groups: list[str] | None = None) -> dict:
    """Ensure an OIDC user exists in the users table. Returns user dict.

    If an admin_group is configured, role is re-evaluated from the IdP
    groups on every login (admin_group members get admin, everyone else
    gets viewer) so group changes take effect immediately.

    If no admin_group is configured, oidc_default_role is used only when
    the user is first created; on subsequent logins the stored role is
    preserved so admin overrides made in the UI are not clobbered.
    """

    from . import oidc_config

    user = db.get_user(email)
    if user:
        if (
            user.get("auth_method") == "oidc"
            and groups is not None
            and oidc_config.get_oidc_config().admin_group
        ):
            role = _resolve_oidc_role(groups)
            if user["role"] != role:
                db.update_user(user["id"], role=role)
                logger.info(f"OIDC user {email} role updated: {user['role']} -> {role}")
                return db.get_user_by_id(user["id"])
        return user

    role = _resolve_oidc_role(groups)
    user_id = db.create_user(email, None, role, "oidc")
    return db.get_user_by_id(user_id)


def _resolve_oidc_role(groups: list[str] | None) -> str:
    """Determine the role for an OIDC user based on their group membership."""
    from . import oidc_config

    config = oidc_config.get_oidc_config()
    normalized_groups = _normalize_oidc_groups(groups)
    if config.admin_group:
        if config.admin_group in normalized_groups:
            return "admin"
        return "viewer"

    default_role = db.get_setting("oidc_default_role", "viewer")
    if default_role not in db.VALID_ROLES:
        default_role = "viewer"
    return default_role


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


def hash_api_token(token: str) -> str:
    """Hash an API token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_api_token() -> tuple[str, str, str]:
    """Generate a new API token. Returns (full_token, token_hash, token_prefix)."""
    token = f"tach_{secrets.token_urlsafe(32)}"
    token_hash = hash_api_token(token)
    token_prefix = token[:9] + "..."
    return token, token_hash, token_prefix


def _authenticate_bearer(request: Request) -> Optional[dict]:
    """Check for Bearer token in Authorization header. Returns session-like dict or None."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]
    token_hash = hash_api_token(token)
    token_row = db.get_api_token_by_hash(token_hash)
    if not token_row:
        return None

    # Check expiry
    if token_row["expires_at"]:
        if token_row["expires_at"] < datetime.now().isoformat():
            return None

    # Look up the owning user
    user = db.get_user_by_id(token_row["user_id"])
    if not user or not user["enabled"]:
        return None

    # Update last used (fire-and-forget, don't block auth)
    try:
        db.update_api_token_last_used(token_row["id"])
    except Exception:
        pass

    return {
        "username": user["username"],
        "role": user["role"],
        "user_id": user["id"],
        "auth_method": "api_token",
        "token_id": token_row["id"],
        "token_scopes": token_row.get("scopes", "read"),
    }


async def require_auth(request: Request) -> dict:
    """Dependency that enforces authentication on every route.

    Checks (in order): session cookie, Bearer token.
    - Page requests (Accept: text/html) -> redirect to /login
    - API requests -> 401
    """
    # 1. Session cookie
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        session = db.get_session(session_id)
        if session:
            return _enrich_session_with_role(session)

    # 2. Bearer token
    token_session = _authenticate_bearer(request)
    if token_session:
        return token_session

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
