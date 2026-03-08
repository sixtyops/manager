"""Syslog forwarder: sends application events to a remote syslog server."""

import logging
import logging.handlers
import socket
from typing import Optional

from . import database as db

logger = logging.getLogger(__name__)

# Singleton syslog handler — replaced when config changes
_syslog_handler: Optional[logging.Handler] = None
_syslog_logger: Optional[logging.Logger] = None
_current_config: dict = {}

# Facility codes (RFC 5424)
FACILITIES = {
    "local0": logging.handlers.SysLogHandler.LOG_LOCAL0,
    "local1": logging.handlers.SysLogHandler.LOG_LOCAL1,
    "local2": logging.handlers.SysLogHandler.LOG_LOCAL2,
    "local3": logging.handlers.SysLogHandler.LOG_LOCAL3,
    "local4": logging.handlers.SysLogHandler.LOG_LOCAL4,
    "local5": logging.handlers.SysLogHandler.LOG_LOCAL5,
    "local6": logging.handlers.SysLogHandler.LOG_LOCAL6,
    "local7": logging.handlers.SysLogHandler.LOG_LOCAL7,
}


def _get_config() -> dict:
    """Read syslog forwarder config from settings."""
    settings = db.get_all_settings()
    return {
        "enabled": settings.get("syslog_forward_enabled", "false") == "true",
        "host": settings.get("syslog_forward_host", ""),
        "port": int(settings.get("syslog_forward_port", "514")),
        "protocol": settings.get("syslog_forward_protocol", "udp"),
        "facility": settings.get("syslog_forward_facility", "local0"),
    }


def _setup_handler(config: dict) -> Optional[logging.Handler]:
    """Create a SysLogHandler from config."""
    global _syslog_handler, _syslog_logger, _current_config

    # Tear down existing handler
    if _syslog_handler and _syslog_logger:
        _syslog_logger.removeHandler(_syslog_handler)
        try:
            _syslog_handler.close()
        except Exception:
            pass
        _syslog_handler = None

    if not config["enabled"] or not config["host"]:
        _current_config = config
        return None

    try:
        socktype = socket.SOCK_DGRAM if config["protocol"] == "udp" else socket.SOCK_STREAM
        facility = FACILITIES.get(config["facility"], logging.handlers.SysLogHandler.LOG_LOCAL0)

        handler = logging.handlers.SysLogHandler(
            address=(config["host"], config["port"]),
            facility=facility,
            socktype=socktype,
        )
        handler.setFormatter(logging.Formatter("tachyon: %(message)s"))

        if _syslog_logger is None:
            _syslog_logger = logging.getLogger("tachyon.syslog")
            _syslog_logger.setLevel(logging.INFO)
            _syslog_logger.propagate = False

        _syslog_logger.addHandler(handler)
        _syslog_handler = handler
        _current_config = config
        logger.info(f"Syslog forwarder configured: {config['host']}:{config['port']} ({config['protocol']})")
        return handler
    except Exception as e:
        logger.error(f"Failed to configure syslog forwarder: {e}")
        _current_config = config
        return None


def reload_config():
    """Reload syslog config from DB and reconnect if changed."""
    config = _get_config()
    if config != _current_config:
        _setup_handler(config)


def send_event(event_type: str, message: str, severity: str = "info"):
    """Send an event to the remote syslog server.

    Args:
        event_type: Category like 'job', 'device', 'auth', 'system'
        message: Human-readable message
        severity: 'info', 'warning', 'error', 'critical'
    """
    if _syslog_logger is None or _syslog_handler is None:
        return

    log_msg = f"[{event_type}] {message}"
    level_map = {
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }
    level = level_map.get(severity, logging.INFO)

    try:
        _syslog_logger.log(level, log_msg)
    except Exception as e:
        logger.debug(f"Syslog send failed: {e}")


def get_status() -> dict:
    """Return current syslog forwarder status."""
    config = _get_config()
    return {
        "enabled": config["enabled"],
        "host": config["host"],
        "port": config["port"],
        "protocol": config["protocol"],
        "facility": config["facility"],
        "connected": _syslog_handler is not None,
    }


def test_connection() -> tuple[bool, str]:
    """Test the syslog connection by sending a test message."""
    config = _get_config()
    if not config["enabled"] or not config["host"]:
        return False, "Syslog forwarding is not enabled or host is not configured"

    # Ensure handler is set up
    reload_config()

    if _syslog_handler is None:
        return False, "Failed to connect to syslog server"

    try:
        send_event("system", "Tachyon syslog forwarding test message", "info")
        return True, f"Test message sent to {config['host']}:{config['port']}"
    except Exception as e:
        return False, f"Failed to send test message: {e}"
