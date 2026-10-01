"""OIDC/SSO authentication configuration for Authentik integration.

Configuration can be set via:
- Database settings (runtime configuration via API)
- Environment variables (bootstrap/deployment fallback)
"""

import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from . import database as db

logger = logging.getLogger(__name__)


# Default OIDC scopes. `groups` is required by Authentik/Keycloak to emit group
# membership, but it is NOT a valid OAuth scope on Microsoft Entra (Azure AD),
# which hard-rejects the whole authorization request (AADSTS650053) rather than
# ignoring it. Entra deployments must drop `groups` from their scopes; the group
# claim there is configured on the app registration, not requested as a scope.
DEFAULT_OIDC_SCOPES = "openid email profile groups"


# ---------------------------------------------------------------------------
# Configuration Data Class
# ---------------------------------------------------------------------------

@dataclass
class OIDCConfig:
    """OIDC provider configuration."""
    enabled: bool = False
    provider_url: str = ""       # e.g. https://authentik.example.com/application/o/tachyon/
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""       # e.g. https://sixtyops.example.com/auth/oidc/callback
    allowed_group: str = ""      # Authentik group name required for access
    admin_group: str = ""        # Authentik group name that grants admin role
    scopes: str = DEFAULT_OIDC_SCOPES


# ---------------------------------------------------------------------------
# Settings Keys
# ---------------------------------------------------------------------------

SETTING_OIDC_ENABLED = "oidc_enabled"
SETTING_OIDC_PROVIDER_URL = "oidc_provider_url"
SETTING_OIDC_CLIENT_ID = "oidc_client_id"
SETTING_OIDC_CLIENT_SECRET = "oidc_client_secret"
SETTING_OIDC_REDIRECT_URI = "oidc_redirect_uri"
SETTING_OIDC_ALLOWED_GROUP = "oidc_allowed_group"
SETTING_OIDC_ADMIN_GROUP = "oidc_admin_group"
SETTING_OIDC_SCOPES = "oidc_scopes"


# ---------------------------------------------------------------------------
# Field <-> env var / DB setting mapping
# ---------------------------------------------------------------------------
#
# Each field can be sourced from an environment variable OR the database. When
# an env var is set (non-empty), it WINS and the field becomes "env-locked":
# the settings UI shows it read-only and set_oidc_config() will not persist it.
# Fields with no env var stay database-backed and remain editable in the UI --
# so you can inject e.g. the provider URL and scopes via compose while still
# setting the client secret from the UI, without restarting the container.
#
# field name -> (env var, db setting key, default)
OIDC_FIELDS: dict[str, tuple[str, str, str]] = {
    "enabled": ("OIDC_ENABLED", SETTING_OIDC_ENABLED, ""),
    "provider_url": ("OIDC_PROVIDER_URL", SETTING_OIDC_PROVIDER_URL, ""),
    "client_id": ("OIDC_CLIENT_ID", SETTING_OIDC_CLIENT_ID, ""),
    "client_secret": ("OIDC_CLIENT_SECRET", SETTING_OIDC_CLIENT_SECRET, ""),
    "redirect_uri": ("OIDC_REDIRECT_URI", SETTING_OIDC_REDIRECT_URI, ""),
    "allowed_group": ("OIDC_ALLOWED_GROUP", SETTING_OIDC_ALLOWED_GROUP, ""),
    "admin_group": ("OIDC_ADMIN_GROUP", SETTING_OIDC_ADMIN_GROUP, ""),
    "scopes": ("OIDC_SCOPES", SETTING_OIDC_SCOPES, DEFAULT_OIDC_SCOPES),
}


# ---------------------------------------------------------------------------
# Configuration Read/Write
# ---------------------------------------------------------------------------

def _env_value(field: str) -> str | None:
    """Env var value for a field if set (non-empty), else None.

    Empty/whitespace-only env vars are treated as unset, so an inherited blank
    (e.g. `OIDC_SCOPES=` in compose) does not lock the field.
    """
    env_var, _, _ = OIDC_FIELDS[field]
    val = os.environ.get(env_var)
    if val is None or val.strip() == "":
        return None
    return val


