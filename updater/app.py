"""Tachyon Management System - Web Application."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

import aiofiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .tachyon import TachyonClient, UpdateResult
from . import database as db
from .poller import init_poller, get_poller
from .scheduler import init_scheduler, get_scheduler
from .firmware_fetcher import init_fetcher, get_fetcher
from .release_checker import init_checker, get_checker, apply_update
from . import services
from .auth import require_auth, require_auth_ws, authenticate, create_session, SESSION_COOKIE_NAME, is_setup_required, is_first_run, complete_setup
from .backup import build_csv_export, process_csv_import
from . import telemetry
from . import slack
from . import ssl_manager
from . import git_backup
from . import radius_config

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


async def broadcast(message: dict):
    """Broadcast message to all connected WebSocket clients."""
    disconnected = set()
    for ws in active_websockets:
        try:
            await ws.send_json(message)
        except Exception:
            disconnected.add(ws)

    for ws in disconnected:
        active_websockets.discard(ws)


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
            _cleanup_completed_jobs(max_age_seconds=3600)
            logger.info("Periodic cleanup completed")
        except Exception as e:
            logger.error(f"Periodic cleanup error: {e}")


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
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    backup_task = asyncio.create_task(_backup_scheduler())
    logger.info("Application started")

    yield

    # Shutdown
    backup_task.cancel()
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await checker.stop()
    await fetcher.stop()
    await scheduler.stop()
    await poller.stop()
    logger.info("Application stopped")


# FastAPI app
app = FastAPI(title="Unofficial Tachyon Networks Bulk Updater", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================================
# Auth Routes (no auth dependency)
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    """Serve the login page."""
    # First run with no password configured - redirect to setup
    if is_first_run():
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Handle login form submission."""
    user = authenticate(username, password)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": True}, status_code=401)

    ip_address = request.client.host if request.client else "unknown"
    session_id = create_session(user, ip_address)

    # Redirect to setup if password hasn't been changed from default
    redirect_url = "/setup" if is_setup_required() else "/"

    response = RedirectResponse(url=redirect_url, status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=True,
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

    return templates.TemplateResponse("setup.html", {
        "request": request,
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

        if not current_password:
            return templates.TemplateResponse("setup.html", {
                "request": request,
                "error": "Current password is required.",
                "first_run": False,
            }, status_code=400)

        user = authenticate(session["username"], current_password)
        if not user:
            return templates.TemplateResponse("setup.html", {
                "request": request,
                "error": "Current password is incorrect.",
                "first_run": False,
            }, status_code=400)
        username = session["username"]
    else:
        username = "admin"

    if not is_setup_required():
        return RedirectResponse(url="/", status_code=302)

    if new_password != confirm_password:
        return templates.TemplateResponse("setup.html", {
            "request": request,
            "error": "New passwords do not match.",
            "first_run": first_run,
        }, status_code=400)

    if len(new_password) < 8:
        return templates.TemplateResponse("setup.html", {
            "request": request,
            "error": "Password must be at least 8 characters.",
            "first_run": first_run,
        }, status_code=400)

    complete_setup(new_password)
    logger.info(f"Admin password {'created' if first_run else 'changed'} by {username} during initial setup")

    if first_run:
        # First run - redirect to login so they can log in with the new password
        return RedirectResponse(url="/login", status_code=303)

    return RedirectResponse(url="/", status_code=303)


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


@app.get("/setup-wizard", response_class=HTMLResponse)
async def setup_wizard_page(request: Request, step: int = 1, session: dict = Depends(require_auth)):
    """Serve the setup wizard page."""
    if not _is_wizard_needed():
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("setup_wizard.html", {
        "request": request, "step": step,
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
    ssl_status = ssl_manager.get_ssl_status()
    backup_status = git_backup.get_backup_status()

    if step == 1:
        if action == "configure" and ssl_domain and ssl_email:
            ok, msg = await ssl_manager.obtain_certificate(ssl_domain, ssl_email)
            if not ok:
                return templates.TemplateResponse("setup_wizard.html", {
                    "request": request, "step": 1, "ssl_status": ssl_status,
                    "backup_status": backup_status, "error": msg, "success": None,
                })
        return templates.TemplateResponse("setup_wizard.html", {
            "request": request, "step": 2,
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
                return templates.TemplateResponse("setup_wizard.html", {
                    "request": request, "step": 2, "ssl_status": ssl_status,
                    "backup_status": backup_status, "error": msg, "success": None,
                })
        return templates.TemplateResponse("setup_wizard.html", {
            "request": request, "step": 3,
            "ssl_status": ssl_manager.get_ssl_status(),
            "backup_status": git_backup.get_backup_status(),
            "error": None, "success": None,
        })
    elif step == 3:
        db.set_setting("setup_wizard_completed", "true")
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url="/setup-wizard", status_code=303)


@app.get("/ssl-setup", response_class=HTMLResponse)
async def ssl_setup_page(request: Request, session: dict = Depends(require_auth)):
    """Serve the SSL setup page."""
    return templates.TemplateResponse("ssl_setup.html", {
        "request": request, "ssl_status": ssl_manager.get_ssl_status(),
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
    return templates.TemplateResponse("ssl_setup.html", {
        "request": request, "ssl_status": status,
        "error": None if success else message,
        "success": message if success else None,
    }, status_code=200 if success else 400)


@app.get("/api/ssl/status")
async def get_ssl_status_api(session: dict = Depends(require_auth)):
    return ssl_manager.get_ssl_status()


@app.get("/backup-setup", response_class=HTMLResponse)
async def backup_setup_page(request: Request, session: dict = Depends(require_auth)):
    """Serve the backup setup page."""
    return templates.TemplateResponse("backup_setup.html", {
        "request": request, "backup_status": git_backup.get_backup_status(),
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
    return templates.TemplateResponse("backup_setup.html", {
        "request": request, "backup_status": git_backup.get_backup_status(),
        "error": None if success else message,
        "success": message if success else None,
    }, status_code=200 if success else 400)


@app.post("/backup-run")
async def backup_run_now(session: dict = Depends(require_auth)):
    """Trigger an immediate backup."""
    await git_backup.run_backup()
    return RedirectResponse(url="/backup-setup", status_code=303)


@app.get("/api/backup/git-status")
async def get_git_backup_status_api(session: dict = Depends(require_auth)):
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
    return templates.TemplateResponse("monitor.html", {"request": request})



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
):
    """Update a tower site."""
    db.update_tower_site(site_id, name=name, location=location, latitude=latitude, longitude=longitude)
    return {"success": True}


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: int, session: dict = Depends(require_auth)):
    """Delete a tower site."""
    db.delete_tower_site(site_id)
    return {"success": True}


# ============================================================================
# Access Point API
# ============================================================================

@app.get("/api/aps")
async def list_aps(site_id: int = None, session: dict = Depends(require_auth)):
    """List access points."""
    aps = db.get_access_points(tower_site_id=site_id, enabled_only=False)
    return {"aps": aps}


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
    """List switches."""
    switches = db.get_switches(tower_site_id=site_id, enabled_only=False)
    return {"switches": switches}


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

@app.get("/api/settings")
async def get_settings(session: dict = Depends(require_auth)):
    """Get all settings."""
    settings = db.get_all_settings()
    # Resolve temperature unit for UI
    temp_unit_setting = settings.get("temperature_unit", "auto")
    resolved_unit = await services.resolve_temperature_unit(temp_unit_setting)
    return {"settings": settings, "resolved_temperature_unit": resolved_unit}


@app.put("/api/settings")
async def update_settings(request: Request, session: dict = Depends(require_auth)):
    """Update settings."""
    data = await request.json()
    db.set_settings(data)
    return {"success": True}


@app.post("/api/slack/test")
async def test_slack_webhook(session: dict = Depends(require_auth)):
    """Send a test notification to the configured Slack webhook."""
    success, message = await slack.send_test_notification()
    return {"success": success, "message": message}


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


@app.put("/api/auth/radius")
async def update_radius_config(request: Request, session: dict = Depends(require_auth)):
    """Update web RADIUS authentication configuration."""
    data = await request.json()

    config = radius_config.RadiusConfig(
        enabled=data.get("enabled", False),
        server=data.get("server", ""),
        secret=data.get("secret", ""),
        port=int(data.get("port", 1812)),
        timeout=int(data.get("timeout", 5)),
    )

    radius_config.set_web_radius_config(config)
    return {"success": True}


@app.put("/api/auth/device-defaults")
async def update_device_auth_config(request: Request, session: dict = Depends(require_auth)):
    """Update global default device credentials."""
    data = await request.json()

    config = radius_config.DeviceAuthConfig(
        enabled=data.get("enabled", False),
        username=data.get("username", ""),
        password=data.get("password", ""),
    )

    radius_config.set_device_auth_config(config)
    return {"success": True}


@app.post("/api/auth/test-radius")
async def test_radius_connection(request: Request, session: dict = Depends(require_auth)):
    """Test RADIUS connection with provided credentials."""
    data = await request.json()
    username = data.get("username", "")
    password = data.get("password", "")

    if not username or not password:
        return {"success": False, "message": "Username and password required"}

    if not radius_config.is_web_radius_enabled():
        return {"success": False, "message": "RADIUS not configured"}

    success = radius_config.authenticate_via_radius(username, password)
    if success:
        return {"success": True, "message": "RADIUS authentication successful"}
    else:
        return {"success": False, "message": "RADIUS authentication failed"}


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
async def export_backup(request: Request, session: dict = Depends(require_auth)):
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
):
    """Import devices from a CSV with encrypted passwords."""
    if not passphrase or len(passphrase) < 8:
        raise HTTPException(400, "Passphrase must be at least 8 characters")
    if conflict_mode not in ("skip", "update"):
        raise HTTPException(400, "conflict_mode must be 'skip' or 'update'")

    try:
        raw = await file.read()
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
    ap_cpe_map: Dict[str, list] = field(default_factory=dict)  # AP IP -> [CPE IPs]
    device_roles: Dict[str, str] = field(default_factory=dict)  # IP -> "ap"/"cpe"
    device_parent: Dict[str, str] = field(default_factory=dict)  # CPE IP -> parent AP IP
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: str = "pending"
    is_scheduled: bool = False
    end_hour: Optional[int] = None
    schedule_timezone: Optional[str] = None


@app.post("/api/upload-firmware")
async def upload_firmware(file: UploadFile = File(...), session: dict = Depends(require_auth)):
    """Upload a firmware file."""
    # Validate filename to prevent path traversal attacks
    safe_filename = validate_firmware_filename(file.filename)
    firmware_path = FIRMWARE_DIR / safe_filename

    async with aiofiles.open(firmware_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    file_size = firmware_path.stat().st_size
    logger.info(f"Firmware uploaded: {safe_filename} ({file_size:,} bytes)")

    db.register_firmware(safe_filename, source="manual")

    return {
        "filename": safe_filename,
        "size": file_size,
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

    quarantine_days = int(db.get_setting("firmware_quarantine_days", "7"))
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
    end_hour: int = None,
    schedule_timezone: str = None,
) -> str:
    """Start an update job from the scheduler (Python args, not Form data)."""
    firmware_path = FIRMWARE_DIR / firmware_file
    if not firmware_path.exists():
        raise RuntimeError(f"Firmware file not found: {firmware_file}")

    # Get downgrade setting
    allow_downgrade = db.get_setting("allow_downgrade", "false") == "true"

    firmware_files = {"tna-30x": str(firmware_path)}
    firmware_names = {"tna-30x": firmware_file}

    if firmware_file_303l:
        path_303l = FIRMWARE_DIR / firmware_file_303l
        if path_303l.exists():
            firmware_files["tna-303l"] = str(path_303l)
            firmware_names["tna-303l"] = firmware_file_303l

    if firmware_file_tns100:
        path_tns100 = FIRMWARE_DIR / firmware_file_tns100
        if path_tns100.exists():
            firmware_files["tns-100"] = str(path_tns100)
            firmware_names["tns-100"] = firmware_file_tns100

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
        end_hour=end_hour,
        schedule_timezone=schedule_timezone,
    )

    for ip in valid_aps:
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

    asyncio.create_task(run_update_job(job, concurrency))
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
    ap_ips = []
    for line in ip_list.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            ip = line.split(",")[0].strip()
            if ip:
                ap_ips.append(ip)

    if not ap_ips:
        raise HTTPException(400, "No valid IPs provided")

    # Build firmware files dict
    firmware_path = FIRMWARE_DIR / firmware_file
    if not firmware_path.exists():
        raise HTTPException(400, f"Firmware file not found: {firmware_file}")

    firmware_files = {"tna-30x": str(firmware_path)}
    firmware_names = {"tna-30x": firmware_file}

    if firmware_file_303l:
        path_303l = FIRMWARE_DIR / firmware_file_303l
        if not path_303l.exists():
            raise HTTPException(400, f"303L firmware file not found: {firmware_file_303l}")
        firmware_files["tna-303l"] = str(path_303l)
        firmware_names["tna-303l"] = firmware_file_303l

    if firmware_file_tns100:
        path_tns100 = FIRMWARE_DIR / firmware_file_tns100
        if not path_tns100.exists():
            raise HTTPException(400, f"TNS100 firmware file not found: {firmware_file_tns100}")
        firmware_files["tns-100"] = str(path_tns100)
        firmware_names["tns-100"] = firmware_file_tns100

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

    asyncio.create_task(run_update_job(job, concurrency))

    return {"job_id": job_id, "device_count": len(job.devices)}


@app.post("/api/update-device")
async def update_single_device_endpoint(
    ip: str = Form(...),
    firmware_file: str = Form(...),
    firmware_file_303l: str = Form(""),
    firmware_file_tns100: str = Form(""),
    bank_mode: str = Form("both"),
    session: dict = Depends(require_auth),
):
    """Start a firmware update for a single device (AP, CPE, or switch)."""
    # Check firmware file exists
    firmware_path = FIRMWARE_DIR / firmware_file
    if not firmware_path.exists():
        raise HTTPException(400, f"Firmware file not found: {firmware_file}")

    firmware_files = {"tna-30x": str(firmware_path)}
    firmware_names = {"tna-30x": firmware_file}

    if firmware_file_303l:
        path_303l = FIRMWARE_DIR / firmware_file_303l
        if not path_303l.exists():
            raise HTTPException(400, f"303L firmware file not found: {firmware_file_303l}")
        firmware_files["tna-303l"] = str(path_303l)
        firmware_names["tna-303l"] = firmware_file_303l

    if firmware_file_tns100:
        path_tns100 = FIRMWARE_DIR / firmware_file_tns100
        if not path_tns100.exists():
            raise HTTPException(400, f"TNS100 firmware file not found: {firmware_file_tns100}")
        firmware_files["tns-100"] = str(path_tns100)
        firmware_names["tns-100"] = firmware_file_tns100

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

    asyncio.create_task(run_update_job(job, concurrency=1))

    return {"job_id": job_id, "device_count": 1}


async def _update_single_device(job: "UpdateJob", ip: str, pass_number: int = 1):
    """Update a single device within a job."""
    device_status = job.devices[ip]

    # Maintenance window cutoff for scheduled jobs
    if job.is_scheduled and job.end_hour is not None and job.schedule_timezone:
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
                return
        except Exception as e:
            logger.warning(f"Maintenance window check failed: {e}")

    device_start_time = datetime.now()

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
        for key, status in status_map.items():
            if key in message:
                device_status.status = status
                break
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
        reboot_timeout = TNS100_REBOOT_TIMEOUT if device_status.role == "switch" else 300
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
            job.cancelled = True

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


async def run_update_job(job: UpdateJob, concurrency: int):
    """Run the firmware update job with phase-based ordering driven by bank_mode."""
    semaphore = asyncio.Semaphore(concurrency)

    ap_ips = [ip for ip, role in job.device_roles.items() if role == "ap"]
    cpe_ips = [ip for ip, role in job.device_roles.items() if role == "cpe"]
    switch_ips = [ip for ip, role in job.device_roles.items() if role == "switch"]

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
        for ip in ips:
            ds = job.devices.get(ip)
            if ds and ds.status == "pending":
                ds.status = "cancelled"
                ds.progress_message = "Cancelled: another device failed to reboot"
                await broadcast({
                    "type": "device_update",
                    "job_id": job.job_id,
                    "ip": ip,
                    "status": ds.status,
                    "message": ds.progress_message,
                    "role": ds.role,
                    "parent_ap": ds.parent_ap,
                })

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

    firmware_name = job.firmware_names.get("30x", "") or list(job.firmware_names.values())[0] if job.firmware_names else "Unknown"
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
                                       device_statuses=device_statuses)

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
        "firmware_names": job.firmware_names,
        "device_type": job.device_type,
        "bank_mode": job.bank_mode,
        "ap_cpe_map": job.ap_cpe_map,
        "device_roles": job.device_roles,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "devices": {ip: asdict(status) for ip, status in job.devices.items()},
    }


def main():
    """Run the application."""
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
