"""Tachyon Management System - Web Application."""

import asyncio
import html as html_module
import ipaddress
import json
import logging
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
from .auth import require_auth, require_auth_ws, authenticate, create_session, SESSION_COOKIE_NAME, is_setup_required, is_first_run, complete_setup, is_request_secure
from .backup import build_csv_export, process_csv_import
from . import telemetry
from . import slack
from . import ssl_manager
from . import git_backup
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

            logger.info("Running scheduled backup")
            success, msg = await git_backup.run_backup()
            if success:
                logger.info(f"Scheduled backup completed: {msg}")
            else:
                logger.error(f"Scheduled backup failed: {msg}")
        except Exception as e:
            logger.error(f"Backup scheduler error: {e}")


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
    db.cleanup_expired_sessions()
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
    state = get_license_state()
    logger.info(f"License: {state.status.value} (tier={state.tier.value})")
    logger.info("Application started")

    yield

    # Shutdown
    backup_task.cancel()
    cleanup_task.cancel()
    radius_task.cancel()
    await radius_svc.stop()
    for task in [cleanup_task, backup_task, radius_task]:
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
    logger.info("Application stopped")


# FastAPI app
app = FastAPI(title="Unofficial Tachyon Networks Auto Updater", lifespan=lifespan)
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


_login_attempts: Dict[str, list] = {}  # IP -> list of timestamps
LOGIN_RATE_LIMIT = 5       # max attempts
LOGIN_RATE_WINDOW = 60     # per N seconds


def _check_login_rate_limit(ip: str) -> bool:
    """Return True if the IP is rate-limited."""
    now = datetime.now()
    cutoff = now.timestamp() - LOGIN_RATE_WINDOW
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if t > cutoff]
    _login_attempts[ip] = attempts
    return len(attempts) >= LOGIN_RATE_LIMIT


def _record_login_attempt(ip: str):
    """Record a login attempt for rate limiting."""
    _login_attempts.setdefault(ip, []).append(datetime.now().timestamp())


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Handle login form submission."""
    ip_address = request.client.host if request.client else "unknown"
    if _check_login_rate_limit(ip_address):
        return render_template(request, "login.html", {
            "error": "Too many login attempts. Please wait and try again.",
        }, status_code=429)

    user = authenticate(username, password)
    if not user:
        _record_login_attempt(ip_address)
        return render_template(request, "login.html", {"error": True}, status_code=401)

    ip_address = request.client.host if request.client else "unknown"
    session_id = create_session(user, ip_address)

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
        "backup_status": git_backup.get_backup_status(),
        "error": None, "success": None,
    })


@app.post("/setup-wizard")
async def setup_wizard_submit(
    request: Request, step: int = Form(...), action: str = Form(...),
    ssl_domain: str = Form(None), ssl_email: str = Form(None),
    backup_repo: str = Form(None), backup_auth: str = Form(None),
    backup_token: str = Form(None), backup_ssh_key: str = Form(None),
    session: dict = Depends(require_auth),
):
    """Handle setup wizard form submissions."""
    # Enforce sequential step progression
    if not _wizard_step_allowed(step):
        return RedirectResponse(url="/setup-wizard?step=1", status_code=303)

    ssl_status = ssl_manager.get_ssl_status()
    backup_status = git_backup.get_backup_status()

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
        if action == "configure" and backup_repo and backup_auth:
            ok, msg = await git_backup.init_backup_repo(
                repo_url=backup_repo, auth_method=backup_auth,
                ssh_key=backup_ssh_key, token=backup_token,
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
            "backup_status": git_backup.get_backup_status(),
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
    session: dict = Depends(require_auth),
):
    """Handle SSL certificate request."""
    success, message = await ssl_manager.obtain_certificate(domain, email)
    status = ssl_manager.get_ssl_status()
    return render_template(request, "ssl_setup.html", {
        "ssl_status": status,
        "error": None if success else message,
        "success": message if success else None,
    }, status_code=200 if success else 400)


@app.get("/api/ssl/status")
async def get_ssl_status_api(session: dict = Depends(require_auth)):
    return ssl_manager.get_ssl_status()


@app.get("/backup-setup", response_class=HTMLResponse)
async def backup_setup_page(request: Request, session: dict = Depends(require_auth)):
    """Serve the backup setup page."""
    return render_template(request, "backup_setup.html", {
        "backup_status": git_backup.get_backup_status(),
        "error": None, "success": None,
    })


@app.post("/backup-setup")
async def backup_setup_submit(
    request: Request, repo_url: str = Form(...), auth_method: str = Form(...),
    token: str = Form(None), ssh_key: str = Form(None),
    session: dict = Depends(require_auth),
):
    """Handle backup configuration."""
    success, message = await git_backup.init_backup_repo(
        repo_url=repo_url, auth_method=auth_method, ssh_key=ssh_key, token=token,
    )
    return render_template(request, "backup_setup.html", {
        "backup_status": git_backup.get_backup_status(),
        "error": None if success else message,
        "success": message if success else None,
    }, status_code=200 if success else 400)


@app.post("/backup-run")
async def backup_run_now(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Trigger an immediate backup."""
    await git_backup.run_backup()
    return RedirectResponse(url="/backup-setup", status_code=303)


