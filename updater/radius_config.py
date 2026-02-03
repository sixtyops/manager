"""RADIUS authentication configuration.

This module provides centralized RADIUS configuration for:
1. Web authentication - Admin login via RADIUS server
2. Device authentication - Default credentials for managed devices

Configuration can be set via:
- Environment variables (bootstrap/deployment)
- Database settings (runtime configuration via API)
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from . import database as db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration Data Classes
# ---------------------------------------------------------------------------

@dataclass
class RadiusConfig:
    """RADIUS server configuration."""
    enabled: bool = False
    server: str = ""
    secret: str = ""
    port: int = 1812
    timeout: int = 5
    retries: int = 1


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

# Web RADIUS settings (stored in database, env vars as fallback)
SETTING_RADIUS_ENABLED = "radius_enabled"
SETTING_RADIUS_SERVER = "radius_server"
SETTING_RADIUS_SECRET = "radius_secret"
SETTING_RADIUS_PORT = "radius_port"
SETTING_RADIUS_TIMEOUT = "radius_timeout"

# Global device auth settings
SETTING_DEVICE_AUTH_ENABLED = "device_default_auth_enabled"
SETTING_DEVICE_AUTH_USERNAME = "device_default_username"
SETTING_DEVICE_AUTH_PASSWORD = "device_default_password"


# ---------------------------------------------------------------------------
# Web RADIUS Configuration
# ---------------------------------------------------------------------------

def get_web_radius_config() -> RadiusConfig:
    """Get RADIUS configuration for web authentication.

    Priority: Database settings > Environment variables
    """
    # Check database first
    db_enabled = db.get_setting(SETTING_RADIUS_ENABLED, "")

    if db_enabled:
        # Use database configuration
        return RadiusConfig(
            enabled=db_enabled.lower() == "true",
            server=db.get_setting(SETTING_RADIUS_SERVER, ""),
            secret=db.get_setting(SETTING_RADIUS_SECRET, ""),
            port=int(db.get_setting(SETTING_RADIUS_PORT, "1812")),
            timeout=int(db.get_setting(SETTING_RADIUS_TIMEOUT, "5")),
        )

    # Fall back to environment variables
    server = os.environ.get("RADIUS_SERVER", "")
    secret = os.environ.get("RADIUS_SECRET", "")

    return RadiusConfig(
        enabled=bool(server and secret),
        server=server,
        secret=secret,
        port=int(os.environ.get("RADIUS_PORT", "1812")),
        timeout=5,
    )


def set_web_radius_config(config: RadiusConfig):
    """Save RADIUS configuration for web authentication to database."""
    db.set_settings({
        SETTING_RADIUS_ENABLED: str(config.enabled).lower(),
        SETTING_RADIUS_SERVER: config.server,
        SETTING_RADIUS_SECRET: config.secret,
        SETTING_RADIUS_PORT: str(config.port),
        SETTING_RADIUS_TIMEOUT: str(config.timeout),
    })
    logger.info(f"Web RADIUS config updated: enabled={config.enabled}, server={config.server}")


def is_web_radius_enabled() -> bool:
    """Check if RADIUS is enabled for web authentication."""
    config = get_web_radius_config()
    return config.enabled and bool(config.server) and bool(config.secret)


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
# RADIUS Authentication (for web login)
# ---------------------------------------------------------------------------

def authenticate_via_radius(username: str, password: str) -> bool:
    """Authenticate user via RADIUS server.

    Returns True if authentication succeeds, False otherwise.
    """
    config = get_web_radius_config()

    if not config.enabled or not config.server or not config.secret:
        return False

    try:
        from pyrad.client import Client
        from pyrad.dictionary import Dictionary
        import pyrad.packet
        import tempfile

        # pyrad requires a dictionary file; use a minimal inline one
        dict_content = (
            "ATTRIBUTE\tUser-Name\t1\tstring\n"
            "ATTRIBUTE\tUser-Password\t2\tstring\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dict", delete=False) as f:
            f.write(dict_content)
            dict_path = f.name

        try:
            client = Client(
                server=config.server,
                secret=config.secret.encode(),
                authport=config.port,
                dict=Dictionary(dict_path),
            )
            client.timeout = config.timeout
            client.retries = config.retries

            req = client.CreateAuthPacket(code=pyrad.packet.AccessRequest)
            req["User-Name"] = username
            req["User-Password"] = req.PwCrypt(password)

            reply = client.SendPacket(req)
            success = reply.code == pyrad.packet.AccessAccept

            if success:
                logger.info(f"RADIUS auth successful for user: {username}")
            else:
                logger.warning(f"RADIUS auth rejected for user: {username}")

            return success

        finally:
            os.unlink(dict_path)

    except Exception as e:
        logger.error(f"RADIUS authentication error: {e}")
        return False


# ---------------------------------------------------------------------------
# Configuration Summary
# ---------------------------------------------------------------------------

def get_auth_config_summary() -> dict:
    """Get a summary of all authentication configuration (for API/UI).

    Note: Secrets are masked for security.
    """
    web_radius = get_web_radius_config()
    device_auth = get_device_auth_config()

    return {
        "web_radius": {
            "enabled": web_radius.enabled,
            "server": web_radius.server,
            "port": web_radius.port,
            "timeout": web_radius.timeout,
            "configured": is_web_radius_enabled(),
        },
        "device_defaults": {
            "enabled": device_auth.enabled,
            "username": device_auth.username,
            "has_password": bool(device_auth.password),
            "configured": is_device_auth_enabled(),
        },
    }
