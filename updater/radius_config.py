"""Build the authentication summary for the API."""

import os
import socket

from .device import credentials


def _detect_host_ip() -> str:
    """Detect the host's primary IP address.

    Prefers HOST_IP env var (set by docker-compose or the user).
    Falls back to Docker host gateway (host.docker.internal), then
    a UDP socket probe.
    """
    # Explicit override
    env_ip = os.environ.get("HOST_IP", "").strip()
    if env_ip:
        return env_ip
    # Docker host gateway (works on Docker Desktop and with extra_hosts)
    try:
        ip = socket.gethostbyname("host.docker.internal")
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # Fallback: UDP probe (returns container IP inside Docker, host IP outside)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def get_auth_config_summary() -> dict:
    """Get a summary of all authentication configuration (for API/UI).

    Note: Secrets are masked for security.
    """
    from .radius_server import get_radius_server_config, get_radius_service
    from . import oidc_config

    oidc = oidc_config.get_oidc_config()
    radius_cfg = get_radius_server_config()
    radius_svc = get_radius_service()

    return {
        "radius": {
            "enabled": radius_cfg.enabled,
            "advertised_address": radius_cfg.advertised_address,
            "detected_ip": _detect_host_ip(),
            "auth_port": radius_cfg.auth_port,
            "secret_set": bool(radius_cfg.shared_secret),
            "configured": bool(radius_cfg.shared_secret),
            "auth_mode": radius_cfg.auth_mode,
            "client_mode": radius_cfg.client_mode,
            "ldap_url": radius_cfg.ldap_url,
            "ldap_bind_dn": radius_cfg.ldap_bind_dn,
            "ldap_has_password": bool(radius_cfg.ldap_bind_password),
            "ldap_base_dn": radius_cfg.ldap_base_dn,
            "ldap_user_filter": radius_cfg.ldap_user_filter,
            "running": radius_svc.is_running if radius_svc else False,
            "error": radius_svc.last_error if radius_svc else "",
        },
        "oidc": {
            "enabled": oidc.enabled,
            "provider_url": oidc.provider_url,
            "client_id": oidc.client_id,
            "client_secret_set": bool(oidc.client_secret),
            "redirect_uri": oidc.redirect_uri,
            "allowed_group": oidc.allowed_group,
            "scopes": oidc.scopes,
            "configured": oidc_config.is_oidc_enabled(),
        },
        "device_defaults": credentials.get_device_auth_summary(),
    }
