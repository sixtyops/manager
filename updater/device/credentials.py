"""Default credentials for managed devices."""

import logging
from dataclasses import dataclass, field

from .. import database as db

logger = logging.getLogger(__name__)

SETTING_DEVICE_AUTH_ENABLED = "device_default_auth_enabled"
SETTING_DEVICE_AUTH_USERNAME = "device_default_username"
SETTING_DEVICE_AUTH_PASSWORD = "device_default_password"


@dataclass
class DeviceAuthConfig:
    enabled: bool = False
    username: str = ""
    password: str = field(default="", repr=False)


def get_device_auth_config() -> DeviceAuthConfig:
    """Read the saved default device credentials."""
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


def get_device_auth_summary() -> dict:
    """Summarize the defaults for the authentication API."""
    config = get_device_auth_config()
    return {
        "enabled": config.enabled,
        "username": config.username,
        "has_password": bool(config.password),
        "configured": config.enabled and bool(config.username) and bool(config.password),
    }
