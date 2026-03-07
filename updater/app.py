"""SixtyOps - Web Application."""

import asyncio
import html as html_module
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

import aiofiles
import bcrypt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .tachyon import TachyonClient, UpdateResult
from . import __version__
from . import database as db
from .poller import init_poller, get_poller
from .scheduler import init_scheduler, get_scheduler, SCHEDULE_END_BUFFER_MINUTES
from .firmware_fetcher import init_fetcher, get_fetcher
from .release_checker import init_checker, get_checker, apply_update, verify_update_on_startup
from . import services
from .auth import require_auth, require_auth_ws, require_role, authenticate, create_session, SESSION_COOKIE_NAME, is_setup_required, is_first_run, complete_setup, is_request_secure, ensure_admin_user, ensure_oidc_user
from .backup import build_csv_export, process_csv_import
from . import telemetry
from . import slack
from . import snmp
from . import ssl_manager
from . import sftp_backup
from . import builtin_radius
from . import radius_config
from . import oidc_config
from .license import (
    Feature, get_license_state, get_nag_info, get_billable_device_count,
    is_feature_enabled, validate_license, clear_license,
    init_license_validator, require_feature,
)
from .radius_server import (
    init_radius_service, get_radius_service,
    get_radius_server_config, set_radius_server_config,
    RadiusServerConfig,
)
from . import radius_users

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent
FIRMWARE_DIR = BASE_DIR.parent / "firmware"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR.parent / "static"
DATA_DIR = BASE_DIR.parent / "data"

# Allowed characters for firmware filenames (security: prevent path traversal)
SAFE_FILENAME_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$')


def validate_firmware_filename(filename: str) -> str:
    """Validate and sanitize firmware filename to prevent path traversal attacks.

    Returns the validated filename or raises HTTPException if invalid.
    """
    if not filename:
        raise HTTPException(400, "Filename is required")

    # Get just the basename to prevent directory traversal
    basename = Path(filename).name

    # Check for empty or dot-only names
    if not basename or basename in ('.', '..'):
        raise HTTPException(400, "Invalid filename")

    # Check filename matches safe pattern
    if not SAFE_FILENAME_PATTERN.match(basename):
        raise HTTPException(400, "Filename contains invalid characters")

    # Verify the resolved path stays within FIRMWARE_DIR
    resolved = (FIRMWARE_DIR / basename).resolve()
    if not str(resolved).startswith(str(FIRMWARE_DIR.resolve())):
        raise HTTPException(400, "Invalid filename")

    return basename


# Ensure directories exist
FIRMWARE_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# Global state
active_websockets: Set[WebSocket] = set()
update_jobs: Dict[str, "UpdateJob"] = {}
_last_data_housekeeping_day: Optional[str] = None


async def broadcast(message: dict):
    """Broadcast message to all connected WebSocket clients."""
    disconnected = set()
    for ws in active_websockets:
        try:
            await asyncio.wait_for(ws.send_json(message), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("WebSocket send timed out, disconnecting slow client")
            disconnected.add(ws)
        except Exception:
            disconnected.add(ws)

    for ws in disconnected:
        active_websockets.discard(ws)


def _cleanup_oidc_states():
    """Remove expired OIDC state entries (older than 10 minutes)."""
    cutoff = (datetime.now() - timedelta(minutes=10)).isoformat()
    db.delete_expired_oidc_states(cutoff)


def _parse_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read bounded int from env, with safe fallback."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"Invalid {name}={raw!r}; using default {default}")
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _selected_firmware_names() -> set[str]:
    """Firmware files currently selected in settings and therefore protected from auto-prune."""
    selected = set()
    for key in ("selected_firmware_30x", "selected_firmware_303l", "selected_firmware_tns100"):
        value = (db.get_setting(key, "") or "").strip()
        if value:
            selected.add(Path(value).name)
    return selected


def _prune_firmware_storage() -> int:
    """Trim old firmware files to keep storage bounded for long-lived appliances."""
    max_files = _parse_int_env("FIRMWARE_RETENTION_MAX_FILES", default=300, minimum=50, maximum=5000)
    max_age_days = _parse_int_env("FIRMWARE_RETENTION_MAX_AGE_DAYS", default=730, minimum=30, maximum=3650)
    if max_files <= 0:
        return 0

    cutoff = datetime.now() - timedelta(days=max_age_days)
    selected = _selected_firmware_names()
    files = [p for p in FIRMWARE_DIR.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0

    # First pass: remove old unselected firmware.
    for path in list(files):
        if path.name in selected:
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if modified >= cutoff:
            continue
        try:
            path.unlink()
            db.unregister_firmware(path.name)
            removed += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning(f"Failed to delete old firmware {path.name}: {exc}")

    # Second pass: enforce max file count by deleting oldest unselected files.
    files = [p for p in FIRMWARE_DIR.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    while len(files) > max_files:
        candidate = None
        for path in files:
            if path.name not in selected:
                candidate = path
                break
        if candidate is None:
            break
        try:
            candidate.unlink()
            db.unregister_firmware(candidate.name)
            removed += 1
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"Failed to enforce firmware count limit for {candidate.name}: {exc}")
            break
        files = [p for p in FIRMWARE_DIR.iterdir() if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime)

    return removed


def _run_backup_git_gc():
    """Compact backup git repo history to prevent unbounded growth."""
    backup_dir = Path("/app/backups")
    if not (backup_dir / ".git").exists():
        return

    gc_days = _parse_int_env("BACKUP_GC_PRUNE_DAYS", default=90, minimum=7, maximum=3650)
    expire = f"{gc_days}.days.ago"
    commands = [
        ["git", "-C", str(backup_dir), "reflog", "expire", f"--expire={expire}", f"--expire-unreachable={expire}", "--all"],
        ["git", "-C", str(backup_dir), "gc", f"--prune={expire}", "--quiet"],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                logger.warning(f"Backup GC command failed ({' '.join(cmd)}): {stderr[:200]}")
        except Exception as exc:
            logger.warning(f"Backup GC command error ({' '.join(cmd)}): {exc}")


def _run_daily_data_housekeeping():
    """Run expensive data hygiene tasks once per day."""
    global _last_data_housekeeping_day

    if os.environ.get("TACHYON_APPLIANCE") != "1":
        return

    today = datetime.now().strftime("%Y-%m-%d")
    if _last_data_housekeeping_day == today:
        return

    removed = _prune_firmware_storage()
    if removed:
        logger.info(f"Firmware retention removed {removed} old file(s)")
    _run_backup_git_gc()
    _last_data_housekeeping_day = today


async def _supervised_task(name: str, coro_func, *args, restart_delay: float = 10.0):
    """Run a coroutine in a loop, restarting on unhandled exceptions."""
    while True:
        try:
            await coro_func(*args)
        except asyncio.CancelledError:
            logger.info(f"Background task '{name}' cancelled")
            raise
        except Exception:
            logger.exception(f"Background task '{name}' crashed, restarting in {restart_delay}s")
            await asyncio.sleep(restart_delay)


async def _periodic_cleanup():
    """Periodically clean up expired sessions, old job history, and stale in-memory jobs."""
    while True:
        await asyncio.sleep(3600)  # Run every hour
        try:
            db.cleanup_expired_sessions()
            db.cleanup_old_job_history(max_age_days=90)
            db.cleanup_old_schedule_log(max_age_days=90)
            db.cleanup_old_rollouts(max_age_days=180)
            db.cleanup_old_device_durations(max_age_days=180)
            db.cleanup_old_device_update_history(max_age_days=180)
            db.cleanup_old_device_configs(max_per_device=50)
            _cleanup_completed_jobs(max_age_seconds=3600)
            _cleanup_oidc_states()
            db.cleanup_old_radius_auth_log(max_age_days=90)
            db.cleanup_old_config_enforce_log(max_age_days=90)
            _run_daily_data_housekeeping()
            logger.info("Periodic cleanup completed")
        except Exception as e:
            logger.error(f"Periodic cleanup error: {e}")

        # SSL certificate renewal check
        try:
            ssl_status = ssl_manager.get_ssl_status()
            if ssl_status.get("needs_renewal") and ssl_status.get("using_letsencrypt"):
                logger.info("SSL certificate needs renewal, attempting auto-renewal...")
                success, message = await ssl_manager.renew_certificate()
                if success:
                    logger.info(f"SSL certificate renewed: {message}")
                else:
                    logger.warning(f"SSL certificate renewal failed: {message}")
        except Exception as e:
            logger.debug(f"SSL renewal check failed: {e}")

        # Disk space monitoring
        try:
            data_path = Path("/data") if os.environ.get("TACHYON_APPLIANCE") == "1" else DATA_DIR
            usage = shutil.disk_usage(str(data_path))
            percent_used = usage.used / usage.total * 100
            if percent_used > 95:
                logger.error(f"CRITICAL: Disk usage at {percent_used:.1f}%")
                await broadcast({
                    "type": "system_alert",
                    "level": "critical",
                    "message": f"Disk space critically low: {percent_used:.1f}% used. Free space: {usage.free // (1024*1024)} MB.",
                })
            elif percent_used > 90:
                logger.warning(f"Disk usage high: {percent_used:.1f}%")
                await broadcast({
                    "type": "system_alert",
                    "level": "warning",
                    "message": f"Disk space low: {percent_used:.1f}% used. Free space: {usage.free // (1024*1024)} MB.",
                })
        except Exception as e:
            logger.debug(f"Disk check failed: {e}")


async def _backup_scheduler():
    """Run daily backups at 5 AM if backup is configured."""
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes
        try:
            settings = db.get_all_settings()
            if settings.get("backup_enabled") != "true":
                continue

            # Check if it's 5 AM
            now = datetime.now()
            if now.hour != 5:
                continue

            # Check if we already ran today
            last_run = settings.get("backup_last_run", "")
            if last_run and last_run[:10] == now.strftime("%Y-%m-%d"):
                continue

            # Guard: skip if SFTP host not configured (e.g. migrating from old git backup)
            if not settings.get("backup_sftp_host"):
                continue

            logger.info("Running scheduled backup")
            success, msg = await sftp_backup.run_backup()
            if success:
                logger.info(f"Scheduled backup completed: {msg}")
            else:
                logger.error(f"Scheduled backup failed: {msg}")
        except Exception as e:
            logger.error(f"Backup scheduler error: {e}")


async def _radius_log_sync():
    """Persist recent FreeRADIUS auth events in the background."""
    while True:
        await asyncio.sleep(300)
        try:
            error = builtin_radius.sync_auth_history(hours=24)
            if error:
                logger.debug("Background RADIUS sync skipped: %s", error)
        except Exception as e:
            logger.error(f"Background RADIUS log sync error: {e}")


async def _radius_health_monitor():
    """Keep the FreeRADIUS container healthy over long-running deployments."""
    while True:
        await asyncio.sleep(60)
        try:
            await builtin_radius.get_runtime().ensure_healthy()
        except Exception as e:
            logger.error(f"Background RADIUS health monitor error: {e}")


def _cleanup_completed_jobs(max_age_seconds: int = 3600):
    """Remove completed jobs from in-memory dict after they've been persisted."""
    now = datetime.now()
    stale_ids = []
    for job_id, job in update_jobs.items():
        if job.status == "completed" and job.completed_at:
            age = (now - job.completed_at).total_seconds()
            if age > max_age_seconds:
                stale_ids.append(job_id)
    for job_id in stale_ids:
        del update_jobs[job_id]
    if stale_ids:
        logger.info(f"Cleaned up {len(stale_ids)} completed jobs from memory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - start/stop background tasks."""
    # Startup
    ensure_admin_user()
    db.cleanup_expired_sessions()
    radius_runtime = builtin_radius.get_runtime()
    await radius_runtime.start()
    poller = init_poller(broadcast, poll_interval=60)
    await poller.start()
    scheduler = init_scheduler(broadcast, _start_scheduled_update, check_interval=60)
    await scheduler.start()
    fetcher = init_fetcher(FIRMWARE_DIR, broadcast)
    await fetcher.start()
    checker = init_checker(broadcast)
    await checker.start()
    await verify_update_on_startup(broadcast)
    license_validator = init_license_validator(broadcast)
    await license_validator.start()
    radius_svc = init_radius_service(broadcast)
    radius_task = asyncio.create_task(
        _supervised_task("radius_server", radius_svc.run_forever)
    )
    cleanup_task = asyncio.create_task(
        _supervised_task("periodic_cleanup", _periodic_cleanup)
    )
    backup_task = asyncio.create_task(
        _supervised_task("backup_scheduler", _backup_scheduler)
    )
    radius_sync_task = asyncio.create_task(
        _supervised_task("radius_log_sync", _radius_log_sync)
    )
    radius_health_task = asyncio.create_task(
        _supervised_task("radius_health_monitor", _radius_health_monitor)
    )
    state = get_license_state()
    logger.info(f"License: {state.status.value} (tier={state.tier.value})")
    logger.info("Application started")

    yield

    # Shutdown
    backup_task.cancel()
    cleanup_task.cancel()
    radius_sync_task.cancel()
    radius_health_task.cancel()
    for task in [cleanup_task, backup_task, radius_sync_task, radius_health_task]:
        try:
            await task
        except asyncio.CancelledError:
            pass
    await license_validator.stop()
    await checker.stop()
    await fetcher.stop()
    await scheduler.stop()
    await poller.stop()
    db.checkpoint_db()
    await radius_runtime.stop()
    logger.info("Application stopped")


# FastAPI app
app = FastAPI(
    title="SixtyOps Firmware Updater",
    description="Automated firmware update management for Tachyon wireless network devices (APs, CPEs, switches).",
    version=__version__,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "devices", "description": "Device inventory management (APs, CPEs, switches)"},
        {"name": "firmware", "description": "Firmware file management and updates"},
        {"name": "jobs", "description": "Update job execution and monitoring"},
        {"name": "settings", "description": "Application configuration"},
        {"name": "analytics", "description": "Update analytics and trends"},
        {"name": "notifications", "description": "Slack and SNMP notification configuration"},
        {"name": "auth", "description": "Authentication, sessions, and user management"},
        {"name": "license", "description": "License management and feature gating"},
        {"name": "config", "description": "Device configuration backup and templates"},
        {"name": "system", "description": "System health, updates, and maintenance"},
    ],
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render_template(
    request: Request,
    template_name: str,
    context: Optional[dict] = None,
    status_code: int = 200,
):
    """Render template using the Request-first TemplateResponse signature."""
    payload = {"request": request}
    if context:
        payload.update(context)
    return templates.TemplateResponse(request, template_name, payload, status_code=status_code)


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self'"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
async def healthz():
    """Lightweight health endpoint for container runtime checks."""
    return {"status": "ok"}


# ============================================================================
# Auth Routes (no auth dependency)
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    """Serve the login page."""
    # First run with no password configured - redirect to setup
    if is_first_run():
        return RedirectResponse(url="/setup", status_code=302)
    return render_template(request, "login.html", {
        "error": error,
        "oidc_enabled": oidc_config.is_oidc_enabled(),
    })


_auth_rate_attempts: Dict[str, list] = {}  # bucket -> list of timestamps
AUTH_RATE_WINDOW = 300  # 5 minutes
LOGIN_RATE_LIMIT = 20
OIDC_RATE_LIMIT = 60


def _client_ip(request: Request) -> str:
    """Best-effort client IP for proxied deployments."""
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(bucket: str, limit: int, window_seconds: int) -> bool:
    """Return True if the bucket is currently rate-limited."""
    now = datetime.now()
    cutoff = now.timestamp() - window_seconds
    attempts = _auth_rate_attempts.get(bucket, [])
    attempts = [t for t in attempts if t > cutoff]
    _auth_rate_attempts[bucket] = attempts
    return len(attempts) >= limit


def _record_rate_limit_event(bucket: str):
    """Record an event in the bucket for rate limiting."""
    _auth_rate_attempts.setdefault(bucket, []).append(datetime.now().timestamp())


def _clear_rate_limit_bucket(bucket: str):
    """Clear a rate-limit bucket after successful authentication."""
    _auth_rate_attempts.pop(bucket, None)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Handle login form submission."""
    ip_address = _client_ip(request)
    bucket = f"login:{ip_address}"
    if _check_rate_limit(bucket, LOGIN_RATE_LIMIT, AUTH_RATE_WINDOW):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "rate_limited",
            "oidc_enabled": oidc_config.is_oidc_enabled(),
        }, status_code=429, headers={"Retry-After": str(AUTH_RATE_WINDOW)})

    user = authenticate(username, password)
    if not user:
        _record_rate_limit_event(bucket)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": True, "oidc_enabled": oidc_config.is_oidc_enabled()},
            status_code=401,
        )

    _clear_rate_limit_bucket(bucket)
    session_id = create_session(user["username"], ip_address)

    # Redirect to setup if password hasn't been changed from default
    redirect_url = "/setup" if is_setup_required() else "/"

    response = RedirectResponse(url=redirect_url, status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=is_request_secure(request),
        max_age=86400,
        samesite="lax",
    )
    return response


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Serve the initial password setup page.

    Accessible without auth on first run (no password configured yet).
    """
    first_run = is_first_run()

    if not first_run:
        # Not first run - require authentication
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_id or not db.get_session(session_id):
            return RedirectResponse(url="/login", status_code=302)

    if not is_setup_required():
        return RedirectResponse(url="/", status_code=302)

    return render_template(request, "setup.html", {
        "error": None,
        "first_run": first_run,
    })


@app.post("/setup")
async def setup_submit(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    current_password: str = Form(None),
):
    """Handle initial password setup submission.

    On first run (no password configured), current_password is not required.
    Otherwise, user must be authenticated and provide current password.
    """
    first_run = is_first_run()

    if not first_run:
        # Not first run - require authentication and current password
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        session = db.get_session(session_id) if session_id else None
        if not session:
            return RedirectResponse(url="/login", status_code=302)

        # Rate-limit password verification to prevent brute-force
        ip_address = request.client.host if request.client else "unknown"
        if _check_login_rate_limit(ip_address):
            return render_template(request, "setup.html", {
                "error": "Too many attempts. Please wait and try again.",
                "first_run": False,
            }, status_code=429)

        if not current_password:
            return render_template(request, "setup.html", {
                "error": "Current password is required.",
                "first_run": False,
            }, status_code=400)

        user = authenticate(session["username"], current_password)
        if not user:
            _record_login_attempt(ip_address)
            return render_template(request, "setup.html", {
                "error": "Current password is incorrect.",
                "first_run": False,
            }, status_code=400)
        username = session["username"]
    else:
        username = "admin"

    if not is_setup_required():
        return RedirectResponse(url="/", status_code=302)

    if new_password != confirm_password:
        return render_template(request, "setup.html", {
            "error": "New passwords do not match.",
            "first_run": first_run,
        }, status_code=400)

    if len(new_password) < 12:
        return render_template(request, "setup.html", {
            "error": "Password must be at least 12 characters.",
            "first_run": first_run,
        }, status_code=400)

    if not complete_setup(new_password):
        # Another request completed setup first (race condition)
        return RedirectResponse(url="/login", status_code=303)
    logger.info(f"Admin password {'created' if first_run else 'changed'} by {username} during initial setup")

    # Invalidate all existing sessions so old credentials can't be reused
    db.delete_all_sessions()

    return RedirectResponse(url="/login", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    """Handle logout."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        db.delete_session(session_id)

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ============================================================================
# Setup Wizard Routes
# ============================================================================

def _is_wizard_needed() -> bool:
    """Check if the setup wizard should be shown."""
    return db.get_setting("setup_wizard_completed", "false") != "true"


def _wizard_step_allowed(step: int) -> bool:
    """Check if a wizard step is allowed based on prior step completion."""
    if step >= 2 and db.get_setting("wizard_step_1_done", "false") != "true":
        return False
    if step >= 3 and db.get_setting("wizard_step_2_done", "false") != "true":
        return False
    return True


@app.get("/setup-wizard", response_class=HTMLResponse)
async def setup_wizard_page(request: Request, step: int = 1, session: dict = Depends(require_auth)):
    """Serve the setup wizard page."""
    if not _is_wizard_needed():
        return RedirectResponse(url="/", status_code=302)
    # Enforce sequential step access
    if not _wizard_step_allowed(step):
        return RedirectResponse(url="/setup-wizard?step=1", status_code=302)
    return render_template(request, "setup_wizard.html", {
        "step": step,
        "ssl_status": ssl_manager.get_ssl_status(),
        "backup_status": sftp_backup.get_backup_status(),
        "error": None, "success": None,
    })


@app.post("/setup-wizard")
async def setup_wizard_submit(
    request: Request, step: int = Form(...), action: str = Form(...),
    ssl_domain: str = Form(None), ssl_email: str = Form(None),
    sftp_host: str = Form(None), sftp_port: int = Form(22),
    sftp_path: str = Form("/backups/tachyon"), sftp_username: str = Form(None),
    auth_method: str = Form("password"), sftp_password: str = Form(None),
    ssh_key: str = Form(None), retention_count: int = Form(30),
    session: dict = Depends(require_auth),
):
    """Handle setup wizard form submissions."""
    # Enforce sequential step progression
    if not _wizard_step_allowed(step):
        return RedirectResponse(url="/setup-wizard?step=1", status_code=303)

    ssl_status = ssl_manager.get_ssl_status()
    backup_status = sftp_backup.get_backup_status()

    if step == 1:
        if action == "configure" and ssl_domain and ssl_email:
            ok, msg = await ssl_manager.obtain_certificate(ssl_domain, ssl_email)
            if not ok:
                return render_template(request, "setup_wizard.html", {
                    "step": 1, "ssl_status": ssl_status,
                    "backup_status": backup_status, "error": msg, "success": None,
                })
        db.set_setting("wizard_step_1_done", "true")
        return render_template(request, "setup_wizard.html", {
            "step": 2,
            "ssl_status": ssl_manager.get_ssl_status(),
            "backup_status": backup_status, "error": None, "success": None,
        })
    elif step == 2:
        if action == "configure" and sftp_host and sftp_username:
            ok, msg = await sftp_backup.configure_backup(
                host=sftp_host, port=sftp_port, path=sftp_path,
                username=sftp_username, auth_method=auth_method,
                password=sftp_password, ssh_key=ssh_key,
                retention_count=retention_count,
            )
            if not ok:
                return render_template(request, "setup_wizard.html", {
                    "step": 2, "ssl_status": ssl_status,
                    "backup_status": backup_status, "error": msg, "success": None,
                })
        db.set_setting("wizard_step_2_done", "true")
        return render_template(request, "setup_wizard.html", {
            "step": 3,
            "ssl_status": ssl_manager.get_ssl_status(),
            "backup_status": sftp_backup.get_backup_status(),
            "error": None, "success": None,
        })
    elif step == 3:
        db.set_setting("setup_wizard_completed", "true")
        # Clean up step-tracking settings
        db.set_setting("wizard_step_1_done", "false")
        db.set_setting("wizard_step_2_done", "false")
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url="/setup-wizard", status_code=303)


@app.get("/ssl-setup", response_class=HTMLResponse)
async def ssl_setup_page(request: Request, session: dict = Depends(require_auth)):
    """Serve the SSL setup page."""
    return render_template(request, "ssl_setup.html", {
        "ssl_status": ssl_manager.get_ssl_status(),
        "error": None, "success": None,
    })


@app.post("/ssl-setup")
async def ssl_setup_submit(
    request: Request, domain: str = Form(...), email: str = Form(...),
    session: dict = Depends(require_role("admin")),
):
    """Handle SSL certificate request."""
    success, message = await ssl_manager.obtain_certificate(domain, email)
    status = ssl_manager.get_ssl_status()
    return render_template(request, "ssl_setup.html", {
        "ssl_status": status,
        "error": None if success else message,
        "success": message if success else None,
    }, status_code=200 if success else 400)


@app.get("/api/ssl/status", tags=["system"])
async def get_ssl_status_api(session: dict = Depends(require_auth)):
    return ssl_manager.get_ssl_status()


@app.get("/backup-setup", response_class=HTMLResponse)
async def backup_setup_page(request: Request, session: dict = Depends(require_auth)):
    """Serve the backup setup page."""
    return templates.TemplateResponse("backup_setup.html", {
        "request": request, "backup_status": sftp_backup.get_backup_status(),
        "error": None, "success": None,
    })


@app.post("/backup-setup")
async def backup_setup_submit(
    request: Request,
    sftp_host: str = Form(...),
    sftp_port: int = Form(22),
    sftp_path: str = Form("/backups/tachyon"),
    sftp_username: str = Form(...),
    auth_method: str = Form("password"),
    sftp_password: str = Form(None),
    ssh_key: str = Form(None),
    retention_count: int = Form(30),
    session: dict = Depends(require_role("admin")),
):
    """Handle SFTP backup configuration."""
    success, message = await sftp_backup.configure_backup(
        host=sftp_host, port=sftp_port, path=sftp_path,
        username=sftp_username, auth_method=auth_method,
        password=sftp_password, ssh_key=ssh_key,
        retention_count=retention_count,
    )
    return templates.TemplateResponse("backup_setup.html", {
        "request": request, "backup_status": sftp_backup.get_backup_status(),
        "error": None if success else message,
        "success": message if success else None,
    }, status_code=200 if success else 400)


@app.post("/backup-run")
async def backup_run_now(request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Trigger an immediate backup."""
    success, message = await sftp_backup.run_backup()
    return templates.TemplateResponse("backup_setup.html", {
        "request": request, "backup_status": sftp_backup.get_backup_status(),
        "error": None if success else message,
        "success": message if success else None,
    })


@app.get("/api/backup/status", tags=["config"])
async def get_backup_status_api(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    return sftp_backup.get_backup_status()


@app.post("/api/backup/run", tags=["config"])
async def api_backup_run_now(session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Trigger an immediate backup and return JSON result."""
    success, message = await sftp_backup.run_backup()
    return {"success": success, "message": message}


@app.post("/api/backup/test-connection", tags=["config"])
async def test_backup_connection_api(session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Test SFTP backup connection."""
    success, message = await sftp_backup.test_backup_connection()
    return {"success": success, "message": message}


# ============================================================================
# Page Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: dict = Depends(require_auth)):
    """Serve the main page (monitor view)."""
    if is_setup_required():
        return RedirectResponse(url="/setup", status_code=302)
    if _is_wizard_needed():
        return RedirectResponse(url="/setup-wizard", status_code=302)
    return render_template(request, "monitor.html")



# ============================================================================
# WebSocket
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    session = await require_auth_ws(websocket)
    if not session:
        await websocket.close(code=4001, reason="Not authenticated")
        return

    await websocket.accept()
    active_websockets.add(websocket)
    logger.info(f"WebSocket connected. Total: {len(active_websockets)}")

    # Send current topology on connect
    poller = get_poller()
    if poller:
        topology = poller.get_topology()
        await websocket.send_json({
            "type": "topology_update",
            "topology": topology,
        })

    # Send active running job state on connect
    for job in update_jobs.values():
        if job.status == "running":
            await websocket.send_json({
                "type": "job_started",
                "job_id": job.job_id,
                "device_count": len(job.devices),
                "firmware_names": job.firmware_names,
                "ap_cpe_map": job.ap_cpe_map,
                "device_roles": job.device_roles,
                "device_parent": job.device_parent,
                "bank_mode": job.bank_mode,
            })
            for ip, ds in job.devices.items():
                await websocket.send_json({
                    "type": "device_update",
                    "job_id": job.job_id,
                    "ip": ip,
                    "status": ds.status,
                    "message": ds.progress_message,
                    "old_version": ds.old_version,
                    "new_version": ds.new_version,
                    "error": ds.error,
                    "bank1_version": ds.bank1_version,
                    "bank2_version": ds.bank2_version,
                    "active_bank": ds.active_bank,
                    "role": ds.role,
                    "parent_ap": ds.parent_ap,
                    "model": ds.model,
                })

    # Send completed job history from database
    for hist in db.get_job_history(limit=20):
        await websocket.send_json({
            "type": "job_history",
            "job_id": hist["job_id"],
            "timestamp": hist["completed_at"],
            "duration": round(hist["duration"]),
            "success_count": hist["success_count"],
            "failed_count": hist["failed_count"],
            "skipped_count": hist["skipped_count"],
            "cancelled_count": hist["cancelled_count"],
            "ap_cpe_map": hist["ap_cpe_map"],
            "device_roles": hist["device_roles"],
            "devices": hist["devices"],
            "timezone": hist.get("timezone"),
        })

    # Send license state
    _ls = get_license_state()
    await websocket.send_json({
        "type": "license_state",
        **_ls.to_dict(),
        **get_nag_info(),
        "features": {f.value: _ls.is_feature_enabled(f) for f in Feature},
    })

    # Send scheduler status (includes rollout info)
    scheduler = get_scheduler()
    if scheduler:
        status = scheduler.get_status()
        await websocket.send_json({
            "type": "scheduler_status",
            **status,
        })
        # Also send dedicated rollout_status message
        if status.get("rollout"):
            await websocket.send_json({
                "type": "rollout_status",
                "rollout": status["rollout"],
            })

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=300)
            except asyncio.TimeoutError:
                # Send a ping to check if client is still alive
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        active_websockets.discard(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(active_websockets)}")


def _validate_ip(ip: str):
    """Validate that a string is a valid IP address, raising HTTPException if not."""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(400, f"Invalid IP address: {ip}")


# ============================================================================
# Tower Site API
# ============================================================================

@app.get("/api/sites", tags=["devices"])
async def list_sites(session: dict = Depends(require_auth)):
    """List all tower sites."""
    sites = db.get_tower_sites()
    return {"sites": sites}


@app.post("/api/sites", tags=["devices"])
async def create_site(
    name: str = Form(...),
    location: str = Form(None),
    latitude: float = Form(None),
    longitude: float = Form(None),
    session: dict = Depends(require_role("admin", "operator")),
    _pro=Depends(require_feature(Feature.TOWER_SITES)),
):
    """Create a new tower site."""
    try:
        site_id = db.create_tower_site(name, location, latitude, longitude)
        return {"id": site_id, "name": name}
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(400, f"Site '{name}' already exists")
        logger.error(f"Failed to create site: {e}")
        raise HTTPException(500, "Failed to create site")


@app.put("/api/sites/{site_id}", tags=["devices"])
async def update_site(
    site_id: int,
    name: str = Form(None),
    location: str = Form(None),
    latitude: float = Form(None),
    longitude: float = Form(None),
    session: dict = Depends(require_role("admin", "operator")),
    _pro=Depends(require_feature(Feature.TOWER_SITES)),
):
    """Update a tower site."""
    db.update_tower_site(site_id, name=name, location=location, latitude=latitude, longitude=longitude)
    return {"success": True}


@app.delete("/api/sites/{site_id}", tags=["devices"])
async def delete_site(site_id: int, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.TOWER_SITES))):
    """Delete a tower site."""
    db.delete_tower_site(site_id)
    return {"success": True}


# ============================================================================
# Access Point API
# ============================================================================

def _strip_credentials(devices: list[dict]) -> list[dict]:
    """Remove password fields from device dicts before returning to client."""
    for d in devices:
        d.pop("password", None)
    return devices


@app.get("/api/aps", tags=["devices"])
async def list_aps(site_id: int = None, session: dict = Depends(require_auth)):
    """List access points (credentials redacted)."""
    aps = db.get_access_points(tower_site_id=site_id, enabled_only=False)
    return {"aps": _strip_credentials(aps)}


@app.post("/api/aps", tags=["devices"])
async def add_ap(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    tower_site_id: int = Form(None),
    session: dict = Depends(require_role("admin", "operator")),
):
    """Add a new access point."""
    _validate_ip(ip)
    ap_id = db.upsert_access_point(ip, username, password, tower_site_id)

    # Trigger immediate poll
    poller = get_poller()
    if poller:
        await poller.poll_ap_now(ip)

    # Broadcast updated scheduler status so predictions reflect the new device
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"id": ap_id, "ip": ip}


@app.post("/api/devices", tags=["devices"])
async def add_device(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    tower_site_id: int = Form(None),
    session: dict = Depends(require_role("admin", "operator")),
):
    """Add a device, auto-classifying as AP or switch based on model.

    Probes the device to get its model. TNA models -> access_points table,
    TNS models -> switches table.
    """
    _validate_ip(ip)
    # Probe device to determine type
    client = TachyonClient(ip, username, password)
    try:
        login_result = await client.login()
        if login_result is not True:
            raise HTTPException(status_code=400, detail=f"Cannot connect to {ip}")
        info = await client.get_ap_info()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to connect to device {ip}: {e}")
        raise HTTPException(status_code=400, detail=f"Cannot connect to {ip}")

    model = info.get("model", "")
    poller = get_poller()

    if _is_tns100_model(model):
        device_id = db.upsert_switch(ip, username, password, tower_site_id)
        device_type = "switch"
        if poller:
            await poller.poll_switch_now(ip)
    else:
        device_id = db.upsert_access_point(ip, username, password, tower_site_id)
        device_type = "ap"
        if poller:
            await poller.poll_ap_now(ip)

    # Broadcast updated scheduler status so predictions reflect the new device
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"id": device_id, "ip": ip, "device_type": device_type, "model": model}


@app.put("/api/aps/{ip}", tags=["devices"])
async def update_ap(
    ip: str,
    username: str = Form(None),
    password: str = Form(None),
    tower_site_id: int = Form(None),
    enabled: bool = Form(None),
    notes: str = Form(None),
    session: dict = Depends(require_role("admin", "operator")),
):
    """Update an access point."""
    ap = db.get_access_point(ip)
    if not ap:
        raise HTTPException(404, f"AP not found: {ip}")

    # Use existing values if not provided
    new_username = username if username else ap["username"]
    new_password = password if password else ap["password"]
    new_site_id = tower_site_id if tower_site_id is not None else ap["tower_site_id"]
    credentials_changed = new_username != ap["username"] or new_password != ap["password"]

    if credentials_changed:
        client = TachyonClient(ip, new_username, new_password)
        try:
            login_result = await client.login()
            if login_result is not True:
                raise HTTPException(400, f"Cannot connect to {ip} with the supplied credentials")
            await client.get_ap_info()
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Failed to validate AP credentials for {ip}: {exc}")
            raise HTTPException(400, f"Cannot connect to {ip} with the supplied credentials")

    kwargs = {}
    if enabled is not None:
        kwargs["enabled"] = enabled
    if notes is not None:
        kwargs["notes"] = notes
    db.upsert_access_point(ip, new_username, new_password, new_site_id, **kwargs)

    if credentials_changed:
        poller = get_poller()
        if poller:
            poller.invalidate_client(ip)
            success = await poller.poll_ap_now(ip)
            refreshed = db.get_access_point(ip)
            if not success or (refreshed and refreshed.get("last_error")):
                detail = refreshed.get("last_error") if refreshed else ""
                raise HTTPException(502, detail or f"Failed to verify {ip}")

    return {"success": True}


@app.delete("/api/aps/{ip}", tags=["devices"])
async def delete_ap(ip: str, session: dict = Depends(require_role("admin", "operator"))):
    """Delete an access point."""
    poller = get_poller()
    if poller:
        poller.invalidate_client(ip)

    db.delete_access_point(ip)

    # Broadcast updated scheduler status so predictions reflect the removal
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"success": True}


