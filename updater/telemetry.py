"""Telemetry module for sending anonymized job statistics to AWS Lambda.

================================================================================
                            TELEMETRY DISCLOSURE
================================================================================

This application collects anonymous usage telemetry after firmware update jobs
complete. This data helps us improve the firmware updater by understanding:
- Which device models are being updated (so we can prioritize support)
- Common failure patterns (so we can improve error handling)
- General usage patterns (job sizes, scheduling usage)

--------------------------------------------------------------------------------
EXACTLY WHAT IS SENT (nothing more):
--------------------------------------------------------------------------------
  - Timestamp of job completion
  - Anonymous install ID (hashed, cannot identify you or your network)
  - Total device count and success/failure/skipped/cancelled counts
  - Job duration in seconds
  - Bank mode used ("both" or "single")
  - Whether job was scheduled or manual
  - Device model names and count per model (e.g., {"T5c": 30, "T5c+": 15})
  - Error categories (e.g., {"timeout": 2, "connection_error": 1})
    * Errors are CATEGORIZED, not sent verbatim
  - Counts broken down by device role (AP, CPE, switch)

--------------------------------------------------------------------------------
WHAT IS NEVER SENT:
--------------------------------------------------------------------------------
  - IP addresses
  - MAC addresses
  - Serial numbers
  - Hostnames or network names
  - Usernames, passwords, or credentials
  - Location or timezone information
  - Raw error messages
  - Any data that could identify you, your network, or your devices

--------------------------------------------------------------------------------
HOW TO DISABLE TELEMETRY:
--------------------------------------------------------------------------------
Set the DISABLE_TELEMETRY environment variable, then restart the app.
In docker-compose.yml, add under the tachyon-mgmt service:
  environment:
    - DISABLE_TELEMETRY=1

--------------------------------------------------------------------------------
WHY WE COLLECT THIS:
--------------------------------------------------------------------------------
  1. To identify device models we don't recognize and add support for them
  2. To understand common failure modes and improve reliability
  3. To prioritize development based on actual usage patterns
  4. We do NOT sell this data or share it with third parties

================================================================================
"""

import asyncio
import hashlib
import logging
import os
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# AWS Lambda endpoint for telemetry
TELEMETRY_ENDPOINT = "https://yv7ychij6cpafomyrxy627o7iu0phbdx.lambda-url.us-east-1.on.aws/"

# Telemetry is enabled by default. To disable, set the DISABLE_TELEMETRY
# environment variable (e.g., DISABLE_TELEMETRY=1 in docker-compose.yml).
# See the TELEMETRY DISCLOSURE above for details on what is sent.
TELEMETRY_ENABLED = os.environ.get("DISABLE_TELEMETRY", "").lower() not in ("1", "true", "yes")


def _generate_anonymous_install_id() -> str:
    """Generate a consistent but anonymous installation identifier.

    This creates a hash that's consistent for the same machine but
    cannot be traced back to any identifying information.
    """
    import platform
    # Use machine-level info that doesn't identify the user
    machine_info = f"{platform.system()}-{platform.machine()}"
    return hashlib.sha256(machine_info.encode()).hexdigest()[:16]


