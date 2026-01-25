"""Email notifications via SMTP for job completions and alerts."""

import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from . import database as db

logger = logging.getLogger(__name__)


def _get_config() -> dict:
    """Read email notification config from settings."""
    settings = db.get_all_settings()
    return {
        "enabled": settings.get("email_enabled", "false") == "true",
        "smtp_host": settings.get("email_smtp_host", ""),
        "smtp_port": int(settings.get("email_smtp_port", "587")),
        "smtp_username": settings.get("email_smtp_username", ""),
        "smtp_password": settings.get("email_smtp_password", ""),
        "smtp_tls": settings.get("email_smtp_tls", "true") == "true",
        "from_address": settings.get("email_from_address", ""),
        "to_addresses": settings.get("email_to_addresses", ""),
    }


def _parse_recipients(to_addresses: str) -> list[str]:
    """Parse comma-separated email addresses."""
    return [addr.strip() for addr in to_addresses.split(",") if addr.strip()]


def _send_email(config: dict, subject: str, body_html: str, body_text: str) -> tuple[bool, str]:
    """Send an email using the configured SMTP server."""
    if not config["enabled"]:
        return False, "Email notifications not enabled"
    if not config["smtp_host"]:
        return False, "SMTP host not configured"
    if not config["from_address"]:
        return False, "From address not configured"

    recipients = _parse_recipients(config["to_addresses"])
    if not recipients:
        return False, "No recipient addresses configured"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["from_address"]
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        if config["smtp_tls"]:
            context = ssl.create_default_context()
            server = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=15)
            server.starttls(context=context)
        else:
            server = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=15)

        if config["smtp_username"] and config["smtp_password"]:
            server.login(config["smtp_username"], config["smtp_password"])

        server.sendmail(config["from_address"], recipients, msg.as_string())
        server.quit()
        return True, f"Email sent to {', '.join(recipients)}"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed"
    except smtplib.SMTPConnectError:
        return False, f"Could not connect to {config['smtp_host']}:{config['smtp_port']}"
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False, f"Failed to send email: {e}"


async def notify_job_completed(
    job_id: str,
    success_count: int,
    failed_count: int,
    skipped_count: int,
    cancelled_count: int,
    duration_seconds: float,
    firmware_name: str,
    is_scheduled: bool = False,
    **kwargs,
):
    """Send email notification for job completion."""
    config = _get_config()
    if not config["enabled"]:
        return

    total = success_count + failed_count + skipped_count + cancelled_count
    status_emoji = "Success" if failed_count == 0 else "Failures Detected"
    trigger = "Scheduled" if is_scheduled else "Manual"
    minutes = int(duration_seconds // 60)
    seconds = int(duration_seconds % 60)

    subject = f"[SixtyOps] Job {job_id} {status_emoji} - {success_count}/{total} devices updated"

    body_text = (
        f"Firmware Update Job {job_id}\n"
        f"Status: {status_emoji}\n"
        f"Trigger: {trigger}\n"
        f"Firmware: {firmware_name}\n"
        f"Duration: {minutes}m {seconds}s\n\n"
        f"Results:\n"
        f"  Success: {success_count}\n"
        f"  Failed: {failed_count}\n"
        f"  Skipped: {skipped_count}\n"
        f"  Cancelled: {cancelled_count}\n"
    )

    body_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: {'#22c55e' if failed_count == 0 else '#ef4444'};">
            Firmware Update Job {job_id}
        </h2>
        <table style="border-collapse: collapse; width: 100%;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Status</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{status_emoji}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Trigger</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{trigger}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Firmware</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{firmware_name}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Duration</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{minutes}m {seconds}s</td></tr>
        </table>
        <h3>Results</h3>
        <table style="border-collapse: collapse;">
            <tr><td style="padding: 4px 12px;">Success</td>
                <td style="padding: 4px 12px; color: #22c55e;"><strong>{success_count}</strong></td></tr>
            <tr><td style="padding: 4px 12px;">Failed</td>
                <td style="padding: 4px 12px; color: #ef4444;"><strong>{failed_count}</strong></td></tr>
            <tr><td style="padding: 4px 12px;">Skipped</td>
                <td style="padding: 4px 12px;">{skipped_count}</td></tr>
            <tr><td style="padding: 4px 12px;">Cancelled</td>
                <td style="padding: 4px 12px;">{cancelled_count}</td></tr>
        </table>
    </div>
    """

    success, message = _send_email(config, subject, body_html, body_text)
    if not success:
        logger.warning(f"Email notification failed for job {job_id}: {message}")


async def notify_device_offline(ip: str, device_type: str, error: str):
    """Send email notification when a device goes offline."""
    config = _get_config()
    if not config["enabled"]:
        return

    subject = f"[SixtyOps] Device Offline: {ip}"
    body_text = (
        f"Device Offline Alert\n\n"
        f"Device: {ip} ({device_type})\n"
        f"Error: {error}\n"
    )
    body_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #ef4444;">Device Offline</h2>
        <table style="border-collapse: collapse; width: 100%;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Device</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{ip} ({device_type})</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Error</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{error}</td></tr>
        </table>
    </div>
    """
    success, message = _send_email(config, subject, body_html, body_text)
    if not success:
        logger.warning(f"Email device-offline notification failed for {ip}: {message}")


async def notify_device_recovered(ip: str, device_type: str):
    """Send email notification when a device recovers."""
    config = _get_config()
    if not config["enabled"]:
        return

    subject = f"[SixtyOps] Device Recovered: {ip}"
    body_text = (
        f"Device Recovered\n\n"
        f"Device: {ip} ({device_type})\n"
        f"Status: Back online\n"
    )
    body_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #22c55e;">Device Recovered</h2>
        <table style="border-collapse: collapse; width: 100%;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Device</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{ip} ({device_type})</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Status</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">Back online</td></tr>
        </table>
    </div>
    """
    success, message = _send_email(config, subject, body_html, body_text)
    if not success:
        logger.warning(f"Email device-recovered notification failed for {ip}: {message}")


def send_test_email() -> tuple[bool, str]:
    """Send a test email to verify SMTP configuration."""
    config = _get_config()
    subject = "[SixtyOps] Test Email Notification"
    body_text = "This is a test email from SixtyOps Firmware Updater."
    body_html = """
    <div style="font-family: Arial, sans-serif;">
        <h2>SixtyOps Email Notification Test</h2>
        <p>This is a test email from SixtyOps Firmware Updater.</p>
        <p>If you received this, email notifications are working correctly.</p>
    </div>
    """
    return _send_email(config, subject, body_html, body_text)


def get_status() -> dict:
    """Return current email notification status."""
    config = _get_config()
    return {
        "enabled": config["enabled"],
        "smtp_host": config["smtp_host"],
        "smtp_port": config["smtp_port"],
        "smtp_tls": config["smtp_tls"],
        "from_address": config["from_address"],
        "to_addresses": config["to_addresses"],
        "has_credentials": bool(config["smtp_username"] and config["smtp_password"]),
    }
