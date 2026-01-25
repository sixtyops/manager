"""Check GitHub releases for application updates."""

import asyncio
import logging
import os
import re
import shlex
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

GITHUB_REPO = os.environ.get("GITHUB_REPO", "sixtyops/manager")
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
CHECK_INTERVAL = int(os.environ.get("AUTOUPDATE_CHECK_INTERVAL", 604800))  # 7 days

# Appliance mode: use docker pull from GHCR instead of git-based updates
APPLIANCE_MODE = os.environ.get("SIXTYOPS_APPLIANCE", "") == "1"
GHCR_IMAGE = os.environ.get("SIXTYOPS_IMAGE", "ghcr.io/sixtyops/manager")

# Appliance platform version file (written during OVA build)
APPLIANCE_VERSION_FILE = Path("/etc/sixtyops/appliance-version")


def get_appliance_version() -> Optional[str]:
    """Read the appliance platform version (set during OVA build)."""
    if APPLIANCE_VERSION_FILE.exists():
        try:
            return APPLIANCE_VERSION_FILE.read_text().strip()
        except (OSError, PermissionError):
            return None
    return None


def parse_min_appliance_version(release_notes: str) -> Optional[str]:
    """Parse minimum required appliance version from release notes.

    Looks for an HTML comment: <!-- min_appliance_version: X.Y -->
    """
    match = re.search(r'<!--\s*min_appliance_version:\s*(\S+)\s*-->', release_notes or "")
    return match.group(1) if match else None


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

        Respects the release_channel setting: 'stable' checks only full
        releases, 'dev' also considers pre-releases.

        Returns dict with 'current_version', 'latest_version', 'update_available', etc.
        """
        current = __version__
        channel = db.get_setting("release_channel", "stable")
        result = {
            "current_version": current,
            "latest_version": None,
            "update_available": False,
            "release_url": None,
            "release_notes": None,
            "release_channel": channel,
            "error": None,
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if channel == "dev":
                    # Fetch recent releases including pre-releases
                    resp = await client.get(
                        GITHUB_API_RELEASES,
                        params={"per_page": 10},
                        headers={"Accept": "application/vnd.github+json"},
                    )
                    resp.raise_for_status()
                    releases = resp.json()
                    # Pick the newest release (first in list, which includes pre-releases)
                    data = releases[0] if releases else {}
                else:
                    # Stable: only the latest non-prerelease
                    resp = await client.get(
                        GITHUB_API_LATEST,
                        headers={"Accept": "application/vnd.github+json"},
                    )
                    resp.raise_for_status()
                    data = resp.json()

            tag_name = data.get("tag_name", "")
            # Strip leading 'v' if present (e.g., "v0.2.0" -> "0.2.0")
            latest = tag_name.lstrip("v")
            full_release_notes = data.get("body", "")
            result["latest_version"] = latest
            result["release_url"] = data.get("html_url", "")
            result["release_notes"] = full_release_notes[:2000]  # Truncate for UI payload

            # Compare versions (only flag upgrades, never downgrades)
            try:
                if version.parse(latest) > version.parse(current):
                    result["update_available"] = True
            except Exception:
                logger.warning(f"Could not parse versions: current={current}, latest={latest}")
                # Don't flag update if we can't reliably compare

            # Check appliance compatibility if in appliance mode
            if APPLIANCE_MODE and result["update_available"]:
                min_ver = parse_min_appliance_version(full_release_notes)
                current_appliance = get_appliance_version()
                if min_ver and current_appliance:
                    try:
                        if version.parse(min_ver) > version.parse(current_appliance):
                            result["appliance_upgrade_required"] = True
                            result["min_appliance_version"] = min_ver
                            result["current_appliance_version"] = current_appliance
                    except Exception:
                        logger.warning(f"Could not parse appliance versions: current={current_appliance}, min={min_ver}")

            # Store in database
            db.set_setting("autoupdate_last_check", datetime.now().isoformat())
            db.set_setting("autoupdate_available_version", latest if result["update_available"] else "")
            db.set_setting("autoupdate_release_url", result["release_url"] if result["update_available"] else "")
            db.set_setting("autoupdate_release_notes", result["release_notes"] if result["update_available"] else "")
            db.set_setting("autoupdate_release_notes_full", full_release_notes if result["update_available"] else "")

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
        status = {
            "current_version": __version__,
            "release_channel": db.get_setting("release_channel", "stable"),
            "enabled": db.get_setting("autoupdate_enabled", "false") == "true",
            "last_check": db.get_setting("autoupdate_last_check", ""),
            "available_version": db.get_setting("autoupdate_available_version", ""),
            "release_url": db.get_setting("autoupdate_release_url", ""),
            "release_notes": db.get_setting("autoupdate_release_notes", ""),
            "update_available": bool(db.get_setting("autoupdate_available_version", "")),
            "docker_socket_available": _docker_socket_available(),
            "can_update": is_safe,
            "blocked_reason": blocked_reason,
            "appliance_mode": APPLIANCE_MODE,
            "appliance_version": get_appliance_version(),
        }

        # Check if available update requires a newer appliance
        if APPLIANCE_MODE and status["update_available"]:
            notes = db.get_setting("autoupdate_release_notes_full", "") or status["release_notes"]
            min_ver = parse_min_appliance_version(notes)
            current_appliance = get_appliance_version()
            if min_ver and current_appliance:
                try:
                    if version.parse(min_ver) > version.parse(current_appliance):
                        status["appliance_upgrade_required"] = True
                        status["min_appliance_version"] = min_ver
                        status["can_update"] = False
                        status["blocked_reason"] = (
                            f"Requires appliance platform v{min_ver} "
                            f"(current: v{current_appliance}). "
                            "Download the latest appliance OVA to upgrade."
                        )
                except Exception:
                    pass

        return status


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
    # Check for active firmware update jobs
    active_jobs = db.get_active_jobs()
    if active_jobs:
        return False, "Firmware update job(s) currently running"

    # Check for active rollout
    rollout = db.get_active_rollout()
    if rollout and rollout.get("status") in ("in_progress", "paused"):
        return False, "A firmware rollout is currently active"

    # Check if we're in a maintenance window
    settings = db.get_all_settings()
    if settings.get("schedule_enabled") == "true":
        schedule_days = [d.strip() for d in settings.get("schedule_days", "").split(",") if d.strip()]
        try:
            start_hour = int(settings.get("schedule_start_hour", "3"))
        except (TypeError, ValueError):
            start_hour = 3
        try:
            end_hour = int(settings.get("schedule_end_hour", "4"))
        except (TypeError, ValueError):
            end_hour = 4

        try:
            tz = settings.get("timezone", "America/Chicago")
            if tz == "auto":
                tz = "America/Chicago"
            time_info = services.get_current_time(tz)
            current_hour = time_info.get("hour", datetime.now().hour)
            current_day = time_info.get("day_of_week", datetime.now().strftime("%a").lower())
        except Exception:
            current_hour = datetime.now().hour
            current_day = datetime.now().strftime("%a").lower()

        if services.is_in_schedule_window(current_hour, current_day, schedule_days, start_hour, end_hour):
            return False, "Currently in firmware maintenance window"

    return True, ""


def _get_repo_dir() -> Optional[Path]:
    """Find the git repository root on the host (bind-mounted into the container)."""
    candidates = [
        Path("/app/repo"),   # Explicit mount for self-update
        Path("/opt/sixtyops"),  # Default install location (install.sh)
    ]
    for path in candidates:
        if (path / ".git").exists():
            return path
    return None


def _get_host_repo_path() -> Optional[str]:
    """Discover the host-side path of the /app/repo bind mount."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "sixtyops-management",
             "--format",
             "{{range .Mounts}}{{if eq .Destination \"/app/repo\"}}{{.Source}}{{end}}{{end}}"],
            capture_output=True, text=True, timeout=10,
        )
        path = result.stdout.strip()
        return path if path else None
    except Exception:
        return None


