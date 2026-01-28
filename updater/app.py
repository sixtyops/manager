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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .tachyon import TachyonClient, UpdateResult
from . import database as db
from .poller import init_poller, get_poller
from . import services

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
# Page Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main page (monitor view)."""
    return templates.TemplateResponse("monitor.html", {"request": request})


@app.get("/firmware", response_class=HTMLResponse)
async def firmware_page(request: Request):
    """Serve the firmware update page."""
    return templates.TemplateResponse("index.html", {"request": request})


# ============================================================================
# WebSocket
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
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
async def list_sites():
    """List all tower sites."""
    sites = db.get_tower_sites()
    return {"sites": sites}


@app.post("/api/sites")
async def create_site(
    name: str = Form(...),
    location: str = Form(None),
    latitude: float = Form(None),
    longitude: float = Form(None),
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
):
    """Update a tower site."""
    db.update_tower_site(site_id, name=name, location=location, latitude=latitude, longitude=longitude)
    return {"success": True}


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: int):
    """Delete a tower site."""
    db.delete_tower_site(site_id)
    return {"success": True}


# ============================================================================
# Access Point API
# ============================================================================

@app.get("/api/aps")
async def list_aps(site_id: int = None):
    """List access points."""
    aps = db.get_access_points(tower_site_id=site_id, enabled_only=False)
    return {"aps": aps}


@app.post("/api/aps")
async def add_ap(
    ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    tower_site_id: int = Form(None),
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
async def delete_ap(ip: str):
    """Delete an access point."""
    poller = get_poller()
    if poller:
        poller.invalidate_client(ip)

    db.delete_access_point(ip)
    return {"success": True}


@app.post("/api/aps/{ip}/poll")
async def poll_ap(ip: str):
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
async def get_topology():
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
async def refresh_topology():
    """Trigger a full topology refresh."""
    poller = get_poller()
    if not poller:
        raise HTTPException(500, "Poller not initialized")

    await poller._poll_all_aps()
    return poller.get_topology()


@app.get("/api/cpes")
async def get_all_cpes():
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
async def get_settings():
    """Get all settings."""
    settings = db.get_all_settings()
    return {"settings": settings}


@app.put("/api/settings")
async def update_settings(request: Request):
    """Update settings."""
    data = await request.json()
    db.set_settings(data)
    return {"success": True}


@app.get("/api/time")
async def get_current_time():
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
async def get_weather():
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
async def get_location():
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


@dataclass
class UpdateJob:
    """A firmware update job."""
    job_id: str
    firmware_file: str
    firmware_name: str
    device_type: str
    username: str
    password: str
    devices: Dict[str, DeviceStatus] = field(default_factory=dict)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: str = "pending"


@app.post("/api/upload-firmware")
async def upload_firmware(file: UploadFile = File(...)):
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
async def list_firmware_files():
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


@app.post("/api/start-update")
async def start_update(
    firmware_file: str = Form(...),
    device_type: str = Form(...),
    ip_list: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    concurrency: int = Form(5),
):
    """Start a firmware update job."""
    ips = []
    for line in ip_list.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            ip = line.split(",")[0].strip()
            if ip:
                ips.append(ip)

    if not ips:
        raise HTTPException(400, "No valid IPs provided")

    firmware_path = FIRMWARE_DIR / firmware_file
    if not firmware_path.exists():
        raise HTTPException(400, f"Firmware file not found: {firmware_file}")

    job_id = str(uuid.uuid4())[:8]
    job = UpdateJob(
        job_id=job_id,
        firmware_file=str(firmware_path),
        firmware_name=firmware_file,
        device_type=device_type,
        username=username,
        password=password,
        started_at=datetime.now(),
        status="running",
    )

    for ip in ips:
        job.devices[ip] = DeviceStatus(ip=ip)

    update_jobs[job_id] = job

    await broadcast({
        "type": "job_started",
        "job_id": job_id,
        "device_count": len(ips),
        "firmware": firmware_file,
    })

    asyncio.create_task(run_update_job(job, concurrency))

    return {"job_id": job_id, "device_count": len(ips)}


async def run_update_job(job: UpdateJob, concurrency: int):
    """Run the firmware update job."""
    semaphore = asyncio.Semaphore(concurrency)

    async def update_device(ip: str):
        async with semaphore:
            device_status = job.devices[ip]
            device_status.status = "connecting"
            device_status.progress_message = "Connecting..."

            await broadcast({
                "type": "device_update",
                "job_id": job.job_id,
                "ip": ip,
                "status": device_status.status,
                "message": device_status.progress_message,
            })

            def progress_callback(device_ip: str, message: str):
                status_map = {
                    "Logging in": "connecting",
                    "Getting device info": "connecting",
                    "Uploading firmware": "uploading",
                    "Installing firmware": "installing",
                    "Rebooting": "rebooting",
                    "Verifying": "verifying",
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
                }))

            if job.device_type == "tachyon":
                client = TachyonClient(ip, job.username, job.password)
                result = await client.update_firmware(job.firmware_file, progress_callback)
            else:
                result = UpdateResult(ip=ip, success=False, error=f"Unsupported device type: {job.device_type}")

            device_status.old_version = result.old_version
            device_status.new_version = result.new_version

            if result.success:
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
            })

    await asyncio.gather(*[update_device(ip) for ip in job.devices.keys()])

    job.completed_at = datetime.now()
    job.status = "completed"

    success_count = sum(1 for d in job.devices.values() if d.status == "success")
    failed_count = sum(1 for d in job.devices.values() if d.status == "failed")

    await broadcast({
        "type": "job_completed",
        "job_id": job.job_id,
        "success_count": success_count,
        "failed_count": failed_count,
        "duration": (job.completed_at - job.started_at).total_seconds(),
    })

    logger.info(f"Job {job.job_id} completed: {success_count} success, {failed_count} failed")


@app.get("/api/job/{job_id}")
async def get_job_status(job_id: str):
    """Get status of an update job."""
    if job_id not in update_jobs:
        raise HTTPException(404, "Job not found")

    job = update_jobs[job_id]
    return {
        "job_id": job.job_id,
        "status": job.status,
        "firmware": job.firmware_name,
        "device_type": job.device_type,
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
