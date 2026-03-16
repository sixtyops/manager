"""SFTP-based backup system for database, settings, and device configs."""

import asyncio
import io
import json
import logging
import sqlite3
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import asyncssh

from . import database as db
from .crypto import encrypt_password, decrypt_password, is_encrypted

logger = logging.getLogger(__name__)

# Paths - match docker-compose volume mounts
STAGING_DIR = Path("/app/backups")
DATA_DIR = Path("/app/data")
DB_FILE = DATA_DIR / "sixtyops.db"
SSH_KEY_PATH = Path("/app/.ssh/backup_key")

# Prevent concurrent backup runs
_backup_lock = asyncio.Lock()


def get_backup_status() -> dict:
    """Get current backup configuration and status."""
    settings = db.get_all_settings()
    host = settings.get("backup_sftp_host", "")
    port = settings.get("backup_sftp_port", "22")
    username = settings.get("backup_sftp_username", "")
    path = settings.get("backup_sftp_path", "")
    return {
        "enabled": settings.get("backup_enabled") == "true",
        "sftp_host": host,
        "sftp_port": port,
        "sftp_path": path,
        "sftp_username": username,
        "sftp_display": f"{username}@{host}:{port}{path}" if host else "",
        "auth_method": settings.get("backup_sftp_auth_method", "password"),
        "retention_count": settings.get("backup_retention_count", "30"),
        "last_run": settings.get("backup_last_run", ""),
        "last_status": settings.get("backup_last_status", ""),
    }


async def configure_backup(
    host: str,
    port: int,
    path: str,
    username: str,
    auth_method: str,
    password: Optional[str] = None,
    ssh_key: Optional[str] = None,
    retention_count: int = 30,
) -> Tuple[bool, str]:
    """Save SFTP backup configuration and test connection.

    Returns (success, message).
    """
    if not host or not username:
        return False, "Host and username are required"

    if auth_method == "password" and not password:
        # Allow keeping existing password on reconfigure
        existing = db.get_setting("backup_sftp_password")
        if not existing:
            return False, "Password is required for password authentication"
        password = None  # signal to skip overwriting
    if auth_method == "key" and not ssh_key:
        return False, "SSH key is required for key authentication"

    # Store SSH key if provided
    if auth_method == "key" and ssh_key:
        SSH_KEY_PATH.parent.mkdir(mode=0o700, exist_ok=True)
        SSH_KEY_PATH.write_text(ssh_key.strip() + "\n")
        SSH_KEY_PATH.chmod(0o600)

    # Save settings
    db.set_setting("backup_sftp_host", host)
    db.set_setting("backup_sftp_port", str(port))
    db.set_setting("backup_sftp_path", path)
    db.set_setting("backup_sftp_username", username)
    db.set_setting("backup_sftp_auth_method", auth_method)
    if auth_method == "password" and password:
        db.set_setting("backup_sftp_password", encrypt_password(password))
    db.set_setting("backup_retention_count", str(retention_count))

    # Test connection before enabling
    success, msg = await test_backup_connection()
    if not success:
        db.set_setting("backup_enabled", "false")
        return False, f"Configuration saved but connection test failed: {msg}"

    db.set_setting("backup_enabled", "true")
    logger.info(f"SFTP backup configured: {username}@{host}:{port}{path}")
    return True, "SFTP backup configured and connection verified"


async def _get_sftp_connection():
    """Create an asyncssh connection from stored settings."""
    settings = db.get_all_settings()
    host = settings.get("backup_sftp_host", "")
    port = int(settings.get("backup_sftp_port", "22"))
    username = settings.get("backup_sftp_username", "")
    auth_method = settings.get("backup_sftp_auth_method", "password")

    connect_kwargs = {
        "host": host,
        "port": port,
        "username": username,
        "known_hosts": None,
        "login_timeout": 30,
    }

    if auth_method == "key" and SSH_KEY_PATH.exists():
        connect_kwargs["client_keys"] = [str(SSH_KEY_PATH)]
    elif auth_method == "password":
        stored_pw = settings.get("backup_sftp_password", "")
        if stored_pw and is_encrypted(stored_pw):
            stored_pw = decrypt_password(stored_pw)
        connect_kwargs["password"] = stored_pw

    return await asyncssh.connect(**connect_kwargs)


async def test_backup_connection() -> Tuple[bool, str]:
    """Test SFTP connectivity without uploading.

    Returns (success, message).
    """
    try:
        async with await _get_sftp_connection() as conn:
            async with conn.start_sftp_client() as sftp:
                remote_path = db.get_setting("backup_sftp_path") or "/"
                try:
                    await sftp.stat(remote_path)
                except asyncssh.SFTPNoSuchFile:
                    try:
                        await sftp.makedirs(remote_path)
                    except Exception as e:
                        return False, f"Remote path does not exist and could not be created: {e}"
                return True, "Connection successful"
    except asyncssh.PermissionDenied:
        return False, "Authentication failed - check username/password or key"
    except asyncssh.DisconnectError as e:
        return False, f"SSH connection failed: {e}"
    except OSError as e:
        return False, f"Connection error: {e}"
    except Exception as e:
        return False, f"Connection failed: {str(e)[:200]}"


