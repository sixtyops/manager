"""SNMP trap notifications for firmware update jobs."""

import asyncio
import ipaddress
import logging
import socket
from typing import Optional

from . import database as db

logger = logging.getLogger(__name__)

# Enterprise OID for Tachyon Firmware Updater (under private enterprise arc)
# Using 1.3.6.1.4.1.99999 as a placeholder enterprise number
ENTERPRISE_OID = "1.3.6.1.4.1.99999"

# Trap OIDs under our enterprise
OID_TRAP_JOB_COMPLETED = f"{ENTERPRISE_OID}.1.1"  # Job completed
OID_TRAP_TEST = f"{ENTERPRISE_OID}.1.99"           # Test trap

# Varbind OIDs for job details
OID_JOB_ID = f"{ENTERPRISE_OID}.2.1"
OID_JOB_STATUS = f"{ENTERPRISE_OID}.2.2"
OID_SUCCESS_COUNT = f"{ENTERPRISE_OID}.2.3"
OID_FAILED_COUNT = f"{ENTERPRISE_OID}.2.4"
OID_SKIPPED_COUNT = f"{ENTERPRISE_OID}.2.5"
OID_CANCELLED_COUNT = f"{ENTERPRISE_OID}.2.6"
OID_DURATION_SECONDS = f"{ENTERPRISE_OID}.2.7"
OID_FIRMWARE_NAME = f"{ENTERPRISE_OID}.2.8"
OID_JOB_TYPE = f"{ENTERPRISE_OID}.2.9"
OID_FAILED_DEVICES = f"{ENTERPRISE_OID}.2.10"
OID_ROLLOUT_PHASE = f"{ENTERPRISE_OID}.2.11"
OID_ROLLOUT_STATUS = f"{ENTERPRISE_OID}.2.12"
OID_MESSAGE = f"{ENTERPRISE_OID}.2.99"

# Device status trap OIDs
OID_TRAP_DEVICE_OFFLINE = f"{ENTERPRISE_OID}.1.2"
OID_TRAP_DEVICE_RECOVERED = f"{ENTERPRISE_OID}.1.3"
OID_DEVICE_IP = f"{ENTERPRISE_OID}.3.1"
OID_DEVICE_TYPE = f"{ENTERPRISE_OID}.3.2"
OID_DEVICE_ERROR = f"{ENTERPRISE_OID}.3.3"

DEFAULT_TRAP_PORT = 162
DEFAULT_COMMUNITY = "public"


def is_pysnmp_available() -> bool:
    """Check if pysnmp-lextudio is installed and importable."""
    try:
        import pysnmp.hlapi.v1arch.asyncio  # noqa: F401
        return True
    except ImportError:
        return False