def get_oidc_env_locked_fields() -> list[str]:
    """Field names pinned by an environment variable (UI read-only)."""
    return [field for field in OIDC_FIELDS if _env_value(field) is not None]


def _resolve(field: str) -> str:
    """Effective raw value for a field: env var wins, else the DB value."""
    _, setting_key, default = OIDC_FIELDS[field]
    env_val = _env_value(field)
    if env_val is not None:
        return env_val
    return db.get_setting(setting_key, default)


def get_oidc_config() -> OIDCConfig:
    """Get OIDC configuration.

    Resolved per field: an environment variable (when set) wins over the stored
    database value; otherwise the database value is used. See OIDC_FIELDS and
    get_oidc_env_locked_fields().
    """
    return OIDCConfig(
        enabled=_resolve("enabled").lower() == "true",
        provider_url=_resolve("provider_url"),
        client_id=_resolve("client_id"),
        client_secret=_resolve("client_secret"),
        redirect_uri=_resolve("redirect_uri"),
        allowed_group=_resolve("allowed_group"),
        admin_group=_resolve("admin_group"),
        scopes=_resolve("scopes"),
    )


def set_oidc_config(config: OIDCConfig):
    """Save OIDC configuration to the database.

    Env-locked fields (pinned by an environment variable) are NOT written --
    the environment stays their single source of truth, so a UI save can't
    drift from or clobber them.
    """
    previous = get_oidc_config()
    locked = set(get_oidc_env_locked_fields())
    values = {
        "enabled": (SETTING_OIDC_ENABLED, str(config.enabled).lower()),
        "provider_url": (SETTING_OIDC_PROVIDER_URL, config.provider_url),
        "client_id": (SETTING_OIDC_CLIENT_ID, config.client_id),
        "client_secret": (SETTING_OIDC_CLIENT_SECRET, config.client_secret),
        "redirect_uri": (SETTING_OIDC_REDIRECT_URI, config.redirect_uri),
        "allowed_group": (SETTING_OIDC_ALLOWED_GROUP, config.allowed_group),
        "admin_group": (SETTING_OIDC_ADMIN_GROUP, config.admin_group),
        "scopes": (SETTING_OIDC_SCOPES, config.scopes),
    }
    to_write = {
        key: val for field, (key, val) in values.items() if field not in locked
    }
    if to_write:
        db.set_settings(to_write)

    current = get_oidc_config()
    if (not current.enabled or not current.provider_url
            or previous.provider_url != current.provider_url):
        db.delete_setting("oidc_end_session_endpoint")
    logger.info(
        f"OIDC config updated: enabled={current.enabled}, "
        f"provider={current.provider_url}, env_locked={sorted(locked)}"
    )


def is_oidc_enabled() -> bool:
    """Check if OIDC is enabled and minimally configured."""
    config = get_oidc_config()
    return (config.enabled
            and bool(config.provider_url)
            and bool(config.client_id)
            and bool(config.client_secret))


def validate_provider_url(url: str, *, allow_private: bool | None = None):
    """Validate OIDC provider URL. Raises ValueError if invalid.

    Set OIDC_ALLOW_PRIVATE_IPS=true env var (or pass allow_private=True) to
    permit private/loopback addresses for self-hosted OIDC providers on LAN.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("OIDC provider URL must use HTTPS")
    if not parsed.hostname:
        raise ValueError("OIDC provider URL has no hostname")

    if allow_private is None:
        allow_private = os.environ.get("OIDC_ALLOW_PRIVATE_IPS", "").lower() == "true"

    try:
        addrs = socket.getaddrinfo(parsed.hostname, None)
        for _, _, _, _, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if not allow_private and (ip.is_private or ip.is_loopback or ip.is_reserved):
                raise ValueError("OIDC provider URL must not resolve to a private/loopback address")
    except socket.gaierror:
        raise ValueError("OIDC provider URL hostname could not be resolved")
