"""Tachyon Management System - Web Application."""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

import aiofiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .tachyon import TachyonClient, UpdateResult
from . import database as db
from .poller import init_poller, get_poller
from . import services
from .auth import require_auth, require_auth_ws, authenticate, create_session, SESSION_COOKIE_NAME

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - start/stop background tasks."""
    # Startup
    db.cleanup_expired_sessions()
    poller = init_poller(broadcast, poll_interval=60)
    await poller.start()
    logger.info("Application started")

    yield

    # Shutdown
    await poller.stop()
    logger.info("Application stopped")


# FastAPI app
app = FastAPI(title="Tachyon Management System", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================================
# Auth Routes (no auth dependency)
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    """Serve the login page."""
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Handle login form submission."""
    user = authenticate(username, password)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": True}, status_code=401)

    ip_address = request.client.host if request.client else "unknown"
    session_id = create_session(user, ip_address)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        max_age=86400,
        samesite="lax",
    )
    return response


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
# Page Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: dict = Depends(require_auth)):
    """Serve the main page (monitor view)."""
    return templates.TemplateResponse("monitor.html", {"request": request})


@app.get("/firmware", response_class=HTMLResponse)
async def firmware_page(request: Request, session: dict = Depends(require_auth)):
    """Serve the firmware manager page."""
    return templates.TemplateResponse("firmware.html", {"request": request})


@app.get("/update", response_class=HTMLResponse)
async def update_page(request: Request, session: dict = Depends(require_auth)):
    """Serve the firmware update page."""
    return templates.TemplateResponse("index.html", {"request": request})


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

    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        active_websockets.discard(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(active_websockets)}")


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
        raise HTTPException(500, str(e))


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
    ap_id = db.upsert_access_point(ip, username, password, tower_site_id)

    # Trigger immediate poll
    poller = get_poller()
    if poller:
        await poller.poll_ap_now(ip)

    return {"id": ap_id, "ip": ip}


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

    return {"ap_id": ap_id, "site_id": site_id, "ip": ip}


# ============================================================================
# Settings API
# ============================================================================

@app.get("/api/settings")
async def get_settings(session: dict = Depends(require_auth)):
    """Get all settings."""
    settings = db.get_all_settings()
    return {"settings": settings}


@app.put("/api/settings")
async def update_settings(request: Request, session: dict = Depends(require_auth)):
    """Update settings."""
    data = await request.json()
    db.set_settings(data)
    return {"success": True}


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

    return weather


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
    update_order: str = "cpe_first"
    ap_cpe_map: Dict[str, list] = field(default_factory=dict)  # AP IP -> [CPE IPs]
    device_roles: Dict[str, str] = field(default_factory=dict)  # IP -> "ap"/"cpe"
    device_parent: Dict[str, str] = field(default_factory=dict)  # CPE IP -> parent AP IP
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: str = "pending"


