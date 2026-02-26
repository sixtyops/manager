"""Git-based backup system for database and configuration."""

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from . import database as db

logger = logging.getLogger(__name__)

# Paths - these match docker-compose volume mounts
BACKUP_DIR = Path("/app/backups")
DATA_DIR = Path("/app/data")
DB_FILE = DATA_DIR / "tachyon.db"
SSH_DIR = Path("/app/.ssh")


def get_backup_status() -> dict:
    """Get current backup configuration and status."""
    settings = db.get_all_settings()
    return {
        "enabled": settings.get("backup_enabled") == "true",
        "repo_url": _mask_token(settings.get("backup_repo_url", "")),
        "auth_method": settings.get("backup_auth_method", ""),
        "last_run": settings.get("backup_last_run", ""),
        "last_status": settings.get("backup_last_status", ""),
    }


def _mask_token(url: str) -> str:
    """Mask any embedded token in a git URL for display."""
    if "oauth2:" in url:
        # https://oauth2:token@github.com/... -> https://oauth2:****@github.com/...
        parts = url.split("@", 1)
        if len(parts) == 2:
            return parts[0].rsplit(":", 1)[0] + ":****@" + parts[1]
    return url


async def _run_git(*args, timeout: int = 60) -> Tuple[int, str, str]:
    """Run a git command in the backup directory."""
    cmd = ["git", "-C", str(BACKUP_DIR)] + list(args)
    env = {
        **os.environ,
        "GIT_SSH_COMMAND": f"ssh -i {SSH_DIR}/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(), stderr.decode()
    except asyncio.TimeoutError:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


async def init_backup_repo(
    repo_url: str,
    auth_method: str,
    ssh_key: Optional[str] = None,
    token: Optional[str] = None,
) -> Tuple[bool, str]:
    """Initialize the backup git repository.

    Returns (success, message).
    """
    if not repo_url:
        return False, "Repository URL is required"

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Configure authentication
    effective_url = repo_url

    if auth_method == "ssh":
        if not ssh_key:
            return False, "SSH key is required for SSH authentication"

        # Write SSH key
        SSH_DIR.mkdir(mode=0o700, exist_ok=True)
        key_path = SSH_DIR / "id_ed25519"
        key_path.write_text(ssh_key.strip() + "\n")
        key_path.chmod(0o600)
        logger.info("SSH key configured for backups")

    elif auth_method == "token":
        if not token:
            return False, "Access token is required for token authentication"

        # Embed token in URL for HTTPS repos
        if repo_url.startswith("https://"):
            # https://github.com/user/repo -> https://oauth2:token@github.com/user/repo
            effective_url = repo_url.replace("https://", f"https://oauth2:{token}@")
        else:
            return False, "Token authentication requires HTTPS URL"

    # Check if already initialized
    git_dir = BACKUP_DIR / ".git"
    if git_dir.exists():
        # Update remote URL
        returncode, _, stderr = await _run_git("remote", "set-url", "origin", effective_url)
        if returncode != 0:
            # Try adding remote instead
            await _run_git("remote", "remove", "origin")
            returncode, _, stderr = await _run_git("remote", "add", "origin", effective_url)
            if returncode != 0:
                return False, f"Failed to configure remote: {stderr}"
    else:
        # Clone the repo (or init if empty)
        returncode, stdout, stderr = await _run_git_clone(effective_url)
        if returncode != 0:
            # If clone failed, try initializing a new repo
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            returncode, _, stderr = await _run_git("init")
            if returncode != 0:
                return False, f"Failed to initialize repository: {stderr}"

            returncode, _, stderr = await _run_git("remote", "add", "origin", effective_url)
            if returncode != 0:
                return False, f"Failed to add remote: {stderr}"

    # Configure git identity
    await _run_git("config", "user.email", "backup@tachyon-mgmt.local")
    await _run_git("config", "user.name", "Tachyon Backup")

    # Verify the remote is reachable before declaring success
    returncode, _, stderr = await _run_git("ls-remote", "--exit-code", "origin")
    if returncode != 0:
        return False, f"Remote repository is unreachable: {stderr[:200]}"

    # Store settings (store original URL, not the one with embedded token)
    db.set_setting("backup_repo_url", repo_url)
    db.set_setting("backup_auth_method", auth_method)
    db.set_setting("backup_enabled", "true")

    logger.info(f"Backup repository configured: {_mask_token(repo_url)}")
    return True, "Backup repository configured successfully"


async def _run_git_clone(url: str) -> Tuple[int, str, str]:
    """Clone a git repository."""
    # Remove existing directory contents first
    if BACKUP_DIR.exists():
        for item in BACKUP_DIR.iterdir():
            if item.name != ".git":
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

    env = {
        **os.environ,
        "GIT_SSH_COMMAND": f"ssh -i {SSH_DIR}/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    }

    cmd = ["git", "clone", "--depth=1", url, str(BACKUP_DIR)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        return proc.returncode, stdout.decode(), stderr.decode()
    except asyncio.TimeoutError:
        return -1, "", "Clone timed out"
    except Exception as e:
        return -1, "", str(e)


async def run_backup() -> Tuple[bool, str]:
    """Run a backup of the database to the git repo.

    Returns (success, message).
    """
    if not BACKUP_DIR.exists() or not (BACKUP_DIR / ".git").exists():
        return False, "Backup repository not configured"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Starting backup at {timestamp}")

    try:
        # Copy database file
        backup_db = BACKUP_DIR / "tachyon.db"
        if DB_FILE.exists():
            shutil.copy2(DB_FILE, backup_db)
        else:
            return False, "Database file not found"

        # Export settings to JSON for readability
        settings = db.get_all_settings()
        # Remove sensitive data
        safe_settings = {k: v for k, v in settings.items()
                         if not any(s in k.lower() for s in ["password", "secret", "token", "key"])}
        settings_file = BACKUP_DIR / "settings.json"
        settings_file.write_text(json.dumps(safe_settings, indent=2, sort_keys=True))

        # Export device list for reference (no passwords)
        devices_file = BACKUP_DIR / "devices.txt"
        aps = db.get_access_points(enabled_only=False)
        switches = db.get_switches(enabled_only=False)

        with devices_file.open("w") as f:
            f.write(f"# Tachyon Device Inventory - {timestamp}\n\n")
            f.write("## Access Points\n")
            for ap in aps:
                name = ap.get("system_name") or "unnamed"
                model = ap.get("model") or "unknown"
                version = ap.get("firmware_version") or "unknown"
                f.write(f"{ap['ip']}\t{name}\t{model}\t{version}\n")

            f.write(f"\n## Switches ({len(switches)} total)\n")
            for sw in switches:
                name = sw.get("system_name") or "unnamed"
                model = sw.get("model") or "unknown"
                version = sw.get("firmware_version") or "unknown"
                f.write(f"{sw['ip']}\t{name}\t{model}\t{version}\n")

        # Git add all changes
        returncode, _, stderr = await _run_git("add", "-A")
        if returncode != 0:
            return False, f"Git add failed: {stderr}"

        # Check if there are changes to commit
        returncode, stdout, _ = await _run_git("status", "--porcelain")
        if not stdout.strip():
            logger.info("No changes to backup")
            db.set_setting("backup_last_run", datetime.now().isoformat())
            db.set_setting("backup_last_status", "success (no changes)")
            return True, "No changes to backup"

        # Commit
        commit_msg = f"Backup {timestamp}"
        returncode, _, stderr = await _run_git("commit", "-m", commit_msg)
        if returncode != 0:
            if "nothing to commit" in stderr:
                db.set_setting("backup_last_run", datetime.now().isoformat())
                db.set_setting("backup_last_status", "success (no changes)")
                return True, "No changes to backup"
            return False, f"Git commit failed: {stderr}"

        # Push - try main first, then master
        returncode, _, stderr = await _run_git("push", "-u", "origin", "main", timeout=120)
        if returncode != 0:
            returncode, _, stderr = await _run_git("push", "-u", "origin", "master", timeout=120)

        if returncode != 0:
            # If push failed, it might be a new repo - try setting upstream
            await _run_git("branch", "-M", "main")
            returncode, _, stderr = await _run_git("push", "-u", "origin", "main", timeout=120)

        if returncode != 0:
            db.set_setting("backup_last_status", f"failed: push error")
            return False, f"Git push failed: {stderr[:200]}"

        db.set_setting("backup_last_run", datetime.now().isoformat())
        db.set_setting("backup_last_status", "success")
        logger.info(f"Backup completed successfully at {timestamp}")
        return True, f"Backup completed at {timestamp}"

    except Exception as e:
        error_msg = str(e)[:100]
        db.set_setting("backup_last_status", f"failed: {error_msg}")
        logger.exception(f"Backup failed: {e}")
        return False, str(e)


async def test_backup_connection() -> Tuple[bool, str]:
    """Test the backup repository connection without making changes.

    Returns (success, message).
    """
    if not BACKUP_DIR.exists() or not (BACKUP_DIR / ".git").exists():
        return False, "Backup repository not configured"

    # Try to fetch from remote
    returncode, _, stderr = await _run_git("fetch", "--dry-run", timeout=30)
    if returncode == 0:
        return True, "Connection successful"
    else:
        return False, f"Connection failed: {stderr[:100]}"
