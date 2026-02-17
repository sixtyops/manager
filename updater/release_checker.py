"""Check GitHub releases for application updates."""

import asyncio
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import httpx
from packaging import version

from . import database as db
from . import services
from . import __version__

logger = logging.getLogger(__name__)

# Global singleton
_checker: Optional["ReleaseChecker"] = None

GITHUB_REPO = os.environ.get("GITHUB_REPO", "isolson/firmware-updater")
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECK_INTERVAL = int(os.environ.get("AUTOUPDATE_CHECK_INTERVAL", 604800))  # 7 days


class ReleaseChecker:
    """Background service that checks GitHub for new releases."""

    def __init__(self, broadcast_func: Callable, check_interval: int = CHECK_INTERVAL):
        self.broadcast_func = broadcast_func
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("Release checker started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Release checker stopped")

    async def _check_loop(self):
        # Initial check on startup (if enabled)
        if db.get_setting("autoupdate_enabled", "false") == "true":
            try:
                await self.check_for_updates()
            except Exception as e:
                logger.exception(f"Release check error on startup: {e}")

        while self._running:
            await asyncio.sleep(self.check_interval)
            if db.get_setting("autoupdate_enabled", "false") == "true":
                try:
                    await self.check_for_updates()
                except Exception as e:
                    logger.exception(f"Release check error: {e}")

    async def check_for_updates(self) -> dict:
        """Check GitHub for the latest release.

        Returns dict with 'current_version', 'latest_version', 'update_available', etc.
        """
        current = __version__
        result = {
            "current_version": current,
            "latest_version": None,
            "update_available": False,
            "release_url": None,
            "release_notes": None,
            "error": None,
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    GITHUB_API_URL,
                    headers={"Accept": "application/vnd.github+json"},
                )
                resp.raise_for_status()
                data = resp.json()

            tag_name = data.get("tag_name", "")
            # Strip leading 'v' if present (e.g., "v0.2.0" -> "0.2.0")
            latest = tag_name.lstrip("v")
            result["latest_version"] = latest
            result["release_url"] = data.get("html_url", "")
            result["release_notes"] = data.get("body", "")[:500]  # Truncate long notes

            # Compare versions
            try:
                if version.parse(latest) > version.parse(current):
                    result["update_available"] = True
            except Exception:
                # Fallback to string comparison
                if latest != current:
                    result["update_available"] = True

            # Store in database
            db.set_setting("autoupdate_last_check", datetime.now().isoformat())
            db.set_setting("autoupdate_available_version", latest if result["update_available"] else "")
            db.set_setting("autoupdate_release_url", result["release_url"] if result["update_available"] else "")
            db.set_setting("autoupdate_release_notes", result["release_notes"] if result["update_available"] else "")

            # Broadcast if update available
            if result["update_available"] and self.broadcast_func:
                await self.broadcast_func({
                    "type": "update_available",
                    "current_version": current,
                    "latest_version": latest,
                    "release_url": result["release_url"],
                    "release_notes": result["release_notes"],
                })

            logger.info(f"Release check complete: current={current}, latest={latest}, "
                        f"update_available={result['update_available']}")

        except httpx.HTTPStatusError as e:
            result["error"] = f"GitHub API error: {e.response.status_code}"
            logger.error(result["error"])
        except Exception as e:
            result["error"] = str(e)
            logger.exception(f"Release check failed: {e}")

        return result

    def get_update_status(self) -> dict:
        """Get current update status from database."""
        is_safe, blocked_reason = _is_safe_to_update()
        return {
            "current_version": __version__,
            "enabled": db.get_setting("autoupdate_enabled", "false") == "true",
            "last_check": db.get_setting("autoupdate_last_check", ""),
            "available_version": db.get_setting("autoupdate_available_version", ""),
            "release_url": db.get_setting("autoupdate_release_url", ""),
            "release_notes": db.get_setting("autoupdate_release_notes", ""),
            "update_available": bool(db.get_setting("autoupdate_available_version", "")),
            "docker_socket_available": _docker_socket_available(),
            "can_update": is_safe,
            "blocked_reason": blocked_reason,
        }


def _docker_socket_available() -> bool:
    """Check if Docker socket is mounted and accessible."""
    socket_path = Path("/var/run/docker.sock")
    return socket_path.exists()