@app.get("/api/backup/git-status")
async def get_git_backup_status_api(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    return git_backup.get_backup_status()


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

@app.get("/api/sites")
async def list_sites(session: dict = Depends(require_auth)):
    """List all tower sites."""
    sites = db.get_tower_sites()
    return {"sites": sites}


@app.post("/api/sites")
async def create_site(
    name: str = Form(...),
    location: str = Form(None),
    latitude: float = Form(None),
    longitude: float = Form(None),
    session: dict = Depends(require_auth),
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


@app.put("/api/sites/{site_id}")
async def update_site(
    site_id: int,
    name: str = Form(None),
    location: str = Form(None),
    latitude: float = Form(None),
    longitude: float = Form(None),
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.TOWER_SITES)),
):
    """Update a tower site."""
    db.update_tower_site(site_id, name=name, location=location, latitude=latitude, longitude=longitude)
    return {"success": True}


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: int, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.TOWER_SITES))):
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


@app.get("/api/aps")
async def list_aps(site_id: int = None, session: dict = Depends(require_auth)):
    """List access points (credentials redacted)."""
    aps = db.get_access_points(tower_site_id=site_id, enabled_only=False)
    return {"aps": _strip_credentials(aps)}


@app.post("/api/aps")
async def add_ap(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    tower_site_id: int = Form(None),
    session: dict = Depends(require_auth),
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


@app.post("/api/devices")
async def add_device(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    tower_site_id: int = Form(None),
    session: dict = Depends(require_auth),
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


@app.put("/api/aps/{ip}")
async def update_ap(
    ip: str,
    username: str = Form(None),
    password: str = Form(None),
    tower_site_id: int = Form(None),
    enabled: bool = Form(None),
    session: dict = Depends(require_auth),
):
    """Update an access point."""
    ap = db.get_access_point(ip)
    if not ap:
        raise HTTPException(404, f"AP not found: {ip}")

    # Use existing values if not provided
    new_username = username if username else ap["username"]
    new_password = password if password else ap["password"]
    new_site_id = tower_site_id if tower_site_id is not None else ap["tower_site_id"]

    db.upsert_access_point(ip, new_username, new_password, new_site_id, enabled=enabled)

    # Invalidate cached client if credentials changed
    if username or password:
        poller = get_poller()
        if poller:
            poller.invalidate_client(ip)

    return {"success": True}


@app.delete("/api/aps/{ip}")
async def delete_ap(ip: str, session: dict = Depends(require_auth)):
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


@app.post("/api/aps/{ip}/poll")
async def poll_ap(ip: str, session: dict = Depends(require_auth)):
    """Trigger immediate poll of an AP."""
    poller = get_poller()
    if not poller:
        raise HTTPException(500, "Poller not initialized")

    success = await poller.poll_ap_now(ip)
    if not success:
        raise HTTPException(404, f"AP not found: {ip}")

    return {"success": True}


# ============================================================================
# Switch API
# ============================================================================

@app.get("/api/switches")
async def list_switches(site_id: int = None, session: dict = Depends(require_auth)):
    """List switches (credentials redacted)."""
    switches = db.get_switches(tower_site_id=site_id, enabled_only=False)
    return {"switches": _strip_credentials(switches)}


@app.post("/api/switches")
async def add_switch(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    tower_site_id: int = Form(None),
    session: dict = Depends(require_auth),
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


@app.put("/api/switches/{ip}")
async def update_switch(
    ip: str,
    username: str = Form(None),
    password: str = Form(None),
    tower_site_id: int = Form(None),
    enabled: bool = Form(None),
    session: dict = Depends(require_auth),
):
    """Update a switch."""
    sw = db.get_switch(ip)
    if not sw:
        raise HTTPException(404, f"Switch not found: {ip}")

    new_username = username if username else sw["username"]
    new_password = password if password else sw["password"]
    new_site_id = tower_site_id if tower_site_id is not None else sw["tower_site_id"]

    db.upsert_switch(ip, new_username, new_password, new_site_id, enabled=enabled)

    if username or password:
        poller = get_poller()
        if poller:
            poller.invalidate_client(ip)

    return {"success": True}


@app.delete("/api/switches/{ip}")
async def delete_switch(ip: str, session: dict = Depends(require_auth)):
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


@app.post("/api/switches/{ip}/poll")
async def poll_switch(ip: str, session: dict = Depends(require_auth)):
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

@app.get("/api/topology")
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


@app.post("/api/topology/refresh")
async def refresh_topology(session: dict = Depends(require_auth)):
    """Trigger a full topology refresh."""
    poller = get_poller()
    if not poller:
        raise HTTPException(500, "Poller not initialized")

    await poller._poll_all_aps()
    return poller.get_topology()


@app.get("/api/cpes")
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


@app.get("/api/device-portal/{ip}", response_class=HTMLResponse)
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

@app.post("/api/quick-add")
async def quick_add(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    site_name: str = Form(None),
    session: dict = Depends(require_auth),
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


@app.get("/api/settings")
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
    "parallel_updates", "bank_mode", "allow_downgrade", "timezone", "zip_code",
    "weather_check_enabled", "min_temperature_c", "temperature_unit",
    "schedule_scope", "schedule_scope_data",
    "firmware_beta_enabled", "firmware_quarantine_days",
    "slack_webhook_url", "autoupdate_enabled", "release_channel",
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


def _validate_settings(filtered: dict):
    """Validate individual setting values before persisting."""
    url = filtered.get("slack_webhook_url")
    if url and not slack.is_valid_slack_url(url):
        raise HTTPException(400, "Slack webhook URL must be a valid https://hooks.slack.com/ URL")

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
    if filtered.get("firmware_beta_enabled") == "true" and not is_feature_enabled(Feature.BETA_FIRMWARE):
        raise HTTPException(403, detail={"error": "feature_locked", "feature": "beta_firmware",
                                         "message": "Beta firmware channel requires a Pro license."})
    if "firmware_quarantine_days" in filtered and not is_feature_enabled(Feature.FIRMWARE_HOLD_CUSTOM):
        current = db.get_setting("firmware_quarantine_days", "7")
        if filtered["firmware_quarantine_days"] != current:
            raise HTTPException(403, detail={"error": "feature_locked", "feature": "firmware_hold_custom",
                                             "message": "Custom firmware hold period requires a Pro license."})


@app.put("/api/settings")
async def update_settings(request: Request, session: dict = Depends(require_auth)):
    """Update settings. Only whitelisted keys are accepted."""
    data = await request.json()
    filtered = {k: v for k, v in data.items() if k in _SETTINGS_WRITABLE}
    if not filtered:
        raise HTTPException(400, "No valid settings keys provided")
    _validate_settings(filtered)
    db.set_settings(filtered)
    return {"success": True}


@app.post("/api/settings/save")
async def save_settings_and_reevaluate(request: Request, session: dict = Depends(require_auth)):
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


@app.post("/api/slack/test")
async def test_slack_webhook(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.SLACK_NOTIFICATIONS))):
    """Send a test notification to the configured Slack webhook."""
    success, message = await slack.send_test_notification()
    return {"success": success, "message": message}


# ============================================================================
# License API
# ============================================================================

@app.get("/api/license")
async def get_license_status(session: dict = Depends(require_auth)):
    """Get current license state, features map, and device counts."""
    state = get_license_state()
    nag = get_nag_info()
    features = {f.value: state.is_feature_enabled(f) for f in Feature}
    return {**state.to_dict(), **nag, "features": features}


@app.post("/api/license/activate")
async def activate_license(request: Request, session: dict = Depends(require_auth)):
    """Activate or update the license key."""
    data = await request.json()
    key = data.get("license_key", "").strip()
    if not key:
        raise HTTPException(400, "License key is required")
    from .license import LicenseStatus
    state = await validate_license(license_key=key)
    return {**state.to_dict(), "success": state.status == LicenseStatus.ACTIVE}


@app.post("/api/license/deactivate")
async def deactivate_license(session: dict = Depends(require_auth)):
    """Remove the license key and revert to free tier."""
    clear_license()
    return {"success": True, "status": "free"}


@app.post("/api/license/validate")
async def force_validate_license(session: dict = Depends(require_auth)):
    """Force re-validate the current license with the server."""
    state = get_license_state()
    if not state.license_key:
        raise HTTPException(400, "No license key configured")
    result = await validate_license()
    return result.to_dict()


# ============================================================================
# System / Appliance API
# ============================================================================

@app.get("/api/system/info")
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


@app.post("/api/system/network")
async def update_network_config(request: Request, session: dict = Depends(require_auth)):
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

@app.get("/api/updates")
async def get_update_status(session: dict = Depends(require_auth)):
    """Get current update status."""
    checker = get_checker()
    if checker:
        return checker.get_update_status()
    return {"error": "Release checker not initialized"}


@app.post("/api/updates/check")
async def check_for_updates(session: dict = Depends(require_auth)):
    """Manually trigger a check for updates."""
    checker = get_checker()
    if checker:
        result = await checker.check_for_updates()
        return result
    return {"error": "Release checker not initialized"}


@app.post("/api/updates/apply")
async def apply_app_update(session: dict = Depends(require_auth)):
    """Apply available update by pulling new Docker image and restarting."""
    result = await apply_update()
    if result.get("success"):
        # Broadcast that update is starting
        await broadcast({"type": "update_started"})
    return result


# ============================================================================
# Authentication Configuration API
# ============================================================================

@app.get("/api/auth/config")
async def get_auth_config(session: dict = Depends(require_auth)):
    """Get authentication configuration (secrets masked)."""
    return radius_config.get_auth_config_summary()


@app.put("/api/auth/device-defaults")
async def update_device_auth_config(request: Request, session: dict = Depends(require_auth)):
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


@app.get("/api/auth/oidc")
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


@app.put("/api/auth/oidc")
async def update_oidc_config_api(request: Request, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.SSO_OIDC))):
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


