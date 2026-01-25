"""Tachyon Networks device firmware update client.

API Endpoints:
- POST /cgi.lua/login - Authenticate, returns token cookie
- GET /cgi.lua/bootbank - Get firmware bank info
- GET /cgi.lua/status?type=system - Get device info
- GET /cgi.lua/status?type=wireless,zones - Get connected peers (CPEs)
- PUT /cgi.lua/update - Upload firmware (multipart: fw=binary, force=false)
- POST /cgi.lua/update - Trigger firmware install (JSON: {reset:false, force:false})
"""

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List

logger = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    """Device information."""
    ip: str
    model: Optional[str] = None
    serial: Optional[str] = None
    mac: Optional[str] = None
    current_version: Optional[str] = None
    bank1_version: Optional[str] = None
    bank2_version: Optional[str] = None
    active_bank: Optional[int] = None


@dataclass
class UpdateResult:
    """Result of a firmware update."""
    ip: str
    success: bool
    old_version: Optional[str] = None
    new_version: Optional[str] = None
    error: Optional[str] = None


class TachyonClient:
    """Client for Tachyon device firmware updates."""

    def __init__(self, ip: str, username: str, password: str, timeout: int = 30):
        self.ip = ip
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token: Optional[str] = None
        self._base_url = f"https://{ip}"

    async def _curl(
        self,
        method: str,
        endpoint: str,
        data: Dict[str, Any] = None,
        form_data: Dict[str, str] = None,
        file_path: str = None,
        save_cookies: str = None,
    ) -> tuple[int, str]:
        """Execute curl command and return (status_code, response_body)."""
        url = f"{self._base_url}{endpoint}"

        cmd = ["curl", "-s", "-k", "-m", str(self.timeout)]

        # Method
        cmd.extend(["-X", method])

        # Auth cookie
        if self._token:
            cmd.extend(["-H", f"Cookie: token={self._token}"])

        # Save cookies to file
        if save_cookies:
            cmd.extend(["-c", save_cookies])

        # JSON data
        if data is not None:
            cmd.extend(["-H", "Content-Type: application/json"])
            cmd.extend(["-d", json.dumps(data)])

        # Multipart form data with file
        if file_path:
            cmd.extend(["-F", f"fw=@{file_path}"])
            if form_data:
                for key, value in form_data.items():
                    cmd.extend(["-F", f"{key}={value}"])

        # Add output format to get status code
        cmd.extend(["-w", "\n%{http_code}"])
        cmd.append(url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"curl failed: {stderr.decode()}")

        output = stdout.decode("utf-8", errors="ignore")
        lines = output.rsplit("\n", 2)

        if len(lines) >= 2:
            body = lines[0]
            status_code = int(lines[-1]) if lines[-1].isdigit() else 0
        else:
            body = output
            status_code = 200

        return status_code, body

    async def login(self) -> bool:
        """Authenticate with the device."""
        cookie_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        cookie_path = cookie_file.name
        cookie_file.close()

        try:
            url = f"{self._base_url}/cgi.lua/login"
            payload = json.dumps({
                "username": self.username,
                "password": self.password,
            })

            cmd = [
                "curl", "-s", "-k", "-m", str(self.timeout),
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-c", cookie_path,
                "-d", payload,
                url,
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"Login curl failed for {self.ip}: {stderr.decode()}")
                return False

            response = stdout.decode("utf-8", errors="ignore")

            # Check for auth failure
            try:
                data = json.loads(response)
                if data.get("statusCode") == 401 or data.get("auth") is False:
                    logger.error(f"Login failed for {self.ip}: Invalid credentials")
                    return False
            except json.JSONDecodeError:
                pass

            # Extract token from cookie file
            with open(cookie_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("\t")
                        if len(parts) >= 7 and parts[5] == "token":
                            self._token = parts[6]
                            logger.info(f"Logged in to {self.ip}")
                            return True

            logger.error(f"No token received from {self.ip}")
            return False

        finally:
            Path(cookie_path).unlink(missing_ok=True)

    async def get_device_info(self) -> DeviceInfo:
        """Get device information."""
        info = DeviceInfo(ip=self.ip)

        # Get system status
        status, body = await self._curl("GET", "/cgi.lua/status?type=system")
        if status == 200:
            try:
                data = json.loads(body)
                system = data.get("system", data)
                info.model = system.get("model")
                info.serial = system.get("serial")
                version = system.get("version", {})
                firmux = version.get("firmux", "")
                info.current_version = self._normalize_version(firmux)
            except json.JSONDecodeError:
                pass

        # Get bootbank info
        status, body = await self._curl("GET", "/cgi.lua/bootbank")
        if status == 200:
            try:
                data = json.loads(body)
                active = data.get("active", {})
                backup = data.get("backup", {})
                info.active_bank = active.get("bootbank", 1)

                active_ver = self._normalize_version(active.get("firmux", ""))
                backup_ver = self._normalize_version(backup.get("firmux", ""))

                if info.active_bank == 1:
                    info.bank1_version = active_ver
                    info.bank2_version = backup_ver
                else:
                    info.bank1_version = backup_ver
                    info.bank2_version = active_ver
            except json.JSONDecodeError:
                pass

        # Get MAC address
        status, body = await self._curl("GET", "/cgi.lua/status?type=interfaces")
        if status == 200:
            try:
                data = json.loads(body)
                mac = data.get("interfaces", {}).get("eth0", {}).get("mac_address")
                if mac:
                    info.mac = mac.upper()
            except json.JSONDecodeError:
                pass

        return info

    def _normalize_version(self, version_str: str) -> str:
        """Normalize version string: '1.12.3 rev 54970' -> '1.12.3.54970'"""
        if not version_str:
            return ""
        if " rev " in version_str:
            parts = version_str.split(" rev ")
            base = parts[0].strip()
            rev = parts[1].strip() if len(parts) > 1 else ""
            return f"{base}.{rev}" if rev else base
        return version_str.strip()

    async def upload_firmware(self, firmware_path: str) -> bool:
        """Upload firmware file to device using PUT."""
        logger.info(f"Uploading firmware to {self.ip}")

        url = f"{self._base_url}/cgi.lua/update"
        cmd = [
            "curl", "-s", "-k", "-m", "300",  # 5 min timeout for upload
            "-X", "PUT",
            "-F", f"fw=@{firmware_path}",
            "-F", "force=false",
        ]

        if self._token:
            cmd.extend(["-H", f"Cookie: token={self._token}"])

        cmd.append(url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(f"Firmware upload failed for {self.ip}: {stderr.decode()}")
            return False

        response = stdout.decode("utf-8", errors="ignore")
        logger.debug(f"Upload response from {self.ip}: {response}")

        # Check for error in response
        try:
            data = json.loads(response)
            if data.get("error"):
                logger.error(f"Upload error from {self.ip}: {data}")
                return False
        except json.JSONDecodeError:
            pass

        logger.info(f"Firmware uploaded to {self.ip}")
        return True

    async def trigger_update(self) -> bool:
        """Trigger firmware installation (device will reboot)."""
        logger.info(f"Triggering firmware update on {self.ip}")

        status, body = await self._curl(
            "POST",
            "/cgi.lua/update",
            data={"reset": False, "force": False}
        )

        # Device may reboot before responding, so empty response is OK
        logger.info(f"Update triggered on {self.ip}")
        return True

    async def wait_for_reboot(self, timeout: int = 180) -> bool:
        """Wait for device to come back online after reboot."""
        logger.info(f"Waiting for {self.ip} to reboot...")

        # Initial wait for device to go down
        await asyncio.sleep(10)

        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            # Try to ping
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", "2", self.ip,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

            if proc.returncode == 0:
                # Device responding to ping, check web server
                await asyncio.sleep(5)

                check_cmd = [
                    "curl", "-s", "-k", "-m", "5",
                    "-o", "/dev/null", "-w", "%{http_code}",
                    f"https://{self.ip}/"
                ]
                proc = await asyncio.create_subprocess_exec(
                    *check_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()

                if proc.returncode == 0:
                    status = stdout.decode().strip()
                    if status and status.isdigit() and int(status) > 0:
                        logger.info(f"{self.ip} is back online")
                        return True

            await asyncio.sleep(3)

        logger.error(f"{self.ip} did not come back online within {timeout}s")
        return False

    async def update_firmware(
        self,
        firmware_path: str,
        progress_callback: Callable[[str, str], None] = None
    ) -> UpdateResult:
        """Perform complete firmware update cycle.

        Args:
            firmware_path: Path to firmware file
            progress_callback: Optional callback(ip, status_message)

        Returns:
            UpdateResult with success/failure details
        """
        def progress(msg: str):
            if progress_callback:
                progress_callback(self.ip, msg)
            logger.info(f"[{self.ip}] {msg}")

        result = UpdateResult(ip=self.ip, success=False)

        try:
            # Login
            progress("Logging in...")
            if not await self.login():
                result.error = "Login failed"
                return result

            # Get current info
            progress("Getting device info...")
            info = await self.get_device_info()
            result.old_version = info.current_version
            progress(f"Current version: {info.current_version}")

            # Upload firmware
            progress("Uploading firmware...")
            if not await self.upload_firmware(firmware_path):
                result.error = "Firmware upload failed"
                return result

            # Trigger update (device reboots)
            progress("Installing firmware...")
            if not await self.trigger_update():
                result.error = "Failed to trigger update"
                return result

            # Wait for reboot
            progress("Rebooting...")
            if not await self.wait_for_reboot():
                result.error = "Device did not come back online"
                return result

            # Re-login and verify
            progress("Verifying...")
            if not await self.login():
                result.error = "Failed to reconnect after reboot"
                return result

            new_info = await self.get_device_info()
            result.new_version = new_info.current_version

            if result.new_version and result.new_version != result.old_version:
                progress(f"Updated: {result.old_version} -> {result.new_version}")
                result.success = True
            else:
                progress(f"Version unchanged: {result.new_version}")
                result.error = "Firmware version did not change"

        except Exception as e:
            result.error = str(e)
            logger.exception(f"Update failed for {self.ip}")

        return result

    async def get_connected_cpes(self) -> List[Dict[str, Any]]:
        """Query /cgi.lua/status?type=wireless,zones for connected peers (CPEs).

        Returns:
            List of CPE dictionaries with signal/distance data.
        """
        from .models import CPEInfo

        cpes = []
        status, body = await self._curl("GET", "/cgi.lua/status?type=wireless,zones")

        if status != 200:
            logger.warning(f"Failed to get wireless status from {self.ip}: HTTP {status}")
            return cpes

        try:
            data = json.loads(body)
            wireless = data.get("wireless", {})
            peers = wireless.get("peers", [])

            for peer in peers:
                cpe = CPEInfo(
                    ip=peer.get("ipv4", ""),
                    mac=peer.get("mac"),
                    system_name=peer.get("system_name"),
                    model=peer.get("model"),
                    firmware_version=peer.get("fw"),
                    link_distance=peer.get("linkDistance"),
                    rx_power=peer.get("rxPower"),
                    combined_signal=peer.get("combinedSignal"),
                    last_local_rssi=peer.get("lastLocalRssi"),
                    tx_rate=peer.get("txRate"),
                    rx_rate=peer.get("rxRate"),
                    mcs=peer.get("mcs"),
                    link_uptime=peer.get("linkUptime"),
                )
                cpes.append(cpe)

            logger.info(f"Found {len(cpes)} connected CPEs on {self.ip}")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse wireless status from {self.ip}: {e}")

        return cpes

    async def get_ap_info(self) -> Dict[str, Any]:
        """Get AP information including system name and model.

        Returns:
            Dictionary with AP info.
        """
        info = {
            "ip": self.ip,
            "mac": None,
            "system_name": None,
            "model": None,
            "firmware_version": None,
        }

        # Get system status
        status, body = await self._curl("GET", "/cgi.lua/status?type=system")
        if status == 200:
            try:
                data = json.loads(body)
                system = data.get("system", data)
                info["model"] = system.get("model")
                info["system_name"] = system.get("name") or system.get("hostname")
                version = system.get("version", {})
                firmux = version.get("firmux", "")
                info["firmware_version"] = self._normalize_version(firmux)
            except json.JSONDecodeError:
                pass

        # Get MAC address
        status, body = await self._curl("GET", "/cgi.lua/status?type=interfaces")
        if status == 200:
            try:
                data = json.loads(body)
                mac = data.get("interfaces", {}).get("eth0", {}).get("mac_address")
                if mac:
                    info["mac"] = mac.upper()
            except json.JSONDecodeError:
                pass

        return info