def _get_compose_dir() -> Optional[Path]:
    """Find the docker-compose.yml directory."""
    # Check common locations
    candidates = [
        Path("/app"),
        Path.cwd(),
        Path(__file__).parent.parent,
    ]
    for path in candidates:
        if (path / "docker-compose.yml").exists():
            return path
    return None


def _get_compose_cmd(compose_dir: Path) -> list[str]:
    """Build the docker compose command with appropriate -f flags.

    In standalone mode (docker-compose.standalone.yml is mounted), both
    compose files must be specified so docker compose operates on all services.
    """
    cmd = ["docker", "compose", "-f", str(compose_dir / "docker-compose.yml")]
    standalone = compose_dir / "docker-compose.standalone.yml"
    if standalone.exists():
        cmd.extend(["-f", str(standalone)])
    return cmd


def _is_safe_to_update() -> tuple[bool, str]:
    """Check if it's safe to update the app (not during maintenance or active rollout).

    Returns (is_safe, reason).
    """
    # Check for active rollout
    rollout = db.get_active_rollout()
    if rollout and rollout.get("status") in ("in_progress", "paused"):
        return False, "A firmware rollout is currently active"

    # Check if we're in a maintenance window
    settings = db.get_all_settings()
    if settings.get("schedule_enabled") == "true":
        schedule_days = [d.strip() for d in settings.get("schedule_days", "").split(",") if d.strip()]
        start_hour = int(settings.get("schedule_start_hour", "3"))
        end_hour = int(settings.get("schedule_end_hour", "4"))

        try:
            time_info = services.get_current_time(settings.get("timezone", "auto"), settings.get("zip_code", ""))
            current_hour = time_info.get("hour", datetime.now().hour)
            current_day = time_info.get("day_of_week", datetime.now().strftime("%a").lower())
        except Exception:
            current_hour = datetime.now().hour
            current_day = datetime.now().strftime("%a").lower()

        if services.is_in_schedule_window(current_hour, current_day, schedule_days, start_hour, end_hour):
            return False, "Currently in firmware maintenance window"

    return True, ""


async def apply_update() -> dict:
    """Pull latest Docker image and recreate container.

    Returns dict with 'success', 'message', and optionally 'commands'.
    """
    # Check if safe to update (not during maintenance or active rollout)
    is_safe, reason = _is_safe_to_update()
    if not is_safe:
        return {
            "success": False,
            "message": f"Cannot update now: {reason}. Please try again later.",
            "blocked_reason": reason,
        }

    compose_dir = _get_compose_dir()
    if not compose_dir:
        return {
            "success": False,
            "message": "Could not find docker-compose.yml",
        }

    compose_cmd = _get_compose_cmd(compose_dir)

    if not _docker_socket_available():
        # Return manual commands if socket not available
        cmd_prefix = " ".join(compose_cmd)
        return {
            "success": False,
            "manual": True,
            "message": "Docker socket not mounted. Run these commands manually:",
            "commands": [
                f"cd {compose_dir}",
                f"{cmd_prefix} pull tachyon-mgmt",
                f"{cmd_prefix} up -d tachyon-mgmt",
            ],
        }

    try:
        # Pull latest image
        logger.info("Pulling latest Docker image...")
        pull_result = subprocess.run(
            compose_cmd + ["pull", "tachyon-mgmt"],
            cwd=compose_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if pull_result.returncode != 0:
            return {
                "success": False,
                "message": f"Docker pull failed: {pull_result.stderr}",
            }

        # Recreate container (this will kill the current process)
        logger.info("Recreating container with new image...")
        # Use subprocess.Popen so we don't wait for it to complete
        subprocess.Popen(
            compose_cmd + ["up", "-d", "tachyon-mgmt"],
            cwd=compose_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        return {
            "success": True,
            "message": "Update started. The application will restart shortly.",
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Docker command timed out",
        }
    except Exception as e:
        logger.exception(f"Update failed: {e}")
        return {
            "success": False,
            "message": str(e),
        }


def get_checker() -> Optional[ReleaseChecker]:
    return _checker


def init_checker(broadcast_func: Callable,
                 check_interval: int = CHECK_INTERVAL) -> ReleaseChecker:
    global _checker
    _checker = ReleaseChecker(broadcast_func, check_interval)
    return _checker
