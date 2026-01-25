"""Tachyon Management System - Web Application."""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiofiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .tachyon import TachyonClient, UpdateResult
from .discovery import network_discovery
from .models import NetworkTopology

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

# Ensure directories exist
FIRMWARE_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# FastAPI app
app = FastAPI(title="Tachyon Management System")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@dataclass
class DeviceStatus:
    """Status of a single device update."""
    ip: str
    status: str = "pending"  # pending, connecting, uploading, installing, rebooting, verifying, success, failed
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
    status: str = "pending"  # pending, running, completed


# Global state
update_jobs: Dict[str, UpdateJob] = {}
active_websockets: Set[WebSocket] = set()


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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    await websocket.accept()
    active_websockets.add(websocket)
    logger.info(f"WebSocket connected. Total: {len(active_websockets)}")

    try:
        while True:
            # Keep connection alive, receive any client messages
            data = await websocket.receive_text()
            # Could handle client commands here if needed
    except WebSocketDisconnect:
        pass
    finally:
        active_websockets.discard(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(active_websockets)}")


@app.post("/api/upload-firmware")
async def upload_firmware(file: UploadFile = File(...)):
    """Upload a firmware file."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    # Save firmware file
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
    # Parse IP list
    ips = []
    for line in ip_list.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            # Handle CSV format (IP,username,password) or just IP
            ip = line.split(",")[0].strip()
            if ip:
                ips.append(ip)

    if not ips:
        raise HTTPException(400, "No valid IPs provided")

    # Verify firmware file exists
    firmware_path = FIRMWARE_DIR / firmware_file
    if not firmware_path.exists():
        raise HTTPException(400, f"Firmware file not found: {firmware_file}")

    # Create job
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

    # Initialize device statuses
    for ip in ips:
        job.devices[ip] = DeviceStatus(ip=ip)

    update_jobs[job_id] = job

    # Broadcast job started
    await broadcast({
        "type": "job_started",
        "job_id": job_id,
        "device_count": len(ips),
        "firmware": firmware_file,
    })

    # Start update task in background
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
                # Map message to status
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

                # Schedule broadcast (can't await in sync callback)
                asyncio.create_task(broadcast({
                    "type": "device_update",
                    "job_id": job.job_id,
                    "ip": device_ip,
                    "status": device_status.status,
                    "message": message,
                }))

            # Perform update based on device type
            if job.device_type == "tachyon":
                client = TachyonClient(ip, job.username, job.password)
                result = await client.update_firmware(job.firmware_file, progress_callback)
            else:
                result = UpdateResult(ip=ip, success=False, error=f"Unsupported device type: {job.device_type}")

            # Update final status
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

    # Run all updates in parallel (limited by semaphore)
    await asyncio.gather(*[update_device(ip) for ip in job.devices.keys()])

    # Mark job complete
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


# ============================================================================
# Discovery & Topology API Endpoints
# ============================================================================

@app.post("/api/discover")
async def discover_network(
    ip_list: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    concurrency: int = Form(5),
):
    """Discover APs and their connected CPEs.

    Args:
        ip_list: Newline-separated list of AP IP addresses
        username: Login username
        password: Login password
        concurrency: Max concurrent discoveries

    Returns:
        NetworkTopology with all discovered APs and CPEs.
    """
    # Parse IP list
    ips = []
    for line in ip_list.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            ip = line.split(",")[0].strip()
            if ip:
                ips.append(ip)

    if not ips:
        raise HTTPException(400, "No valid IPs provided")

    # Run discovery
    topology = await network_discovery.discover_network(
        ips, username, password, concurrency
    )

    # Broadcast topology update
    await broadcast({
        "type": "topology_update",
        "topology": topology.to_dict(),
    })

    return topology.to_dict()


@app.get("/api/topology")
async def get_topology():
    """Get cached network topology.

    Returns:
        Cached NetworkTopology, or empty topology if not discovered yet.
    """
    topology = network_discovery.get_cached_topology()
    if topology:
        return topology.to_dict()
    return {"aps": [], "discovered_at": None, "total_aps": 0, "total_cpes": 0, "overall_health": {"green": 0, "yellow": 0, "red": 0}}


@app.post("/api/topology/refresh")
async def refresh_topology():
    """Refresh topology using cached AP list.

    Returns:
        Updated NetworkTopology.
    """
    topology = await network_discovery.refresh_topology()
    if not topology:
        raise HTTPException(400, "No cached topology to refresh. Run discovery first.")

    # Broadcast topology update
    await broadcast({
        "type": "topology_update",
        "topology": topology.to_dict(),
    })

    return topology.to_dict()


@app.get("/api/cpe/{ip}/metrics")
async def get_cpe_metrics(ip: str):
    """Get signal and distance metrics for a specific CPE.

    Args:
        ip: CPE IP address

    Returns:
        CPE metrics including signal health.
    """
    cpe = network_discovery.get_cpe_by_ip(ip)
    if not cpe:
        raise HTTPException(404, f"CPE not found: {ip}")
    return cpe.to_dict()


@app.get("/api/cpes")
async def get_all_cpes():
    """Get all CPEs from cached topology.

    Returns:
        List of all CPEs with their metrics.
    """
    cpes = network_discovery.get_all_cpes()
    return {"cpes": [cpe.to_dict() for cpe in cpes]}


# ============================================================================
# Page Routes
# ============================================================================

@app.get("/topology", response_class=HTMLResponse)
async def topology_page(request: Request):
    """Serve the topology tree view page."""
    return templates.TemplateResponse("topology.html", {"request": request})


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    """Serve the signal monitoring page."""
    return templates.TemplateResponse("monitor.html", {"request": request})


def main():
    """Run the application."""
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