def is_valid_trap_host(host: str) -> bool:
    """Validate an SNMP trap destination (IP or hostname)."""
    if not host or not host.strip():
        return False
    host = host.strip()
    # Check if it's a valid IP address
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # Check if it's a valid hostname (basic check)
    if len(host) > 253:
        return False
    import re
    return bool(re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$', host))


def _get_snmp_config() -> Optional[dict]:
    """Read SNMP trap settings from DB. Returns None if not configured."""
    enabled = db.get_setting("snmp_traps_enabled", "false")
    if enabled != "true":
        return None
    host = db.get_setting("snmp_trap_host", "")
    if not host:
        return None
    try:
        port = int(db.get_setting("snmp_trap_port", str(DEFAULT_TRAP_PORT)))
    except (ValueError, TypeError):
        port = DEFAULT_TRAP_PORT
    return {
        "host": host,
        "port": port,
        "community": db.get_setting("snmp_trap_community", DEFAULT_COMMUNITY),
        "version": db.get_setting("snmp_trap_version", "2c"),
    }


async def send_snmp_trap(trap_oid: str, varbinds: list[tuple[str, str, str]],
                         config: Optional[dict] = None) -> bool:
    """Send an SNMPv2c trap to the configured destination.

    Args:
        trap_oid: The trap OID identifying the event type
        varbinds: List of (oid, type, value) tuples. Type is 's' for string,
                  'i' for integer, 'u' for unsigned.
        config: Optional config override (for testing). If None, reads from DB.

    Returns True if sent successfully, False otherwise.
    """
    if config is None:
        config = _get_snmp_config()
    if not config:
        return False

    try:
        from pysnmp.hlapi.v1arch.asyncio import (
            CommunityData,
            ObjectIdentity,
            ObjectType,
            OctetString,
            Integer32,
            SnmpEngine,
            UdpTransportTarget,
            send_notification,
        )

        transport = await UdpTransportTarget.create((config["host"], config["port"]))

        # Build varbind list
        var_bind_list = []
        for oid, vtype, value in varbinds:
            if vtype == 'i':
                var_bind_list.append(
                    ObjectType(ObjectIdentity(oid), Integer32(int(value)))
                )
            else:
                var_bind_list.append(
                    ObjectType(ObjectIdentity(oid), OctetString(str(value)))
                )

        error_indication, error_status, error_index, var_binds = await send_notification(
            SnmpEngine(),
            CommunityData(config["community"]),
            transport,
            "trap",
            ObjectIdentity(trap_oid),
            *var_bind_list,
        )

        if error_indication:
            logger.warning(f"SNMP trap error: {error_indication}")
            return False

        logger.info(f"SNMP trap sent to {config['host']}:{config['port']}")
        return True

    except ImportError:
        logger.error("pysnmp-lextudio is not installed. Install with: pip install pysnmp-lextudio")
        return False
    except Exception as e:
        logger.error(f"Failed to send SNMP trap: {e}")
        return False


async def notify_job_completed(
    job_id: str,
    success_count: int,
    failed_count: int,
    skipped_count: int,
    cancelled_count: int,
    duration_seconds: float,
    devices: dict,
    firmware_name: str,
    is_scheduled: bool = False,
    rollout_info: Optional[dict] = None,
    next_job_info: Optional[dict] = None,
):
    """Send an SNMP trap when a firmware update job completes.

    Follows the same signature as slack.notify_job_completed for consistency.
    """
    config = _get_snmp_config()
    if not config:
        return

    # Determine job status
    if failed_count > 0:
        status = "completed_with_failures"
    elif success_count == 0 and skipped_count > 0:
        status = "all_skipped"
    else:
        status = "success"

    job_type = "scheduled" if is_scheduled else "manual"

    # Build failed device summary
    failed_devices = []
    for ip, info in devices.items():
        if info.get("status") == "failed":
            failed_devices.append(f"{ip}: {info.get('error', 'Unknown')}")

    varbinds = [
        (OID_JOB_ID, 's', job_id),
        (OID_JOB_STATUS, 's', status),
        (OID_SUCCESS_COUNT, 'i', str(success_count)),
        (OID_FAILED_COUNT, 'i', str(failed_count)),
        (OID_SKIPPED_COUNT, 'i', str(skipped_count)),
        (OID_CANCELLED_COUNT, 'i', str(cancelled_count)),
        (OID_DURATION_SECONDS, 'i', str(int(duration_seconds))),
        (OID_FIRMWARE_NAME, 's', firmware_name),
        (OID_JOB_TYPE, 's', job_type),
    ]

    if failed_devices:
        varbinds.append((OID_FAILED_DEVICES, 's', "; ".join(failed_devices[:10])))

    if rollout_info:
        varbinds.append((OID_ROLLOUT_PHASE, 's', rollout_info.get("phase", "")))
        varbinds.append((OID_ROLLOUT_STATUS, 's', rollout_info.get("status", "")))

    # Fire and forget
    asyncio.create_task(_send_with_retry(OID_TRAP_JOB_COMPLETED, varbinds, config))


async def _send_with_retry(trap_oid: str, varbinds: list, config: dict,
                           max_retries: int = 2):
    """Send trap with retry on failure."""
    for attempt in range(max_retries + 1):
        success = await send_snmp_trap(trap_oid, varbinds, config=config)
        if success:
            return
        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)
    logger.error(f"SNMP trap delivery failed after {max_retries + 1} attempts to {config.get('host')}:{config.get('port')}")


async def send_test_trap() -> tuple[bool, str]:
    """Send a test SNMP trap to verify configuration.

    Returns (success, message) tuple.
    """
    if not is_pysnmp_available():
        return False, "pysnmp-lextudio is not installed. Install with: pip install pysnmp-lextudio"

    config = _get_snmp_config()
    if not config:
        return False, "SNMP traps not configured or not enabled"

    if not is_valid_trap_host(config["host"]):
        return False, f"Invalid trap host: {config['host']}"

    varbinds = [
        (OID_MESSAGE, 's', "Test trap from SixtyOps Firmware Updater"),
    ]

    success = await send_snmp_trap(OID_TRAP_TEST, varbinds, config=config)
    if success:
        return True, f"Test trap sent to {config['host']}:{config['port']}"
    else:
        return False, "Failed to send test trap - check host and network connectivity"


async def notify_device_offline(ip: str, device_type: str, error: str):
    """Send an SNMP trap when a device goes offline."""
    config = _get_snmp_config()
    if not config:
        return

    varbinds = [
        (OID_DEVICE_IP, 's', ip),
        (OID_DEVICE_TYPE, 's', device_type),
        (OID_DEVICE_ERROR, 's', error[:200] if error else "Unknown"),
    ]

    asyncio.create_task(_send_with_retry(OID_TRAP_DEVICE_OFFLINE, varbinds, config=config))


async def notify_device_recovered(ip: str, device_type: str):
    """Send an SNMP trap when a device recovers from offline state."""
    config = _get_snmp_config()
    if not config:
        return

    varbinds = [
        (OID_DEVICE_IP, 's', ip),
        (OID_DEVICE_TYPE, 's', device_type),
    ]

    asyncio.create_task(_send_with_retry(OID_TRAP_DEVICE_RECOVERED, varbinds, config=config))