@app.post("/api/auth/test-oidc")
async def test_oidc_discovery(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.SSO_OIDC))):
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
    user = authenticate_oidc_user(email, groups)
    if not user:
        return RedirectResponse(url="/login?error=oidc_unauthorized", status_code=302)

    # Create session using existing infrastructure
    ip_address = request.client.host if request.client else "unknown"
    session_id = create_session(user, ip_address)

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


@app.get("/api/time")
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


@app.get("/api/weather")
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


@app.get("/api/scheduler/status")
async def get_scheduler_status(session: dict = Depends(require_auth)):
    """Get current scheduler status."""
    scheduler = get_scheduler()
    if not scheduler:
        return {"state": "disabled", "block_reason": "Scheduler not initialized"}
    return scheduler.get_status()


@app.get("/api/rollout/current")
async def get_current_rollout(session: dict = Depends(require_auth)):
    """Get the current active/paused rollout with progress."""
    rollout = db.get_active_rollout()
    if not rollout:
        return {"rollout": None}

    progress = db.get_rollout_progress(rollout["id"])
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
        }
    }


@app.post("/api/rollout/{rollout_id}/resume")
async def resume_rollout(rollout_id: int, session: dict = Depends(require_auth)):
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


@app.post("/api/rollout/{rollout_id}/cancel")
async def cancel_rollout(rollout_id: int, session: dict = Depends(require_auth)):
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


