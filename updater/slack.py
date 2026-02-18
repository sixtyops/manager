"""Slack webhook notifications for firmware update jobs."""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx

from . import database as db

logger = logging.getLogger(__name__)


def is_valid_slack_url(url: str) -> bool:
    """Validate that a URL is a legitimate Slack webhook."""
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.hostname.endswith(".slack.com"))
    except Exception:
        return False


async def send_slack_notification(payload: dict) -> bool:
    """Send a notification to the configured Slack webhook.

    Returns True if sent successfully, False otherwise.
    """
    webhook_url = db.get_setting("slack_webhook_url", "")
    if not webhook_url:
        return False

    if not is_valid_slack_url(webhook_url):
        logger.warning("Slack webhook URL rejected: not a valid https://hooks.slack.com/ URL")
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json=payload)
            if response.status_code == 200:
                logger.info("Slack notification sent successfully")
                return True
            else:
                logger.warning(f"Slack webhook returned status {response.status_code}")
                return False
    except Exception as e:
        logger.error(f"Failed to send Slack notification: {e}")
        return False


def _format_duration(seconds: float) -> str:
    """Format duration in human-readable form."""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h {mins}m"


def _build_device_summary(devices: dict) -> str:
    """Build a summary of device results for the message."""
    failed_devices = []
    for ip, info in devices.items():
        if info.get("status") == "failed":
            error = info.get("error", "Unknown error")
            role = info.get("role", "device")
            failed_devices.append(f"• {ip} ({role}): {error}")

    if failed_devices:
        return "\n".join(failed_devices[:10])  # Limit to 10 to avoid huge messages
    return ""


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
    """Send a Slack notification when a job completes.

    Args:
        job_id: The job identifier
        success_count: Number of successful updates
        failed_count: Number of failed updates
        skipped_count: Number of skipped devices
        cancelled_count: Number of cancelled updates
        duration_seconds: How long the job took
        devices: Dict of device IP -> status info
        firmware_name: Name of the firmware file used
        is_scheduled: Whether this was an automatic scheduled job
        rollout_info: Current rollout state (phase, progress, etc.)
        next_job_info: Information about the next scheduled job
    """
    webhook_url = db.get_setting("slack_webhook_url", "")
    if not webhook_url:
        return

    # Determine status emoji and color
    if failed_count > 0:
        status_emoji = ":warning:"
        color = "#f59e0b"  # Amber
        status_text = "Completed with failures"
    elif success_count == 0 and skipped_count > 0:
        status_emoji = ":fast_forward:"
        color = "#6b7280"  # Gray
        status_text = "All devices skipped"
    else:
        status_emoji = ":white_check_mark:"
        color = "#10b981"  # Green
        status_text = "Completed successfully"

    job_type = "Scheduled Update" if is_scheduled else "Manual Update"

    # Build the main message blocks
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{status_emoji} Firmware Update {status_text}",
                "emoji": True,
            }
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Job Type:*\n{job_type}"},
                {"type": "mrkdwn", "text": f"*Firmware:*\n{firmware_name}"},
                {"type": "mrkdwn", "text": f"*Duration:*\n{_format_duration(duration_seconds)}"},
                {"type": "mrkdwn", "text": f"*Job ID:*\n`{job_id[:8]}`"},
            ]
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Success:*\n{success_count}"},
                {"type": "mrkdwn", "text": f"*Failed:*\n{failed_count}"},
                {"type": "mrkdwn", "text": f"*Skipped:*\n{skipped_count}"},
                {"type": "mrkdwn", "text": f"*Cancelled:*\n{cancelled_count}"},
            ]
        },
    ]

    # Add failed device details if there are failures
    if failed_count > 0:
        failed_summary = _build_device_summary(devices)
        if failed_summary:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Failed Devices:*\n{failed_summary}"
                }
            })

    # Add rollout progress if this is a scheduled job
    if is_scheduled and rollout_info:
        phase = rollout_info.get("phase", "unknown")
        status = rollout_info.get("status", "unknown")
        progress = rollout_info.get("progress", {})

        total = progress.get("total", 0)
        updated = progress.get("updated", 0)
        pending = progress.get("pending", 0)

        phase_display = {
            "canary": "Canary (1 device)",
            "pct10": "10% Rollout",
            "pct50": "50% Rollout",
            "pct100": "100% Rollout",
        }.get(phase, phase)

        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Rollout Progress:*\nPhase: {phase_display}\nStatus: {status.capitalize()}\nDevices: {updated}/{total} updated, {pending} pending"
            }
        })

        # If rollout was paused due to failures
        if status == "paused":
            pause_reason = rollout_info.get("pause_reason", "Unknown")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":pause_button: *Rollout Paused:* {pause_reason}\n_Resume from the web UI after investigating failures._"
                }
            })

    # Add next job information
    if next_job_info:
        next_window = next_job_info.get("next_window", "")
        next_phase = next_job_info.get("next_phase", "")
        estimated_devices = next_job_info.get("estimated_devices", 0)
        estimated_completion = next_job_info.get("estimated_completion", "")

        next_text_parts = []
        if next_window:
            next_text_parts.append(f"*Next Window:* {next_window}")
        if next_phase:
            phase_display = {
                "canary": "Canary (1 device)",
                "pct10": "10% Rollout",
                "pct50": "50% Rollout",
                "pct100": "100% Rollout",
            }.get(next_phase, next_phase)
            next_text_parts.append(f"*Next Phase:* {phase_display}")
        if estimated_devices:
            next_text_parts.append(f"*Estimated Devices:* {estimated_devices}")
        if estimated_completion:
            next_text_parts.append(f"*Est. Completion:* {estimated_completion}")

        if next_text_parts:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Next Scheduled Job:*\n" + "\n".join(next_text_parts)
                }
            })

    # Add timestamp
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"Completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            }
        ]
    })

    payload = {
        "blocks": blocks,
        "attachments": [{"color": color, "blocks": []}]  # Color bar
    }

    # Fire and forget - don't block job completion on webhook
    asyncio.create_task(_send_with_retry(payload))


async def _send_with_retry(payload: dict, max_retries: int = 2):
    """Send webhook with retry on failure."""
    for attempt in range(max_retries + 1):
        success = await send_slack_notification(payload)
        if success:
            return
        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
    logger.warning("Failed to send Slack notification after retries")


async def send_test_notification() -> tuple[bool, str]:
    """Send a test notification to verify webhook configuration.

    Returns (success, message) tuple.
    """
    webhook_url = db.get_setting("slack_webhook_url", "")
    if not webhook_url:
        return False, "No Slack webhook URL configured"

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":test_tube: Firmware Updater Test Notification",
                    "emoji": True,
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "This is a test notification from the Tachyon Firmware Updater.\nIf you see this, your Slack webhook is configured correctly!"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    }
                ]
            }
        ]
    }

    success = await send_slack_notification(payload)
    if success:
        return True, "Test notification sent successfully"
    else:
        return False, "Failed to send test notification - check webhook URL"
