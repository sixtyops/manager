"""OIDC/SSO authentication configuration for Authentik integration.

Configuration can be set via:
- Database settings (runtime configuration via API)
- Environment variables (bootstrap/deployment fallback)
"""

import logging
import os
from dataclasses import dataclass

from . import database as db

logger = logging.getLogger(__name__)


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
    redirect_uri: str = ""       # e.g. https://tachyon.example.com/auth/oidc/callback
    allowed_group: str = ""      # Authentik group name required for access
    scopes: str = "openid email profile"


# ---------------------------------------------------------------------------
# Settings Keys
# ---------------------------------------------------------------------------

SETTING_OIDC_ENABLED = "oidc_enabled"
SETTING_OIDC_PROVIDER_URL = "oidc_provider_url"
SETTING_OIDC_CLIENT_ID = "oidc_client_id"
SETTING_OIDC_CLIENT_SECRET = "oidc_client_secret"
SETTING_OIDC_REDIRECT_URI = "oidc_redirect_uri"
SETTING_OIDC_ALLOWED_GROUP = "oidc_allowed_group"
SETTING_OIDC_SCOPES = "oidc_scopes"


# ---------------------------------------------------------------------------
# Configuration Read/Write
# ---------------------------------------------------------------------------

def get_oidc_config() -> OIDCConfig:
    """Get OIDC configuration.

    Priority: Database settings > Environment variables
    """
    db_enabled = db.get_setting(SETTING_OIDC_ENABLED, "")

    if db_enabled:
        return OIDCConfig(
            enabled=db_enabled.lower() == "true",
            provider_url=db.get_setting(SETTING_OIDC_PROVIDER_URL, ""),
            client_id=db.get_setting(SETTING_OIDC_CLIENT_ID, ""),
            client_secret=db.get_setting(SETTING_OIDC_CLIENT_SECRET, ""),
            redirect_uri=db.get_setting(SETTING_OIDC_REDIRECT_URI, ""),
            allowed_group=db.get_setting(SETTING_OIDC_ALLOWED_GROUP, ""),
            scopes=db.get_setting(SETTING_OIDC_SCOPES, "openid email profile"),
        )

    # Fall back to environment variables
    provider_url = os.environ.get("OIDC_PROVIDER_URL", "")
    client_id = os.environ.get("OIDC_CLIENT_ID", "")

    return OIDCConfig(
        enabled=bool(provider_url and client_id),
        provider_url=provider_url,
        client_id=client_id,
        client_secret=os.environ.get("OIDC_CLIENT_SECRET", ""),
        redirect_uri=os.environ.get("OIDC_REDIRECT_URI", ""),
        allowed_group=os.environ.get("OIDC_ALLOWED_GROUP", ""),
        scopes=os.environ.get("OIDC_SCOPES", "openid email profile"),
    )


def set_oidc_config(config: OIDCConfig):
    """Save OIDC configuration to database."""
    db.set_settings({
        SETTING_OIDC_ENABLED: str(config.enabled).lower(),
        SETTING_OIDC_PROVIDER_URL: config.provider_url,
        SETTING_OIDC_CLIENT_ID: config.client_id,
        SETTING_OIDC_CLIENT_SECRET: config.client_secret,
        SETTING_OIDC_REDIRECT_URI: config.redirect_uri,
        SETTING_OIDC_ALLOWED_GROUP: config.allowed_group,
        SETTING_OIDC_SCOPES: config.scopes,
    })
    logger.info(f"OIDC config updated: enabled={config.enabled}, provider={config.provider_url}")


def is_oidc_enabled() -> bool:
    """Check if OIDC is enabled and minimally configured."""
    config = get_oidc_config()
    return (config.enabled
            and bool(config.provider_url)
            and bool(config.client_id)
            and bool(config.client_secret))