@app.post("/api/rollout/{rollout_id}/reset")
async def reset_rollout(rollout_id: int, session: dict = Depends(require_auth)):
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


@app.get("/api/location")
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

@app.post("/api/backup/export")
async def export_backup(request: Request, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
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


@app.post("/api/backup/import")
async def import_backup(
    file: UploadFile = File(...),
    passphrase: str = Form(...),
    conflict_mode: str = Form("skip"),
    session: dict = Depends(require_auth),
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


@app.post("/api/upload-firmware")
async def upload_firmware(file: UploadFile = File(...), session: dict = Depends(require_auth)):
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


@app.get("/api/firmware-files")
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


@app.delete("/api/firmware-files/{filename:path}")
async def delete_firmware_file(filename: str, session: dict = Depends(require_auth)):
    """Delete a firmware file."""
    # Validate filename to prevent path traversal attacks
    safe_filename = validate_firmware_filename(filename)
    path = FIRMWARE_DIR / safe_filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    db.unregister_firmware(safe_filename)
    return {"success": True}


@app.post("/api/firmware-fetch")
async def trigger_firmware_fetch(session: dict = Depends(require_auth)):
    """Trigger an on-demand firmware check and download."""
    fetcher = get_fetcher()
    if not fetcher:
        raise HTTPException(503, "Firmware fetcher not initialized")
    result = await fetcher.check_and_download()
    return result


@app.post("/api/firmware-reselect")
async def firmware_reselect(session: dict = Depends(require_auth)):
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


@app.get("/api/firmware-fetch/status")
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


@app.get("/api/fleet-status")
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

            # Skip CPEs already at target firmware version (or newer if downgrade disabled)
            if cpe_fw:
                target_version = _extract_version_from_filename(Path(cpe_fw).name)
                current_version = cpe.get("firmware_version", "").replace(".r", ".")
                if target_version and current_version == target_version:
                    logger.info(f"Skipping CPE {cpe_ip}: already at target version {target_version}")
                    continue
                if target_version and current_version and not allow_downgrade:
                    if _parse_version(current_version) > _parse_version(target_version):
                        logger.info(f"Skipping CPE {cpe_ip}: version {current_version} > target {target_version} (downgrade disabled)")
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

    # Enroll enabled switches (skip those already at target version)
    switches = db.get_switches(enabled_only=True)
    valid_switches = []
    for sw in switches:
        sw_ip = sw["ip"]
        sw_fw = _select_firmware_for_model(sw.get("model"), firmware_files)

        # Skip switches already at target firmware version (or newer if downgrade disabled)
        if sw_fw:
            target_version = _extract_version_from_filename(Path(sw_fw).name)
            current_version = sw.get("firmware_version", "").replace(".r", ".")
            if target_version and current_version == target_version:
                logger.info(f"Skipping switch {sw_ip}: already at target version {target_version}")
                continue
            if target_version and current_version and not allow_downgrade:
                if _parse_version(current_version) > _parse_version(target_version):
                    logger.info(f"Skipping switch {sw_ip}: version {current_version} > target {target_version} (downgrade disabled)")
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


@app.post("/api/start-update")
async def start_update(
    firmware_file: str = Form(...),
    device_type: str = Form(...),
    ip_list: str = Form(...),
    concurrency: int = Form(2),
    firmware_file_303l: str = Form(""),
    firmware_file_tns100: str = Form(""),
    bank_mode: str = Form("both"),
    session: dict = Depends(require_auth),
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

        credentials[ip] = (ap["username"], ap["password"])
        device_roles[ip] = "ap"
        device_firmware_map[ip] = _select_firmware_for_model(ap.get("model"), firmware_files) or str(firmware_path)

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
        credentials[sw_ip] = (sw["username"], sw["password"])
        device_roles[sw_ip] = "switch"
        sw_fw = _select_firmware_for_model(sw.get("model"), firmware_files)
        if sw_fw is None and _is_tns100_model(sw.get("model")):
            device_firmware_map[sw_ip] = "__missing_tns100__"
        elif sw_fw is None:
            device_firmware_map[sw_ip] = str(firmware_path)
        else:
            device_firmware_map[sw_ip] = sw_fw

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


@app.post("/api/update-device")
async def update_single_device_endpoint(
    ip: str = Form(...),
    firmware_file: str = Form(...),
    firmware_file_303l: str = Form(""),
    firmware_file_tns100: str = Form(""),
    bank_mode: str = Form("both"),
    session: dict = Depends(require_auth),
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

    if job.device_type in ("tachyon", "mixed"):
        username, password = job.credentials[ip]
        client = TachyonClient(ip, username, password)
        reboot_timeout = TNS100_REBOOT_TIMEOUT if device_status.role == "switch" else AP_REBOOT_TIMEOUT
        result = await client.update_firmware(fw_path, progress_callback, pass_number=pass_number, reboot_timeout=reboot_timeout)
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
            learned_version = None
            for ds in job.devices.values():
                if ds.status == "success" and ds.new_version:
                    learned_version = ds.new_version
                    break
            # Pass device statuses so rollout devices get marked correctly
            device_statuses = {ip: ds.status for ip, ds in job.devices.items()}
            scheduler.on_job_completed(job.job_id, success_count, failed_count,
                                       learned_version=learned_version,
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

    logger.info(f"Job {job.job_id} completed: {success_count} success, {failed_count} failed, {skipped_count} skipped, {cancelled_count} cancelled")


@app.get("/api/job/{job_id}")
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


@app.post("/api/job/{job_id}/cancel")
async def cancel_job(job_id: str, session: dict = Depends(require_auth)):
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


@app.get("/api/device-history")
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
PROTECTED_CONFIG_KEYS = {"network", "ethernet"}


def _validate_fragment_safety(fragment: dict):
    """Raise ValueError if fragment tries to modify protected config sections."""
    if not isinstance(fragment, dict):
        return
    for key in PROTECTED_CONFIG_KEYS:
        if key in fragment:
            raise ValueError(
                f"Config templates cannot modify the '{key}' section — "
                f"this could make devices unreachable"
            )


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base. Overlay values win for scalars.
    Lists in overlay replace lists in base entirely."""
    from copy import deepcopy
    result = deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _check_config_compliance(device_config: dict, templates: list[dict]) -> bool:
    """Check if a device config matches all enabled templates.

    For each template, extract the same key paths from the device config
    and compare. Returns True if all templates match.
    """
    if not templates:
        return True
    config = device_config
    if isinstance(config, str):
        config = json.loads(config)

    for template in templates:
        fragment = json.loads(template["config_fragment"]) if isinstance(template["config_fragment"], str) else template["config_fragment"]
        if not _fragment_matches(config, fragment):
            return False
    return True


def _fragment_matches(config: dict, fragment: dict) -> bool:
    """Check if all keys in fragment match corresponding values in config."""
    for key, value in fragment.items():
        if key not in config:
            return False
        if isinstance(value, dict) and isinstance(config[key], dict):
            if not _fragment_matches(config[key], value):
                return False
        elif config[key] != value:
            return False
    return True


@app.get("/api/configs")
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


@app.get("/api/configs/{ip}")
async def get_config_history(ip: str, limit: int = 20, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Get config snapshot history for a device."""
    history = db.get_device_config_history(ip, limit=limit)
    return {"history": history}


@app.get("/api/configs/{ip}/latest")
async def get_latest_config(ip: str, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Get the latest config JSON for a device."""
    config = db.get_latest_device_config(ip)
    if not config:
        raise HTTPException(404, "No config found for this device")
    config["config_json"] = json.loads(config["config_json"]) if isinstance(config["config_json"], str) else config["config_json"]
    return config


@app.get("/api/configs/{ip}/snapshot/{config_id}")
async def get_config_snapshot(ip: str, config_id: int, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_BACKUP))):
    """Get a specific config snapshot."""
    config = db.get_device_config_by_id(config_id)
    if not config or config["ip"] != ip:
        raise HTTPException(404, "Config snapshot not found")
    config["config_json"] = json.loads(config["config_json"]) if isinstance(config["config_json"], str) else config["config_json"]
    return config


@app.get("/api/configs/{ip}/download/{config_id}")
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


@app.post("/api/configs/{ip}/poll")
async def poll_device_config(ip: str, session: dict = Depends(require_auth)):
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


@app.post("/api/configs/poll")
async def poll_all_configs(session: dict = Depends(require_auth)):
    """Trigger config poll for all devices."""
    poller = get_poller()
    if poller:
        asyncio.create_task(poller.poll_all_configs())
        return {"success": True, "message": "Config poll started"}
    raise HTTPException(500, "Poller not initialized")


# ============================================================================
# Config Templates
# ============================================================================

@app.get("/api/config-templates")
async def list_config_templates(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_TEMPLATES))):
    """List all config templates."""
    templates = db.get_config_templates()
    for t in templates:
        t["config_fragment"] = json.loads(t["config_fragment"]) if isinstance(t["config_fragment"], str) else t["config_fragment"]
        if t.get("form_data"):
            t["form_data"] = json.loads(t["form_data"]) if isinstance(t["form_data"], str) else t["form_data"]
    return {"templates": templates}


@app.post("/api/config-templates")
async def create_config_template(request: Request, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_TEMPLATES))):
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

    try:
        template_id = db.save_config_template(
            name=name,
            category=category,
            config_fragment=fragment_str,
            form_data=form_data_str,
            description=data.get("description"),
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"Template with name '{name}' already exists")
        raise

    return {"id": template_id, "success": True}


@app.put("/api/config-templates/{template_id}")
async def update_config_template_api(template_id: int, request: Request, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_TEMPLATES))):
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

    db.update_config_template(template_id, **updates)
    return {"success": True}


@app.delete("/api/config-templates/{template_id}")
async def delete_config_template_api(template_id: int, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_TEMPLATES))):
    """Delete a config template."""
    existing = db.get_config_template(template_id)
    if not existing:
        raise HTTPException(404, "Template not found")
    db.delete_config_template(template_id)
    return {"success": True}


# ============================================================================
# Config Compliance
# ============================================================================

@app.get("/api/config-compliance")
async def get_config_compliance(session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_COMPLIANCE))):
    """Get per-device config compliance status."""
    all_configs = db.get_all_latest_configs()
    templates = db.get_config_templates(enabled_only=True)

    devices = {}
    for ip, cfg in all_configs.items():
        config_data = json.loads(cfg["config_json"]) if isinstance(cfg["config_json"], str) else cfg["config_json"]
        compliant = _check_config_compliance(config_data, templates)
        devices[ip] = {
            "compliant": compliant,
            "checked_at": cfg["fetched_at"],
        }

    return {"devices": devices}


@app.get("/api/config-prefill/{category}")
async def get_config_prefill(category: str, session: dict = Depends(require_auth)):
    """Get pre-fill data for a config category by analyzing fleet configs.

    Only returns data if no saved template exists for this category.
    """
    existing = db.get_config_template_by_category(category)
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
        "discovery": ["services", "discovery"],
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
        if section:
            values.append(section)

    if not values:
        return {"prefilled": False, "reason": "no_data"}

    # Find most common value (simple: use the first one if >80% match)
    canonical = [json.dumps(v, sort_keys=True) for v in values]
    from collections import Counter
    counts = Counter(canonical)
    most_common, count = counts.most_common(1)[0]
    threshold = int(len(values) * 0.8)

    if count >= threshold:
        return {
            "prefilled": True,
            "data": json.loads(most_common),
            "device_count": len(values),
            "match_count": count,
        }

    return {
        "prefilled": False,
        "reason": "no_dominant_value",
        "unique_values": len(counts),
        "device_count": len(values),
    }


# ============================================================================
# Config Push (Mass Operations)
# ============================================================================

@app.post("/api/config-push")
async def push_config_templates(request: Request, session: dict = Depends(require_auth), _pro=Depends(require_feature(Feature.CONFIG_PUSH))):
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

@app.get("/api/radius-server/config")
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


@app.put("/api/radius-server/config")
async def update_radius_server_config_api(
    request: Request,
    session: dict = Depends(require_auth),
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


@app.post("/api/radius-server/restart")
async def restart_radius_server_api(
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Restart the RADIUS server."""
    svc = get_radius_service()
    if not svc:
        raise HTTPException(500, "RADIUS service not initialized")
    await svc.restart()
    return {"status": "ok"}


@app.get("/api/radius-server/status")
async def get_radius_server_status_api(
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Get RADIUS server running status and stats."""
    svc = get_radius_service()
    if not svc:
        return {"running": False, "error": "Not initialized", "stats": {}}
    return svc.get_status()


@app.get("/api/radius-server/users")
async def list_radius_users_api(
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """List all RADIUS users."""
    return radius_users.get_radius_users()


@app.post("/api/radius-server/users")
async def create_radius_user_api(
    request: Request,
    session: dict = Depends(require_auth),
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


@app.put("/api/radius-server/users/{user_id}")
async def update_radius_user_api(
    user_id: int,
    request: Request,
    session: dict = Depends(require_auth),
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


@app.delete("/api/radius-server/users/{user_id}")
async def delete_radius_user_api(
    user_id: int,
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Delete a RADIUS user."""
    if not radius_users.delete_radius_user(user_id):
        raise HTTPException(404, "User not found")
    return {"status": "ok"}


@app.get("/api/radius-server/auth-log")
async def get_radius_auth_log_api(
    limit: int = 50,
    offset: int = 0,
    session: dict = Depends(require_auth),
    _pro=Depends(require_feature(Feature.RADIUS_SERVER)),
):
    """Get recent RADIUS authentication attempts."""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM radius_auth_log ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (min(limit, 200), offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM radius_auth_log").fetchone()[0]
    return {"entries": [dict(r) for r in rows], "total": total}


@app.post("/api/radius-server/test-ldap")
async def test_ldap_connection_api(
    request: Request,
    session: dict = Depends(require_auth),
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


@app.post("/api/radius-server/push-to-devices")
async def push_radius_to_devices_api(
    request: Request,
    session: dict = Depends(require_auth),
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


def main():
    """Run the application."""
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
