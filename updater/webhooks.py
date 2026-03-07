"""Generic HTTP webhook notifications for firmware update events."""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx

from . import database as db

logger = logging.getLogger(__name__)


def is_valid_webhook_url(url: str) -> bool:
    """Validate that a URL is a valid HTTP(S) webhook endpoint."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.hostname is not None
    except Exception:
        return False


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Generate HMAC-SHA256 signature for webhook payload."""
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


async def send_webhook(event_type: str, payload: dict) -> bool:
    """Send a generic HTTP webhook with optional HMAC signing.

    Returns True if sent successfully, False otherwise.
    """
    if db.get_setting("webhook_enabled", "false") != "true":
        return False

    url = db.get_setting("webhook_url", "")
    if not url or not is_valid_webhook_url(url):
        return False

    # Check if this event type is enabled
    enabled_events = db.get_setting("webhook_events", "").split(",")
    enabled_events = [e.strip() for e in enabled_events if e.strip()]
    if enabled_events and event_type not in enabled_events:
        return False

    method = db.get_setting("webhook_method", "POST").upper()
    if method not in ("POST", "PUT"):
        method = "POST"

    # Build headers
    headers = {"Content-Type": "application/json"}
    custom_headers_str = db.get_setting("webhook_headers", "{}")
    try:
        custom_headers = json.loads(custom_headers_str)
        if isinstance(custom_headers, dict):
            headers.update(custom_headers)
    except (json.JSONDecodeError, TypeError):
        pass

    # Add event type header
    headers["X-Webhook-Event"] = event_type

    # Build body
    body = {
        "event": event_type,
        "timestamp": datetime.now().isoformat(),
        "data": payload,
    }
    body_bytes = json.dumps(body).encode()

    # HMAC signing
    secret = db.get_setting("webhook_secret", "")
    if secret:
        headers["X-Webhook-Signature"] = f"sha256={_sign_payload(body_bytes, secret)}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.request(method, url, content=body_bytes, headers=headers)
            if 200 <= response.status_code < 300:
                logger.info(f"Webhook sent successfully: {event_type}")
                return True
            else:
                logger.warning(f"Webhook returned status {response.status_code}")
                return False
    except Exception as e:
        logger.error(f"Failed to send webhook: {e}")
        return False


async def _send_with_retry(event_type: str, payload: dict, max_retries: int = 2):
    """Send webhook with retry on failure."""
    for attempt in range(max_retries + 1):
        success = await send_webhook(event_type, payload)
        if success:
            return
        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)
    logger.warning(f"Failed to send webhook '{event_type}' after retries")


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
):
    """Send a webhook when a firmware update job completes."""
    if db.get_setting("webhook_enabled", "false") != "true":
        return

    event_type = "job_failed" if failed_count > 0 else "job_completed"

    failed_devices = []
    for ip, info in devices.items():
        if info.get("status") == "failed":
            failed_devices.append({
                "ip": ip,
                "role": info.get("role", "device"),
                "error": info.get("error", "Unknown error"),
            })

    payload = {
        "job_id": job_id,
        "status": "completed_with_failures" if failed_count > 0 else "success",
        "job_type": "scheduled" if is_scheduled else "manual",
        "firmware": firmware_name,
        "duration_seconds": round(duration_seconds, 1),
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "cancelled_count": cancelled_count,
        "total_devices": success_count + failed_count + skipped_count + cancelled_count,
        "failed_devices": failed_devices[:10],
    }

    asyncio.create_task(_send_with_retry(event_type, payload))


async def notify_device_offline(ip: str, device_type: str, error: str):
    """Send a webhook when a device goes offline."""
    if db.get_setting("webhook_enabled", "false") != "true":
        return

    payload = {
        "ip": ip,
        "device_type": device_type,
        "error": error,
    }
    asyncio.create_task(_send_with_retry("device_offline", payload))


async def notify_device_recovered(ip: str, device_type: str):
    """Send a webhook when a device recovers from offline state."""
    if db.get_setting("webhook_enabled", "false") != "true":
        return

    payload = {
        "ip": ip,
        "device_type": device_type,
    }
    asyncio.create_task(_send_with_retry("device_recovered", payload))


async def send_test_webhook() -> tuple[bool, str]:
    """Send a test webhook to verify configuration.

    Returns (success, message) tuple.
    """
    if db.get_setting("webhook_enabled", "false") != "true":
        return False, "Webhooks are not enabled"

    url = db.get_setting("webhook_url", "")
    if not url:
        return False, "No webhook URL configured"

    if not is_valid_webhook_url(url):
        return False, "Invalid webhook URL"

    payload = {
        "message": "Test webhook from SixtyOps firmware updater",
        "test": True,
    }

    success = await send_webhook("test", payload)
    if success:
        return True, "Test webhook sent successfully"
    else:
        return False, "Failed to send test webhook - check URL and configuration"
