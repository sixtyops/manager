"""Device authentication configuration.

This module provides default credentials for managed devices (APs/CPEs).

Configuration can be set via:
- Database settings (runtime configuration via API)
"""

import logging
from dataclasses import dataclass

from . import database as db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration Data Classes
# ---------------------------------------------------------------------------

@dataclass
class DeviceAuthConfig:
    """Default device authentication configuration.

    Used when a device doesn't have specific credentials configured.
    Allows global default credentials for all managed devices.
    """
    enabled: bool = False
    username: str = ""
    password: str = ""


# ---------------------------------------------------------------------------
# Settings Keys
# ---------------------------------------------------------------------------

# Global device auth settings
SETTING_DEVICE_AUTH_ENABLED = "device_default_auth_enabled"
SETTING_DEVICE_AUTH_USERNAME = "device_default_username"
SETTING_DEVICE_AUTH_PASSWORD = "device_default_password"


# ---------------------------------------------------------------------------
# Device Default Authentication
# ---------------------------------------------------------------------------

def get_device_auth_config() -> DeviceAuthConfig:
    """Get default device authentication configuration.

    These credentials are used when:
    - A device doesn't have specific credentials configured
    - Adding new devices with 'use default credentials' option
    - CPE authentication probing when parent AP creds fail
    """
    enabled = db.get_setting(SETTING_DEVICE_AUTH_ENABLED, "false")

    return DeviceAuthConfig(
        enabled=enabled.lower() == "true",
        username=db.get_setting(SETTING_DEVICE_AUTH_USERNAME, ""),
        password=db.get_setting(SETTING_DEVICE_AUTH_PASSWORD, ""),
    )


def set_device_auth_config(config: DeviceAuthConfig):
    """Save default device authentication configuration to database."""
    db.set_settings({
        SETTING_DEVICE_AUTH_ENABLED: str(config.enabled).lower(),
        SETTING_DEVICE_AUTH_USERNAME: config.username,
        SETTING_DEVICE_AUTH_PASSWORD: config.password,
    })
    logger.info(f"Device default auth config updated: enabled={config.enabled}")


def is_device_auth_enabled() -> bool:
    """Check if default device authentication is enabled."""
    config = get_device_auth_config()
    return config.enabled and bool(config.username) and bool(config.password)


def get_device_credentials(device_username: str = None, device_password: str = None) -> tuple[str, str]:
    """Get effective credentials for device authentication.

    Args:
        device_username: Device-specific username (may be empty)
        device_password: Device-specific password (may be empty)

    Returns:
        Tuple of (username, password) to use for authentication.
        Returns device-specific credentials if provided, otherwise falls back
        to global defaults if enabled.
    """
    # Use device-specific credentials if provided
    if device_username and device_password:
        return device_username, device_password

    # Fall back to global defaults
    config = get_device_auth_config()
    if config.enabled and config.username and config.password:
        return config.username, config.password

    # No credentials available
    return device_username or "", device_password or ""


# ---------------------------------------------------------------------------
# Configuration Summary
# ---------------------------------------------------------------------------

def get_auth_config_summary() -> dict:
    """Get a summary of all authentication configuration (for API/UI).

    Note: Secrets are masked for security.
    """
    from . import oidc_config

    device_auth = get_device_auth_config()
    oidc = oidc_config.get_oidc_config()

    return {
        "oidc": {
            "enabled": oidc.enabled,
            "provider_url": oidc.provider_url,
            "client_id": oidc.client_id,
            "redirect_uri": oidc.redirect_uri,
            "allowed_group": oidc.allowed_group,
            "scopes": oidc.scopes,
            "configured": oidc_config.is_oidc_enabled(),
        },
        "device_defaults": {
            "enabled": device_auth.enabled,
            "username": device_auth.username,
            "has_password": bool(device_auth.password),
            "configured": is_device_auth_enabled(),
        },
    }