def _build_watchdog_script(
    host_repo_dir: str,
    rollback_ref: str,
    has_standalone: bool,
) -> str:
    """Build the shell script that the watchdog container runs.

    The watchdog: builds the new image, tags the old image for rollback,
    swaps the container, monitors health, and rolls back on failure.
    """
    compose_cmd = f"docker compose -f {host_repo_dir}/docker-compose.yml"
    if has_standalone:
        compose_cmd += f" -f {host_repo_dir}/docker-compose.standalone.yml"

    # Use .replace() instead of f-string to avoid escaping {{ }} for docker --format
    return """#!/bin/sh
# SixtyOps update watchdog — build, swap, monitor health, rollback on failure
set -e

CONTAINER="sixtyops-management"
REPO="__REPO__"
ROLLBACK_REF="__ROLLBACK_REF__"
COMPOSE="__COMPOSE_CMD__"

echo "[watchdog] Starting update build..."

# Build new image (current container keeps running)
cd "$REPO"
$COMPOSE build sixtyops-mgmt
if [ $? -ne 0 ]; then
    echo "[watchdog] Build failed. Reverting git checkout..."
    apk add --no-cache git > /dev/null 2>&1
    git config --global --add safe.directory "$REPO"
    git -C "$REPO" checkout "$ROLLBACK_REF"
    echo "[watchdog] Reverted to $ROLLBACK_REF. No container swap performed."
    exit 1
fi

# Tag current image for rollback before swapping
IMAGE=$(docker inspect --format='{{.Config.Image}}' "$CONTAINER" 2>/dev/null || echo "")
if [ -n "$IMAGE" ]; then
    ROLLBACK_IMAGE="${IMAGE%%:*}:rollback"
    docker tag "$IMAGE" "$ROLLBACK_IMAGE"
    echo "[watchdog] Tagged $IMAGE as $ROLLBACK_IMAGE"
fi

# Swap to new container
echo "[watchdog] Swapping to new container..."
$COMPOSE up -d sixtyops-mgmt

# Monitor health (90 seconds: 18 checks x 5s)
echo "[watchdog] Monitoring health..."
HEALTHY=false
for i in $(seq 1 18); do
    sleep 5
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "not_found")
    case "$STATUS" in
        healthy)
            echo "[watchdog] Health check passed on attempt $i"
            HEALTHY=true
            break
            ;;
        *)
            echo "[watchdog] Health check $i/18: $STATUS"
            ;;
    esac
done

if [ "$HEALTHY" = "true" ]; then
    echo "[watchdog] Update successful!"
    # Clean up rollback image
    if [ -n "$ROLLBACK_IMAGE" ]; then
        docker rmi "$ROLLBACK_IMAGE" 2>/dev/null || true
    fi
    rm -f "$REPO/.update-watchdog.sh"
    exit 0
fi

# ----- Health check failed — roll back -----
echo "[watchdog] Health check failed after 90s. Rolling back..."

# Install git for rollback
apk add --no-cache git > /dev/null 2>&1
git config --global --add safe.directory "$REPO"

# Revert source to previous ref
git -C "$REPO" checkout "$ROLLBACK_REF"

# Re-tag rollback image as current so compose uses it without rebuilding
if [ -n "$ROLLBACK_IMAGE" ] && [ -n "$IMAGE" ]; then
    docker tag "$ROLLBACK_IMAGE" "$IMAGE"
fi

# Restart from old image (--no-build since we re-tagged it)
$COMPOSE up -d --no-build sixtyops-mgmt

echo "[watchdog] Rollback initiated. Monitoring recovery..."
for i in $(seq 1 12); do
    sleep 5
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "not_found")
    if [ "$STATUS" = "healthy" ]; then
        echo "[watchdog] Rollback successful."
        docker rmi "$ROLLBACK_IMAGE" 2>/dev/null || true
        rm -f "$REPO/.update-watchdog.sh"
        exit 1
    fi
done

echo "[watchdog] WARNING: Rollback also failed health check."
rm -f "$REPO/.update-watchdog.sh"
exit 1
""".replace("__REPO__", host_repo_dir) \
   .replace("__ROLLBACK_REF__", rollback_ref) \
   .replace("__COMPOSE_CMD__", compose_cmd)