@app.post("/api/aps/{ip}/poll", tags=["devices"])
async def poll_ap(ip: str, session: dict = Depends(require_role("admin", "operator"))):
    """Trigger immediate poll of an AP."""
    poller = get_poller()
    if not poller:
        raise HTTPException(500, "Poller not initialized")
    if not db.get_access_point(ip):
        raise HTTPException(404, f"AP not found: {ip}")

    success = await poller.poll_ap_now(ip)
    if not success:
        refreshed = db.get_access_point(ip) or {}
        raise HTTPException(502, refreshed.get("last_error") or f"Failed to poll {ip}")

    return {"success": True}


# ============================================================================
# Switch API
# ============================================================================

@app.get("/api/switches", tags=["devices"])
async def list_switches(site_id: int = None, session: dict = Depends(require_auth)):
    """List switches (credentials redacted)."""
    switches = db.get_switches(tower_site_id=site_id, enabled_only=False)
    return {"switches": _strip_credentials(switches)}


@app.post("/api/switches", tags=["devices"])
async def add_switch(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    tower_site_id: int = Form(None),
    session: dict = Depends(require_role("admin", "operator")),
):
    """Add a new switch."""
    _validate_ip(ip)
    sw_id = db.upsert_switch(ip, username, password, tower_site_id)

    poller = get_poller()
    if poller:
        await poller.poll_switch_now(ip)

    # Broadcast updated scheduler status so predictions reflect the new device
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"id": sw_id, "ip": ip}


@app.put("/api/switches/{ip}", tags=["devices"])
async def update_switch(
    ip: str,
    username: str = Form(None),
    password: str = Form(None),
    tower_site_id: int = Form(None),
    enabled: bool = Form(None),
    notes: str = Form(None),
    session: dict = Depends(require_role("admin", "operator")),
):
    """Update a switch."""
    sw = db.get_switch(ip)
    if not sw:
        raise HTTPException(404, f"Switch not found: {ip}")

    new_username = username if username else sw["username"]
    new_password = password if password else sw["password"]
    new_site_id = tower_site_id if tower_site_id is not None else sw["tower_site_id"]

    kwargs = {}
    if enabled is not None:
        kwargs["enabled"] = enabled
    if notes is not None:
        kwargs["notes"] = notes
    db.upsert_switch(ip, new_username, new_password, new_site_id, **kwargs)

    if username or password:
        poller = get_poller()
        if poller:
            poller.invalidate_client(ip)

    return {"success": True}


@app.delete("/api/switches/{ip}", tags=["devices"])
async def delete_switch(ip: str, session: dict = Depends(require_role("admin", "operator"))):
    """Delete a switch."""
    poller = get_poller()
    if poller:
        poller.invalidate_client(ip)

    db.delete_switch(ip)

    # Broadcast updated scheduler status so predictions reflect the removal
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"success": True}


# ============================================================================
# Bulk Device Operations
# ============================================================================

@app.post("/api/devices/bulk-enable", tags=["devices"])
async def bulk_enable_devices(
    request: Request,
    session: dict = Depends(require_role("admin", "operator")),
):
    """Enable multiple devices."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    device_type = body.get("device_type", "ap")
    ips = body.get("ips", [])
    if not ips or device_type not in ("ap", "switch"):
        raise HTTPException(400, "Provide ips list and device_type (ap or switch)")
    try:
        count = db.bulk_set_enabled(device_type, ips, True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, "affected": count}


@app.post("/api/devices/bulk-disable", tags=["devices"])
async def bulk_disable_devices(
    request: Request,
    session: dict = Depends(require_role("admin", "operator")),
):
    """Disable multiple devices."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    device_type = body.get("device_type", "ap")
    ips = body.get("ips", [])
    if not ips or device_type not in ("ap", "switch"):
        raise HTTPException(400, "Provide ips list and device_type (ap or switch)")
    try:
        count = db.bulk_set_enabled(device_type, ips, False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, "affected": count}


@app.post("/api/devices/bulk-delete", tags=["devices"])
async def bulk_delete_devices(
    request: Request,
    session: dict = Depends(require_role("admin")),
):
    """Delete multiple devices. Admin only."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    device_type = body.get("device_type", "ap")
    ips = body.get("ips", [])
    if not ips or device_type not in ("ap", "switch"):
        raise HTTPException(400, "Provide ips list and device_type (ap or switch)")
    poller = get_poller()
    if poller:
        for ip in ips:
            poller.invalidate_client(ip)
    try:
        count = db.bulk_delete_devices(device_type, ips)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, "deleted": count}


@app.post("/api/devices/bulk-move", tags=["devices"])
async def bulk_move_devices(
    request: Request,
    session: dict = Depends(require_role("admin", "operator")),
):
    """Move multiple devices to a site."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    device_type = body.get("device_type", "ap")
    ips = body.get("ips", [])
    site_id = body.get("site_id")
    if not ips or device_type not in ("ap", "switch"):
        raise HTTPException(400, "Provide ips list and device_type (ap or switch)")
    try:
        count = db.bulk_move_to_site(device_type, ips, site_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, "affected": count}


@app.post("/api/switches/{ip}/poll", tags=["devices"])
async def poll_switch(ip: str, session: dict = Depends(require_role("admin", "operator"))):
    """Trigger immediate poll of a switch."""
    poller = get_poller()
    if not poller:
        raise HTTPException(500, "Poller not initialized")

    success = await poller.poll_switch_now(ip)
    if not success:
        raise HTTPException(404, f"Switch not found: {ip}")

    return {"success": True}


# ============================================================================
# Topology API
# ============================================================================

@app.get("/api/topology", tags=["devices"])
async def get_topology(session: dict = Depends(require_auth)):
    """Get current network topology."""
    poller = get_poller()
    if poller:
        return poller.get_topology()

    return {
        "sites": [],
        "total_aps": 0,
        "total_cpes": 0,
        "total_switches": 0,
        "overall_health": {"green": 0, "yellow": 0, "red": 0},
    }


@app.post("/api/topology/refresh", tags=["devices"])
async def refresh_topology(session: dict = Depends(require_role("admin", "operator"))):
    """Trigger a full topology refresh."""
    poller = get_poller()
    if not poller:
        raise HTTPException(500, "Poller not initialized")

    await poller._poll_all_aps()
    return poller.get_topology()


@app.get("/api/cpes", tags=["devices"])
async def get_all_cpes(session: dict = Depends(require_auth)):
    """Get all CPEs."""
    cpes = db.get_all_cpes()
    return {"cpes": cpes}


# ============================================================================
# Device Portal (auto-login redirect)
# ============================================================================