def _aggregate_model_stats(devices: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate model statistics from device results.

    Returns counts per model and flags unknown models.
    """
    model_counts: Counter = Counter()
    unknown_models: List[str] = []

    for device_data in devices.values():
        model = device_data.get("model")
        if model:
            model_counts[model] += 1
        else:
            model_counts["unknown"] += 1

    # Check for models that might be unknown/unrecognized patterns
    known_model_prefixes = ["T5c", "T5c+", "T-52"]  # Add known model patterns

    for model, count in model_counts.items():
        if model != "unknown":
            is_known = any(model.startswith(prefix) for prefix in known_model_prefixes)
            if not is_known:
                unknown_models.append(model)

    return {
        "model_distribution": dict(model_counts),
        "unknown_model_count": model_counts.get("unknown", 0),
        "unrecognized_models": unknown_models,  # Models that don't match known patterns
        "total_models_seen": len(model_counts),
    }


def _aggregate_error_types(devices: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Categorize and count error types without exposing specific details.

    Groups errors into categories rather than sending raw error messages.
    """
    error_categories: Counter = Counter()

    for device_data in devices.values():
        error = device_data.get("error")
        if error:
            # Categorize errors without exposing specific details
            error_lower = error.lower()
            if "timeout" in error_lower or "timed out" in error_lower:
                error_categories["timeout"] += 1
            elif "connection" in error_lower or "connect" in error_lower:
                error_categories["connection_error"] += 1
            elif "auth" in error_lower or "login" in error_lower or "credential" in error_lower:
                error_categories["authentication_error"] += 1
            elif "upload" in error_lower:
                error_categories["upload_error"] += 1
            elif "install" in error_lower:
                error_categories["install_error"] += 1
            elif "reboot" in error_lower:
                error_categories["reboot_error"] += 1
            elif "version" in error_lower or "verify" in error_lower:
                error_categories["verification_error"] += 1
            else:
                error_categories["other_error"] += 1

    return dict(error_categories)


def _aggregate_role_stats(devices: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Aggregate success/failure counts by device role."""
    role_stats: Dict[str, Counter] = {
        "ap": Counter(),
        "cpe": Counter(),
        "switch": Counter(),
    }

    for device_data in devices.values():
        role = device_data.get("role", "ap")
        status = device_data.get("status", "unknown")
        if role in role_stats:
            role_stats[role][status] += 1

    return {role: dict(counts) for role, counts in role_stats.items()}


def build_telemetry_payload(
    job_id: str,
    success_count: int,
    failed_count: int,
    skipped_count: int,
    cancelled_count: int,
    duration_seconds: float,
    bank_mode: str,
    is_scheduled: bool,
    devices: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Build an anonymized telemetry payload from job results.

    This function aggregates job statistics without including any
    personally identifiable or device-specific information.
    """
    total_devices = success_count + failed_count + skipped_count + cancelled_count

    # Aggregate model statistics
    model_stats = _aggregate_model_stats(devices)

    # Aggregate error categories
    error_stats = _aggregate_error_types(devices)

    # Aggregate by device role
    role_stats = _aggregate_role_stats(devices)

    return {
        "event": "job_completed",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "install_id": _generate_anonymous_install_id(),

        # Job summary (no identifiers)
        "job": {
            "total_devices": total_devices,
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "cancelled_count": cancelled_count,
            "success_rate": round(success_count / total_devices, 3) if total_devices > 0 else 0,
            "duration_seconds": round(duration_seconds, 1),
            "bank_mode": bank_mode,
            "is_scheduled": is_scheduled,
        },

        # Model information
        "models": model_stats,

        # Error categorization
        "errors": error_stats,

        # Per-role breakdown
        "by_role": role_stats,
    }


async def send_telemetry(
    job_id: str,
    success_count: int,
    failed_count: int,
    skipped_count: int,
    cancelled_count: int,
    duration_seconds: float,
    bank_mode: str,
    is_scheduled: bool,
    devices: Dict[str, Dict[str, Any]],
) -> bool:
    """Send anonymized telemetry to AWS Lambda after job completion.

    This is a fire-and-forget operation that should not block the main flow.
    Any errors are logged but do not affect the job completion.

    Returns True if telemetry was sent successfully, False otherwise.
    """
    if not TELEMETRY_ENABLED:
        logger.debug("Telemetry disabled, skipping send")
        return False

    if not TELEMETRY_ENDPOINT:
        logger.debug("Telemetry endpoint not configured, skipping send")
        return False

    try:
        payload = build_telemetry_payload(
            job_id=job_id,
            success_count=success_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            cancelled_count=cancelled_count,
            duration_seconds=duration_seconds,
            bank_mode=bank_mode,
            is_scheduled=is_scheduled,
            devices=devices,
        )

        logger.debug(f"Sending telemetry for job (devices: {payload['job']['total_devices']})")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                TELEMETRY_ENDPOINT,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status == 200:
                    logger.debug("Telemetry sent successfully")
                    return True
                else:
                    logger.warning(f"Telemetry endpoint returned status {response.status}")
                    return False

    except asyncio.TimeoutError:
        logger.warning("Telemetry request timed out")
        return False
    except Exception as e:
        logger.warning(f"Failed to send telemetry: {e}")
        return False


async def send_telemetry_background(
    job_id: str,
    success_count: int,
    failed_count: int,
    skipped_count: int,
    cancelled_count: int,
    duration_seconds: float,
    bank_mode: str,
    is_scheduled: bool,
    devices: Dict[str, Dict[str, Any]],
) -> None:
    """Wrapper to send telemetry as a background task.

    Use this with asyncio.create_task() to avoid blocking job completion.
    """
    await send_telemetry(
        job_id=job_id,
        success_count=success_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        cancelled_count=cancelled_count,
        duration_seconds=duration_seconds,
        bank_mode=bank_mode,
        is_scheduled=is_scheduled,
        devices=devices,
    )