def _launch_watchdog(
    repo_dir: Path,
    host_repo_dir: str,
    rollback_ref: str,
    has_standalone: bool,
) -> bool:
    """Write the watchdog script and launch it in a detached docker:cli container.

    Returns True if the watchdog was launched successfully.
    """
    try:
        # Remove any leftover watchdog container from a previous attempt
        subprocess.run(
            ["docker", "rm", "-f", "sixtyops-update-watchdog"],
            capture_output=True, timeout=10,
        )

        # Write watchdog script to the repo dir (persists on host via bind mount)
        script = _build_watchdog_script(host_repo_dir, rollback_ref, has_standalone)
        watchdog_path = repo_dir / ".update-watchdog.sh"
        watchdog_path.write_text(script)
        watchdog_path.chmod(0o755)

        host_script = f"{host_repo_dir}/.update-watchdog.sh"

        # Launch watchdog in a detached container that survives our restart.
        # docker:cli is Alpine-based with docker CLI + compose plugin.
        result = subprocess.run(
            [
                "docker", "run", "--rm", "-d",
                "--name", "sixtyops-update-watchdog",
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", f"{host_repo_dir}:{host_repo_dir}",
                "-w", host_repo_dir,
                "docker:cli",
                "sh", host_script,
            ],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode == 0:
            logger.info(f"Update watchdog launched: {result.stdout.strip()[:12]}")
            return True
        else:
            logger.error(f"Failed to launch watchdog: {result.stderr}")
            return False

    except Exception as e:
        logger.error(f"Failed to launch watchdog: {e}")
        return False


# ── Appliance mode: docker pull instead of git-based updates ──


def _build_appliance_watchdog_script(
    compose_dir: str,
    has_standalone: bool,
) -> str:
    """Build watchdog script for appliance mode (docker pull, no git)."""
    compose_cmd = f"docker compose -f {compose_dir}/docker-compose.yml"
    if has_standalone:
        compose_cmd += f" -f {compose_dir}/docker-compose.standalone.yml"

    return """#!/bin/sh
# SixtyOps appliance update watchdog — swap, monitor health, rollback on failure
set -e

CONTAINER="sixtyops-management"
COMPOSE="__COMPOSE_CMD__"

echo "[watchdog] Starting appliance update..."

# Tag current image for rollback before swapping
IMAGE=$(docker inspect --format='{{.Config.Image}}' "$CONTAINER" 2>/dev/null || echo "")
if [ -n "$IMAGE" ]; then
    ROLLBACK_IMAGE="${IMAGE%%:*}:rollback"
    docker tag "$IMAGE" "$ROLLBACK_IMAGE"
    echo "[watchdog] Tagged $IMAGE as $ROLLBACK_IMAGE"
fi

# Swap to new container (image already pulled)
echo "[watchdog] Swapping to new container..."
$COMPOSE up -d --no-build sixtyops-mgmt

# Monitor health (90 seconds: 18 checks x 5s)
echo "[watchdog] Monitoring health..."
HEALTHY=false
for i in $(seq 1 18); do
    sleep 5
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "not_found")
    case "$STATUS" in
        healthy)
            echo "[watchdog] Health check passed on attempt $i"
            HEALTHY=true
            break
            ;;
        *)
            echo "[watchdog] Health check $i/18: $STATUS"
            ;;
    esac
done

if [ "$HEALTHY" = "true" ]; then
    echo "[watchdog] Update successful!"
    if [ -n "$ROLLBACK_IMAGE" ]; then
        docker rmi "$ROLLBACK_IMAGE" 2>/dev/null || true
    fi
    rm -f "__COMPOSE_DIR__/.update-watchdog.sh"
    exit 0
fi

# ----- Health check failed — roll back -----
echo "[watchdog] Health check failed after 90s. Rolling back..."

# Re-tag rollback image as current so compose uses it
if [ -n "$ROLLBACK_IMAGE" ] && [ -n "$IMAGE" ]; then
    docker tag "$ROLLBACK_IMAGE" "$IMAGE"
fi

# Restart from old image
$COMPOSE up -d --no-build sixtyops-mgmt

echo "[watchdog] Rollback initiated. Monitoring recovery..."
for i in $(seq 1 12); do
    sleep 5
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "not_found")
    if [ "$STATUS" = "healthy" ]; then
        echo "[watchdog] Rollback successful."
        docker rmi "$ROLLBACK_IMAGE" 2>/dev/null || true
        rm -f "__COMPOSE_DIR__/.update-watchdog.sh"
        exit 1
    fi
done

echo "[watchdog] WARNING: Rollback also failed health check."
rm -f "__COMPOSE_DIR__/.update-watchdog.sh"
exit 1
""".replace("__COMPOSE_CMD__", compose_cmd) \
   .replace("__COMPOSE_DIR__", compose_dir)


def _launch_appliance_watchdog(
    compose_dir: Path,
    has_standalone: bool,
) -> bool:
    """Launch the appliance watchdog in a detached docker:cli container."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", "sixtyops-update-watchdog"],
            capture_output=True, timeout=10,
        )

        script = _build_appliance_watchdog_script(str(compose_dir), has_standalone)
        watchdog_path = compose_dir / ".update-watchdog.sh"
        watchdog_path.write_text(script)
        watchdog_path.chmod(0o755)

        host_script = f"{compose_dir}/.update-watchdog.sh"

        result = subprocess.run(
            [
                "docker", "run", "--rm", "-d",
                "--name", "sixtyops-update-watchdog",
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", f"{compose_dir}:{compose_dir}",
                "-w", str(compose_dir),
                "docker:cli",
                "sh", host_script,
            ],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode == 0:
            logger.info(f"Appliance watchdog launched: {result.stdout.strip()[:12]}")
            return True
        else:
            logger.error(f"Failed to launch appliance watchdog: {result.stderr}")
            return False

    except Exception as e:
        logger.error(f"Failed to launch appliance watchdog: {e}")
        return False


async def _apply_update_appliance(target_version: str, target_tag: str) -> dict:
    """Apply update in appliance mode: pull image from GHCR, swap containers."""
    if not _docker_socket_available():
        return {"success": False, "message": "Docker socket not available"}

    compose_dir = _get_compose_dir()
    if not compose_dir:
        return {"success": False, "message": "Cannot find compose directory"}

    image_ref = f"{GHCR_IMAGE}:{target_tag}"

    try:
        # Pull new image
        logger.info(f"Pulling image {image_ref}...")
        pull_result = subprocess.run(
            ["docker", "pull", image_ref],
            capture_output=True, text=True, timeout=300,
        )
        if pull_result.returncode != 0:
            return {
                "success": False,
                "message": f"Docker pull failed: {pull_result.stderr.strip()}",
            }

        # Store pending update info (persists through restart)
        db.set_settings({
            "autoupdate_pending_version": target_version,
            "autoupdate_pending_at": datetime.now().isoformat(),
        })

        has_standalone = (compose_dir / "docker-compose.standalone.yml").exists()

        # Launch watchdog for health-checked swap with rollback
        launched = _launch_appliance_watchdog(compose_dir, has_standalone)
        if not launched:
            logger.warning("Appliance watchdog failed, falling back to direct swap")
            compose_cmd = _get_compose_cmd(compose_dir)
            subprocess.Popen(
                compose_cmd + ["up", "-d", "--no-build", "sixtyops-mgmt"],
                cwd=compose_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        return {
            "success": True,
            "message": f"Updating to {target_tag}. The application will restart shortly.",
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Docker pull timed out"}
    except Exception as e:
        logger.exception(f"Appliance update failed: {e}")
        return {"success": False, "message": str(e)}


async def apply_update() -> dict:
    """Fetch the target release tag and launch the update watchdog.

    The watchdog (a detached docker:cli container) handles: build, image tagging,
    container swap, health monitoring, and automatic rollback on failure.

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

    # Determine which version we're updating to
    target_version = db.get_setting("autoupdate_available_version", "")
    if not target_version:
        return {
            "success": False,
            "message": "No update version available. Run a check first.",
        }

    target_tag = f"v{target_version}"

    if APPLIANCE_MODE:
        # Check appliance compatibility before applying
        release_notes = db.get_setting("autoupdate_release_notes_full", "") or db.get_setting("autoupdate_release_notes", "")
        min_ver = parse_min_appliance_version(release_notes)
        current_appliance = get_appliance_version()
        if min_ver and current_appliance:
            try:
                if version.parse(min_ver) > version.parse(current_appliance):
                    return {
                        "success": False,
                        "message": (
                            f"This update requires appliance platform v{min_ver} "
                            f"(current: v{current_appliance}). "
                            "Download the latest appliance OVA to upgrade."
                        ),
                        "appliance_upgrade_required": True,
                    }
            except Exception:
                pass
        return await _apply_update_appliance(target_version, target_tag)

    repo_dir = _get_repo_dir()

    if not _docker_socket_available():
        return {
            "success": False,
            "manual": True,
            "message": "Docker socket not mounted. Run these commands on the host:",
            "commands": [
                "cd /opt/sixtyops",
                f"git fetch origin tag {target_tag}",
                f"git checkout {target_tag}",
                "docker compose up -d --build",
            ],
        }

    if not repo_dir:
        return {
            "success": False,
            "manual": True,
            "message": "Git repo not mounted. Run these commands on the host:",
            "commands": [
                "cd /opt/sixtyops",
                f"git fetch origin tag {target_tag}",
                f"git checkout {target_tag}",
                "docker compose up -d --build",
            ],
        }

    compose_cmd = _get_compose_cmd(repo_dir)
    git_cmd = ["git", "-C", str(repo_dir)]

    try:
        # Mark repo as safe (container UID may differ from host repo owner)
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", str(repo_dir)],
            capture_output=True, timeout=5,
        )

        # Save current ref for rollback
        save_result = subprocess.run(
            git_cmd + ["rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        rollback_ref = save_result.stdout.strip() if save_result.returncode == 0 else None
        if not rollback_ref:
            return {
                "success": False,
                "message": "Could not determine current version for rollback",
            }

        # Fetch the specific release tag (not all of main)
        logger.info(f"Fetching tag {target_tag} in {repo_dir}...")
        fetch_result = subprocess.run(
            git_cmd + ["fetch", "origin", "tag", target_tag, "--force"],
            capture_output=True, text=True, timeout=60,
        )
        if fetch_result.returncode != 0:
            return {
                "success": False,
                "message": f"Git fetch failed: {fetch_result.stderr}",
            }

        # Checkout the exact tag — no unreviewed code from main
        logger.info(f"Checking out {target_tag}...")
        checkout_result = subprocess.run(
            git_cmd + ["checkout", target_tag],
            capture_output=True, text=True, timeout=30,
        )
        if checkout_result.returncode != 0:
            return {
                "success": False,
                "message": f"Git checkout failed: {checkout_result.stderr}",
            }

        # Verify the checked-out version matches what we expect
        version_file = repo_dir / "updater" / "__init__.py"
        if version_file.exists():
            content = version_file.read_text()
            if f'"{target_version}"' not in content:
                # Revert checkout
                subprocess.run(git_cmd + ["checkout", rollback_ref],
                               capture_output=True, timeout=30)
                return {
                    "success": False,
                    "message": f"Version mismatch: tag {target_tag} does not contain version {target_version}",
                }

        # Store pending update info (persists in DB through restart)
        db.set_settings({
            "autoupdate_pending_version": target_version,
            "autoupdate_pending_at": datetime.now().isoformat(),
            "autoupdate_rollback_ref": rollback_ref,
        })

        # Discover host repo path for the watchdog container
        host_repo_dir = _get_host_repo_path()
        has_standalone = (repo_dir / "docker-compose.standalone.yml").exists()

        if host_repo_dir:
            # Launch watchdog: build, swap, health check, rollback on failure
            launched = _launch_watchdog(repo_dir, host_repo_dir, rollback_ref, has_standalone)
            if not launched:
                logger.warning("Watchdog failed to launch, falling back to direct update")
                subprocess.Popen(
                    compose_cmd + ["up", "-d", "--build", "sixtyops-mgmt"],
                    cwd=repo_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            # Can't determine host path — proceed without rollback
            logger.warning("Could not determine host repo path; no rollback available")
            subprocess.Popen(
                compose_cmd + ["up", "-d", "--build", "sixtyops-mgmt"],
                cwd=repo_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        return {
            "success": True,
            "message": f"Updating to {target_tag}. The application will restart shortly.",
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


async def verify_update_on_startup(broadcast_func: Optional[Callable] = None):
    """Check if an app update was recently applied and broadcast the result.

    Call this during app startup, after DB is initialized. If we just restarted
    after an update, the pending version in the DB will match (success) or not
    match (rollback) our current __version__.
    """
    pending = db.get_setting("autoupdate_pending_version", "")
    if not pending:
        return

    # Check for stuck state: pending for too long without resolution
    pending_at = db.get_setting("autoupdate_pending_at", "")
    if pending_at and pending != __version__:
        try:
            pending_time = datetime.fromisoformat(pending_at)
            age_minutes = (datetime.now() - pending_time).total_seconds() / 60
            if age_minutes > 15:
                logger.warning(
                    f"Update to v{pending} has been pending for {age_minutes:.0f} minutes "
                    f"without completing. Clearing stuck state."
                )
                db.set_settings({
                    "autoupdate_pending_version": "",
                    "autoupdate_pending_at": "",
                    "autoupdate_rollback_ref": "",
                })
                if broadcast_func:
                    await broadcast_func({
                        "type": "update_failed",
                        "attempted_version": pending,
                        "current_version": __version__,
                        "reason": "Update timed out without completing",
                    })
                return
        except (ValueError, TypeError):
            pass  # Malformed timestamp, fall through to existing logic

    if pending == __version__:
        logger.info(f"App update to v{__version__} completed successfully")
        db.set_settings({
            "autoupdate_pending_version": "",
            "autoupdate_pending_at": "",
            "autoupdate_available_version": "",
            "autoupdate_rollback_ref": "",
        })
        if broadcast_func:
            await broadcast_func({
                "type": "update_completed",
                "version": __version__,
                "success": True,
            })
    else:
        logger.warning(
            f"App update to v{pending} may have been rolled back "
            f"(running v{__version__})"
        )
        db.set_settings({
            "autoupdate_pending_version": "",
            "autoupdate_pending_at": "",
            "autoupdate_rollback_ref": "",
        })
        if broadcast_func:
            await broadcast_func({
                "type": "update_rolled_back",
                "attempted_version": pending,
                "current_version": __version__,
            })


def get_checker() -> Optional[ReleaseChecker]:
    return _checker


def init_checker(broadcast_func: Callable,
                 check_interval: int = CHECK_INTERVAL) -> ReleaseChecker:
    global _checker
    _checker = ReleaseChecker(broadcast_func, check_interval)
    return _checker