def _build_device_portal_html(ip: str, safe_form_name: str) -> str:
    """Build the auto-login HTML page for a device."""
    escaped_ip = html_module.escape(ip)
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Connecting to {escaped_ip}...</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0; background: #111827; color: #e5e7eb;
        }}
        .container {{ text-align: center; }}
        .spinner {{
            width: 40px; height: 40px; margin: 0 auto 16px;
            border: 3px solid #374151; border-top-color: #60a5fa;
            border-radius: 50%; animation: spin 0.8s linear infinite;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        .fallback {{ display: none; margin-top: 20px; font-size: 0.9rem; color: #9ca3af; }}
        .fallback a {{ color: #60a5fa; text-decoration: none; }}
        .fallback a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="spinner" id="spinner"></div>
        <p id="status">Logging in to {escaped_ip}...</p>
        <div id="fallback" class="fallback">
            <p>Auto-login may not have succeeded.</p>
            <p><a href="https://{escaped_ip}/">Open {escaped_ip} manually</a></p>
        </div>
    </div>
    <iframe name="loginFrame" style="display:none;"></iframe>
    <form id="loginForm" method="POST" action="https://{escaped_ip}/cgi.lua/login"
          target="loginFrame" enctype="text/plain" style="display:none;">
        <input name='{safe_form_name}' value='"}}'>

    </form>
    <script>
        document.getElementById('loginForm').submit();
        setTimeout(function() {{
            window.location.href = 'https://{escaped_ip}/';
        }}, 2000);
        setTimeout(function() {{
            document.getElementById('fallback').style.display = 'block';
            document.getElementById('spinner').style.display = 'none';
            document.getElementById('status').textContent = 'Redirecting...';
        }}, 5000);
    </script>
</body>
</html>"""


@app.get("/api/device-portal/{ip}", tags=["devices"], response_class=HTMLResponse)
async def device_portal(ip: str, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.DEVICE_PORTAL))):
    """Auto-login portal: authenticate to a device and redirect to its web UI."""
    _validate_ip(ip)

    username = None
    password = None

    # Check APs first
    ap = db.get_access_point(ip)
    if ap:
        username, password = ap["username"], ap["password"]
    else:
        # Check switches
        sw = db.get_switch(ip)
        if sw:
            username, password = sw["username"], sw["password"]
        else:
            # Check CPEs (inherit parent AP credentials)
            cpe = db.get_cpe_by_ip(ip)
            if cpe:
                parent_ap = db.get_access_point(cpe["ap_ip"])
                if parent_ap:
                    username, password = parent_ap["username"], parent_ap["password"]

    if not username or not password:
        raise HTTPException(404, "Device not found or missing credentials")

    # Build JSON body via enctype="text/plain" trick:
    # Input name becomes the body prefix, value becomes "="}
    # Result: {"username":"...","password":"...","_":"="}
    json_name = f'{{"username":{json.dumps(username)},"password":{json.dumps(password)},"_":"'
    safe_name = html_module.escape(json_name, quote=True)

    page = _build_device_portal_html(ip, safe_name)

    return HTMLResponse(
        content=page,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


# ============================================================================
# Quick Add (combines site + AP creation)
# ============================================================================

@app.post("/api/quick-add", tags=["devices"])
async def quick_add(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    site_name: str = Form(None),
    session: dict = Depends(require_role("admin", "operator")),
):
    """Quick add an AP, optionally creating a new site."""
    _validate_ip(ip)
    site_id = None

    if site_name:
        # Try to find existing site or create new
        sites = db.get_tower_sites()
        existing = next((s for s in sites if s["name"] == site_name), None)

        if existing:
            site_id = existing["id"]
        else:
            site_id = db.create_tower_site(site_name)

    # Add the AP
    ap_id = db.upsert_access_point(ip, username, password, site_id)

    # Trigger immediate poll
    poller = get_poller()
    if poller:
        await poller.poll_ap_now(ip)

    # Broadcast updated scheduler status so predictions reflect the new device
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"ap_id": ap_id, "site_id": site_id, "ip": ip}


# ============================================================================
# Settings API
# ============================================================================

_SETTINGS_SENSITIVE = {
    "admin_password_hash", "oidc_client_secret",
    "device_default_password", "license_key",
    "radius_server_secret", "radius_server_ldap_bind_password",
}


@app.get("/api/settings", tags=["settings"])
async def get_settings(session: dict = Depends(require_auth)):
    """Get all settings. Sensitive values are redacted."""
    settings = db.get_all_settings()
    for key in _SETTINGS_SENSITIVE:
        if key in settings and settings[key]:
            settings[key] = "********"
    # Resolve temperature unit for UI
    temp_unit_setting = settings.get("temperature_unit", "auto")
    resolved_unit = await services.resolve_temperature_unit(temp_unit_setting)
    return {"settings": settings, "resolved_temperature_unit": resolved_unit}


_SETTINGS_WRITABLE = {
    "schedule_enabled", "schedule_days", "schedule_start_hour", "schedule_end_hour",
    "parallel_updates", "bandwidth_limit_kbps",
    "bank_mode", "allow_downgrade", "timezone", "zip_code",
    "weather_check_enabled", "min_temperature_c", "temperature_unit",
    "schedule_scope", "schedule_scope_data",
    "rollout_canary_aps", "rollout_canary_switches",
    "firmware_beta_enabled", "firmware_quarantine_days",
    "slack_webhook_url",
    "snmp_traps_enabled", "snmp_trap_host", "snmp_trap_port",
    "snmp_trap_community", "snmp_trap_version",
    "autoupdate_enabled", "release_channel",
    "selected_firmware_30x", "selected_firmware_303l", "selected_firmware_tns100",
    "pre_update_reboot",
}


def _parse_int_field(value, field: str) -> int:
    """Parse an integer field value or raise a 400."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid integer for {field}")


def _parse_float_field(value, field: str) -> float:
    """Parse a float field value or raise a 400."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid number for {field}")


def _parse_ip_csv(value: str) -> list[str]:
    return [ip.strip() for ip in (value or "").split(",") if ip.strip()]


def _resolve_rollout_scopes_for_validation(settings: dict) -> tuple[set[str], set[str]]:
    scope = settings.get("schedule_scope", "all")
    scope_data = settings.get("schedule_scope_data", "")

    if scope == "all":
        return (
            {ap["ip"] for ap in db.get_access_points(enabled_only=True)},
            {sw["ip"] for sw in db.get_switches(enabled_only=True)},
        )

    if scope == "sites":
        site_ids = [int(s.strip()) for s in scope_data.split(",") if s.strip().isdigit()]
        ap_ips = set()
        sw_ips = set()
        for site_id in site_ids:
            ap_ips.update(ap["ip"] for ap in db.get_access_points(tower_site_id=site_id, enabled_only=True))
            sw_ips.update(sw["ip"] for sw in db.get_switches(tower_site_id=site_id, enabled_only=True))
        return ap_ips, sw_ips

    if scope == "aps":
        ap_ips = {ip for ip in _parse_ip_csv(scope_data) if (db.get_access_point(ip) or {}).get("enabled", 0)}
        site_ids = {
            ap.get("tower_site_id")
            for ip in ap_ips
            for ap in [db.get_access_point(ip)]
            if ap and ap.get("tower_site_id") is not None
        }
        sw_ips = set()
        for site_id in site_ids:
            sw_ips.update(sw["ip"] for sw in db.get_switches(tower_site_id=site_id, enabled_only=True))
        return ap_ips, sw_ips

    return set(), set()


def _validate_settings(filtered: dict):
    """Validate individual setting values before persisting."""
    url = filtered.get("slack_webhook_url")
    if url and not slack.is_valid_slack_url(url):
        raise HTTPException(400, "Slack webhook URL must be a valid https://hooks.slack.com/ URL")

    # Validate SNMP trap settings
    trap_host = filtered.get("snmp_trap_host")
    if trap_host and not snmp.is_valid_trap_host(trap_host):
        raise HTTPException(400, "SNMP trap host must be a valid IP address or hostname")
    if "snmp_trap_port" in filtered:
        trap_port = _parse_int_field(filtered["snmp_trap_port"], "snmp_trap_port")
        if not 1 <= trap_port <= 65535:
            raise HTTPException(400, "snmp_trap_port must be between 1 and 65535")
    if "snmp_trap_version" in filtered:
        if filtered["snmp_trap_version"] not in ("2c",):
            raise HTTPException(400, "snmp_trap_version must be '2c'")

    # Validate enum/boolean-like fields
    if "bank_mode" in filtered:
        filtered["bank_mode"] = str(filtered["bank_mode"]).lower()
    if "bank_mode" in filtered and filtered["bank_mode"] not in ("one", "both"):
        raise HTTPException(400, "bank_mode must be 'one' or 'both'")
    if "release_channel" in filtered:
        filtered["release_channel"] = str(filtered["release_channel"]).lower()
    if "release_channel" in filtered and filtered["release_channel"] not in ("stable", "dev"):
        raise HTTPException(400, "release_channel must be 'stable' or 'dev'")
    if "temperature_unit" in filtered:
        filtered["temperature_unit"] = str(filtered["temperature_unit"]).lower()
    if "temperature_unit" in filtered and filtered["temperature_unit"] not in ("auto", "c", "f"):
        raise HTTPException(400, "temperature_unit must be 'auto', 'c', or 'f'")

    bool_like = {
        "schedule_enabled", "allow_downgrade", "weather_check_enabled",
        "firmware_beta_enabled", "autoupdate_enabled", "pre_update_reboot",
        "snmp_traps_enabled",
    }
    for key in bool_like:
        if key in filtered:
            value = str(filtered[key]).lower()
            if value not in ("true", "false"):
                raise HTTPException(400, f"{key} must be 'true' or 'false'")
            filtered[key] = value

    # Validate numeric fields and ranges
    if "schedule_start_hour" in filtered:
        start_hour = _parse_int_field(filtered["schedule_start_hour"], "schedule_start_hour")
        if not 0 <= start_hour <= 23:
            raise HTTPException(400, "schedule_start_hour must be between 0 and 23")
    if "schedule_end_hour" in filtered:
        end_hour = _parse_int_field(filtered["schedule_end_hour"], "schedule_end_hour")
        if not 0 <= end_hour <= 23:
            raise HTTPException(400, "schedule_end_hour must be between 0 and 23")
    if "parallel_updates" in filtered:
        parallel = _parse_int_field(filtered["parallel_updates"], "parallel_updates")
        if parallel < 1 or parallel > 32:
            raise HTTPException(400, "parallel_updates must be between 1 and 32")
    if "bandwidth_limit_kbps" in filtered:
        bw = _parse_int_field(filtered["bandwidth_limit_kbps"], "bandwidth_limit_kbps")
        if bw < 0 or bw > 1000000:
            raise HTTPException(400, "bandwidth_limit_kbps must be between 0 and 1000000 (0 = unlimited)")
    if "firmware_quarantine_days" in filtered:
        hold_days = _parse_int_field(filtered["firmware_quarantine_days"], "firmware_quarantine_days")
        if hold_days < 0 or hold_days > 365:
            raise HTTPException(400, "firmware_quarantine_days must be between 0 and 365")
    if "min_temperature_c" in filtered:
        min_temp = _parse_float_field(filtered["min_temperature_c"], "min_temperature_c")
        if min_temp < -100 or min_temp > 100:
            raise HTTPException(400, "min_temperature_c must be between -100 and 100")

    # Validate schedule_days format
    if "schedule_days" in filtered:
        allowed_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        day_values = [d.strip().lower() for d in str(filtered["schedule_days"]).split(",") if d.strip()]
        if not all(d in allowed_days for d in day_values):
            raise HTTPException(400, "schedule_days must be comma-separated day abbreviations (mon..sun)")

    # Validate firmware filenames to prevent traversal via settings
    for fw_key in ("selected_firmware_30x", "selected_firmware_303l", "selected_firmware_tns100"):
        if fw_key in filtered and filtered[fw_key]:
            validate_firmware_filename(str(filtered[fw_key]))

    # License-gated settings
    if filtered.get("slack_webhook_url") and not is_feature_enabled(Feature.SLACK_NOTIFICATIONS):
        raise HTTPException(403, detail={"error": "feature_locked", "feature": "slack_notifications",
                                         "message": "Slack notifications require a Pro license."})
    if filtered.get("snmp_traps_enabled") == "true" and not is_feature_enabled(Feature.SNMP_TRAPS):
        raise HTTPException(403, detail={"error": "feature_locked", "feature": "snmp_traps",
                                         "message": "SNMP trap notifications require a Pro license."})
    if filtered.get("snmp_traps_enabled") == "true" and not snmp.is_pysnmp_available():
        raise HTTPException(400, "Cannot enable SNMP traps: pysnmp-lextudio is not installed")
    if filtered.get("firmware_beta_enabled") == "true" and not is_feature_enabled(Feature.BETA_FIRMWARE):
        raise HTTPException(403, detail={"error": "feature_locked", "feature": "beta_firmware",
                                         "message": "Beta firmware channel requires a Pro license."})
    if "firmware_quarantine_days" in filtered and not is_feature_enabled(Feature.FIRMWARE_HOLD_CUSTOM):
        current = db.get_setting("firmware_quarantine_days", "7")
        if filtered["firmware_quarantine_days"] != current:
            raise HTTPException(403, detail={"error": "feature_locked", "feature": "firmware_hold_custom",
                                             "message": "Custom firmware hold period requires a Pro license."})

    effective_settings = db.get_all_settings()
    effective_settings.update(filtered)
    ap_scope, switch_scope = _resolve_rollout_scopes_for_validation(effective_settings)
    canary_specs = (
        ("rollout_canary_aps", db.get_access_point, ap_scope, "AP"),
        ("rollout_canary_switches", db.get_switch, switch_scope, "switch"),
    )
    for key, getter, scope, label in canary_specs:
        if key not in filtered:
            continue
        invalid = []
        missing = []
        disabled = []
        out_of_scope = []
        for ip in _parse_ip_csv(filtered.get(key, "")):
            try:
                _validate_ip(ip)
            except HTTPException:
                invalid.append(ip)
                continue
            device = getter(ip)
            if not device:
                missing.append(ip)
                continue
            if not device.get("enabled", 1):
                disabled.append(ip)
                continue
            if scope and ip not in scope:
                out_of_scope.append(ip)
        if invalid or missing or disabled or out_of_scope:
            parts = []
            if invalid:
                parts.append(f"invalid IPs: {', '.join(invalid)}")
            if missing:
                parts.append(f"unknown {label}s: {', '.join(missing)}")
            if disabled:
                parts.append(f"disabled {label}s: {', '.join(disabled)}")
            if out_of_scope:
                parts.append(f"out of rollout scope: {', '.join(out_of_scope)}")
            raise HTTPException(400, f"Invalid {label} canary selection ({'; '.join(parts)})")


@app.put("/api/settings", tags=["settings"])
async def update_settings(request: Request, session: dict = Depends(require_role("admin"))):
    """Update settings. Only whitelisted keys are accepted."""
    data = await request.json()
    filtered = {k: v for k, v in data.items() if k in _SETTINGS_WRITABLE}
    if not filtered:
        raise HTTPException(400, "No valid settings keys provided")
    _validate_settings(filtered)
    db.set_settings(filtered)
    return {"success": True}


@app.post("/api/settings/save", tags=["settings"])
async def save_settings_and_reevaluate(request: Request, session: dict = Depends(require_role("admin"))):
    """Save settings, re-select firmware, and force scheduler re-evaluation."""
    data = await request.json()
    filtered = {k: v for k, v in data.items() if k in _SETTINGS_WRITABLE}
    if not filtered:
        raise HTTPException(400, "No valid settings keys provided")

    _validate_settings(filtered)
    db.set_settings(filtered)

    fetcher = get_fetcher()
    if fetcher:
        beta_enabled = db.get_setting("firmware_beta_enabled", "false") == "true"
        fetcher.reselect(beta_enabled)

    scheduler = get_scheduler()
    if scheduler:
        await scheduler.force_check()

    return {"success": True}


@app.post("/api/slack/test", tags=["notifications"])
async def test_slack_webhook(session: dict = Depends(require_role("admin")), _pro=Depends(require_feature(Feature.SLACK_NOTIFICATIONS))):
    """Send a test notification to the configured Slack webhook."""
    success, message = await slack.send_test_notification()
    return {"success": success, "message": message}


@app.post("/api/snmp/test", tags=["notifications"])
async def test_snmp_trap(session: dict = Depends(require_role("admin")), _pro=Depends(require_feature(Feature.SNMP_TRAPS))):
    """Send a test SNMP trap to verify configuration."""
    success, message = await snmp.send_test_trap()
    return {"success": success, "message": message}


# ============================================================================
# License API
# ============================================================================

@app.get("/api/license", tags=["license"])
async def get_license_status(session: dict = Depends(require_auth)):
    """Get current license state, features map, and device counts."""
    state = get_license_state()
    nag = get_nag_info()
    features = {f.value: state.is_feature_enabled(f) for f in Feature}
    return {**state.to_dict(), **nag, "features": features}


@app.post("/api/license/activate", tags=["license"])
async def activate_license(request: Request, session: dict = Depends(require_role("admin"))):
    """Activate or update the license key."""
    data = await request.json()
    key = data.get("license_key", "").strip()
    if not key:
        raise HTTPException(400, "License key is required")
    from .license import LicenseStatus
    state = await validate_license(license_key=key)
    return {**state.to_dict(), "success": state.status == LicenseStatus.ACTIVE}


@app.post("/api/license/deactivate", tags=["license"])
async def deactivate_license(session: dict = Depends(require_role("admin"))):
    """Remove the license key and revert to free tier."""
    clear_license()
    return {"success": True, "status": "free"}


@app.post("/api/license/validate", tags=["license"])
async def force_validate_license(session: dict = Depends(require_role("admin"))):
    """Force re-validate the current license with the server."""
    state = get_license_state()
    if not state.license_key:
        raise HTTPException(400, "No license key configured")
    result = await validate_license()
    return result.to_dict()


# ============================================================================
# System / Appliance API
# ============================================================================

@app.get("/api/system/info", tags=["system"])
async def get_system_info(session: dict = Depends(require_auth)):
    """Get system information (version, uptime, disk usage, machine ID)."""
    import shutil
    import platform

    from .release_checker import get_appliance_version

    appliance_mode = os.environ.get("TACHYON_APPLIANCE") == "1"
    info = {
        "version": __version__,
        "appliance_mode": appliance_mode,
        "appliance_version": get_appliance_version(),
        "os": platform.system(),
        "os_version": platform.release(),
        "uptime_seconds": None,
        "disk_usage": None,
        "machine_id": None,
    }

    try:
        with open("/proc/uptime") as f:
            info["uptime_seconds"] = float(f.read().split()[0])
    except (FileNotFoundError, ValueError):
        pass

    data_path = Path("/data") if appliance_mode else DATA_DIR
    try:
        usage = shutil.disk_usage(str(data_path))
        info["disk_usage"] = {
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "percent": round(usage.used / usage.total * 100, 1),
        }
    except (FileNotFoundError, OSError):
        pass

    try:
        with open("/sys/class/dmi/id/product_uuid") as f:
            info["machine_id"] = f.read().strip()[:8].upper()
    except (FileNotFoundError, PermissionError):
        pass

    return info


@app.post("/api/system/network", tags=["system"])
async def update_network_config(request: Request, session: dict = Depends(require_role("admin"))):
    """Update network configuration (appliance mode only)."""
    if os.environ.get("TACHYON_APPLIANCE") != "1":
        raise HTTPException(404, "Not available in this deployment mode")

    data = await request.json()
    mode = data.get("mode", "dhcp")
    if mode not in ("dhcp", "static"):
        raise HTTPException(400, "Mode must be 'dhcp' or 'static'")

    def _validate_ip_field(value: str, field_name: str) -> str:
        """Validate IP address format and reject shell metacharacters."""
        if not value:
            return ""
        # Reject any shell metacharacters
        if re.search(r'[;|&$`\\\'"\n\r(){}!<>]', value):
            raise HTTPException(400, f"Invalid characters in {field_name}")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            # Also allow CIDR notation for netmask-like values
            parts = value.split(".")
            if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                raise HTTPException(400, f"Invalid IP address format for {field_name}")
        return value

    config_lines = [f"MODE={mode}"]
    if mode == "static":
        for field_name in ("address", "netmask", "gateway", "dns"):
            value = data.get(field_name, "")
            if field_name in ("address", "gateway") and not value:
                raise HTTPException(400, f"{field_name} is required for static IP")
            if value:
                value = _validate_ip_field(value, field_name)
            config_lines.append(f"{field_name.upper()}={value}")

    network_conf = Path("/data/network/network.conf")
    network_conf.parent.mkdir(parents=True, exist_ok=True)
    network_conf.write_text("\n".join(config_lines) + "\n")

    return {"success": True, "mode": mode}


# ============================================================================
# Auto-Update API
# ============================================================================

@app.get("/api/updates", tags=["jobs"])
async def get_update_status(session: dict = Depends(require_auth)):
    """Get current update status."""
    checker = get_checker()
    if checker:
        return checker.get_update_status()
    return {"error": "Release checker not initialized"}


@app.post("/api/updates/check", tags=["jobs"])
async def check_for_updates(session: dict = Depends(require_role("admin", "operator"))):
    """Manually trigger a check for updates."""
    checker = get_checker()
    if checker:
        result = await checker.check_for_updates()
        return result
    return {"error": "Release checker not initialized"}


@app.post("/api/updates/apply", tags=["jobs"])
async def apply_app_update(session: dict = Depends(require_role("admin"))):
    """Apply available update by pulling new Docker image and restarting."""
    result = await apply_update()
    if result.get("success"):
        # Broadcast that update is starting
        await broadcast({"type": "update_started"})
    return result


# ============================================================================
# Authentication Configuration API
# ============================================================================

@app.get("/api/auth/config", tags=["auth"])
async def get_auth_config(session: dict = Depends(require_auth)):
    """Get authentication configuration (secrets masked)."""
    return radius_config.get_auth_config_summary()


@app.get("/api/auth/radius", tags=["auth"])
async def get_builtin_radius_config(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Get built-in RADIUS server configuration and summary."""
    return {
        **builtin_radius.get_public_config_summary(),
        "stats": builtin_radius.get_stats(limit=5),
    }


@app.post("/api/auth/radius/test", tags=["auth"])
async def test_builtin_radius(session: dict = Depends(require_role("admin")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Test the built-in RADIUS server health."""
    try:
        success, message = builtin_radius.test_radius_server()
        return {"success": success, "message": message}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


@app.put("/api/auth/radius", tags=["auth"])
async def update_builtin_radius_config(request: Request, session: dict = Depends(require_role("admin")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Update built-in RADIUS server settings."""
    data = await request.json()
    try:
        port = int(data.get("port", 1812) or 1812)
    except (TypeError, ValueError):
        raise HTTPException(400, "Port must be a number")
    config = builtin_radius.BuiltinRadiusConfig(
        enabled=bool(data.get("enabled", False)),
        host=(data.get("host", "") or "").strip(),
        port=port,
        secret=data.get("secret", ""),
    )

    if not config.secret:
        existing = builtin_radius.get_config()
        config.secret = existing.secret

    if config.enabled and not config.host:
        raise HTTPException(400, "Device host is required when built-in RADIUS is enabled")
    if config.enabled and not config.secret:
        raise HTTPException(400, "Shared secret is required when built-in RADIUS is enabled")

    builtin_radius.set_config(config)
    await builtin_radius.get_runtime().reload()
    return builtin_radius.get_public_config_summary()


@app.get("/api/auth/radius/users", tags=["auth"])
async def list_builtin_radius_users(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """List built-in RADIUS users."""
    return {"users": builtin_radius.list_users()}


@app.post("/api/auth/radius/users", tags=["auth"])
async def create_builtin_radius_user(request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Create a built-in RADIUS user."""
    data = await request.json()
    try:
        user = builtin_radius.create_user(
            username=data.get("username", ""),
            password=data.get("password", ""),
            enabled=bool(data.get("enabled", True)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await builtin_radius.get_runtime().reload()
    return user


@app.put("/api/auth/radius/users/{user_id}", tags=["auth"])
async def update_builtin_radius_user(user_id: int, request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Update a built-in RADIUS user."""
    data = await request.json()
    try:
        user = builtin_radius.update_user(
            user_id=user_id,
            username=data.get("username", ""),
            password=data.get("password", ""),
            enabled=bool(data.get("enabled", True)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await builtin_radius.get_runtime().reload()
    return user


@app.delete("/api/auth/radius/users/{user_id}", tags=["auth"])
async def delete_builtin_radius_user(user_id: int, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Delete a built-in RADIUS user."""
    deleted = builtin_radius.delete_user(user_id)
    if not deleted:
        raise HTTPException(404, "User not found")
    await builtin_radius.get_runtime().reload()
    return {"success": True}


@app.get("/api/auth/radius/clients", tags=["auth"])
async def list_builtin_radius_clients(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """List manual RADIUS client overrides."""
    return {"clients": builtin_radius.list_client_overrides()}


@app.post("/api/auth/radius/clients", tags=["auth"])
async def create_builtin_radius_client(request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Create a manual RADIUS client override."""
    data = await request.json()
    try:
        client = builtin_radius.create_client_override(
            client_spec=data.get("client_spec", ""),
            shortname=data.get("shortname", ""),
            enabled=bool(data.get("enabled", True)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await builtin_radius.get_runtime().reload()
    return client


@app.put("/api/auth/radius/clients/{override_id}", tags=["auth"])
async def update_builtin_radius_client(override_id: int, request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Update a manual RADIUS client override."""
    data = await request.json()
    try:
        client = builtin_radius.update_client_override(
            override_id=override_id,
            client_spec=data.get("client_spec", ""),
            shortname=data.get("shortname", ""),
            enabled=bool(data.get("enabled", True)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await builtin_radius.get_runtime().reload()
    return client


@app.delete("/api/auth/radius/clients/{override_id}", tags=["auth"])
async def delete_builtin_radius_client(override_id: int, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Delete a manual RADIUS client override."""
    deleted = builtin_radius.delete_client_override(override_id)
    if not deleted:
        raise HTTPException(404, "Client override not found")
    await builtin_radius.get_runtime().reload()
    return {"success": True}


@app.get("/api/auth/radius/stats", tags=["auth"])
async def get_builtin_radius_stats(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Get built-in RADIUS server stats and recent auth events."""
    return builtin_radius.get_stats()


@app.post("/api/auth/radius/secret-review", tags=["auth"])
async def mark_builtin_radius_secret_reviewed(session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Mark a legacy built-in RADIUS secret as reviewed today without changing it."""
    try:
        builtin_radius.mark_secret_reviewed()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return builtin_radius.get_public_config_summary()


@app.get("/api/auth/radius/rollout", tags=["auth"])
async def get_builtin_radius_rollout(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Get current built-in Radius device migration rollout state."""
    rollout = builtin_radius.get_current_rollout()
    if not rollout:
        return {"rollout": None}
    return {
        "rollout": {
            **rollout,
            "progress": builtin_radius.get_rollout_progress(rollout["id"]),
            "devices": _serialize_radius_rollout_devices(rollout["id"]),
        }
    }


@app.post("/api/auth/radius/rollout/start", tags=["auth"])
async def start_builtin_radius_rollout(session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Start staged Radius migration for managed devices."""
    if builtin_radius.get_active_rollout():
        raise HTTPException(400, "A Radius rollout is already active")

    config = builtin_radius.get_config()
    if not config.enabled or not config.secret or not config.host:
        raise HTTPException(400, "Built-in Radius must be enabled with a device host and shared secret before rollout")

    try:
        await _refresh_radius_rollout_inventory()
        template = _get_radius_rollout_template()
        _validate_radius_rollout_template(template, config)
        template["fragment"] = _apply_builtin_radius_settings_to_fragment(template["fragment"], config)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    devices = _radius_rollout_targets()
    if not devices:
        raise HTTPException(400, "No enabled APs, switches, or verified CPEs available for Radius rollout")

    service_username, _ = builtin_radius.get_management_service_credentials(create_if_missing=True)
    await builtin_radius.get_runtime().reload()
    rollout_id = builtin_radius.create_rollout(template["id"], service_username)
    _start_radius_rollout_task(rollout_id)
    rollout = builtin_radius.get_rollout(rollout_id)
    return {"rollout": rollout}


@app.post("/api/auth/radius/rollout/{rollout_id}/resume", tags=["auth"])
async def resume_builtin_radius_rollout(rollout_id: int, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Resume a paused Radius migration rollout."""
    rollout = builtin_radius.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(404, "Radius rollout not found")
    if rollout["status"] != "paused":
        raise HTTPException(400, "Radius rollout is not paused")

    builtin_radius.update_rollout_status(rollout_id, "active")
    _start_radius_rollout_task(rollout_id)
    return {"rollout": builtin_radius.get_rollout(rollout_id)}


@app.post("/api/auth/radius/rollout/{rollout_id}/cancel", tags=["auth"])
async def cancel_builtin_radius_rollout(rollout_id: int, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.RADIUS_AUTH))):
    """Cancel an active or paused Radius migration rollout."""
    rollout = builtin_radius.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(404, "Radius rollout not found")
    if rollout["status"] not in ("active", "paused"):
        raise HTTPException(400, "Radius rollout cannot be cancelled")

    builtin_radius.update_rollout_status(rollout_id, "cancelled")
    return {"success": True}


@app.put("/api/auth/device-defaults", tags=["auth"])
async def update_device_auth_config(request: Request, session: dict = Depends(require_role("admin"))):
    """Update global default device credentials."""
    data = await request.json()

    config = radius_config.DeviceAuthConfig(
        enabled=data.get("enabled", False),
        username=data.get("username", ""),
        password=data.get("password", ""),
    )

    # Preserve existing password if not provided (field is cleared on UI load)
    if not config.password:
        existing = radius_config.get_device_auth_config()
        config.password = existing.password

    radius_config.set_device_auth_config(config)
    return {"success": True}


@app.get("/api/auth/oidc", tags=["auth"])
async def get_oidc_config_api(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.SSO_OIDC))):
    """Get OIDC/SSO configuration (secret masked)."""
    config = oidc_config.get_oidc_config()
    return {
        "enabled": config.enabled,
        "provider_url": config.provider_url,
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "allowed_group": config.allowed_group,
        "scopes": config.scopes,
        "configured": oidc_config.is_oidc_enabled(),
    }


@app.put("/api/auth/oidc", tags=["auth"])
async def update_oidc_config_api(request: Request, session: dict = Depends(require_role("admin")), _pro=Depends(require_feature(Feature.SSO_OIDC))):
    """Update OIDC/SSO configuration."""
    data = await request.json()

    config = oidc_config.OIDCConfig(
        enabled=data.get("enabled", False),
        provider_url=data.get("provider_url", ""),
        client_id=data.get("client_id", ""),
        client_secret=data.get("client_secret", ""),
        redirect_uri=data.get("redirect_uri", ""),
        allowed_group=data.get("allowed_group", ""),
        scopes=data.get("scopes", "openid email profile"),
    )

    # Preserve existing secret if not provided (field is cleared on UI load)
    if not config.client_secret:
        existing = oidc_config.get_oidc_config()
        config.client_secret = existing.client_secret

    if config.provider_url:
        try:
            oidc_config.validate_provider_url(config.provider_url)
        except ValueError as e:
            raise HTTPException(400, str(e))

    oidc_config.set_oidc_config(config)
    return {"success": True}


@app.post("/api/auth/test-oidc", tags=["auth"])
async def test_oidc_discovery(session: dict = Depends(require_role("admin")), _pro=Depends(require_feature(Feature.SSO_OIDC))):
    """Test OIDC discovery endpoint reachability."""
    config = oidc_config.get_oidc_config()
    if not config.provider_url:
        return {"success": False, "message": "OIDC provider URL not configured"}

    discovery_url = config.provider_url.rstrip("/") + "/.well-known/openid-configuration"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(discovery_url)
            if resp.status_code == 200:
                data = resp.json()
                issuer = data.get("issuer", "unknown")
                return {"success": True, "message": f"OIDC provider reachable (issuer: {issuer})"}
            else:
                return {"success": False, "message": f"Discovery returned HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"Failed to reach OIDC provider: {e}"}


# ============================================================================
# OIDC SSO Login Flow (no auth dependency)
# ============================================================================

@app.get("/auth/oidc/login")
async def oidc_login(request: Request, _pro=Depends(require_feature(Feature.SSO_OIDC))):
    """Initiate OIDC Authorization Code flow with PKCE."""
    import secrets as _secrets
    import hashlib
    import base64

    ip_address = _client_ip(request)
    bucket = f"oidc:{ip_address}"
    if _check_rate_limit(bucket, OIDC_RATE_LIMIT, AUTH_RATE_WINDOW):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "rate_limited", "oidc_enabled": oidc_config.is_oidc_enabled()},
            status_code=429,
            headers={"Retry-After": str(AUTH_RATE_WINDOW)},
        )
    _record_rate_limit_event(bucket)

    config = oidc_config.get_oidc_config()
    if not oidc_config.is_oidc_enabled():
        return RedirectResponse(url="/login", status_code=302)

    # Generate state, nonce, and PKCE verifier
    state = _secrets.token_urlsafe(32)
    nonce = _secrets.token_urlsafe(32)
    code_verifier = _secrets.token_urlsafe(64)

    # S256 PKCE challenge
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    # Store state for validation in callback
    db.set_setting(f"oidc_state_{state}", json.dumps({
        "nonce": nonce,
        "code_verifier": code_verifier,
        "created_at": datetime.now().isoformat(),
    }))

    # Discover authorization endpoint
    discovery_url = config.provider_url.rstrip("/") + "/.well-known/openid-configuration"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            discovery = resp.json()
    except Exception as e:
        logger.error(f"OIDC discovery failed: {e}")
        return RedirectResponse(url="/login?error=oidc_discovery_failed", status_code=302)

    authorization_endpoint = discovery.get("authorization_endpoint")
    if not authorization_endpoint:
        return RedirectResponse(url="/login?error=oidc_discovery_failed", status_code=302)

    # Build authorization URL
    from urllib.parse import urlencode
    params = urlencode({
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": config.redirect_uri,
        "scope": config.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })

    return RedirectResponse(url=f"{authorization_endpoint}?{params}", status_code=302)


@app.get("/auth/oidc/callback")
async def oidc_callback(request: Request, code: str = None, state: str = None, error: str = None, _pro=Depends(require_feature(Feature.SSO_OIDC))):
    """Handle OIDC callback from Authentik."""
    ip_address = _client_ip(request)
    bucket = f"oidc:{ip_address}"
    if _check_rate_limit(bucket, OIDC_RATE_LIMIT, AUTH_RATE_WINDOW):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "rate_limited", "oidc_enabled": oidc_config.is_oidc_enabled()},
            status_code=429,
            headers={"Retry-After": str(AUTH_RATE_WINDOW)},
        )
    _record_rate_limit_event(bucket)

    if not oidc_config.is_oidc_enabled():
        return RedirectResponse(url="/login", status_code=302)

    from .auth import authenticate_oidc_user, create_session, SESSION_COOKIE_NAME

    if error:
        logger.warning(f"OIDC callback error: {error}")
        return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

    if not code or not state:
        return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

    # Validate and consume state
    stored_raw = db.get_setting(f"oidc_state_{state}", "")
    if not stored_raw:
        return RedirectResponse(url="/login?error=invalid_state", status_code=302)

    db.delete_setting(f"oidc_state_{state}")  # One-time use

    try:
        state_data = json.loads(stored_raw)
    except (json.JSONDecodeError, TypeError):
        return RedirectResponse(url="/login?error=invalid_state", status_code=302)

    config = oidc_config.get_oidc_config()

    # Discover token and JWKS endpoints
    discovery_url = config.provider_url.rstrip("/") + "/.well-known/openid-configuration"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            discovery = resp.json()
    except Exception as e:
        logger.error(f"OIDC discovery failed during callback: {e}")
        return RedirectResponse(url="/login?error=oidc_discovery_failed", status_code=302)

    token_endpoint = discovery.get("token_endpoint")
    jwks_uri = discovery.get("jwks_uri")
    issuer = discovery.get("issuer")

    if not token_endpoint or not jwks_uri:
        return RedirectResponse(url="/login?error=oidc_discovery_failed", status_code=302)

    # Exchange authorization code for tokens
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(token_endpoint, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": state_data["code_verifier"],
            })
            token_resp.raise_for_status()
            tokens = token_resp.json()
    except Exception as e:
        logger.error(f"OIDC token exchange failed: {e}")
        return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

    id_token = tokens.get("id_token")
    if not id_token:
        return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

    # Validate and decode the id_token
    try:
        from authlib.jose import jwt as authlib_jwt, JsonWebKey

        async with httpx.AsyncClient(timeout=10) as client:
            jwks_resp = await client.get(jwks_uri)
            jwks_resp.raise_for_status()
            jwks = JsonWebKey.import_key_set(jwks_resp.json())

        claims = authlib_jwt.decode(id_token, jwks)
        claims.validate()

        # Validate issuer and audience
        if claims.get("iss") != issuer:
            logger.warning(f"OIDC issuer mismatch: {claims.get('iss')} != {issuer}")
            return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

        if claims.get("aud") != config.client_id:
            # aud can be a string or list
            aud = claims.get("aud")
            if isinstance(aud, list) and config.client_id not in aud:
                logger.warning(f"OIDC audience mismatch")
                return RedirectResponse(url="/login?error=oidc_denied", status_code=302)
            elif isinstance(aud, str) and aud != config.client_id:
                logger.warning(f"OIDC audience mismatch")
                return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

        # Validate nonce
        if claims.get("nonce") != state_data.get("nonce"):
            logger.warning("OIDC nonce mismatch")
            return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

    except Exception as e:
        logger.error(f"OIDC id_token validation failed: {e}")
        return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

    email = claims.get("email", "")
    groups = claims.get("groups", [])

    if not email:
        return RedirectResponse(url="/login?error=oidc_denied", status_code=302)

    # Validate group membership
    oidc_username = authenticate_oidc_user(email, groups)
    if not oidc_username:
        return RedirectResponse(url="/login?error=oidc_unauthorized", status_code=302)

    # Ensure OIDC user exists in users table (auto-creates with default role)
    db_user = ensure_oidc_user(oidc_username)
    if db_user and not db_user.get("enabled", True):
        return RedirectResponse(url="/login?error=oidc_unauthorized", status_code=302)

    # Create session using existing infrastructure
    _clear_rate_limit_bucket(bucket)
    session_id = create_session(oidc_username, ip_address)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=is_request_secure(request),
        max_age=86400,
        samesite="lax",
    )
    return response


# ---------------------------------------------------------------------------
# User management API (RBAC)
# ---------------------------------------------------------------------------

@app.get("/api/users/me", tags=["auth"])
async def get_current_user(session: dict = Depends(require_auth)):
    """Get current authenticated user info."""
    return {"username": session["username"], "role": session.get("role", "viewer")}


@app.get("/api/users", tags=["auth"], dependencies=[Depends(require_role("admin"))])
async def list_users_api():
    """List all users (admin only)."""
    return {"users": db.list_users()}


@app.post("/api/users", tags=["auth"], dependencies=[Depends(require_role("admin"))])
async def create_user_api(request: Request):
    """Create a new local user (admin only)."""
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "viewer")

    if not username:
        raise HTTPException(400, "Username is required")
    if not password or len(password) < 12:
        raise HTTPException(400, "Password must be at least 12 characters")
    if role not in db.VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {', '.join(db.VALID_ROLES)}")
    if db.get_user(username):
        raise HTTPException(409, "Username already exists")

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user_id = db.create_user(username, password_hash, role, "local")
    return {"id": user_id, "username": username, "role": role}


@app.put("/api/users/{user_id}", tags=["auth"], dependencies=[Depends(require_role("admin"))])
async def update_user_api(user_id: int, request: Request, session: dict = Depends(require_auth)):
    """Update a user (admin only)."""
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    data = await request.json()
    updates = {}

    if "role" in data:
        new_role = data["role"]
        if new_role not in db.VALID_ROLES:
            raise HTTPException(400, f"Invalid role. Must be one of: {', '.join(db.VALID_ROLES)}")
        # Guard: cannot remove last admin
        if user["role"] == "admin" and new_role != "admin" and db.count_admin_users() <= 1:
            raise HTTPException(400, "Cannot remove the last admin user")
        updates["role"] = new_role

    if "password" in data and data["password"]:
        if len(data["password"]) < 12:
            raise HTTPException(400, "Password must be at least 12 characters")
        updates["password_hash"] = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()

    if "enabled" in data:
        enabled = bool(data["enabled"])
        # Guard: cannot disable self
        if not enabled and user["username"] == session["username"]:
            raise HTTPException(400, "Cannot disable your own account")
        # Guard: cannot disable last admin
        if not enabled and user["role"] == "admin" and db.count_admin_users() <= 1:
            raise HTTPException(400, "Cannot disable the last admin user")
        updates["enabled"] = enabled

    if not updates:
        raise HTTPException(400, "No valid fields to update")

    db.update_user(user_id, **updates)
    return {"ok": True}


@app.delete("/api/users/{user_id}", tags=["auth"], dependencies=[Depends(require_role("admin"))])
async def delete_user_api(user_id: int, session: dict = Depends(require_auth)):
    """Delete a user (admin only)."""
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    # Guard: cannot delete self
    if user["username"] == session["username"]:
        raise HTTPException(400, "Cannot delete your own account")
    # Guard: cannot delete last admin
    if user["role"] == "admin" and db.count_admin_users() <= 1:
        raise HTTPException(400, "Cannot delete the last admin user")

    db.delete_sessions_for_user(user["username"])
    db.delete_user(user_id)
    return {"ok": True}


@app.get("/api/time", tags=["system"])
async def get_current_time(session: dict = Depends(require_auth)):
    """Get current time info with timezone."""
    settings = db.get_all_settings()
    tz = settings.get("timezone", "auto")
    zip_code = settings.get("zip_code", "")

    # Get timezone
    if tz == "auto":
        tz = await services.get_timezone()

    time_info = services.get_current_time(tz)
    return time_info


@app.get("/api/weather", tags=["system"])
async def get_weather(session: dict = Depends(require_auth)):
    """Get current weather."""
    settings = db.get_all_settings()
    zip_code = settings.get("zip_code", "")

    coords = await services.get_coordinates(zip_code if zip_code else None)
    if not coords:
        return {"error": "Could not determine location"}

    weather = await services.get_weather_forecast(coords[0], coords[1])
    if not weather:
        return {"error": "Could not fetch weather"}

    weather["fetched_at"] = datetime.now().isoformat()
    return weather


@app.get("/api/scheduler/status", tags=["system"])
async def get_scheduler_status(session: dict = Depends(require_auth)):
    """Get current scheduler status."""
    scheduler = get_scheduler()
    if not scheduler:
        return {"state": "disabled", "block_reason": "Scheduler not initialized"}
    return scheduler.get_status()


@app.get("/api/rollout/current", tags=["system"])
async def get_current_rollout(session: dict = Depends(require_auth)):
    """Get the current active/paused rollout with progress."""
    rollout = db.get_active_rollout()
    if not rollout:
        return {"rollout": None}

    progress = db.get_rollout_progress(rollout["id"])
    failed_devices = []
    if rollout["status"] == "paused":
        failed_devices = db.get_rollout_devices_by_status(rollout["id"], "failed")
    return {
        "rollout": {
            "id": rollout["id"],
            "phase": rollout["phase"],
            "status": rollout["status"],
            "target_version": rollout.get("target_version"),
            "firmware_file": rollout["firmware_file"],
            "firmware_file_303l": rollout.get("firmware_file_303l"),
            "progress": progress,
            "pause_reason": rollout.get("pause_reason"),
            "created_at": rollout.get("created_at"),
            "updated_at": rollout.get("updated_at"),
            "failed_devices": failed_devices,
        }
    }


@app.post("/api/rollout/{rollout_id}/resume", tags=["system"])
async def resume_rollout(rollout_id: int, session: dict = Depends(require_role("admin", "operator"))):
    """Resume a paused rollout."""
    rollout = db.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(404, "Rollout not found")
    if rollout["status"] != "paused":
        raise HTTPException(400, "Rollout is not paused")

    db.resume_rollout(rollout_id)
    db.log_schedule_event("rollout_resumed", f"Rollout {rollout_id} resumed by user")

    # Broadcast updated status
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"success": True}


@app.post("/api/rollout/{rollout_id}/cancel", tags=["system"])
async def cancel_rollout(rollout_id: int, session: dict = Depends(require_role("admin", "operator"))):
    """Cancel an active/paused rollout."""
    rollout = db.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(404, "Rollout not found")
    if rollout["status"] not in ("active", "paused"):
        raise HTTPException(400, "Rollout cannot be cancelled")

    db.cancel_rollout(rollout_id)
    db.log_schedule_event("rollout_cancelled", f"Rollout {rollout_id} cancelled by user")

    # Broadcast updated status
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"success": True}


@app.post("/api/rollout/{rollout_id}/reset", tags=["system"])
async def reset_rollout(rollout_id: int, session: dict = Depends(require_role("admin", "operator"))):
    """Reset a paused rollout - cancels it so a fresh rollout starts next window."""
    rollout = db.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(404, "Rollout not found")
    if rollout["status"] != "paused":
        raise HTTPException(400, "Rollout is not paused")

    db.cancel_rollout(rollout_id)
    db.log_schedule_event("rollout_reset", f"Rollout {rollout_id} reset by user (will restart fresh)")

    # Broadcast updated status
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"success": True}


@app.post("/api/rollout/canary/trigger", tags=["system"])
async def trigger_canary_rollout(session: dict = Depends(require_role("admin", "operator"))):
    """Trigger the canary phase immediately, outside the maintenance window."""
    scheduler = get_scheduler()
    if not scheduler:
        raise HTTPException(503, "Scheduler not initialized")

    try:
        await scheduler.trigger_canary_now()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {"success": True, "status": scheduler.get_status()}


@app.get("/api/location", tags=["system"])
async def get_location(session: dict = Depends(require_auth)):
    """Get detected location info."""
    settings = db.get_all_settings()
    zip_code = settings.get("zip_code", "")

    if zip_code:
        location = await services.get_location_from_zip(zip_code)
        if location:
            return {"source": "zip", **location}

    location = await services.get_location_from_ip()
    if location:
        return {
            "source": "ip",
            "city": location.get("city"),
            "state": location.get("regionName"),
            "timezone": location.get("timezone"),
            "lat": location.get("lat"),
            "lon": location.get("lon"),
        }

    return {"error": "Could not determine location"}


# ============================================================================
# Backup API
# ============================================================================

@app.post("/api/backup/export", tags=["config"])
async def export_backup(request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Export devices as CSV with encrypted passwords."""
    data = await request.json()
    passphrase = data.get("passphrase", "")
    if not passphrase or len(passphrase) < 8:
        raise HTTPException(400, "Passphrase must be at least 8 characters")

    try:
        csv_content, _ = build_csv_export(passphrase)
    except Exception as e:
        logger.error(f"Backup export failed: {e}")
        raise HTTPException(500, "Export failed")

    filename = f"tachyon-devices-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/backup/import", tags=["config"])
async def import_backup(
    file: UploadFile = File(...),
    passphrase: str = Form(...),
    conflict_mode: str = Form("skip"),
    session: dict = Depends(require_role("admin", "operator")),
    _pro=Depends(require_feature(Feature.CONFIG_BACKUP)),
):
    """Import devices from a CSV with encrypted passwords."""
    if not passphrase or len(passphrase) < 8:
        raise HTTPException(400, "Passphrase must be at least 8 characters")
    if conflict_mode not in ("skip", "update"):
        raise HTTPException(400, "conflict_mode must be 'skip' or 'update'")

    try:
        raw = await file.read()
        if len(raw) > MAX_CSV_IMPORT_SIZE:
            raise HTTPException(413, f"CSV file exceeds maximum size ({MAX_CSV_IMPORT_SIZE // (1024 * 1024)} MB)")
        csv_content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "File is not valid UTF-8 text")

    try:
        results = process_csv_import(csv_content, passphrase, conflict_mode)
    except ValueError as e:
        # ValueError contains user-friendly messages from process_csv_import
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Backup import failed: {e}")
        raise HTTPException(500, "Import failed")

    # Trigger a poll so new devices appear in the UI
    poller = get_poller()
    if poller:
        asyncio.create_task(poller._poll_all_aps())

    return results


# ============================================================================
# Firmware Update API (existing functionality)
# ============================================================================

@dataclass
class DeviceStatus:
    """Status of a single device update."""
    ip: str
    status: str = "pending"
    old_version: Optional[str] = None
    new_version: Optional[str] = None
    error: Optional[str] = None
    progress_message: str = ""
    bank1_version: Optional[str] = None
    bank2_version: Optional[str] = None
    active_bank: Optional[int] = None
    role: str = "ap"
    parent_ap: Optional[str] = None
    model: Optional[str] = None
    # Stage tracking for history
    stage_history: list = field(default_factory=list)
    current_stage: Optional[str] = None
    current_stage_started: Optional[str] = None
    # Smoke test results
    smoke_warnings: Optional[list] = None
    smoke_checks: Optional[list] = None


@dataclass
class UpdateJob:
    """A firmware update job."""
    job_id: str
    firmware_files: Dict[str, str] = field(default_factory=dict)  # pattern key -> path
    firmware_names: Dict[str, str] = field(default_factory=dict)  # pattern key -> display name
    device_firmware_map: Dict[str, str] = field(default_factory=dict)  # IP -> firmware path
    device_type: str = "tachyon"
    credentials: Dict[str, tuple] = field(default_factory=dict)  # IP -> (username, password)
    devices: Dict[str, DeviceStatus] = field(default_factory=dict)
    bank_mode: str = "both"
    cancelled: bool = False
    cancel_reason: Optional[str] = None
    ap_cpe_map: Dict[str, list] = field(default_factory=dict)  # AP IP -> [CPE IPs]
    device_roles: Dict[str, str] = field(default_factory=dict)  # IP -> "ap"/"cpe"
    device_parent: Dict[str, str] = field(default_factory=dict)  # CPE IP -> parent AP IP
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: str = "pending"
    is_scheduled: bool = False
    start_hour: Optional[int] = None
    end_hour: Optional[int] = None
    schedule_days: list[str] = field(default_factory=list)
    schedule_timezone: Optional[str] = None
    pre_update_reboot: bool = True
    enforce_window_cutoff: bool = True


async def _finalize_crashed_job(job: "UpdateJob", error: Exception):
    """Finalize a job if its task crashes unexpectedly."""
    if job.status == "completed":
        return

    err_text = f"Internal job error: {error}"
    for ds in job.devices.values():
        if ds.status not in ("success", "failed", "skipped", "cancelled"):
            ds.status = "failed"
            ds.error = err_text
            ds.progress_message = err_text

    job.completed_at = datetime.now()
    job.status = "completed"

    success_count = sum(1 for d in job.devices.values() if d.status == "success")
    failed_count = sum(1 for d in job.devices.values() if d.status == "failed")
    skipped_count = sum(1 for d in job.devices.values() if d.status == "skipped")
    cancelled_count = sum(1 for d in job.devices.values() if d.status == "cancelled")

    resolved_tz = job.schedule_timezone
    if not resolved_tz:
        settings = db.get_all_settings()
        tz_setting = settings.get("timezone", "auto")
        resolved_tz = await services.get_timezone() if tz_setting == "auto" else tz_setting

    await broadcast({
        "type": "job_completed",
        "job_id": job.job_id,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "cancelled_count": cancelled_count,
        "duration": (job.completed_at - job.started_at).total_seconds() if job.started_at else 0,
        "timezone": resolved_tz,
        "error": err_text,
        "devices": {
            ip: {
                "status": ds.status,
                "message": ds.progress_message,
                "old_version": ds.old_version,
                "new_version": ds.new_version,
                "error": ds.error,
                "bank1_version": ds.bank1_version,
                "bank2_version": ds.bank2_version,
                "active_bank": ds.active_bank,
                "role": ds.role,
                "parent_ap": ds.parent_ap,
                "model": ds.model,
            }
            for ip, ds in job.devices.items()
        },
        "ap_cpe_map": job.ap_cpe_map,
        "device_roles": job.device_roles,
    })

    if job.is_scheduled:
        scheduler = get_scheduler()
        if scheduler:
            device_statuses = {ip: ds.status for ip, ds in job.devices.items()}
            scheduler.on_job_completed(
                job.job_id,
                success_count,
                failed_count if failed_count > 0 else 1,
                learned_version=None,
                device_statuses=device_statuses,
                cancel_reason=job.cancel_reason,
            )


def _spawn_update_job(job: "UpdateJob", concurrency: int):
    """Spawn update task with crash guard so scheduler state cannot get stuck."""
    task = asyncio.create_task(run_update_job(job, concurrency))

    def _on_done(done_task: asyncio.Task):
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.exception(f"Update job {job.job_id} crashed", exc_info=exc)
            asyncio.create_task(_finalize_crashed_job(job, exc))

    task.add_done_callback(_on_done)


MAX_FIRMWARE_SIZE = 500 * 1024 * 1024   # 500 MB
MAX_CSV_IMPORT_SIZE = 10 * 1024 * 1024  # 10 MB


@app.post("/api/upload-firmware", tags=["firmware"])
async def upload_firmware(file: UploadFile = File(...), session: dict = Depends(require_role("admin", "operator"))):
    """Upload a firmware file."""
    # Validate filename to prevent path traversal attacks
    safe_filename = validate_firmware_filename(file.filename)
    firmware_path = FIRMWARE_DIR / safe_filename

    total_size = 0
    chunk_size = 1024 * 1024  # 1 MB chunks
    try:
        async with aiofiles.open(firmware_path, "wb") as f:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_FIRMWARE_SIZE:
                    raise HTTPException(413, f"Firmware file exceeds maximum size ({MAX_FIRMWARE_SIZE // (1024 * 1024)} MB)")
                await f.write(chunk)
    except HTTPException:
        firmware_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        firmware_path.unlink(missing_ok=True)
        logger.error(f"Firmware upload failed for {safe_filename}: {e}")
        raise HTTPException(500, "Failed to upload firmware")
    finally:
        await file.close()

    logger.info(f"Firmware uploaded: {safe_filename} ({total_size:,} bytes)")

    db.register_firmware(safe_filename, source="manual")

    return {
        "filename": safe_filename,
        "size": total_size,
    }


@app.get("/api/firmware-files", tags=["firmware"])
async def list_firmware_files(session: dict = Depends(require_auth)):
    """List available firmware files."""
    import json as _json
    auto_fetched_raw = db.get_setting("firmware_auto_fetched_files", "")
    try:
        auto_fetched = _json.loads(auto_fetched_raw) if auto_fetched_raw else []
    except (ValueError, TypeError):
        auto_fetched = []

    channels_raw = db.get_setting("firmware_channels", "")
    try:
        channels = _json.loads(channels_raw) if channels_raw else {}
    except (ValueError, TypeError):
        channels = {}

    try:
        quarantine_days = int(db.get_setting("firmware_quarantine_days", "7"))
    except (TypeError, ValueError):
        quarantine_days = 7
    registry = {r["filename"]: r for r in db.get_firmware_registry()}

    files = []
    for f in FIRMWARE_DIR.iterdir():
        if f.is_file() and f.suffix in {".bin", ".img", ".npk", ".tar", ".gz"}:
            q_info = db.get_firmware_quarantine_info(f.name, quarantine_days)
            reg = registry.get(f.name)
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "source": "auto" if f.name in auto_fetched else "manual",
                "channel": channels.get(f.name, ""),
                "added_at": reg["added_at"] if reg else None,
                "quarantine_cleared": q_info["cleared"],
                "quarantine_clears_at": q_info["clears_at"],
                "quarantine_remaining_hours": q_info["remaining_hours"],
            })
    return {
        "files": sorted(files, key=lambda x: x["modified"], reverse=True),
        "quarantine_days": quarantine_days,
    }


@app.delete("/api/firmware-files/{filename:path}", tags=["firmware"])
async def delete_firmware_file(filename: str, session: dict = Depends(require_role("admin", "operator"))):
    """Delete a firmware file."""
    # Validate filename to prevent path traversal attacks
    safe_filename = validate_firmware_filename(filename)
    path = FIRMWARE_DIR / safe_filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    db.unregister_firmware(safe_filename)
    return {"success": True}


@app.post("/api/firmware-fetch", tags=["firmware"])
async def trigger_firmware_fetch(session: dict = Depends(require_role("admin", "operator"))):
    """Trigger an on-demand firmware check and download."""
    fetcher = get_fetcher()
    if not fetcher:
        raise HTTPException(503, "Firmware fetcher not initialized")
    result = await fetcher.check_and_download()
    return result


@app.post("/api/firmware-reselect", tags=["firmware"])
async def firmware_reselect(session: dict = Depends(require_role("admin", "operator"))):
    """Re-run firmware auto-selection (e.g. after toggling beta)."""
    fetcher = get_fetcher()
    if not fetcher:
        raise HTTPException(503, "Firmware fetcher not initialized")
    beta_enabled = db.get_setting("firmware_beta_enabled", "false") == "true"
    fetcher.reselect(beta_enabled)

    # Broadcast updated scheduler status immediately so the UI reflects
    # any firmware selection change (e.g. rollout cancelled due to new firmware)
    scheduler = get_scheduler()
    if scheduler:
        await scheduler._broadcast_status()

    return {"success": True}


@app.get("/api/firmware-fetch/status", tags=["firmware"])
async def firmware_fetch_status(session: dict = Depends(require_auth)):
    """Get firmware fetch status."""
    import json as _json
    last_check = db.get_setting("firmware_last_check", "")
    last_error = db.get_setting("firmware_last_check_error", "")
    auto_fetched_raw = db.get_setting("firmware_auto_fetched_files", "")
    try:
        auto_fetched = _json.loads(auto_fetched_raw) if auto_fetched_raw else []
    except (ValueError, TypeError):
        auto_fetched = []
    return {
        "last_check": last_check,
        "last_error": last_error,
        "auto_fetched_files": auto_fetched,
    }


@app.get("/api/fleet-status", tags=["system"])
async def get_fleet_status(session: dict = Depends(require_auth)):
    """Get firmware version status for all devices."""
    settings = db.get_all_settings()
    allow_downgrade = settings.get("allow_downgrade", "false") == "true"

    # Build target versions from selected firmware filenames
    targets = {}
    for setting_key, fw_type in [
        ("selected_firmware_30x", "tna-30x"),
        ("selected_firmware_303l", "tna-303l"),
        ("selected_firmware_tns100", "tns-100"),
    ]:
        filename = settings.get(setting_key, "")
        if filename:
            version = _extract_version_from_filename(filename)
            targets[fw_type] = {"file": filename, "version": version}

    # Build site name lookup
    sites = db.get_tower_sites()
    site_names = {s["id"]: s["name"] for s in sites}

    # Collect all devices
    devices = []
    aps = db.get_access_points(enabled_only=False)
    all_cpes = db.get_all_cpes()
    cpes_by_ap = {}
    for cpe in all_cpes:
        cpes_by_ap.setdefault(cpe["ap_ip"], []).append(cpe)

    summary = {"total": 0, "current": 0, "behind": 0, "unknown": 0}

    for ap in aps:
        fw_type = _get_firmware_type_for_model(ap.get("model"))
        target = targets.get(fw_type)
        target_version = target["version"] if target else ""
        status = _device_version_status(ap.get("firmware_version"), target_version, allow_downgrade)
        summary["total"] += 1
        summary[status] += 1

        devices.append({
            "ip": ap["ip"],
            "system_name": ap.get("system_name"),
            "model": ap.get("model"),
            "role": "ap",
            "parent_ap": None,
            "site_name": site_names.get(ap.get("tower_site_id"), "Unassigned"),
            "firmware_version": ap.get("firmware_version") or "",
            "bank1_version": ap.get("bank1_version"),
            "bank2_version": ap.get("bank2_version"),
            "active_bank": ap.get("active_bank"),
            "target_version": target_version,
            "firmware_type": fw_type,
            "status": status,
            "auth_status": None,
            "enabled": bool(ap.get("enabled", 1)),
        })

        for cpe in cpes_by_ap.get(ap["ip"], []):
            cpe_fw_type = _get_firmware_type_for_model(cpe.get("model"))
            cpe_target = targets.get(cpe_fw_type)
            cpe_target_version = cpe_target["version"] if cpe_target else ""
            cpe_status = _device_version_status(cpe.get("firmware_version"), cpe_target_version, allow_downgrade)
            summary["total"] += 1
            summary[cpe_status] += 1

            devices.append({
                "ip": cpe["ip"],
                "system_name": cpe.get("system_name"),
                "model": cpe.get("model"),
                "role": "cpe",
                "parent_ap": ap["ip"],
                "site_name": site_names.get(ap.get("tower_site_id"), "Unassigned"),
                "firmware_version": cpe.get("firmware_version") or "",
                "bank1_version": cpe.get("bank1_version"),
                "bank2_version": cpe.get("bank2_version"),
                "active_bank": cpe.get("active_bank"),
                "target_version": cpe_target_version,
                "firmware_type": cpe_fw_type,
                "status": cpe_status,
                "auth_status": cpe.get("auth_status"),
                "enabled": bool(ap.get("enabled", 1)),
            })

    # Include switches
    switches = db.get_switches(enabled_only=False)
    for sw in switches:
        fw_type = _get_firmware_type_for_model(sw.get("model"))
        target = targets.get(fw_type)
        target_version = target["version"] if target else ""
        status = _device_version_status(sw.get("firmware_version"), target_version, allow_downgrade)
        summary["total"] += 1
        summary[status] += 1

        devices.append({
            "ip": sw["ip"],
            "system_name": sw.get("system_name"),
            "model": sw.get("model"),
            "role": "switch",
            "parent_ap": None,
            "site_name": site_names.get(sw.get("tower_site_id"), "Unassigned"),
            "firmware_version": sw.get("firmware_version") or "",
            "bank1_version": sw.get("bank1_version"),
            "bank2_version": sw.get("bank2_version"),
            "active_bank": sw.get("active_bank"),
            "target_version": target_version,
            "firmware_type": fw_type,
            "status": status,
            "auth_status": None,
            "enabled": bool(sw.get("enabled", 1)),
        })

    return {"devices": devices, "summary": summary, "targets": targets}


def _device_version_status(current: Optional[str], target: str, allow_downgrade: bool = False) -> str:
    """Determine version status: 'current', 'behind', or 'unknown'.

    Args:
        current: Current firmware version on device
        target: Target firmware version
        allow_downgrade: If True, devices with newer firmware than target are 'behind'
    """
    if not current or not target:
        return "unknown"
    cmp = _compare_versions(current, target)
    if cmp == 0:
        return "current"
    if cmp > 0:
        # Device is newer than target
        return "behind" if allow_downgrade else "current"
    return "behind"


def _select_firmware_for_model(model: Optional[str], firmware_files: Dict[str, str]) -> Optional[str]:
    """Select the correct firmware path for a device model.

    Args:
        model: Device model string (e.g. "TNA-303L-65")
        firmware_files: Dict of pattern key -> firmware path (e.g. {"tna-30x": "/path", "tna-303l": "/path"})

    Returns:
        Firmware path or None if no match.
    """
    if not model:
        # No model info - use the default "tna-30x" firmware if available
        return firmware_files.get("tna-30x") or next(iter(firmware_files.values()), None)

    model_lower = model.lower()

    # Check known model patterns
    for model_key, patterns in TachyonClient.MODEL_FIRMWARE_PATTERNS.items():
        if model_lower == model_key or model_lower.startswith(model_key):
            # Found the model - look for matching firmware
            for pattern in patterns:
                if pattern in firmware_files:
                    return firmware_files[pattern]
            return None  # Model known but no matching firmware provided

    # Unknown model - use default "tna-30x" firmware
    return firmware_files.get("tna-30x") or next(iter(firmware_files.values()), None)


def _is_303l_model(model: Optional[str]) -> bool:
    """Check if a model is a TNA-303L variant."""
    if not model:
        return False
    model_lower = model.lower()
    return model_lower.startswith("tna-303l")


def _is_tns100_model(model: Optional[str]) -> bool:
    """Check if a model is a TNS-100 variant."""
    if not model:
        return False
    return model.lower().startswith("tns-100")


TNS100_REBOOT_TIMEOUT = 900  # 15 minutes for switches
AP_REBOOT_TIMEOUT = 480      # 8 minutes for APs (increased from 5 min due to slower reboots)


def _extract_version_from_filename(filename: str) -> str:
    """Extract normalized version from firmware filename.

    'tna-30x-2.5.1-r54970.bin' -> '2.5.1.54970'
    """
    match = re.search(
        r"(?:tna-30x|tna30x|tna-303l|tna303l|tns-100|tns100)-(\d+\.\d+\.\d+)-r(\d+)",
        filename,
        re.IGNORECASE,
    )
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match2 = re.search(r"(\d+\.\d+\.\d+)", filename)
    if match2:
        return match2.group(1)
    return ""


def _parse_version(version: str) -> tuple:
    """Parse version string into tuple for comparison.

    Handles formats like '1.12.3.54970' or '1.12.3.r54970'.
    Returns tuple of integers for comparison.
    """
    if not version:
        return (0,)
    # Normalize .r to .
    normalized = version.replace(".r", ".")
    parts = []
    for part in normalized.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _normalize_version(value: Optional[str]) -> str:
    """Normalize firmware version strings for comparisons."""
    if not value:
        return ""
    return str(value).strip().replace(".r", ".")


def _get_active_inactive_bank_versions(device: dict) -> tuple[str, str]:
    """Return (active_version, inactive_version) for a device record."""
    bank1 = _normalize_version(device.get("bank1_version"))
    bank2 = _normalize_version(device.get("bank2_version"))
    firmware = _normalize_version(device.get("firmware_version"))

    try:
        active_bank = int(device.get("active_bank")) if device.get("active_bank") is not None else None
    except (TypeError, ValueError):
        active_bank = None

    if active_bank == 1:
        return (bank1 or firmware, bank2)
    if active_bank == 2:
        return (bank2 or firmware, bank1)
    return (firmware, "")


def _device_needs_update_for_bank_mode(
    device: dict,
    target_version: str,
    bank_mode: str,
    allow_downgrade: bool = False,
) -> bool:
    """Evaluate whether a device should be enrolled for update.

    Rules:
    - `bank_mode=one`: skip if active bank is already on target.
    - `bank_mode=both`: include when active bank is on target but inactive bank differs.
    """
    target = _normalize_version(target_version)
    if not target:
        return True

    active_version, inactive_version = _get_active_inactive_bank_versions(device)
    if not active_version:
        return True

    target_parsed = _parse_version(target)
    active_parsed = _parse_version(active_version)

    if active_version == target:
        if bank_mode == "both":
            # Unknown inactive bank state -> don't force a dual-bank rewrite.
            if not inactive_version:
                return False
            if inactive_version == target:
                return False
            if not allow_downgrade and _parse_version(inactive_version) > target_parsed:
                return False
            return True
        return False

    if not allow_downgrade and active_parsed > target_parsed:
        return False

    return True



def _get_firmware_type_for_model(model: Optional[str]) -> Optional[str]:
    """Get the firmware type key for a device model."""
    if not model:
        return "tna-30x"
    model_lower = model.lower()
    for model_key, patterns in TachyonClient.MODEL_FIRMWARE_PATTERNS.items():
        if model_lower == model_key or model_lower.startswith(model_key):
            return patterns[0] if patterns else None
    return "tna-30x"


def _compare_versions(a: str, b: str) -> int:
    """Compare two version strings. Returns <0 if a<b, 0 if equal, >0 if a>b."""
    def parts(v):
        if not v:
            return [0]
        # Normalize: "1.12.2-r54885" -> "1.12.2.54885"
        v = v.replace("-", ".").lower()
        result = []
        for seg in v.split("."):
            seg = seg.lstrip("r")  # strip revision prefix
            try:
                result.append(int(seg))
            except ValueError:
                continue  # skip non-numeric segments
        return result or [0]
    pa, pb = parts(a), parts(b)
    while len(pa) < len(pb):
        pa.append(0)
    while len(pb) < len(pa):
        pb.append(0)
    for x, y in zip(pa, pb):
        if x != y:
            return x - y
    return 0


async def _start_scheduled_update(
    ap_ips: list[str],
    firmware_file: str,
    firmware_file_303l: str = "",
    firmware_file_tns100: str = "",
    bank_mode: str = "both",
    concurrency: int = 2,
    start_hour: int = None,
    end_hour: int = None,
    schedule_days: list[str] = None,
    schedule_timezone: str = None,
    switch_ips: list[str] | None = None,
    enforce_window_cutoff: bool = True,
) -> str:
    """Start an update job from the scheduler (Python args, not Form data)."""
    if concurrency < 1:
        raise RuntimeError("parallel_updates must be >= 1")

    safe_fw = validate_firmware_filename(firmware_file)
    firmware_path = FIRMWARE_DIR / safe_fw
    if not firmware_path.exists():
        raise RuntimeError(f"Firmware file not found: {safe_fw}")

    # Get downgrade setting
    allow_downgrade = db.get_setting("allow_downgrade", "false") == "true"

    firmware_files = {"tna-30x": str(firmware_path)}
    firmware_names = {"tna-30x": safe_fw}

    if firmware_file_303l:
        safe_303l = validate_firmware_filename(firmware_file_303l)
        path_303l = FIRMWARE_DIR / safe_303l
        if path_303l.exists():
            firmware_files["tna-303l"] = str(path_303l)
            firmware_names["tna-303l"] = safe_303l

    if firmware_file_tns100:
        safe_tns100 = validate_firmware_filename(firmware_file_tns100)
        path_tns100 = FIRMWARE_DIR / safe_tns100
        if path_tns100.exists():
            firmware_files["tns-100"] = str(path_tns100)
            firmware_names["tns-100"] = safe_tns100

    credentials = {}
    ap_cpe_map = {}
    device_roles = {}
    device_parent = {}
    device_firmware_map = {}

    valid_aps = []
    for ip in ap_ips:
        ap = db.get_access_point(ip)
        if not ap:
            continue
        valid_aps.append(ip)
        credentials[ip] = (ap["username"], ap["password"])
        device_roles[ip] = "ap"
        device_firmware_map[ip] = _select_firmware_for_model(ap.get("model"), firmware_files) or str(firmware_path)

        cpes = db.get_cpes_for_ap(ip)
        cpe_ips = []
        for cpe in cpes:
            cpe_ip = cpe.get("ip")
            if not cpe_ip or cpe.get("auth_status") != "ok":
                continue
            cpe_model = cpe.get("model")
            cpe_fw = _select_firmware_for_model(cpe_model, firmware_files)

            # Skip CPEs already satisfied by bank_mode/target policy
            if cpe_fw:
                target_version = _extract_version_from_filename(Path(cpe_fw).name)
                if target_version and not _device_needs_update_for_bank_mode(
                    cpe,
                    target_version,
                    bank_mode=bank_mode,
                    allow_downgrade=allow_downgrade,
                ):
                    logger.info(
                        f"Skipping CPE {cpe_ip}: bank_mode={bank_mode}, target={target_version} already satisfied"
                    )
                    continue

            cpe_ips.append(cpe_ip)
            device_roles[cpe_ip] = "cpe"
            device_parent[cpe_ip] = ip
            credentials[cpe_ip] = (ap["username"], ap["password"])
            if cpe_fw is None and _is_303l_model(cpe_model):
                device_firmware_map[cpe_ip] = "__missing_303l__"
            elif cpe_fw is None:
                device_firmware_map[cpe_ip] = str(firmware_path)
            else:
                device_firmware_map[cpe_ip] = cpe_fw
        ap_cpe_map[ip] = cpe_ips

    # Enroll the requested switches (or all enabled switches for legacy callers)
    if switch_ips is None:
        switches = db.get_switches(enabled_only=True)
    else:
        switches = []
        for ip in switch_ips:
            sw = db.get_switch(ip)
            if sw and sw.get("enabled", 1):
                switches.append(sw)
    valid_switches = []
    for sw in switches:
        sw_ip = sw["ip"]
        sw_fw = _select_firmware_for_model(sw.get("model"), firmware_files)

        # Skip switches already satisfied by bank_mode/target policy
        if sw_fw:
            target_version = _extract_version_from_filename(Path(sw_fw).name)
            if target_version and not _device_needs_update_for_bank_mode(
                sw,
                target_version,
                bank_mode=bank_mode,
                allow_downgrade=allow_downgrade,
            ):
                logger.info(
                    f"Skipping switch {sw_ip}: bank_mode={bank_mode}, target={target_version} already satisfied"
                )
                continue

        valid_switches.append(sw_ip)
        credentials[sw_ip] = (sw["username"], sw["password"])
        device_roles[sw_ip] = "switch"
        if sw_fw is None and _is_tns100_model(sw.get("model")):
            device_firmware_map[sw_ip] = "__missing_tns100__"
        elif sw_fw is None:
            device_firmware_map[sw_ip] = str(firmware_path)
        else:
            device_firmware_map[sw_ip] = sw_fw

    if not valid_aps and not valid_switches:
        raise RuntimeError("No valid APs or switches found for scheduled update")

    pre_update_reboot = db.get_setting("pre_update_reboot", "true") == "true"

    job_id = str(uuid.uuid4())[:8]
    job = UpdateJob(
        job_id=job_id,
        firmware_files=firmware_files,
        firmware_names=firmware_names,
        device_firmware_map=device_firmware_map,
        device_type="tachyon",
        credentials=credentials,
        bank_mode=bank_mode,
        ap_cpe_map=ap_cpe_map,
        device_roles=device_roles,
        device_parent=device_parent,
        started_at=datetime.now(),
        status="running",
        is_scheduled=True,
        start_hour=start_hour,
        end_hour=end_hour,
        schedule_days=list(schedule_days or []),
        schedule_timezone=schedule_timezone,
        pre_update_reboot=pre_update_reboot,
        enforce_window_cutoff=enforce_window_cutoff,
    )

    for ip in valid_aps:
        job.devices[ip] = DeviceStatus(ip=ip, role="ap")
    for ap_ip, cpe_ips in ap_cpe_map.items():
        for cpe_ip in cpe_ips:
            job.devices[cpe_ip] = DeviceStatus(ip=cpe_ip, role="cpe", parent_ap=ap_ip)
    for sw_ip in valid_switches:
        job.devices[sw_ip] = DeviceStatus(ip=sw_ip, role="switch")

    update_jobs[job_id] = job

    await broadcast({
        "type": "job_started",
        "job_id": job_id,
        "device_count": len(job.devices),
        "firmware": firmware_file,
        "ap_cpe_map": ap_cpe_map,
        "device_roles": device_roles,
        "device_parent": device_parent,
        "bank_mode": bank_mode,
    })

    _spawn_update_job(job, concurrency)
    return job_id


@app.post("/api/start-update", tags=["jobs"])
async def start_update(
    firmware_file: str = Form(...),
    device_type: str = Form(...),
    ip_list: str = Form(...),
    concurrency: int = Form(2),
    firmware_file_303l: str = Form(""),
    firmware_file_tns100: str = Form(""),
    bank_mode: str = Form("both"),
    session: dict = Depends(require_role("admin", "operator")),
):
    """Start a firmware update job."""
    if concurrency < 1 or concurrency > 32:
        raise HTTPException(400, "concurrency must be between 1 and 32")

    ap_ips = []
    for line in ip_list.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            ip = line.split(",")[0].strip()
            if ip:
                ap_ips.append(ip)

    if not ap_ips:
        raise HTTPException(400, "No valid IPs provided")

    # Validate and build firmware files dict
    safe_fw = validate_firmware_filename(firmware_file)
    firmware_path = FIRMWARE_DIR / safe_fw
    if not firmware_path.exists():
        raise HTTPException(400, f"Firmware file not found: {safe_fw}")

    firmware_files = {"tna-30x": str(firmware_path)}
    firmware_names = {"tna-30x": safe_fw}
    allow_downgrade = db.get_setting("allow_downgrade", "false") == "true"

    if firmware_file_303l:
        safe_303l = validate_firmware_filename(firmware_file_303l)
        path_303l = FIRMWARE_DIR / safe_303l
        if not path_303l.exists():
            raise HTTPException(400, f"303L firmware file not found: {safe_303l}")
        firmware_files["tna-303l"] = str(path_303l)
        firmware_names["tna-303l"] = safe_303l

    if firmware_file_tns100:
        safe_tns100 = validate_firmware_filename(firmware_file_tns100)
        path_tns100 = FIRMWARE_DIR / safe_tns100
        if not path_tns100.exists():
            raise HTTPException(400, f"TNS100 firmware file not found: {safe_tns100}")
        firmware_files["tns-100"] = str(path_tns100)
        firmware_names["tns-100"] = safe_tns100

    # Look up stored credentials for each AP and discover CPEs
    credentials = {}
    missing_aps = []
    ap_cpe_map = {}
    device_roles = {}
    device_parent = {}
    device_firmware_map = {}

    for ip in ap_ips:
        ap = db.get_access_point(ip)
        if not ap:
            missing_aps.append(ip)
            continue

        ap_fw = _select_firmware_for_model(ap.get("model"), firmware_files) or str(firmware_path)
        ap_target_version = _extract_version_from_filename(Path(ap_fw).name)
        include_ap = _device_needs_update_for_bank_mode(
            ap,
            ap_target_version,
            bank_mode=bank_mode,
            allow_downgrade=allow_downgrade,
        )
        if include_ap:
            credentials[ip] = (ap["username"], ap["password"])
            device_roles[ip] = "ap"
            device_firmware_map[ip] = ap_fw
        else:
            logger.info(
                f"Skipping AP {ip}: bank_mode={bank_mode}, target={ap_target_version or 'unknown'} already satisfied"
            )

        # Get CPEs with auth_status='ok' for this AP
        cpes = db.get_cpes_for_ap(ip)
        cpe_ips = []
        for cpe in cpes:
            cpe_ip = cpe.get("ip")
            if not cpe_ip:
                continue
            if cpe.get("auth_status") != "ok":
                continue

            cpe_ips.append(cpe_ip)
            device_roles[cpe_ip] = "cpe"
            device_parent[cpe_ip] = ip
            # CPEs inherit AP credentials
            credentials[cpe_ip] = (ap["username"], ap["password"])

            # Assign firmware based on CPE model
            cpe_model = cpe.get("model")
            cpe_fw = _select_firmware_for_model(cpe_model, firmware_files)
            cpe_target_version = _extract_version_from_filename(Path(cpe_fw).name) if cpe_fw else ""
            if cpe_target_version and not _device_needs_update_for_bank_mode(
                cpe,
                cpe_target_version,
                bank_mode=bank_mode,
                allow_downgrade=allow_downgrade,
            ):
                logger.info(
                    f"Skipping CPE {cpe_ip}: bank_mode={bank_mode}, target={cpe_target_version} already satisfied"
                )
                continue

            if cpe_fw is None and _is_303l_model(cpe_model):
                # 303L CPE but no 303L firmware provided - will fail with clear error
                device_firmware_map[cpe_ip] = "__missing_303l__"
            elif cpe_fw is None:
                device_firmware_map[cpe_ip] = str(firmware_path)
            else:
                device_firmware_map[cpe_ip] = cpe_fw

        ap_cpe_map[ip] = cpe_ips

    if missing_aps:
        raise HTTPException(400, f"No stored credentials for: {', '.join(missing_aps)}")

    # Enroll enabled switches
    switches = db.get_switches(enabled_only=True)
    for sw in switches:
        sw_ip = sw["ip"]
        sw_fw = _select_firmware_for_model(sw.get("model"), firmware_files)
        sw_target_version = _extract_version_from_filename(Path(sw_fw).name) if sw_fw else ""
        if sw_target_version and not _device_needs_update_for_bank_mode(
            sw,
            sw_target_version,
            bank_mode=bank_mode,
            allow_downgrade=allow_downgrade,
        ):
            logger.info(
                f"Skipping switch {sw_ip}: bank_mode={bank_mode}, target={sw_target_version} already satisfied"
            )
            continue

        credentials[sw_ip] = (sw["username"], sw["password"])
        device_roles[sw_ip] = "switch"
        if sw_fw is None and _is_tns100_model(sw.get("model")):
            device_firmware_map[sw_ip] = "__missing_tns100__"
        elif sw_fw is None:
            device_firmware_map[sw_ip] = str(firmware_path)
        else:
            device_firmware_map[sw_ip] = sw_fw

    if not device_roles:
        raise HTTPException(
            400,
            f"No devices require update for bank_mode='{bank_mode}' and selected target firmware.",
        )

    pre_update_reboot = db.get_setting("pre_update_reboot", "true") == "true"

    job_id = str(uuid.uuid4())[:8]
    job = UpdateJob(
        job_id=job_id,
        firmware_files=firmware_files,
        firmware_names=firmware_names,
        device_firmware_map=device_firmware_map,
        device_type=device_type,
        credentials=credentials,
        bank_mode=bank_mode,
        ap_cpe_map=ap_cpe_map,
        device_roles=device_roles,
        device_parent=device_parent,
        started_at=datetime.now(),
        status="running",
        pre_update_reboot=pre_update_reboot,
    )

    # Create device statuses for all devices (APs + CPEs + Switches)
    for ip in ap_ips:
        if ip in device_roles and device_roles[ip] == "ap":
            job.devices[ip] = DeviceStatus(ip=ip, role="ap")
    for ap_ip, cpe_ips in ap_cpe_map.items():
        for cpe_ip in cpe_ips:
            job.devices[cpe_ip] = DeviceStatus(ip=cpe_ip, role="cpe", parent_ap=ap_ip)
    for sw in switches:
        job.devices[sw["ip"]] = DeviceStatus(ip=sw["ip"], role="switch")

    update_jobs[job_id] = job

    await broadcast({
        "type": "job_started",
        "job_id": job_id,
        "device_count": len(job.devices),
        "firmware": firmware_file,
        "ap_cpe_map": ap_cpe_map,
        "device_roles": device_roles,
        "device_parent": device_parent,
        "bank_mode": bank_mode,
    })

    _spawn_update_job(job, concurrency)

    return {"job_id": job_id, "device_count": len(job.devices)}


@app.post("/api/update-device", tags=["jobs"])
async def update_single_device_endpoint(
    ip: str = Form(...),
    firmware_file: str = Form(...),
    firmware_file_303l: str = Form(""),
    firmware_file_tns100: str = Form(""),
    bank_mode: str = Form("both"),
    session: dict = Depends(require_role("admin", "operator")),
    _pro=Depends(require_feature(Feature.UPDATE_SINGLE_DEVICE)),
):
    """Start a firmware update for a single device (AP, CPE, or switch)."""
    # Validate and check firmware file exists
    safe_fw = validate_firmware_filename(firmware_file)
    firmware_path = FIRMWARE_DIR / safe_fw
    if not firmware_path.exists():
        raise HTTPException(400, f"Firmware file not found: {safe_fw}")

    firmware_files = {"tna-30x": str(firmware_path)}
    firmware_names = {"tna-30x": safe_fw}

    if firmware_file_303l:
        safe_303l = validate_firmware_filename(firmware_file_303l)
        path_303l = FIRMWARE_DIR / safe_303l
        if not path_303l.exists():
            raise HTTPException(400, f"303L firmware file not found: {safe_303l}")
        firmware_files["tna-303l"] = str(path_303l)
        firmware_names["tna-303l"] = safe_303l

    if firmware_file_tns100:
        safe_tns100 = validate_firmware_filename(firmware_file_tns100)
        path_tns100 = FIRMWARE_DIR / safe_tns100
        if not path_tns100.exists():
            raise HTTPException(400, f"TNS100 firmware file not found: {safe_tns100}")
        firmware_files["tns-100"] = str(path_tns100)
        firmware_names["tns-100"] = safe_tns100

    # Look up device - check APs first, then switches, then CPEs
    ap = db.get_access_point(ip)
    credentials = {}
    device_roles = {}
    ap_cpe_map = {}
    device_parent = {}
    device_firmware_map = {}

    if ap:
        # It's an AP - update just this AP (no CPEs)
        credentials[ip] = (ap["username"], ap["password"])
        device_roles[ip] = "ap"
        device_firmware_map[ip] = _select_firmware_for_model(ap.get("model"), firmware_files) or str(firmware_path)
        ap_cpe_map[ip] = []
    else:
        # Check if it's a switch
        sw = db.get_switch(ip)
        if sw:
            credentials[ip] = (sw["username"], sw["password"])
            device_roles[ip] = "switch"
            sw_fw = _select_firmware_for_model(sw.get("model"), firmware_files)
            if sw_fw is None and _is_tns100_model(sw.get("model")):
                device_firmware_map[ip] = "__missing_tns100__"
            elif sw_fw is None:
                device_firmware_map[ip] = str(firmware_path)
            else:
                device_firmware_map[ip] = sw_fw
        else:
            # Check if it's a CPE
            all_cpes = db.get_all_cpes()
            cpe = next((c for c in all_cpes if c["ip"] == ip), None)
            if not cpe:
                raise HTTPException(404, f"Device not found: {ip}")

            # Get parent AP credentials
            parent_ap = db.get_access_point(cpe["ap_ip"])
            if not parent_ap:
                raise HTTPException(400, f"Parent AP {cpe['ap_ip']} not found for CPE {ip}")

            credentials[ip] = (parent_ap["username"], parent_ap["password"])
            device_roles[ip] = "cpe"
            device_parent[ip] = cpe["ap_ip"]
            cpe_fw = _select_firmware_for_model(cpe.get("model"), firmware_files)
            if cpe_fw is None and _is_303l_model(cpe.get("model")):
                device_firmware_map[ip] = "__missing_303l__"
            elif cpe_fw is None:
                device_firmware_map[ip] = str(firmware_path)
            else:
                device_firmware_map[ip] = cpe_fw
            # Create a dummy AP entry in ap_cpe_map so the job structure works
            ap_cpe_map[cpe["ap_ip"]] = [ip]

    pre_update_reboot = db.get_setting("pre_update_reboot", "true") == "true"

    job_id = str(uuid.uuid4())[:8]
    job = UpdateJob(
        job_id=job_id,
        firmware_files=firmware_files,
        firmware_names=firmware_names,
        device_firmware_map=device_firmware_map,
        device_type="mixed",
        credentials=credentials,
        bank_mode=bank_mode,
        ap_cpe_map=ap_cpe_map,
        device_roles=device_roles,
        device_parent=device_parent,
        started_at=datetime.now(),
        status="running",
        pre_update_reboot=pre_update_reboot,
    )

    role = device_roles[ip]
    job.devices[ip] = DeviceStatus(
        ip=ip, role=role,
        parent_ap=device_parent.get(ip)
    )

    update_jobs[job_id] = job

    await broadcast({
        "type": "job_started",
        "job_id": job_id,
        "device_count": 1,
        "firmware": firmware_file,
        "ap_cpe_map": ap_cpe_map,
        "device_roles": device_roles,
        "device_parent": device_parent,
        "bank_mode": bank_mode,
    })

    _spawn_update_job(job, concurrency=1)

    return {"job_id": job_id, "device_count": 1}


def _request_job_cancel(job: "UpdateJob", reason: str):
    """Cancel a running job and preserve the first meaningful reason."""
    job.cancelled = True
    if not job.cancel_reason:
        job.cancel_reason = reason


def _minutes_until_window_end(now: datetime, start_hour: int, end_hour: int) -> int:
    """Compute minutes remaining in the active schedule window."""
    if start_hour < end_hour:
        # Same-day window (e.g., 03:00-04:00)
        return (end_hour - now.hour) * 60 - now.minute
    # Overnight window (e.g., 20:00-04:00)
    if now.hour >= start_hour:
        return (24 - now.hour + end_hour) * 60 - now.minute
    return (end_hour - now.hour) * 60 - now.minute


async def _scheduled_job_guard(job: "UpdateJob") -> tuple[bool, str]:
    """Fail-safe runtime gate for scheduled jobs."""
    if not job.is_scheduled:
        return (True, "")

    if (
        job.start_hour is None
        or job.end_hour is None
        or not job.schedule_timezone
        or not job.schedule_days
    ):
        return (False, "Scheduled update missing maintenance window metadata")

    time_ok, time_result = await services.validate_time_sources(job.schedule_timezone)
    if not time_ok:
        return (False, f"Time anomaly: {time_result}")

    if not isinstance(time_result, datetime):
        return (False, "Time validation returned an invalid timestamp")

    now = time_result
    current_day = now.strftime("%a").lower()
    if not services.is_in_schedule_window(
        now.hour,
        current_day,
        job.schedule_days,
        job.start_hour,
        job.end_hour,
    ):
        return (False, "Outside maintenance window")

    if _minutes_until_window_end(now, job.start_hour, job.end_hour) <= SCHEDULE_END_BUFFER_MINUTES:
        return (False, "Maintenance window ending")

    return (True, "")


async def _update_single_device(job: "UpdateJob", ip: str, pass_number: int = 1):
    """Update a single device within a job."""
    device_status = job.devices[ip]

    # Maintenance window cutoff for scheduled jobs
    if job.is_scheduled and job.enforce_window_cutoff and job.end_hour is not None and job.schedule_timezone:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(job.schedule_timezone)
            now = datetime.now(tz)
            # Handle overnight windows (e.g., 20:00-04:00)
            if job.end_hour > now.hour:
                # Same day: end is later today
                minutes_until_end = (job.end_hour - now.hour) * 60 - now.minute
            else:
                # Overnight: end is tomorrow morning
                minutes_until_end = (24 - now.hour + job.end_hour) * 60 - now.minute
            if minutes_until_end < 10:
                device_status.status = "skipped"
                device_status.progress_message = "Maintenance window ending"
                await broadcast({
                    "type": "device_update",
                    "job_id": job.job_id,
                    "ip": ip,
                    "status": device_status.status,
                    "message": device_status.progress_message,
                    "role": device_status.role,
                    "parent_ap": device_status.parent_ap,
                })
                now_iso = datetime.now().isoformat()
                try:
                    db.save_device_update_history(
                        job_id=job.job_id, ip=ip, role=device_status.role,
                        pass_number=pass_number, status="skipped",
                        old_version=None, new_version=None, model=None,
                        error="Maintenance window ending", failed_stage=None,
                        stages=[], duration_seconds=0,
                        started_at=now_iso, completed_at=now_iso,
                    )
                except Exception as e:
                    logger.warning(f"Failed to save device history for {ip}: {e}")
                return
        except Exception as e:
            logger.warning(f"Maintenance window check failed: {e}")


    device_start_time = datetime.now()

    # Reset stage tracking for this pass
    device_status.stage_history = []
    device_status.current_stage = "connecting"
    device_status.current_stage_started = device_start_time.isoformat()

    prefix = f"Pass {pass_number}: " if pass_number > 1 else ""
    device_status.status = "connecting"
    device_status.progress_message = f"{prefix}Connecting..."

    await broadcast({
        "type": "device_update",
        "job_id": job.job_id,
        "ip": ip,
        "status": device_status.status,
        "message": device_status.progress_message,
        "role": device_status.role,
        "parent_ap": device_status.parent_ap,
    })

    # Check for missing firmware
    fw_path = job.device_firmware_map.get(ip, "")
    missing_fw_error = None
    if fw_path == "__missing_303l__":
        missing_fw_error = "TNA-303L device requires 303L firmware, but none was provided"
    elif fw_path == "__missing_tns100__":
        missing_fw_error = "TNS-100 device requires TNS100 firmware, but none was provided"

    if missing_fw_error:
        device_status.status = "failed"
        device_status.error = missing_fw_error
        device_status.progress_message = missing_fw_error
        await broadcast({
            "type": "device_update",
            "job_id": job.job_id,
            "ip": ip,
            "status": device_status.status,
            "message": device_status.progress_message,
            "error": device_status.error,
            "role": device_status.role,
            "parent_ap": device_status.parent_ap,
        })
        now_iso = datetime.now().isoformat()
        try:
            db.save_device_update_history(
                job_id=job.job_id, ip=ip, role=device_status.role,
                pass_number=pass_number, status="failed",
                old_version=None, new_version=None, model=device_status.model,
                error=missing_fw_error, failed_stage="connecting",
                stages=[], duration_seconds=0,
                started_at=device_start_time.isoformat(), completed_at=now_iso,
            )
        except Exception as e:
            logger.warning(f"Failed to save device history for {ip}: {e}")
        return

    def progress_callback(device_ip: str, message: str):
        status_map = {
            "Logging in": "connecting",
            "Getting device info": "connecting",
            "Uploading firmware": "uploading",
            "Installing firmware": "installing",
            "Rebooting": "rebooting",
            "Verifying": "verifying",
            "Smoke testing": "smoke_testing",
            "Skipped": "skipped",
        }
        new_stage = None
        for key, status in status_map.items():
            if key in message:
                new_stage = status
                device_status.status = status
                break

        # Record stage transitions
        if new_stage and new_stage != device_status.current_stage:
            now_iso = datetime.now().isoformat()
            if device_status.current_stage and device_status.current_stage_started:
                device_status.stage_history.append({
                    "stage": device_status.current_stage,
                    "started_at": device_status.current_stage_started,
                    "completed_at": now_iso,
                    "success": True,
                })
            device_status.current_stage = new_stage
            device_status.current_stage_started = now_iso

        device_status.progress_message = f"{prefix}{message}"

        asyncio.create_task(broadcast({
            "type": "device_update",
            "job_id": job.job_id,
            "ip": device_ip,
            "status": device_status.status,
            "message": device_status.progress_message,
            "role": device_status.role,
            "parent_ap": device_status.parent_ap,
        }))

    # Capture pre-update CPE count for smoke test comparison
    pre_update_cpe_count = len(job.ap_cpe_map.get(ip, [])) if device_status.role == "ap" else 0

    client = None
    if job.device_type in ("tachyon", "mixed"):
        username, password = job.credentials[ip]
        client = TachyonClient(ip, username, password)
        reboot_timeout = TNS100_REBOOT_TIMEOUT if device_status.role == "switch" else AP_REBOOT_TIMEOUT
        try:
            bw_limit = int(db.get_setting("bandwidth_limit_kbps", "0"))
        except (ValueError, TypeError):
            bw_limit = 0
        result = await client.update_firmware(fw_path, progress_callback, pass_number=pass_number, reboot_timeout=reboot_timeout, bandwidth_limit_kbps=bw_limit)
    else:
        result = UpdateResult(ip=ip, success=False, error=f"Unsupported device type: {job.device_type}")

    device_status.old_version = result.old_version
    device_status.new_version = result.new_version
    device_status.bank1_version = result.bank1_version
    device_status.bank2_version = result.bank2_version
    device_status.active_bank = result.active_bank
    device_status.model = result.model

    if result.skipped:
        device_status.status = "skipped"
        device_status.progress_message = f"{prefix}Already on {result.new_version}"
    elif result.success:
        device_status.status = "success"
        device_status.progress_message = f"{prefix}Updated to {result.new_version}"
        duration_secs = (datetime.now() - device_start_time).total_seconds()
        try:
            db.save_device_duration(job.job_id, ip, device_status.role, duration_secs, job.bank_mode)
        except Exception as e:
            logger.warning(f"Failed to save device duration for {ip}: {e}")

        # Post-update smoke tests
        if client:
            try:
                progress_callback(ip, "Smoke testing...")
                smoke_result = await client.run_smoke_tests(
                    role=device_status.role,
                    pre_update_cpe_count=pre_update_cpe_count,
                )
                device_status.smoke_checks = smoke_result.checks
                if not smoke_result.passed:
                    device_status.smoke_warnings = smoke_result.warnings
                    warning_summary = "; ".join(smoke_result.warnings[:3])
                    if len(warning_summary) > 200:
                        warning_summary = warning_summary[:197] + "..."
                    device_status.progress_message = f"{prefix}Updated to {result.new_version} (warnings: {warning_summary})"
                    logger.warning(f"Smoke test warnings for {ip}: {smoke_result.warnings}")
                else:
                    device_status.progress_message = f"{prefix}Updated to {result.new_version} (smoke tests passed)"
            except Exception as e:
                logger.warning(f"Smoke test error for {ip} (non-fatal): {e}")
                device_status.smoke_warnings = [f"Smoke test error: {e}"]
            # Restore status — progress_callback sets it to smoke_testing
            device_status.status = "success"
    else:
        device_status.status = "failed"
        device_status.error = result.error
        device_status.progress_message = f"{prefix}{result.error or 'Update failed'}"

        # If device didn't come back online, cancel the job
        if result.error and "did not come back online" in result.error:
            _request_job_cancel(job, "Cancelled: device did not come back online")

    await broadcast({
        "type": "device_update",
        "job_id": job.job_id,
        "ip": ip,
        "status": device_status.status,
        "message": device_status.progress_message,
        "old_version": device_status.old_version,
        "new_version": device_status.new_version,
        "error": device_status.error,
        "bank1_version": device_status.bank1_version,
        "bank2_version": device_status.bank2_version,
        "active_bank": device_status.active_bank,
        "role": device_status.role,
        "parent_ap": device_status.parent_ap,
        "model": device_status.model,
        "smoke_warnings": device_status.smoke_warnings,
        "smoke_checks": device_status.smoke_checks,
    })

    # Finalize stage tracking and persist device update history
    now = datetime.now()
    now_iso = now.isoformat()
    if device_status.current_stage and device_status.current_stage_started:
        is_success = device_status.status in ("success", "skipped")
        device_status.stage_history.append({
            "stage": device_status.current_stage,
            "started_at": device_status.current_stage_started,
            "completed_at": now_iso,
            "success": is_success,
        })
    failed_stage = device_status.current_stage if device_status.status == "failed" else None

    # Enrich smoke_testing stage with check details and warnings
    for stage in device_status.stage_history:
        if stage["stage"] == "smoke_testing":
            stage["smoke_checks"] = device_status.smoke_checks or []
            stage["smoke_warnings"] = device_status.smoke_warnings or []
            stage["has_warnings"] = bool(device_status.smoke_warnings)
            break

    duration_secs = (now - device_start_time).total_seconds()
    try:
        db.save_device_update_history(
            job_id=job.job_id, ip=ip, role=device_status.role,
            pass_number=pass_number, status=device_status.status,
            old_version=device_status.old_version, new_version=device_status.new_version,
            model=device_status.model, error=device_status.error,
            failed_stage=failed_stage, stages=device_status.stage_history,
            duration_seconds=duration_secs,
            started_at=device_start_time.isoformat(), completed_at=now_iso,
        )
    except Exception as e:
        logger.warning(f"Failed to save device update history for {ip}: {e}")


async def run_update_job(job: UpdateJob, concurrency: int):
    """Run the firmware update job with phase-based ordering driven by bank_mode."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    # For scheduled jobs, enforce maintenance window/time-source checks only at start.
    # Once started, let the in-flight job complete even if it runs past window end.
    is_allowed, block_reason = await _scheduled_job_guard(job)
    if not is_allowed:
        _request_job_cancel(job, block_reason)
        logger.warning(f"Job {job.job_id} cancelled before start: {block_reason}")

    ap_ips = [ip for ip, role in job.device_roles.items() if role == "ap"]
    cpe_ips = [ip for ip, role in job.device_roles.items() if role == "cpe"]
    switch_ips = [ip for ip, role in job.device_roles.items() if role == "switch"]

    # Pre-update reboot phase: reboot all devices before firmware update
    if job.pre_update_reboot:
        all_ips = cpe_ips + ap_ips + switch_ips
        if all_ips:
            logger.info(f"Job {job.job_id}: starting pre-update reboot phase ({len(all_ips)} devices)")

            async def _reboot_device(ip):
                async with semaphore:
                    if job.cancelled:
                        return
                    ds = job.devices.get(ip)
                    if not ds:
                        return
                    ds.status = "pre-rebooting"
                    ds.progress_message = "Pre-update reboot..."
                    await broadcast({
                        "type": "device_update",
                        "job_id": job.job_id,
                        "ip": ip,
                        "status": ds.status,
                        "message": ds.progress_message,
                        "role": ds.role,
                        "parent_ap": ds.parent_ap,
                    })
                    username, password = job.credentials.get(ip, ("", ""))
                    client = TachyonClient(ip, username, password)
                    try:
                        login_result = await client.login()
                        if login_result is not True:
                            ds.status = "failed"
                            ds.error = login_result if isinstance(login_result, str) else "Login failed"
                            ds.progress_message = f"Pre-reboot login failed: {ds.error}"
                            _request_job_cancel(job, "Cancelled: another device failed to reboot")
                            return
                        timeout = TNS100_REBOOT_TIMEOUT if ds.role == "switch" else AP_REBOOT_TIMEOUT
                        if not await client.reboot(timeout=timeout):
                            ds.status = "failed"
                            ds.error = "Device did not come back online after pre-update reboot"
                            ds.progress_message = ds.error
                            _request_job_cancel(job, "Cancelled: another device failed to reboot")
                            return
                        ds.status = "pending"
                        ds.progress_message = "Rebooted, waiting for update..."
                    except Exception as e:
                        ds.status = "failed"
                        ds.error = f"Pre-update reboot failed: {e}"
                        ds.progress_message = ds.error
                        _request_job_cancel(job, "Cancelled: another device failed to reboot")
                        return
                    await broadcast({
                        "type": "device_update",
                        "job_id": job.job_id,
                        "ip": ip,
                        "status": ds.status,
                        "message": ds.progress_message,
                        "role": ds.role,
                        "parent_ap": ds.parent_ap,
                    })

            await asyncio.gather(
                *[_reboot_device(ip) for ip in all_ips],
                return_exceptions=True,
            )

            if job.cancelled:
                cancel_message = job.cancel_reason or "Cancelled: another device failed to reboot"
                # Mark any remaining pending devices as cancelled
                for ip in all_ips:
                    ds = job.devices.get(ip)
                    if ds and ds.status == "pending":
                        ds.status = "cancelled"
                        ds.progress_message = cancel_message
                        await broadcast({
                            "type": "device_update",
                            "job_id": job.job_id,
                            "ip": ip,
                            "status": ds.status,
                            "message": ds.progress_message,
                            "role": ds.role,
                            "parent_ap": ds.parent_ap,
                        })
                        now_iso = datetime.now().isoformat()
                        try:
                            db.save_device_update_history(
                                job_id=job.job_id, ip=ip, role=ds.role,
                                pass_number=1, status="cancelled",
                                old_version=None, new_version=None, model=None,
                                error=cancel_message,
                                failed_stage=None, stages=[], duration_seconds=0,
                                started_at=now_iso, completed_at=now_iso,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to save cancelled device history for {ip}: {e}")

    # Build phase list based on bank_mode
    # Switches always run last, after all APs and CPEs complete
    if job.bank_mode == "both":
        phases = [
            (cpe_ips, 1, "CPEs pass 1"),
            (ap_ips, 1, "APs pass 1"),
            (ap_ips, 2, "APs pass 2"),
            (cpe_ips, 2, "CPEs pass 2"),
            (switch_ips, 1, "Switches pass 1"),
            (switch_ips, 2, "Switches pass 2"),
        ]
    else:
        phases = [
            (ap_ips, 1, "APs"),
            (cpe_ips, 1, "CPEs"),
            (switch_ips, 1, "Switches"),
        ]

    async def _mark_cancelled(ips):
        """Mark unstarted devices as cancelled."""
        cancel_message = job.cancel_reason or "Cancelled: another device failed to reboot"
        for ip in ips:
            ds = job.devices.get(ip)
            if ds and ds.status == "pending":
                ds.status = "cancelled"
                ds.progress_message = cancel_message
                await broadcast({
                    "type": "device_update",
                    "job_id": job.job_id,
                    "ip": ip,
                    "status": ds.status,
                    "message": ds.progress_message,
                    "role": ds.role,
                    "parent_ap": ds.parent_ap,
                })
                now_iso = datetime.now().isoformat()
                try:
                    db.save_device_update_history(
                        job_id=job.job_id, ip=ip, role=ds.role,
                        pass_number=1, status="cancelled",
                        old_version=None, new_version=None, model=None,
                        error=cancel_message,
                        failed_stage=None, stages=[], duration_seconds=0,
                        started_at=now_iso, completed_at=now_iso,
                    )
                except Exception as e:
                    logger.warning(f"Failed to save cancelled device history for {ip}: {e}")

    async def _run_device(ip, pass_number):
        async with semaphore:
            if job.cancelled:
                return
            await _update_single_device(job, ip, pass_number=pass_number)

    for device_ips, pass_number, phase_label in phases:
        if job.cancelled:
            # Mark remaining devices in this and subsequent phases
            await _mark_cancelled(device_ips)
            continue

        if not device_ips:
            continue

        logger.info(f"Job {job.job_id}: starting phase '{phase_label}' ({len(device_ips)} devices)")

        # Reset status for pass 2 devices
        if pass_number > 1:
            for ip in device_ips:
                ds = job.devices.get(ip)
                if ds:
                    ds.status = "pending"
                    ds.progress_message = f"Pass {pass_number}: Waiting..."
                    await broadcast({
                        "type": "device_update",
                        "job_id": job.job_id,
                        "ip": ip,
                        "status": ds.status,
                        "message": ds.progress_message,
                        "role": ds.role,
                        "parent_ap": ds.parent_ap,
                    })

        await asyncio.gather(
            *[_run_device(ip, pass_number) for ip in device_ips],
            return_exceptions=True,
        )

        # Check cancellation after phase completes
        if job.cancelled:
            # Mark remaining phases' devices
            all_remaining = []
            for future_ips, _, _ in phases:
                for ip in future_ips:
                    ds = job.devices.get(ip)
                    if ds and ds.status == "pending":
                        all_remaining.append(ip)
            await _mark_cancelled(all_remaining)

    job.completed_at = datetime.now()
    job.status = "completed"

    # Brief pause so final device_update broadcasts reach clients before job_completed
    await asyncio.sleep(0.5)

    success_count = sum(1 for d in job.devices.values() if d.status == "success")
    failed_count = sum(1 for d in job.devices.values() if d.status == "failed")
    skipped_count = sum(1 for d in job.devices.values() if d.status == "skipped")
    cancelled_count = sum(1 for d in job.devices.values() if d.status == "cancelled")

    # Resolve timezone for the completed job
    resolved_tz = job.schedule_timezone
    if not resolved_tz:
        settings = db.get_all_settings()
        tz_setting = settings.get("timezone", "auto")
        if tz_setting == "auto":
            resolved_tz = await services.get_timezone()
        else:
            resolved_tz = tz_setting

    await broadcast({
        "type": "job_completed",
        "job_id": job.job_id,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "cancelled_count": cancelled_count,
        "duration": (job.completed_at - job.started_at).total_seconds(),
        "timezone": resolved_tz,
        "devices": {
            ip: {
                "status": ds.status,
                "message": ds.progress_message,
                "old_version": ds.old_version,
                "new_version": ds.new_version,
                "error": ds.error,
                "bank1_version": ds.bank1_version,
                "bank2_version": ds.bank2_version,
                "active_bank": ds.active_bank,
                "role": ds.role,
                "parent_ap": ds.parent_ap,
                "model": ds.model,
                "smoke_warnings": ds.smoke_warnings,
            }
            for ip, ds in job.devices.items()
        },
        "ap_cpe_map": job.ap_cpe_map,
        "device_roles": job.device_roles,
    })

    # Persist to database
    devices_dict = {
        ip: {
            "status": ds.status,
            "old_version": ds.old_version,
            "new_version": ds.new_version,
            "error": ds.error,
            "role": ds.role,
            "parent_ap": ds.parent_ap,
            "model": ds.model,
        }
        for ip, ds in job.devices.items()
    }
    try:
        db.save_job_history(
            job_id=job.job_id,
            started_at=job.started_at.isoformat() if job.started_at else None,
            completed_at=job.completed_at.isoformat() if job.completed_at else None,
            duration=(job.completed_at - job.started_at).total_seconds(),
            bank_mode=job.bank_mode,
            success_count=success_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            cancelled_count=cancelled_count,
            devices=devices_dict,
            ap_cpe_map=job.ap_cpe_map,
            device_roles=job.device_roles,
            timezone=resolved_tz,
        )
    except Exception as e:
        logger.error(f"Failed to persist job history for {job.job_id}: {e}")

    # Send anonymized telemetry (non-blocking background task)
    asyncio.create_task(telemetry.send_telemetry_background(
        job_id=job.job_id,
        success_count=success_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        cancelled_count=cancelled_count,
        duration_seconds=(job.completed_at - job.started_at).total_seconds(),
        bank_mode=job.bank_mode,
        is_scheduled=job.is_scheduled,
        devices=devices_dict,
    ))

    # Send Slack notification (non-blocking)
    rollout_info = None
    next_job_info = None
    if job.is_scheduled:
        scheduler = get_scheduler()
        if scheduler:
            status = scheduler.get_status()
            rollout_info = status.get("rollout")
            if rollout_info:
                predictions = rollout_info.get("predictions")
                if predictions:
                    remaining = predictions.get("remaining_phases", [])
                    next_phase = None
                    estimated_devices = 0
                    for p in remaining:
                        if p.get("estimated_devices", 0) > 0:
                            next_phase = p.get("phase")
                            estimated_devices = p.get("estimated_devices")
                            break
                    next_job_info = {
                        "next_window": status.get("next_window", ""),
                        "next_phase": next_phase,
                        "estimated_devices": estimated_devices,
                        "estimated_completion": predictions.get("estimated_completion_date"),
                    }

    # Notify scheduler if this was a scheduled job
    if job.is_scheduled:
        scheduler = get_scheduler()
        if scheduler:
            learned_versions = {}
            for ds in job.devices.values():
                if ds.status == "success" and ds.new_version:
                    fw_type = _get_firmware_type_for_model(ds.model)
                    if fw_type and fw_type not in learned_versions:
                        learned_versions[fw_type] = ds.new_version
            # Pass device statuses so rollout devices get marked correctly
            device_statuses = {ip: ds.status for ip, ds in job.devices.items()}
            scheduler.on_job_completed(job.job_id, success_count, failed_count,
                                       learned_versions=learned_versions,
                                       device_statuses=device_statuses,
                                       cancel_reason=job.cancel_reason)

    firmware_name = (
        job.firmware_names.get("tna-30x", "")
        or list(job.firmware_names.values())[0]
        if job.firmware_names else "Unknown"
    )
    try:
        await slack.notify_job_completed(
            job_id=job.job_id,
            success_count=success_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            cancelled_count=cancelled_count,
            duration_seconds=(job.completed_at - job.started_at).total_seconds(),
            devices=devices_dict,
            firmware_name=firmware_name,
            is_scheduled=job.is_scheduled,
            rollout_info=rollout_info,
            next_job_info=next_job_info,
        )
    except Exception as e:
        logger.error(f"Slack notification failed for job {job.job_id}: {e}")

    try:
        await snmp.notify_job_completed(
            job_id=job.job_id,
            success_count=success_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            cancelled_count=cancelled_count,
            duration_seconds=(job.completed_at - job.started_at).total_seconds(),
            devices=devices_dict,
            firmware_name=firmware_name,
            is_scheduled=job.is_scheduled,
            rollout_info=rollout_info,
            next_job_info=next_job_info,
        )
    except Exception as e:
        logger.error(f"SNMP notification failed for job {job.job_id}: {e}")

    logger.info(f"Job {job.job_id} completed: {success_count} success, {failed_count} failed, {skipped_count} skipped, {cancelled_count} cancelled")


@app.get("/api/job/{job_id}", tags=["jobs"])
async def get_job_status(job_id: str, session: dict = Depends(require_auth)):
    """Get status of an update job."""
    if job_id not in update_jobs:
        raise HTTPException(404, "Job not found")

    job = update_jobs[job_id]
    return {
        "job_id": job.job_id,
        "status": job.status,
        "cancelled": job.cancelled,
        "cancel_reason": job.cancel_reason,
        "firmware_names": job.firmware_names,
        "device_type": job.device_type,
        "bank_mode": job.bank_mode,
        "ap_cpe_map": job.ap_cpe_map,
        "device_roles": job.device_roles,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "devices": {ip: asdict(status) for ip, status in job.devices.items()},
    }


@app.post("/api/job/{job_id}/cancel", tags=["jobs"])
async def cancel_job(job_id: str, session: dict = Depends(require_role("admin", "operator"))):
    """Request cancellation of an active update job."""
    job = update_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job.status == "completed":
        raise HTTPException(400, "Job already completed")

    if not job.cancelled:
        _request_job_cancel(job, "Cancelled by user")
        logger.info(f"User requested cancellation for job {job_id}")
        await broadcast({
            "type": "job_cancel_requested",
            "job_id": job_id,
            "message": job.cancel_reason,
        })

    return {
        "job_id": job_id,
        "cancelled": True,
        "message": job.cancel_reason or "Cancellation requested",
    }


# ============================================================================
# Analytics API
# ============================================================================

@app.get("/api/analytics/summary", tags=["analytics"])
async def get_analytics_summary(days: int = 90, session: dict = Depends(require_auth)):
    """Get aggregate update statistics over a time window."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    return db.get_analytics_summary(days)


@app.get("/api/analytics/trends", tags=["analytics"])
async def get_analytics_trends(days: int = 30, session: dict = Depends(require_auth)):
    """Get daily success/failure trends."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    return {"trends": db.get_analytics_trends(days)}


@app.get("/api/analytics/models", tags=["analytics"])
async def get_analytics_models(days: int = 90, session: dict = Depends(require_auth)):
    """Get update success/failure breakdown by device model."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    return {"models": db.get_analytics_by_model(days)}


@app.get("/api/analytics/errors", tags=["analytics"])
async def get_analytics_errors(days: int = 90, limit: int = 10, session: dict = Depends(require_auth)):
    """Get top error messages from failed updates."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    limit = max(1, min(limit, 200))
    return {"errors": db.get_analytics_errors(days, limit)}


@app.get("/api/analytics/reliability", tags=["analytics"])
async def get_analytics_reliability(days: int = 90, limit: int = 20, session: dict = Depends(require_auth)):
    """Get per-device reliability stats, worst performers first."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    limit = max(1, min(limit, 200))
    return {"devices": db.get_analytics_device_reliability(days, limit)}


@app.get("/api/uptime/device", tags=["analytics"])
async def get_device_uptime(ip: str, days: int = 30, session: dict = Depends(require_auth)):
    """Get availability/uptime data for a specific device."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    result = db.get_device_availability(ip, days)
    return result


@app.get("/api/uptime/fleet", tags=["analytics"])
async def get_fleet_uptime(device_type: str = None, days: int = 30, session: dict = Depends(require_auth)):
    """Get fleet-wide availability stats, worst performers first."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    if device_type and device_type not in ("ap", "switch"):
        raise HTTPException(400, "device_type must be 'ap' or 'switch'")
    devices = db.get_fleet_availability(device_type, days)
    return {"devices": devices}


@app.get("/api/uptime/events", tags=["analytics"])
async def get_uptime_events(ip: str, days: int = 30, limit: int = 100, session: dict = Depends(require_auth)):
    """Get raw uptime events for a device."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    limit = max(1, min(limit, 1000))
    events = db.get_uptime_events(ip, days, limit)
    return {"events": events}


@app.get("/api/device-history", tags=["config"])
async def get_device_history_api(
    ip: str = None, action: str = None, status: str = None,
    limit: int = 100, offset: int = 0,
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.DEVICE_HISTORY)),
):
    """Get filterable device update/config history."""
    history = db.get_device_update_history(ip=ip, action=action, status=status, limit=limit, offset=offset)
    return {"history": history}


# ============================================================================
# Device Config Backup & Management
# ============================================================================

# Tracks IPs currently being pushed to, preventing overlapping pushes
_config_pushing_ips: set = set()
_config_push_lock = asyncio.Lock()

# ============================================================================
# ============================================================================

def _canonical_config_json(config: dict) -> str:
    """Serialize config dict to deterministic compact JSON for storage and hashing."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def _compute_config_hash(config: dict) -> str:
    """Compute deterministic SHA-256 hash of a config dict."""
    import hashlib
    return hashlib.sha256(_canonical_config_json(config).encode()).hexdigest()


# Top-level config keys that templates must never modify (prevents bricking devices)
from .config_utils import (
    PROTECTED_CONFIG_KEYS,
    validate_fragment_safety as _validate_fragment_safety,
    deep_merge,
    check_config_compliance as _check_config_compliance,
    fragment_matches as _fragment_matches,
)


@app.get("/api/configs", tags=["config"])
async def get_configs_summary(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """List all devices with their latest config summary."""
    all_configs = db.get_all_latest_configs()
    result = {}
    for ip, cfg in all_configs.items():
        result[ip] = {
            "id": cfg["id"],
            "config_hash": cfg["config_hash"],
            "model": cfg["model"],
            "fetched_at": cfg["fetched_at"],
        }
    return {"configs": result}


@app.get("/api/configs/{ip}", tags=["config"])
async def get_config_history(ip: str, limit: int = 20, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Get config snapshot history for a device."""
    history = db.get_device_config_history(ip, limit=limit)
    return {"history": history}


@app.get("/api/configs/{ip}/latest", tags=["config"])
async def get_latest_config(ip: str, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Get the latest config JSON for a device."""
    config = db.get_latest_device_config(ip)
    if not config:
        raise HTTPException(404, "No config found for this device")
    config["config_json"] = json.loads(config["config_json"]) if isinstance(config["config_json"], str) else config["config_json"]
    return config


@app.get("/api/configs/{ip}/snapshot/{config_id}", tags=["config"])
async def get_config_snapshot(ip: str, config_id: int, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Get a specific config snapshot."""
    config = db.get_device_config_by_id(config_id)
    if not config or config["ip"] != ip:
        raise HTTPException(404, "Config snapshot not found")
    config["config_json"] = json.loads(config["config_json"]) if isinstance(config["config_json"], str) else config["config_json"]
    return config


@app.get("/api/configs/{ip}/download/{config_id}", tags=["config"])
async def download_config_tar(ip: str, config_id: int, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Download a config snapshot as a .tar file with config.json + CONTROL."""
    import io
    import tarfile

    config = db.get_device_config_by_id(config_id)
    if not config or config["ip"] != ip:
        raise HTTPException(404, "Config snapshot not found")

    config_json_str = config["config_json"]
    if isinstance(config_json_str, str):
        config_data = json.loads(config_json_str)
    else:
        config_data = config_json_str
    pretty_json = json.dumps(config_data, indent=2)

    hardware_id = config.get("hardware_id") or "tn-110-prs"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # Add config.json
        json_bytes = pretty_json.encode("utf-8")
        json_info = tarfile.TarInfo(name="config.json")
        json_info.size = len(json_bytes)
        tar.addfile(json_info, io.BytesIO(json_bytes))

        # Add CONTROL
        control_bytes = hardware_id.encode("utf-8")
        control_info = tarfile.TarInfo(name="CONTROL")
        control_info.size = len(control_bytes)
        tar.addfile(control_info, io.BytesIO(control_bytes))

    buf.seek(0)
    device_name = ip.replace(".", "-")
    filename = f"config-{device_name}-{config['fetched_at'][:10]}.tar"

    return StreamingResponse(
        buf,
        media_type="application/x-tar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/configs/{ip}/poll", tags=["config"])
async def poll_device_config(ip: str, session: dict = Depends(require_role("admin", "operator"))):
    """Trigger immediate config fetch for one device."""
    # Find the device (AP, CPE, or switch)
    device = db.get_access_point(ip)
    role = "ap"
    if not device:
        device = db.get_switch(ip)
        role = "switch"
    if not device:
        cpe = db.get_cpe_by_ip(ip)
        if cpe:
            # For CPEs, need to find parent AP credentials
            ap = db.get_access_point(cpe["ap_ip"])
            if ap:
                device = {"ip": ip, "username": ap["username"], "password": ap["password"]}
                role = "cpe"
    if not device:
        raise HTTPException(404, "Device not found")

    client = TachyonClient(ip, device["username"], device["password"])
    login_result = await client.login()
    if login_result is not True:
        raise HTTPException(502, f"Login failed: {login_result}")

    config = await client.get_config()
    if config is None:
        raise HTTPException(502, "Failed to fetch config from device")

    config_json = _canonical_config_json(config)
    config_hash = _compute_config_hash(config)

    existing_hash = db.get_latest_config_hash(ip)
    changed = existing_hash != config_hash

    # Get model and hardware_id
    model = device.get("model")
    hardware_id = client.get_hardware_id(model)

    db.save_device_config(ip, config_json, config_hash, model, hardware_id)

    return {"success": True, "changed": changed, "config_hash": config_hash}


@app.post("/api/configs/poll", tags=["config"])
async def poll_all_configs(session: dict = Depends(require_role("admin", "operator"))):
    """Trigger config poll for all devices."""
    poller = get_poller()
    if poller:
        asyncio.create_task(poller.poll_all_configs())
        return {"success": True, "message": "Config poll started"}
    raise HTTPException(500, "Poller not initialized")


# ============================================================================
# Config Templates
# ============================================================================

@app.get("/api/config-templates", tags=["config"])
async def list_config_templates(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_TEMPLATES))):
    """List all config templates."""
    templates = db.get_config_templates()
    for t in templates:
        t["config_fragment"] = json.loads(t["config_fragment"]) if isinstance(t["config_fragment"], str) else t["config_fragment"]
        if t.get("form_data"):
            t["form_data"] = json.loads(t["form_data"]) if isinstance(t["form_data"], str) else t["form_data"]
    return {"templates": templates}


@app.post("/api/config-templates", tags=["config"])
async def create_config_template(request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.CONFIG_TEMPLATES))):
    """Create a new config template."""
    data = await request.json()
    name = data.get("name")
    category = data.get("category")
    config_fragment = data.get("config_fragment")
    if not name or not category or not config_fragment:
        raise HTTPException(400, "name, category, and config_fragment are required")

    # Validate fragment is valid JSON and doesn't touch protected keys
    if isinstance(config_fragment, str):
        try:
            config_fragment = json.loads(config_fragment)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Invalid JSON in config_fragment: {e}")
    try:
        _validate_fragment_safety(config_fragment)
    except ValueError as e:
        raise HTTPException(400, str(e))

    fragment_str = json.dumps(config_fragment)
    form_data_str = json.dumps(data["form_data"]) if data.get("form_data") else None

    scope = data.get("scope", "global")
    site_id = data.get("site_id")
    device_types = data.get("device_types")

    if scope == "site":
        if not site_id:
            raise HTTPException(400, "site_id required when scope is 'site'")
        site = db.get_tower_site(site_id)
        if not site:
            raise HTTPException(400, f"Invalid site_id: {site_id}")

    device_types_str = json.dumps(device_types) if device_types else None

    try:
        template_id = db.save_config_template(
            name=name,
            category=category,
            config_fragment=fragment_str,
            form_data=form_data_str,
            description=data.get("description"),
            scope=scope,
            site_id=site_id,
            device_types=device_types_str,
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"Template with name '{name}' already exists")
        raise

    return {"id": template_id, "success": True}


@app.put("/api/config-templates/{template_id}", tags=["config"])
async def update_config_template_api(template_id: int, request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.CONFIG_TEMPLATES))):
    """Update a config template."""
    existing = db.get_config_template(template_id)
    if not existing:
        raise HTTPException(404, "Template not found")

    data = await request.json()
    updates = {}
    if "name" in data:
        updates["name"] = data["name"]
    if "category" in data:
        updates["category"] = data["category"]
    if "config_fragment" in data:
        frag = data["config_fragment"]
        if isinstance(frag, str):
            try:
                frag = json.loads(frag)
            except json.JSONDecodeError as e:
                raise HTTPException(400, f"Invalid JSON in config_fragment: {e}")
        try:
            _validate_fragment_safety(frag)
        except ValueError as e:
            raise HTTPException(400, str(e))
        updates["config_fragment"] = json.dumps(frag)
    if "form_data" in data:
        updates["form_data"] = json.dumps(data["form_data"]) if isinstance(data["form_data"], dict) else data["form_data"]
    if "description" in data:
        updates["description"] = data["description"]
    if "enabled" in data:
        updates["enabled"] = 1 if data["enabled"] else 0
    if "scope" in data:
        updates["scope"] = data["scope"]
    if "site_id" in data:
        if data.get("scope") == "site" and data["site_id"]:
            site = db.get_tower_site(data["site_id"])
            if not site:
                raise HTTPException(400, f"Invalid site_id: {data['site_id']}")
        updates["site_id"] = data["site_id"]
    if "device_types" in data:
        updates["device_types"] = json.dumps(data["device_types"]) if data["device_types"] else None

    db.update_config_template(template_id, **updates)
    return {"success": True}


@app.delete("/api/config-templates/{template_id}", tags=["config"])
async def delete_config_template_api(template_id: int, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.CONFIG_TEMPLATES))):
    """Delete a config template."""
    existing = db.get_config_template(template_id)
    if not existing:
        raise HTTPException(404, "Template not found")
    db.delete_config_template(template_id)
    return {"success": True}


# ============================================================================
# Config Compliance
# ============================================================================

@app.get("/api/config-compliance", tags=["config"])
async def get_config_compliance(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_COMPLIANCE))):
    """Get per-device config compliance status using scoped templates."""
    all_configs = db.get_all_latest_configs()
    effective = db.get_all_effective_templates()

    # For devices with configs but not in access_points/switches (e.g. CPEs),
    # fall back to global templates
    global_templates = None

    devices = {}
    for ip, cfg in all_configs.items():
        config_data = json.loads(cfg["config_json"]) if isinstance(cfg["config_json"], str) else cfg["config_json"]
        device_templates = effective.get(ip)
        if device_templates is None:
            # Device not in AP/switch tables — use global templates
            if global_templates is None:
                global_templates = db.get_config_templates_for_device(ip, site_id=None)
            device_templates = global_templates
        compliant = _check_config_compliance(config_data, device_templates)
        devices[ip] = {
            "compliant": compliant,
            "checked_at": cfg["fetched_at"],
        }

    return {"devices": devices}


# ============================================================================
# Config Enforce Log
# ============================================================================

@app.get("/api/config-enforce/status", tags=["config"])
async def get_config_enforce_status(session: dict = Depends(require_auth)):
    """Get current auto-enforce status and recent log entries."""
    poller = get_poller()
    running = poller._enforce_running if poller else False
    enabled = db.get_setting("config_auto_enforce", "false") == "true"
    failures = db.get_enforce_failures(since_hours=24)
    recent = db.get_config_enforce_log(limit=10)
    return {
        "enabled": enabled,
        "running": running,
        "failure_count": len(failures),
        "recent": recent,
    }


@app.get("/api/config-enforce/log", tags=["config"])
async def get_config_enforce_log_api(
    ip: str = None,
    limit: int = 50,
    session: dict = Depends(require_auth),
):
    """Get config enforcement log entries."""
    entries = db.get_config_enforce_log(ip=ip, limit=limit)
    return {"entries": entries}


def _strip_empty_prefill_value(value):
    """Remove empty/default-like leaves from nested dict/list values."""
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            normalized = _strip_empty_prefill_value(child)
            if normalized is not None:
                cleaned[key] = normalized
        return cleaned or None
    if isinstance(value, list):
        cleaned = []
        for child in value:
            normalized = _strip_empty_prefill_value(child)
            if normalized is not None:
                cleaned.append(normalized)
        return cleaned or None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if value is None or value is False:
        return None
    return value


def _normalize_prefill_section(category: str, section):
    """Return a normalized non-default section for prefill matching."""
    if category == "users":
        if not isinstance(section, list):
            return None
        users = []
        for user in section:
            if not isinstance(user, dict):
                continue
            username = str(user.get("username") or "").strip()
            if not username:
                continue
            normalized_user = {"username": username}
            if "level" in user:
                normalized_user["level"] = user["level"]
            users.append(normalized_user)
        return users or None

    if not isinstance(section, dict):
        return None

    if category == "ntp":
        servers_raw = section.get("servers", [])
        if not isinstance(servers_raw, list):
            servers_raw = [servers_raw]
        servers = [s.strip() for s in servers_raw if isinstance(s, str) and s.strip()]
        enabled = section.get("enabled", True)
        if enabled is True and not servers:
            return None
        normalized = {"enabled": bool(enabled)}
        if servers:
            normalized["servers"] = servers
        return normalized

    if category == "radius":
        method = str(section.get("method", "local")).strip().lower() or "local"
        radius = section.get("radius")
        radius = radius if isinstance(radius, dict) else {}
        normalized_radius = {}
        for key, value in radius.items():
            normalized = _strip_empty_prefill_value(value)
            if normalized is not None:
                normalized_radius[key] = normalized
        if method == "local" and not normalized_radius:
            return None
        normalized = {"method": method}
        if normalized_radius:
            normalized["radius"] = normalized_radius
        return normalized

    return _strip_empty_prefill_value(section)


@app.get("/api/config-prefill/{category}", tags=["config"])
async def get_config_prefill(category: str, session: dict = Depends(require_auth)):
    """Get pre-fill data for a config category by analyzing fleet configs.

    Only returns data if no saved template exists for this category.
    """
    existing = db.get_config_template_by_category(category, scope="global")
    if existing:
        return {"prefilled": False, "reason": "template_exists"}

    all_configs = db.get_all_latest_configs()
    if not all_configs:
        return {"prefilled": False, "reason": "no_configs"}

    # Extract the relevant section from each device config
    section_map = {
        "snmp": ["services", "snmp"],
        "ntp": ["services", "ntp"],
        "radius": ["system", "auth"],
        "users": ["system", "users"],
        "syslog": ["services", "syslog"],
        "watchdog": ["services", "watchdog"],
    }

    path = section_map.get(category)
    if not path:
        return {"prefilled": False, "reason": "unknown_category"}

    # Collect values from all devices
    values = []
    for ip, cfg in all_configs.items():
        config_data = json.loads(cfg["config_json"]) if isinstance(cfg["config_json"], str) else cfg["config_json"]
        section = config_data
        for key in path:
            section = section.get(key, {}) if isinstance(section, dict) else {}
        normalized = _normalize_prefill_section(category, section)
        if normalized is not None:
            values.append(normalized)

    if not values:
        return {"prefilled": False, "reason": "no_non_default_data"}

    # First-run suggestion rule: same non-default value on >2 devices.
    canonical = [json.dumps(v, sort_keys=True, separators=(",", ":")) for v in values]
    from collections import Counter
    counts = Counter(canonical)
    most_common, count = counts.most_common(1)[0]

    if count > 2:
        return {
            "prefilled": True,
            "data": json.loads(most_common),
            "device_count": len(values),
            "match_count": count,
        }

    return {
        "prefilled": False,
        "reason": "insufficient_matches",
        "required_matches": 3,
        "unique_values": len(counts),
        "device_count": len(values),
        "match_count": count,
    }


# ============================================================================
# Config Push (Mass Operations)
# ============================================================================

@app.post("/api/config-push", tags=["config"])
async def push_config_templates(request: Request, session: dict = Depends(require_role("admin", "operator")), _pro=Depends(require_feature(Feature.CONFIG_PUSH))):
    """Push config template(s) to devices.

    Body: {
        "template_ids": [1, 2],
        "targets": [
            {"type": "ap", "ip": "10.0.0.1"},
            {"type": "site", "id": 5},
            {"type": "cpe", "ip": "10.0.0.50"}
        ]
    }
    """
    data = await request.json()
    template_ids = data.get("template_ids", [])
    targets = data.get("targets", [])

    if not template_ids or not targets:
        raise HTTPException(400, "template_ids and targets are required")

    # Load templates
    templates = []
    for tid in template_ids:
        t = db.get_config_template(tid)
        if not t:
            raise HTTPException(404, f"Template {tid} not found")
        fragment = json.loads(t["config_fragment"]) if isinstance(t["config_fragment"], str) else t["config_fragment"]
        # Safety net: re-validate even though creation should have caught this
        try:
            _validate_fragment_safety(fragment)
        except ValueError as e:
            raise HTTPException(400, f"Template '{t['name']}' contains unsafe keys: {e}")
        templates.append({"id": t["id"], "name": t["name"], "fragment": fragment})

    # Resolve targets to device IPs with credentials
    device_list = []  # [{ip, username, password, role, model}]
    seen_ips = set()

    for target in targets:
        target_type = target.get("type")
        if target_type == "ap":
            ap = db.get_access_point(target["ip"])
            if ap and ap["ip"] not in seen_ips:
                device_list.append({"ip": ap["ip"], "username": ap["username"], "password": ap["password"], "role": "ap", "model": ap.get("model")})
                seen_ips.add(ap["ip"])
        elif target_type == "switch":
            sw = db.get_switch(target["ip"])
            if sw and sw["ip"] not in seen_ips:
                device_list.append({"ip": sw["ip"], "username": sw["username"], "password": sw["password"], "role": "switch", "model": sw.get("model")})
                seen_ips.add(sw["ip"])
        elif target_type == "cpe":
            cpe = db.get_cpe_by_ip(target["ip"])
            if cpe:
                ap = db.get_access_point(cpe["ap_ip"])
                if ap and cpe["ip"] not in seen_ips:
                    device_list.append({"ip": cpe["ip"], "username": ap["username"], "password": ap["password"], "role": "cpe", "model": cpe.get("model")})
                    seen_ips.add(cpe["ip"])
        elif target_type == "site":
            site_id = target.get("id")
            # Get all APs and switches in this site
            for ap in db.get_access_points(tower_site_id=site_id):
                if ap["ip"] not in seen_ips:
                    device_list.append({"ip": ap["ip"], "username": ap["username"], "password": ap["password"], "role": "ap", "model": ap.get("model")})
                    seen_ips.add(ap["ip"])
                    # Also include CPEs for this AP
                    for cpe in db.get_cpes_for_ap(ap["ip"]):
                        if cpe["ip"] and cpe["ip"] not in seen_ips and cpe.get("auth_status") == "ok":
                            device_list.append({"ip": cpe["ip"], "username": ap["username"], "password": ap["password"], "role": "cpe", "model": cpe.get("model")})
                            seen_ips.add(cpe["ip"])
            for sw in db.get_switches(tower_site_id=site_id):
                if sw["ip"] not in seen_ips:
                    device_list.append({"ip": sw["ip"], "username": sw["username"], "password": sw["password"], "role": "switch", "model": sw.get("model")})
                    seen_ips.add(sw["ip"])

    if not device_list:
        raise HTTPException(400, "No valid devices found for the given targets")

    # Run config push in background
    job_id = str(uuid.uuid4())[:8]
    template_names = ", ".join(t["name"] for t in templates)
    asyncio.create_task(_run_config_push(job_id, device_list, templates))

    return {
        "job_id": job_id,
        "device_count": len(device_list),
        "template_names": template_names,
    }


async def _run_config_push(job_id: str, device_list: list, templates: list):
    """Execute config push to devices concurrently."""
    sem = asyncio.Semaphore(5)
    success_count = 0
    failed_count = 0
    template_names = ", ".join(t["name"] for t in templates)

    async def push_to_device(device: dict):
        nonlocal success_count, failed_count
        ip = device["ip"]
        started_at = datetime.now().isoformat()

        # Acquire push lock for this IP
        async with _config_push_lock:
            if ip in _config_pushing_ips:
                failed_count += 1
                await broadcast({"type": "config_push_update", "job_id": job_id, "ip": ip, "status": "failed", "error": "Push already in progress for this device"})
                return
            _config_pushing_ips.add(ip)

        try:
            async with sem:
                try:
                    await broadcast({"type": "config_push_update", "job_id": job_id, "ip": ip, "status": "connecting"})

                    client = TachyonClient(ip, device["username"], device["password"])
                    login_result = await client.login()
                    if login_result is not True:
                        raise RuntimeError(f"Login failed: {login_result}")

                    await broadcast({"type": "config_push_update", "job_id": job_id, "ip": ip, "status": "fetching_config"})

                    current_config = await client.get_config()
                    if current_config is None:
                        raise RuntimeError("Failed to fetch current config")

                    # Safety: save pre-push config snapshot so we have a "before" backup
                    pre_push_json = _canonical_config_json(current_config)
                    pre_push_hash = _compute_config_hash(current_config)
                    model = device.get("model")
                    hardware_id = client.get_hardware_id(model)
                    db.save_device_config(ip, pre_push_json, pre_push_hash, model, hardware_id)

                    # Merge all templates into current config
                    merged = current_config
                    for t in templates:
                        merged = deep_merge(merged, t["fragment"])

                    # Safety: dry_run first to validate the merged config
                    await broadcast({"type": "config_push_update", "job_id": job_id, "ip": ip, "status": "validating"})
                    dry_result = await client.apply_config(merged, dry_run=True)
                    if not dry_result.get("success"):
                        error_msg = dry_result.get("error", dry_result.get("raw_response", "Dry run validation failed"))
                        raise RuntimeError(f"Dry run rejected: {error_msg}")

                    await broadcast({"type": "config_push_update", "job_id": job_id, "ip": ip, "status": "applying"})

                    result = await client.apply_config(merged)
                    completed_at = datetime.now().isoformat()
                    duration = (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds()

                    if result.get("success"):
                        success_count += 1
                        db.save_device_update_history(
                            job_id=job_id, ip=ip, role=device["role"], pass_number=1,
                            status="success", old_version=None, new_version=None,
                            model=None, error=None, failed_stage=None,
                            stages=[], duration_seconds=duration,
                            started_at=started_at, completed_at=completed_at,
                            action="config_push",
                        )
                        await broadcast({"type": "config_push_update", "job_id": job_id, "ip": ip, "status": "success"})
                    else:
                        failed_count += 1
                        error_msg = result.get("error", result.get("raw_response", "Unknown error"))
                        db.save_device_update_history(
                            job_id=job_id, ip=ip, role=device["role"], pass_number=1,
                            status="failed", old_version=None, new_version=None,
                            model=None, error=str(error_msg), failed_stage="apply",
                            stages=[], duration_seconds=duration,
                            started_at=started_at, completed_at=completed_at,
                            action="config_push",
                        )
                        await broadcast({"type": "config_push_update", "job_id": job_id, "ip": ip, "status": "failed", "error": str(error_msg)})

                except Exception as e:
                    failed_count += 1
                    completed_at = datetime.now().isoformat()
                    duration = (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds()
                    db.save_device_update_history(
                        job_id=job_id, ip=ip, role=device["role"], pass_number=1,
                        status="failed", old_version=None, new_version=None,
                        model=None, error=str(e), failed_stage="connect",
                        stages=[], duration_seconds=duration,
                        started_at=started_at, completed_at=completed_at,
                        action="config_push",
                    )
                    await broadcast({"type": "config_push_update", "job_id": job_id, "ip": ip, "status": "failed", "error": str(e)})
        finally:
            _config_pushing_ips.discard(ip)

    await asyncio.gather(*[push_to_device(d) for d in device_list])

    await broadcast({
        "type": "config_push_complete",
        "job_id": job_id,
        "success_count": success_count,
        "failed_count": failed_count,
        "template_names": template_names,
    })

    # Re-poll configs for affected devices after push
    poller = get_poller()
    if poller:
        affected_ips = [d["ip"] for d in device_list]
        asyncio.create_task(poller.poll_configs_for_ips(affected_ips))


# ============================================================================
# RADIUS Server Management
# ============================================================================

@app.get("/api/radius-server/config", tags=["auth"])
async def get_radius_server_config_api(
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Get RADIUS server configuration (secrets omitted)."""
    config = get_radius_server_config()
    svc = get_radius_service()
    return {
        "enabled": config.enabled,
        "auth_port": config.auth_port,
        "has_secret": bool(config.shared_secret),
        "auth_mode": config.auth_mode,
        "advertised_address": config.advertised_address,
        "ldap_url": config.ldap_url,
        "ldap_bind_dn": config.ldap_bind_dn,
        "ldap_has_password": bool(config.ldap_bind_password),
        "ldap_base_dn": config.ldap_base_dn,
        "ldap_user_filter": config.ldap_user_filter,
        "running": svc.is_running if svc else False,
        "error": svc.last_error if svc else "",
    }


@app.put("/api/radius-server/config", tags=["auth"])
async def update_radius_server_config_api(
    request: Request,
    session: dict = Depends(require_role("admin")),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Update RADIUS server configuration."""
    data = await request.json()
    config = get_radius_server_config()

    if "enabled" in data:
        config.enabled = bool(data["enabled"])
    if "auth_port" in data:
        port = int(data["auth_port"])
        if not (1024 <= port <= 65535):
            raise HTTPException(400, "Port must be 1024-65535")
        config.auth_port = port
    if "shared_secret" in data and data["shared_secret"]:
        secret = data["shared_secret"]
        if len(secret) < 8:
            raise HTTPException(400, "Shared secret must be at least 8 characters")
        if len(set(secret)) == 1:
            raise HTTPException(400, "Shared secret is too weak")
        config.shared_secret = secret
    if "auth_mode" in data:
        if data["auth_mode"] not in ("local", "ldap"):
            raise HTTPException(400, "auth_mode must be 'local' or 'ldap'")
        config.auth_mode = data["auth_mode"]
    if "advertised_address" in data:
        config.advertised_address = data["advertised_address"].strip()
    if "ldap_url" in data:
        url = data["ldap_url"].strip()
        if url and not (url.startswith("ldaps://") or url.startswith("ldap://")):
            raise HTTPException(400, "LDAP URL must start with ldaps:// or ldap://")
        config.ldap_url = url
    if "ldap_bind_dn" in data:
        config.ldap_bind_dn = data["ldap_bind_dn"].strip()
    if "ldap_bind_password" in data and data["ldap_bind_password"]:
        config.ldap_bind_password = data["ldap_bind_password"]
    if "ldap_base_dn" in data:
        config.ldap_base_dn = data["ldap_base_dn"].strip()
    if "ldap_user_filter" in data:
        config.ldap_user_filter = data["ldap_user_filter"].strip()

    set_radius_server_config(config)

    # Restart service so new config applies immediately, even if currently idle.
    svc = get_radius_service()
    if svc:
        await svc.restart()

    return {"status": "ok"}


@app.post("/api/radius-server/restart", tags=["auth"])
async def restart_radius_server_api(
    session: dict = Depends(require_role("admin")),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Restart the RADIUS server."""
    svc = get_radius_service()
    if not svc:
        raise HTTPException(500, "RADIUS service not initialized")
    await svc.restart()
    return {"status": "ok"}


@app.get("/api/radius-server/status", tags=["auth"])
async def get_radius_server_status_api(
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Get RADIUS server running status and stats."""
    svc = get_radius_service()
    if not svc:
        return {"running": False, "error": "Not initialized", "stats": {}}
    return svc.get_status()


@app.get("/api/radius-server/users", tags=["auth"])
async def list_radius_users_api(
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """List all RADIUS users."""
    return radius_users.get_radius_users()


@app.post("/api/radius-server/users", tags=["auth"])
async def create_radius_user_api(
    request: Request,
    session: dict = Depends(require_role("admin", "operator")),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Create a RADIUS user."""
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    description = data.get("description", "")
    try:
        user_id = radius_users.create_radius_user(username, password, description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(409, f"Username '{username}' already exists")
        raise
    return {"id": user_id, "username": username}


@app.put("/api/radius-server/users/{user_id}", tags=["auth"])
async def update_radius_user_api(
    user_id: int,
    request: Request,
    session: dict = Depends(require_role("admin", "operator")),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Update a RADIUS user."""
    data = await request.json()
    try:
        updated = radius_users.update_radius_user(user_id, **data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(409, "Username already exists")
        raise
    if not updated:
        raise HTTPException(404, "User not found")
    return {"status": "ok"}


@app.delete("/api/radius-server/users/{user_id}", tags=["auth"])
async def delete_radius_user_api(
    user_id: int,
    session: dict = Depends(require_role("admin", "operator")),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Delete a RADIUS user."""
    if not radius_users.delete_radius_user(user_id):
        raise HTTPException(404, "User not found")
    return {"status": "ok"}


@app.get("/api/radius-server/auth-log", tags=["auth"])
async def get_radius_auth_log_api(
    limit: int = 50,
    offset: int = 0,
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Get recent RADIUS authentication attempts."""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM radius_auth_log ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
            (min(limit, 200), offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM radius_auth_log").fetchone()[0]
    return {"entries": [dict(r) for r in rows], "total": total}


@app.post("/api/radius-server/test-ldap", tags=["auth"])
async def test_ldap_connection_api(
    request: Request,
    session: dict = Depends(require_role("admin")),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Test LDAP connectivity with current configuration."""
    data = await request.json()
    test_username = data.get("test_username", "testuser")

    config = get_radius_server_config()
    # Override with any provided fields for testing
    if data.get("ldap_url"):
        config.ldap_url = data["ldap_url"]
    if data.get("ldap_bind_dn"):
        config.ldap_bind_dn = data["ldap_bind_dn"]
    if data.get("ldap_bind_password"):
        config.ldap_bind_password = data["ldap_bind_password"]
    if data.get("ldap_base_dn"):
        config.ldap_base_dn = data["ldap_base_dn"]

    try:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        return {"success": False, "error": "ldap3 library not installed"}

    if not config.ldap_url:
        return {"success": False, "error": "LDAP URL not configured"}

    try:
        url = config.ldap_url.strip()
        if url.startswith("ldaps://"):
            srv = ldap3.Server(url, use_ssl=True, connect_timeout=10)
        else:
            tls = ldap3.Tls(validate=2)
            srv = ldap3.Server(url, use_ssl=False, tls=tls, connect_timeout=10)

        conn = ldap3.Connection(
            srv, user=config.ldap_bind_dn,
            password=config.ldap_bind_password,
            auto_bind=True, receive_timeout=5,
        )
        if url.startswith("ldap://"):
            conn.start_tls()

        # Search for test user
        safe_username = escape_filter_chars(test_username)
        search_filter = config.ldap_user_filter.replace("{username}", safe_username)
        conn.search(config.ldap_base_dn, search_filter, attributes=["dn"])
        found = len(conn.entries)
        conn.unbind()

        return {
            "success": True,
            "message": f"LDAP connection successful. Search for '{test_username}' found {found} result(s).",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/radius-server/push-to-devices", tags=["auth"])
async def push_radius_to_devices_api(
    request: Request,
    session: dict = Depends(require_role("admin", "operator")),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Push this RADIUS server's config to managed devices."""
    config = get_radius_server_config()
    if not config.advertised_address:
        raise HTTPException(400, "Advertised address must be configured before pushing to devices")
    if not config.shared_secret:
        raise HTTPException(400, "Shared secret must be configured before pushing to devices")

    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    target_ips = data.get("target_ips")  # None = all devices

    # Build RADIUS config fragment for devices
    config_fragment = {
        "system": {
            "auth": {
                "method": "radius",
                "radius": {
                    "auth_server1": config.advertised_address,
                    "auth_port": config.auth_port,
                    "auth_secret": config.shared_secret,
                },
            }
        }
    }

    # Get target devices
    with db.get_db() as conn:
        if target_ips:
            placeholders = ",".join("?" for _ in target_ips)
            devices = conn.execute(
                f"SELECT ip, username, password FROM access_points WHERE enabled = 1 AND ip IN ({placeholders})",
                target_ips,
            ).fetchall()
            devices += conn.execute(
                f"SELECT ip, username, password FROM switches WHERE enabled = 1 AND ip IN ({placeholders})",
                target_ips,
            ).fetchall()
        else:
            devices = conn.execute(
                "SELECT ip, username, password FROM access_points WHERE enabled = 1"
            ).fetchall()
            devices += conn.execute(
                "SELECT ip, username, password FROM switches WHERE enabled = 1"
            ).fetchall()

    if not devices:
        raise HTTPException(404, "No target devices found")

    device_list = []
    for d in devices:
        d = dict(d)
        # Decrypt device password if encrypted
        from .crypto import is_encrypted as _is_enc, decrypt_password as _dec_pw
        if d.get("password") and _is_enc(d["password"]):
            d["password"] = _dec_pw(d["password"])
        device_list.append(d)

    # Use the existing config push infrastructure
    job_id = f"radius-push-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    asyncio.create_task(
        _run_radius_push(job_id, device_list, config_fragment)
    )

    return {
        "job_id": job_id,
        "device_count": len(device_list),
        "message": f"Pushing RADIUS config to {len(device_list)} device(s)",
    }


async def _run_radius_push(job_id: str, device_list: list[dict], config_fragment: dict):
    """Push RADIUS config to devices using TachyonClient."""
    from .tachyon import TachyonClient

    success_count = 0
    failed_count = 0

    for d in device_list:
        ip = d["ip"]
        try:
            client = TachyonClient(ip, d.get("username", "admin"), d.get("password", ""))
            login_result = await client.login()
            if login_result is not True:
                raise Exception(f"Login failed: {login_result}")
            result = await client.apply_config(config_fragment)
            success_count += 1
            await broadcast({
                "type": "config_push_update", "job_id": job_id,
                "ip": ip, "status": "success",
            })
        except Exception as e:
            failed_count += 1
            logger.warning("RADIUS config push failed for %s: %s", ip, e)
            await broadcast({
                "type": "config_push_update", "job_id": job_id,
                "ip": ip, "status": "failed", "error": str(e),
            })

    await broadcast({
        "type": "config_push_complete", "job_id": job_id,
        "success_count": success_count, "failed_count": failed_count,
        "template_names": ["RADIUS Server"],
    })
_radius_rollout_task: Optional[asyncio.Task] = None
_radius_rollout_lock = asyncio.Lock()


def _radius_rollout_targets() -> list[dict]:
    devices = []
    seen_ips = set()
    for ap in db.get_access_points(enabled_only=True):
        if ap["ip"] not in seen_ips:
            devices.append({
                "ip": ap["ip"],
                "role": "ap",
                "username": ap["username"],
                "password": ap["password"],
            })
            seen_ips.add(ap["ip"])
        for cpe in db.get_cpes_for_ap(ap["ip"]):
            cpe_ip = cpe.get("ip")
            if not cpe_ip or cpe.get("auth_status") != "ok" or cpe_ip in seen_ips:
                continue
            devices.append({
                "ip": cpe_ip,
                "role": "cpe",
                "username": ap["username"],
                "password": ap["password"],
                "parent_ap_ip": ap["ip"],
            })
            seen_ips.add(cpe_ip)
    for switch in db.get_switches(enabled_only=True):
        if switch["ip"] in seen_ips:
            continue
        devices.append({
            "ip": switch["ip"],
            "role": "switch",
            "username": switch["username"],
            "password": switch["password"],
        })
        seen_ips.add(switch["ip"])
    devices.sort(key=lambda item: ipaddress.ip_address(item["ip"]))
    return devices


def _resolve_radius_rollout_device(ip: str, role: str) -> Optional[dict]:
    if role == "ap":
        ap = db.get_access_point(ip)
        if not ap:
            return None
        return {
            "ip": ap["ip"],
            "role": "ap",
            "username": ap["username"],
            "password": ap["password"],
        }
    if role == "switch":
        switch = db.get_switch(ip)
        if not switch:
            return None
        return {
            "ip": switch["ip"],
            "role": "switch",
            "username": switch["username"],
            "password": switch["password"],
        }
    if role == "cpe":
        cpe = db.get_cpe_by_ip(ip)
        if not cpe:
            return None
        parent_ap = db.get_access_point(cpe["ap_ip"])
        if not parent_ap:
            return None
        return {
            "ip": cpe["ip"],
            "role": "cpe",
            "username": parent_ap["username"],
            "password": parent_ap["password"],
            "parent_ap_ip": parent_ap["ip"],
        }
    return None


def _get_radius_rollout_template() -> dict:
    template = db.get_config_template_by_category("radius")
    if not template or not template.get("enabled"):
        raise ValueError("Save and enable a Radius config template before starting rollout")

    form_data = template.get("form_data")
    if isinstance(form_data, str) and form_data:
        form_data = json.loads(form_data)
    form_data = form_data or {}

    fragment = template.get("config_fragment")
    if isinstance(fragment, str):
        fragment = json.loads(fragment)

    if form_data.get("method") != "radius":
        raise ValueError("Radius rollout requires the saved Radius config template to use method=radius")
    if not form_data.get("server") or not form_data.get("secret"):
        raise ValueError("Radius rollout requires a saved server and shared secret in the Radius config template")

    return {
        "id": template["id"],
        "name": template["name"],
        "fragment": fragment,
        "form_data": form_data,
    }


def _validate_radius_rollout_template(template: dict, config: builtin_radius.BuiltinRadiusConfig):
    form_data = template.get("form_data") or {}
    if not config.host:
        raise ValueError("Set the built-in Radius device host before starting rollout")
    if (form_data.get("server") or "").strip() != config.host:
        raise ValueError("Saved Radius config template server does not match the built-in Radius device host")
    try:
        template_port = int(form_data.get("port", config.port) or config.port)
    except (TypeError, ValueError):
        raise ValueError("Saved Radius config template port is invalid")
    if template_port != config.port:
        raise ValueError("Saved Radius config template port does not match the built-in Radius port")
    if (form_data.get("secret") or "") != config.secret:
        raise ValueError("Saved Radius config template secret does not match the built-in Radius secret")


def _apply_builtin_radius_settings_to_fragment(fragment: dict, config: builtin_radius.BuiltinRadiusConfig) -> dict:
    system = fragment.setdefault("system", {})
    auth = system.setdefault("auth", {})
    radius = auth.setdefault("radius", {})
    auth["method"] = "radius"
    radius["auth_server1"] = config.host
    radius["auth_port"] = config.port
    radius["auth_secret"] = config.secret
    return fragment


def _radius_rollout_batch_size(phase: str, candidate_count: int) -> int:
    if phase == "canary":
        return 1
    if phase == "pct10":
        return max(1, math.ceil(candidate_count * 0.1))
    if phase == "pct50":
        return max(1, math.ceil(candidate_count * 0.5))
    return candidate_count


def _resolve_radius_rollout_phase_devices(rollout: dict, devices: list[dict]) -> list[dict]:
    existing = builtin_radius.get_rollout_devices(rollout["id"])
    existing_by_ip = {row["ip"]: row for row in existing}
    current_by_ip = {device["ip"]: device for device in devices}

    current_phase_rows = [
        row for row in existing
        if row["phase_assigned"] == rollout["phase"] and row["status"] in ("pending", "failed")
    ]
    if current_phase_rows:
        resolved = []
        for row in current_phase_rows:
            device = current_by_ip.get(row["ip"]) or _resolve_radius_rollout_device(row["ip"], row["device_type"])
            if device:
                resolved.append(device)
            else:
                builtin_radius.mark_rollout_device(
                    rollout["id"],
                    row["ip"],
                    "skipped",
                    "Device missing from inventory",
                )
        return resolved

    unassigned = [device for device in devices if device["ip"] not in existing_by_ip]
    if not unassigned:
        return []

    batch_size = _radius_rollout_batch_size(rollout["phase"], len(unassigned))
    batch = unassigned[:batch_size]
    for device in batch:
        builtin_radius.assign_device_to_rollout(rollout["id"], device["ip"], device["role"], rollout["phase"])
    return batch


def _serialize_radius_rollout_devices(rollout_id: int) -> list[dict]:
    rows = builtin_radius.get_rollout_devices(rollout_id)
    serialized = []
    for row in rows:
        entry = dict(row)
        if entry.get("device_type") == "cpe":
            cpe = db.get_cpe_by_ip(entry["ip"])
            if cpe:
                entry["parent_ap_ip"] = cpe.get("ap_ip")
                entry["repair_target_ip"] = cpe.get("ap_ip")
        elif entry.get("device_type") == "ap":
            entry["repair_target_ip"] = entry["ip"]
        serialized.append(entry)
    return serialized


async def _refresh_radius_rollout_inventory():
    poller = get_poller()
    if not poller:
        raise ValueError("Poller not initialized")

    failures = []
    for ap in db.get_access_points(enabled_only=True):
        ok = await poller.poll_ap_now(ap["ip"])
        if ok:
            continue
        refreshed = db.get_access_point(ap["ip"]) or {}
        failures.append(f"{ap['ip']} ({refreshed.get('last_error') or 'Immediate reprobe failed'})")

    if failures:
        raise ValueError(
            "Radius rollout preflight failed for APs: "
            + ", ".join(failures)
            + ". Fix AP credentials or connectivity before starting rollout."
        )


async def _push_radius_to_device(rollout_id: int, device: dict, fragment: dict, service_username: str, service_password: str) -> tuple[bool, str]:
    ip = device["ip"]
    builtin_radius.mark_rollout_device(rollout_id, ip, "pending")
    try:
        client = TachyonClient(ip, device["username"], device["password"])
        login_result = await client.login()
        if login_result is not True:
            if device["role"] == "cpe" and device.get("parent_ap_ip"):
                raise RuntimeError(
                    f"Inherited AP credentials from {device['parent_ap_ip']} failed. "
                    "Update the AP credentials inline and resume rollout."
                )
            raise RuntimeError(f"Manual credential login failed: {login_result}")

        current_config = await client.get_config()
        if current_config is None:
            raise RuntimeError("Failed to fetch current config")

        merged = deep_merge(current_config, fragment)
        dry_result = await client.apply_config(merged, dry_run=True)
        if not dry_result.get("success"):
            error_msg = dry_result.get("error", dry_result.get("raw_response", "Dry run validation failed"))
            raise RuntimeError(f"Dry run rejected: {error_msg}")

        apply_result = await client.apply_config(merged)
        if not apply_result.get("success"):
            error_msg = apply_result.get("error", apply_result.get("raw_response", "Config apply failed"))
            raise RuntimeError(str(error_msg))

        await asyncio.sleep(2)
        verify_client = TachyonClient(ip, service_username, service_password)
        verify_result = await verify_client.login()
        if verify_result is not True:
            raise RuntimeError(f"Radius verification failed: {verify_result}")

        if device["role"] in ("ap", "switch"):
            db.update_device_credentials(device["role"], ip, service_username, service_password)
        builtin_radius.mark_rollout_device(rollout_id, ip, "updated")
        return True, ""
    except Exception as exc:
        builtin_radius.mark_rollout_device(rollout_id, ip, "failed", str(exc))
        return False, str(exc)


def _broadcast_radius_rollout_state():
    rollout = builtin_radius.get_current_rollout()
    payload = {"type": "radius_rollout_status", "rollout": None}
    if rollout:
        payload["rollout"] = {
            **rollout,
            "progress": builtin_radius.get_rollout_progress(rollout["id"]),
            "devices": _serialize_radius_rollout_devices(rollout["id"]),
        }
    return broadcast(payload)


async def _run_radius_rollout(rollout_id: int):
    async with _radius_rollout_lock:
        try:
            template = _get_radius_rollout_template()
            current_config = builtin_radius.get_config()
            _validate_radius_rollout_template(template, current_config)
            template["fragment"] = _apply_builtin_radius_settings_to_fragment(template["fragment"], current_config)
            service_username, service_password = builtin_radius.get_management_service_credentials(create_if_missing=True)

            while True:
                rollout = builtin_radius.get_rollout(rollout_id)
                if not rollout or rollout["status"] != "active":
                    await _broadcast_radius_rollout_state()
                    return

                devices = _radius_rollout_targets()
                phase_devices = _resolve_radius_rollout_phase_devices(rollout, devices)
                if not phase_devices:
                    builtin_radius.complete_rollout_phase(rollout_id)
                    refreshed = builtin_radius.get_rollout(rollout_id)
                    await _broadcast_radius_rollout_state()
                    if not refreshed or refreshed["status"] == "completed":
                        return
                    continue

                results = await asyncio.gather(*[
                    _push_radius_to_device(
                        rollout_id,
                        device,
                        template["fragment"],
                        service_username,
                        service_password,
                    )
                    for device in phase_devices
                ])
                failures = [error for ok, error in results if not ok]
                if failures:
                    builtin_radius.update_rollout_status(
                        rollout_id,
                        "paused",
                        f"{len(failures)} device(s) failed during {rollout['phase']} phase",
                    )
                    await _broadcast_radius_rollout_state()
                    return

                builtin_radius.complete_rollout_phase(rollout_id)
                await _broadcast_radius_rollout_state()
                refreshed = builtin_radius.get_rollout(rollout_id)
                if not refreshed or refreshed["status"] == "completed":
                    return
        except Exception as exc:
            logger.exception("Radius rollout %s failed", rollout_id)
            builtin_radius.update_rollout_status(rollout_id, "paused", str(exc))
            await _broadcast_radius_rollout_state()


def _start_radius_rollout_task(rollout_id: int):
    global _radius_rollout_task
    if _radius_rollout_task and not _radius_rollout_task.done():
        return
    _radius_rollout_task = asyncio.create_task(_run_radius_rollout(rollout_id))


def main():
    """Run the application."""
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