async def run_backup() -> Tuple[bool, str]:
    """Run a full backup and upload to SFTP server.

    Returns (success, message).
    """
    settings = db.get_all_settings()
    if settings.get("backup_enabled") != "true":
        return False, "Backup not configured"
    if not settings.get("backup_sftp_host"):
        return False, "SFTP host not configured"

    if _backup_lock.locked():
        return False, "Backup already in progress"

    async with _backup_lock:
        return await _run_backup_locked()


async def _run_backup_locked() -> Tuple[bool, str]:
    """Build tar.gz archive and upload via SFTP."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    archive_name = f"sixtyops-backup-{timestamp}.tar.gz"
    logger.info(f"Starting backup: {archive_name}")

    try:
        # Build archive in memory
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            _add_database(tar)
            _add_settings(tar)
            _add_device_inventory(tar, timestamp)
            _add_device_configs(tar)

        archive_bytes = buf.getvalue()

        # Upload via SFTP (5 minute timeout for large archives)
        remote_path = db.get_setting("backup_sftp_path") or "/backups/sixtyops"
        async with await _get_sftp_connection() as conn:
            async with conn.start_sftp_client() as sftp:
                # Ensure remote directory exists
                try:
                    await sftp.stat(remote_path)
                except asyncssh.SFTPNoSuchFile:
                    await sftp.makedirs(remote_path)

                remote_file = f"{remote_path}/{archive_name}"
                try:
                    async with asyncio.timeout(300):
                        async with sftp.open(remote_file, "wb") as f:
                            await f.write(archive_bytes)
                except TimeoutError:
                    return False, "Upload timed out after 5 minutes"

                # Enforce retention policy
                await _enforce_retention(sftp, remote_path)

        db.set_setting("backup_last_run", datetime.now().isoformat())
        db.set_setting("backup_last_status", "success")
        logger.info(f"Backup uploaded: {archive_name} ({len(archive_bytes)} bytes)")
        return True, f"Backup completed: {archive_name}"

    except Exception as e:
        error_msg = str(e)[:200]
        db.set_setting("backup_last_run", datetime.now().isoformat())
        db.set_setting("backup_last_status", f"failed: {error_msg}")
        logger.exception(f"Backup failed: {e}")
        return False, f"Backup failed: {error_msg}"


def _add_database(tar: tarfile.TarFile):
    """Add a consistent SQLite snapshot to the archive."""
    if not DB_FILE.exists():
        logger.warning("Database file not found, skipping database backup")
        return

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staging_db = STAGING_DIR / "sixtyops.db"
    try:
        src = sqlite3.connect(str(DB_FILE))
        dst = sqlite3.connect(str(staging_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        tar.add(str(staging_db), arcname="sixtyops.db")
    finally:
        staging_db.unlink(missing_ok=True)


def _add_settings(tar: tarfile.TarFile):
    """Add sanitized settings JSON (no passwords, secrets, tokens, or keys)."""
    settings = db.get_all_settings()
    safe = {k: v for k, v in settings.items()
            if not any(s in k.lower() for s in ["password", "secret", "token", "key"])}
    data = json.dumps(safe, indent=2, sort_keys=True).encode()
    info = tarfile.TarInfo(name="settings.json")
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _add_device_inventory(tar: tarfile.TarFile, timestamp: str):
    """Add device inventory text file (no credentials)."""
    aps = db.get_access_points(enabled_only=False)
    switches = db.get_switches(enabled_only=False)

    lines = [f"# SixtyOps Device Inventory - {timestamp}\n"]
    lines.append(f"\n## Access Points ({len(aps)} total)\n")
    for ap in aps:
        name = ap.get("system_name") or "unnamed"
        model = ap.get("model") or "unknown"
        ver = ap.get("firmware_version") or "unknown"
        lines.append(f"{ap['ip']}\t{name}\t{model}\t{ver}\n")

    lines.append(f"\n## Switches ({len(switches)} total)\n")
    for sw in switches:
        name = sw.get("system_name") or "unnamed"
        model = sw.get("model") or "unknown"
        ver = sw.get("firmware_version") or "unknown"
        lines.append(f"{sw['ip']}\t{name}\t{model}\t{ver}\n")

    data = "".join(lines).encode()
    info = tarfile.TarInfo(name="devices.txt")
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _add_device_configs(tar: tarfile.TarFile):
    """Add individual device config JSON files under configs/ directory."""
    all_configs = db.get_all_latest_configs()
    for ip, config in all_configs.items():
        config_json = config.get("config_json", "{}")
        if isinstance(config_json, str):
            try:
                parsed = json.loads(config_json)
                pretty = json.dumps(parsed, indent=2)
            except json.JSONDecodeError:
                pretty = config_json
        else:
            pretty = json.dumps(config_json, indent=2)

        safe_ip = ip.replace(".", "-")
        model = config.get("model") or "unknown"
        filename = f"configs/{safe_ip}_{model}.json"
        data = pretty.encode()
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


async def _enforce_retention(sftp, remote_path: str):
    """Delete oldest backups beyond the retention count."""
    retention = int(db.get_setting("backup_retention_count") or "30")
    try:
        entries = await sftp.listdir(remote_path)
        backups = sorted(
            [e for e in entries if e.startswith("sixtyops-backup-") and e.endswith(".tar.gz")]
        )
        if len(backups) > retention:
            for old in backups[:len(backups) - retention]:
                await sftp.remove(f"{remote_path}/{old}")
                logger.info(f"Retention cleanup: removed {old}")
    except Exception as e:
        logger.warning(f"Retention cleanup failed: {e}")