@app.post("/api/upload-firmware")
async def upload_firmware(file: UploadFile = File(...), session: dict = Depends(require_auth)):
    """Upload a firmware file."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    firmware_path = FIRMWARE_DIR / file.filename

    async with aiofiles.open(firmware_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    file_size = firmware_path.stat().st_size
    logger.info(f"Firmware uploaded: {file.filename} ({file_size:,} bytes)")

    return {
        "filename": file.filename,
        "size": file_size,
        "path": str(firmware_path),
    }


@app.get("/api/firmware-files")
async def list_firmware_files(session: dict = Depends(require_auth)):
    """List available firmware files."""
    files = []
    for f in FIRMWARE_DIR.iterdir():
        if f.is_file() and f.suffix in {".bin", ".img", ".npk", ".tar", ".gz"}:
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return {"files": sorted(files, key=lambda x: x["modified"], reverse=True)}


@app.delete("/api/firmware-files/{filename}")
async def delete_firmware_file(filename: str, session: dict = Depends(require_auth)):
    """Delete a firmware file."""
    path = FIRMWARE_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"success": True}


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


@app.post("/api/start-update")
async def start_update(
    firmware_file: str = Form(...),
    device_type: str = Form(...),
    ip_list: str = Form(...),
    concurrency: int = Form(5),
    firmware_file_303l: str = Form(""),
    update_order: str = Form("cpe_first"),
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

    job_id = str(uuid.uuid4())[:8]
    job = UpdateJob(
        job_id=job_id,
        firmware_files=firmware_files,
        firmware_names=firmware_names,
        device_firmware_map=device_firmware_map,
        device_type=device_type,
        credentials=credentials,
        update_order=update_order,
        ap_cpe_map=ap_cpe_map,
        device_roles=device_roles,
        device_parent=device_parent,
        started_at=datetime.now(),
        status="running",
    )

    # Create device statuses for all devices (APs + CPEs)
    for ip in ap_ips:
        job.devices[ip] = DeviceStatus(ip=ip, role="ap")
    for ap_ip, cpe_ips in ap_cpe_map.items():
        for cpe_ip in cpe_ips:
            job.devices[cpe_ip] = DeviceStatus(ip=cpe_ip, role="cpe", parent_ap=ap_ip)

    update_jobs[job_id] = job

    await broadcast({
        "type": "job_started",
        "job_id": job_id,
        "device_count": len(job.devices),
        "firmware": firmware_file,
        "ap_cpe_map": ap_cpe_map,
        "device_roles": device_roles,
        "device_parent": device_parent,
        "update_order": update_order,
    })

    asyncio.create_task(run_update_job(job, concurrency))

    return {"job_id": job_id, "device_count": len(job.devices)}


async def _update_single_device(job: "UpdateJob", ip: str):
    """Update a single device within a job."""
    device_status = job.devices[ip]
    device_status.status = "connecting"
    device_status.progress_message = "Connecting..."

    await broadcast({
        "type": "device_update",
        "job_id": job.job_id,
        "ip": ip,
        "status": device_status.status,
        "message": device_status.progress_message,
        "role": device_status.role,
        "parent_ap": device_status.parent_ap,
    })

    # Check for missing 303L firmware
    fw_path = job.device_firmware_map.get(ip, "")
    if fw_path == "__missing_303l__":
        device_status.status = "failed"
        device_status.error = "TNA-303L device requires 303L firmware, but none was provided"
        device_status.progress_message = device_status.error
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
        device_status.progress_message = message

        asyncio.create_task(broadcast({
            "type": "device_update",
            "job_id": job.job_id,
            "ip": device_ip,
            "status": device_status.status,
            "message": message,
            "role": device_status.role,
            "parent_ap": device_status.parent_ap,
        }))

    if job.device_type == "tachyon":
        username, password = job.credentials[ip]
        client = TachyonClient(ip, username, password)
        result = await client.update_firmware(fw_path, progress_callback)
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
        device_status.progress_message = f"Already on {result.new_version}"
    elif result.success:
        device_status.status = "success"
        device_status.progress_message = f"Updated to {result.new_version}"
    else:
        device_status.status = "failed"
        device_status.error = result.error
        device_status.progress_message = result.error or "Update failed"

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
    """Run the firmware update job with AP/CPE ordering."""
    semaphore = asyncio.Semaphore(concurrency)

    async def update_ap_group(ap_ip: str):
        """Update an AP and its CPEs respecting the configured order."""
        async with semaphore:
            cpe_ips = job.ap_cpe_map.get(ap_ip, [])

            if job.update_order == "cpe_first":
                # Update all CPEs concurrently, then the AP
                if cpe_ips:
                    await asyncio.gather(
                        *[_update_single_device(job, cpe_ip) for cpe_ip in cpe_ips],
                        return_exceptions=True,
                    )
                await _update_single_device(job, ap_ip)
            else:
                # Update the AP first, then all CPEs concurrently
                await _update_single_device(job, ap_ip)
                if cpe_ips:
                    await asyncio.gather(
                        *[_update_single_device(job, cpe_ip) for cpe_ip in cpe_ips],
                        return_exceptions=True,
                    )

    # Run AP groups in parallel (bounded by semaphore)
    ap_ips = [ip for ip, role in job.device_roles.items() if role == "ap"]
    await asyncio.gather(
        *[update_ap_group(ap_ip) for ap_ip in ap_ips],
        return_exceptions=True,
    )

    job.completed_at = datetime.now()
    job.status = "completed"

    success_count = sum(1 for d in job.devices.values() if d.status == "success")
    failed_count = sum(1 for d in job.devices.values() if d.status == "failed")
    skipped_count = sum(1 for d in job.devices.values() if d.status == "skipped")

    await broadcast({
        "type": "job_completed",
        "job_id": job.job_id,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "duration": (job.completed_at - job.started_at).total_seconds(),
    })

    logger.info(f"Job {job.job_id} completed: {success_count} success, {failed_count} failed, {skipped_count} skipped")


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
        "update_order": job.update_order,
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
