"""Tachyon Networks device firmware update client.

API Endpoints:
- POST /cgi.lua/login - Authenticate, returns token cookie
- GET /cgi.lua/bootbank - Get firmware bank info
- GET /cgi.lua/status?type=system - Get device info
- GET /cgi.lua/status?type=wireless,zones - Get connected peers (CPEs)
- PUT /cgi.lua/update - Upload firmware (multipart: fw=binary, force=false)
- POST /cgi.lua/update - Trigger firmware install (JSON: {reset:false, force:false})
- POST /reboot - Reboot device

Security Note:
    By default, SSL certificate verification is disabled (-k flag) because Tachyon
    network devices use self-signed certificates. This is standard for network device
    management software. Set TACHYON_VERIFY_SSL=1 to enable strict verification if
    your devices have valid certificates from a trusted CA.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List

logger = logging.getLogger(__name__)

# SSL verification for device connections (disabled by default for self-signed certs)
VERIFY_SSL = os.environ.get("TACHYON_VERIFY_SSL", "").lower() in ("1", "true", "yes")


def _extract_version_from_firmware(firmware_path: str) -> str:
    """Extract normalized version from firmware filename.

    'tna-30x-2.5.1-r54970.bin' -> '2.5.1.54970'
    """
    filename = Path(firmware_path).name
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


def _normalize_version(version: str) -> str:
    """Normalize version string for comparison.

    '1.12.4.r7782' -> '1.12.4.7782'
    """
    return version.replace(".r", ".") if version else ""


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
    skipped: bool = False
    bank1_version: Optional[str] = None
    bank2_version: Optional[str] = None
    active_bank: Optional[int] = None
    model: Optional[str] = None


@dataclass
class SmokeTestResult:
    """Result of post-update smoke tests."""
    passed: bool = True
    warnings: list = field(default_factory=list)
    checks: list = field(default_factory=list)


class TachyonClient:
    """Client for Tachyon device firmware updates."""

    # CONTROL file content per hardware model (for config .tar downloads)
    # TODO: Verify these values for each model - defaulting to "tn-110-prs"
    MODEL_HARDWARE_IDS = {
        "tna-301": "tn-110-prs",
        "tna-302": "tn-110-prs",
        "tna-303x": "tn-110-prs",
        "tna-303l": "tn-110-prs",
        "tna-303l-65": "tn-110-prs",
        "tns-100": "tn-110-prs",
    }

    # Firmware pattern mappings for model validation
    MODEL_FIRMWARE_PATTERNS = {
        # TNA-30x standard series uses tna-30x firmware
        "tna-301": ["tna-30x", "tna30x"],
        "tna-302": ["tna-30x", "tna30x"],
        "tna-303x": ["tna-30x", "tna30x"],
        # TNA-303L series uses tna-303l firmware
        "tna-303l": ["tna-303l", "tna303l"],
        "tna-303l-65": ["tna-303l", "tna303l"],
        # TNS-100 series uses tns-100 firmware
        "tns-100": ["tns-100", "tns100"],
    }

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

        cmd = ["curl", "-s", "-m", str(self.timeout)]
        if not VERIFY_SSL:
            cmd.append("-k")  # Skip certificate verification for self-signed certs

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
        cookie_path = None
        try:
            cookie_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            cookie_path = cookie_file.name
            cookie_file.close()

            url = f"{self._base_url}/cgi.lua/login"
            payload = json.dumps({
                "username": self.username,
                "password": self.password,
            })

            cmd = ["curl", "-s", "-m", str(self.timeout)]
            if not VERIFY_SSL:
                cmd.append("-k")
            cmd.extend([
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-c", cookie_path,
                "-d", payload,
                url,
            ])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode().strip()
                logger.error(f"Login curl failed for {self.ip}: {error_msg}")
                return "Device not reachable"

            response = stdout.decode("utf-8", errors="ignore")

            # Check for auth failure
            try:
                data = json.loads(response)
                if data.get("statusCode") == 401 or "Authorization Failed" in str(data):
                    logger.error(f"Login failed for {self.ip}: Invalid credentials")
                    return "Invalid credentials"
                if data.get("auth") is False:
                    logger.error(f"Login failed for {self.ip}: auth=false in response")
                    return "Invalid credentials"
            except json.JSONDecodeError:
                pass

            # Check raw response for error messages
            if "Authorization Failed" in response or "Invalid credentials" in response:
                logger.error(f"Login failed for {self.ip}: {response[:200]}")
                return "Invalid credentials"

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
            return "Login failed: no token received"

        finally:
            if cookie_path:
                try:
                    Path(cookie_path).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning(f"Failed to clean up cookie file {cookie_path}: {e}")

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

    def validate_firmware_for_model(self, firmware_path: str, model: str) -> tuple[bool, str]:
        """Validate that firmware file is compatible with the device model.

        Args:
            firmware_path: Path to firmware file.
            model: Device model string (e.g., "TNA-303L-65").

        Returns:
            Tuple of (is_valid, error_message). error_message is empty if valid.
        """
        import os
        filename = os.path.basename(firmware_path).lower()
        model_key = model.lower()

        patterns = self.MODEL_FIRMWARE_PATTERNS.get(model_key)
        if not patterns:
            logger.debug(f"No firmware pattern defined for model {model}, allowing any firmware")
            return True, ""

        for pattern in patterns:
            if pattern in filename:
                logger.debug(f"Firmware {filename} matches pattern {pattern} for model {model}")
                return True, ""

        expected = " or ".join(patterns)
        return False, f"Firmware mismatch: model {model} requires firmware with '{expected}' in filename, but got '{filename}'"

    async def upload_firmware(self, firmware_path: str) -> bool:
        """Upload firmware file to device using PUT."""
        logger.info(f"Uploading firmware to {self.ip}")

        url = f"{self._base_url}/cgi.lua/update"
        cmd = ["curl", "-s", "-m", "300"]  # 5 min timeout for upload
        if not VERIFY_SSL:
            cmd.append("-k")
        cmd.extend([
            "-X", "PUT",
            "-F", f"fw=@{firmware_path}",
            "-F", "force=false",
        ])

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
            if isinstance(data, dict):
                if data.get("error") or data.get("statusCode", 200) >= 400:
                    logger.error(f"Upload error from {self.ip}: {data}")
                    return False
        except json.JSONDecodeError:
            pass

        logger.info(f"Firmware uploaded to {self.ip}")
        return True

    async def trigger_update(self) -> bool:
        """Trigger firmware installation (device will reboot)."""
        logger.info(f"Triggering firmware update on {self.ip}")

        try:
            status, body = await self._curl(
                "POST",
                "/cgi.lua/update",
                data={"reset": False, "force": False}
            )

            # Check for errors in response (device may reboot before responding)
            if body:
                try:
                    data = json.loads(body)
                    if isinstance(data, dict):
                        if data.get("error"):
                            logger.error(f"Update trigger error from {self.ip}: {data.get('error')}")
                            return False
                        if data.get("statusCode", 200) >= 400:
                            logger.error(f"Update trigger failed with status: {data}")
                            return False
                except json.JSONDecodeError:
                    pass

            logger.info(f"Update triggered on {self.ip}")
            return True
        except Exception:
            # Connection error expected during reboot
            logger.info(f"Update triggered on {self.ip} (connection closed)")
            return True

    async def wait_for_reboot(self, timeout: int = 300) -> bool:
        """Wait for device to come back online after reboot.

        Two-phase approach:
        1. Wait for ping response
        2. Wait for web server to be ready
        """
        logger.info(f"Waiting for {self.ip} to reboot...")

        # Initial wait for device to go down
        await asyncio.sleep(10)

        start_time = asyncio.get_event_loop().time()
        ping_responded = False

        while asyncio.get_event_loop().time() - start_time < timeout:
            if not ping_responded:
                # Phase 1: Wait for device to respond to ping (or curl if ping unavailable)
                responded = False
                proc = None
                use_curl_fallback = False
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ping", "-c", "1", "-W", "2", self.ip,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if proc.returncode == 0:
                        responded = True
                    elif b"Operation not permitted" in stderr or b"not permitted" in stderr:
                        # ping failed due to missing capabilities, use curl instead
                        use_curl_fallback = True
                except FileNotFoundError:
                    # ping not available (e.g., minimal Docker image), use curl instead
                    use_curl_fallback = True

                if use_curl_fallback:
                    try:
                        check_cmd = ["curl", "-s", "-m", "3"]
                        if not VERIFY_SSL:
                            check_cmd.append("-k")
                        check_cmd.extend([
                            "-o", "/dev/null", "-w", "%{http_code}",
                            f"https://{self.ip}/"
                        ])
                        proc = await asyncio.create_subprocess_exec(
                            *check_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        stdout, _ = await proc.communicate()
                        if proc.returncode == 0:
                            status = stdout.decode().strip()
                            responded = status and status.isdigit() and int(status) > 0
                    except Exception:
                        if proc and proc.returncode is None:
                            try:
                                proc.kill()
                                await proc.wait()
                            except ProcessLookupError:
                                pass

                if responded:
                    logger.info(f"{self.ip} responding, waiting for web server...")
                    ping_responded = True
                    # Wait for web services to initialize
                    await asyncio.sleep(10)
                    continue
            else:
                # Phase 2: Check if web server is up
                check_cmd = ["curl", "-s", "-m", "5"]
                if not VERIFY_SSL:
                    check_cmd.append("-k")
                check_cmd.extend([
                    "-o", "/dev/null", "-w", "%{http_code}",
                    f"https://{self.ip}/"
                ])
                proc = None
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *check_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    stdout, _ = await proc.communicate()

                    if proc.returncode == 0:
                        status = stdout.decode().strip()
                        if status and status.isdigit() and int(status) > 0:
                            logger.info(f"{self.ip} web server is up, device ready")
                            return True
                except Exception:
                    if proc and proc.returncode is None:
                        try:
                            proc.kill()
                            await proc.wait()
                        except ProcessLookupError:
                            pass

            await asyncio.sleep(3)

        logger.error(f"{self.ip} did not come back online within {timeout}s")
        return False

    async def reboot(self, timeout: int = 300) -> bool:
        """Reboot the device and wait for it to come back online."""
        logger.info(f"Rebooting {self.ip}...")
        try:
            await self._curl("POST", "/reboot")
        except Exception:
            # Connection may drop immediately during reboot
            logger.info(f"Reboot triggered on {self.ip} (connection closed)")
        return await self.wait_for_reboot(timeout=timeout)

    async def update_firmware(
        self,
        firmware_path: str,
        progress_callback: Callable[[str, str], None] = None,
        pass_number: int = 1,
        reboot_timeout: int = 300,
    ) -> UpdateResult:
        """Perform complete firmware update cycle.

        Args:
            firmware_path: Path to firmware file
            progress_callback: Optional callback(ip, status_message)
            pass_number: Update pass (1 or 2). On pass 2, version unchanged
                         is treated as success (both banks now match).

        Returns:
            UpdateResult with success/failure details
        """
        def progress(msg: str):
            if progress_callback:
                progress_callback(self.ip, msg)
            logger.info(f"[{self.ip}] {msg}")

        result = UpdateResult(ip=self.ip, success=False)

        try:
            # Login (with retries for connectivity errors, e.g. CPE reassociating after AP reboot)
            login_result = None
            max_login_retries = 20
            login_retry_delay = 15  # 20 * 15s = 5 minutes max
            for attempt in range(1, max_login_retries + 1):
                progress(f"Logging in...{f' (attempt {attempt}/{max_login_retries})' if attempt > 1 else ''}")
                login_result = await self.login()
                if login_result is True:
                    break
                # Don't retry auth failures — only connectivity issues
                if isinstance(login_result, str) and "credentials" in login_result.lower():
                    break
                if attempt < max_login_retries:
                    progress(f"Device not reachable, retrying in {login_retry_delay}s... (attempt {attempt}/{max_login_retries})")
                    await asyncio.sleep(login_retry_delay)
            if login_result is not True:
                result.error = login_result if isinstance(login_result, str) else "Login failed"
                return result

            # Get current info
            progress("Getting device info...")
            info = await self.get_device_info()
            result.old_version = info.current_version
            result.model = info.model
            result.bank1_version = info.bank1_version
            result.bank2_version = info.bank2_version
            result.active_bank = info.active_bank
            progress(f"Current version: {info.current_version}")

            # Validate firmware is compatible with device model
            if info.model:
                valid, error_msg = self.validate_firmware_for_model(firmware_path, info.model)
                if not valid:
                    result.error = error_msg
                    progress(f"Firmware validation failed: {error_msg}")
                    return result

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
            if not await self.wait_for_reboot(timeout=reboot_timeout):
                result.error = "Device did not come back online"
                return result

            # Re-login and verify
            progress("Verifying...")
            login_result = await self.login()
            if login_result is not True:
                result.error = "Failed to reconnect after reboot"
                return result

            new_info = await self.get_device_info()
            result.new_version = new_info.current_version
            result.bank1_version = new_info.bank1_version
            result.bank2_version = new_info.bank2_version
            result.active_bank = new_info.active_bank

            # Extract and normalize versions for comparison
            target_version = _extract_version_from_firmware(firmware_path)
            new_version_normalized = _normalize_version(result.new_version or "")
            old_version_normalized = _normalize_version(result.old_version or "")

            # Verify against target firmware version
            if target_version and new_version_normalized == target_version:
                # Device is now running target firmware
                if old_version_normalized != target_version:
                    progress(f"Updated: {result.old_version} -> {result.new_version}")
                elif pass_number >= 2:
                    progress(f"Updated: both banks now on {result.new_version}")
                else:
                    progress(f"Verified: already on {result.new_version}")
                    result.skipped = True
                result.success = True
            elif target_version:
                # Device is NOT running target firmware - update failed
                result.error = f"Version mismatch: expected {target_version}, got {result.new_version}"
                progress(f"Failed: {result.error}")
            else:
                # No target version to compare - fall back to old behavior
                if result.new_version and result.new_version != result.old_version:
                    progress(f"Updated: {result.old_version} -> {result.new_version}")
                    result.success = True
                elif pass_number >= 2:
                    progress(f"Updated: both banks now on {result.new_version}")
                    result.success = True
                else:
                    progress(f"Skipped: already on {result.new_version}")
                    result.skipped = True
                    result.success = True

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
        """Get AP information including system name, model, and location.

        Returns:
            Dictionary with AP info.
        """
        info = {
            "ip": self.ip,
            "mac": None,
            "system_name": None,
            "model": None,
            "firmware_version": None,
            "location": None,
            "latitude": None,
            "longitude": None,
            "zone": None,
        }

        # Get system status
        status, body = await self._curl("GET", "/cgi.lua/status?type=system")
        if status == 200:
            try:
                data = json.loads(body)
                system = data.get("system", data)
                general = system.get("general", {})

                # Basic info
                info["model"] = system.get("model")
                info["serial"] = system.get("serial")

                # Name/hostname from general section
                info["system_name"] = general.get("name") or general.get("hostname") or system.get("name")

                # Firmware version
                version = system.get("version", {})
                firmux = version.get("firmux", "")
                info["firmware_version"] = self._normalize_version(firmux)

                # Location from general section
                info["location"] = general.get("location") or system.get("location") or system.get("site")
                info["latitude"] = general.get("latitude") or system.get("latitude")
                info["longitude"] = general.get("longitude") or system.get("longitude")
            except json.JSONDecodeError:
                pass

        # Get zone/location from zones endpoint
        status, body = await self._curl("GET", "/cgi.lua/status?type=zones")
        if status == 200:
            try:
                data = json.loads(body)
                zones = data.get("zones", {})
                # Get the first zone name as the zone identifier
                if zones:
                    zone_names = list(zones.keys())
                    if zone_names:
                        info["zone"] = zone_names[0]
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

    async def get_config(self) -> Optional[dict]:
        """Download full device config via GET /cgi.lua/apiv1/config."""
        status, body = await self._curl("GET", "/cgi.lua/apiv1/config")
        if status == 200:
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse config from {self.ip}")
                return None
        logger.warning(f"Failed to get config from {self.ip}: HTTP {status}")
        return None

    async def run_smoke_tests(self, role: str = "ap", pre_update_cpe_count: int = 0) -> SmokeTestResult:
        """Run post-update smoke tests on a device.

        Args:
            role: Device role ("ap", "cpe", "switch").
            pre_update_cpe_count: Number of CPEs connected before update (AP only).

        Returns:
            SmokeTestResult with pass/fail and any warnings.
        """
        result = SmokeTestResult()

        # Check 1: Device responds to status query
        try:
            info = await self.get_device_info()
            if info and info.current_version:
                result.checks.append({"check": "device_responsive", "passed": True, "detail": f"Running {info.current_version}"})
            else:
                result.warnings.append("Device did not return valid status info")
                result.checks.append({"check": "device_responsive", "passed": False, "detail": "No version info returned"})
        except Exception as e:
            result.warnings.append(f"Device status check failed: {e}")
            result.checks.append({"check": "device_responsive", "passed": False, "detail": str(e)})

        # Check 2: Configuration readable
        try:
            config = await self.get_config()
            if config is not None:
                result.checks.append({"check": "config_intact", "passed": True, "detail": "Config readable"})
            else:
                result.warnings.append("Could not read device configuration")
                result.checks.append({"check": "config_intact", "passed": False, "detail": "Config returned None"})
        except Exception as e:
            result.warnings.append(f"Config check failed: {e}")
            result.checks.append({"check": "config_intact", "passed": False, "detail": str(e)})

        # Check 3: Connected CPEs (APs only)
        if role == "ap":
            try:
                cpes = await self.get_connected_cpes()
                cpe_count = len(cpes)

                if pre_update_cpe_count > 0 and cpe_count == 0:
                    result.warnings.append(f"AP had {pre_update_cpe_count} CPEs before update, now has 0")
                    result.checks.append({"check": "cpe_connectivity", "passed": False, "detail": f"0/{pre_update_cpe_count} CPEs connected"})
                elif pre_update_cpe_count > 0 and cpe_count < pre_update_cpe_count:
                    result.warnings.append(f"CPE count dropped: {pre_update_cpe_count} -> {cpe_count}")
                    result.checks.append({"check": "cpe_connectivity", "passed": False, "detail": f"{cpe_count}/{pre_update_cpe_count} CPEs connected"})
                else:
                    result.checks.append({"check": "cpe_connectivity", "passed": True, "detail": f"{cpe_count} CPEs connected"})

                # Check 4: Signal levels on connected CPEs
                low_signal_cpes = []
                for cpe in cpes:
                    signal = getattr(cpe, "combined_signal", None)
                    if signal is None or signal == 0:
                        signal = getattr(cpe, "last_local_rssi", None)
                    if signal is not None and signal != 0:
                        try:
                            signal_val = float(signal)
                            if signal_val < -80:
                                low_signal_cpes.append(f"{getattr(cpe, 'ip', '?')} ({signal_val}dBm)")
                        except (ValueError, TypeError):
                            pass

                if low_signal_cpes:
                    result.warnings.append(f"Low signal CPEs: {', '.join(low_signal_cpes)}")
                    result.checks.append({"check": "cpe_signal_levels", "passed": False, "detail": f"{len(low_signal_cpes)} CPEs with low signal"})
                elif cpes:
                    result.checks.append({"check": "cpe_signal_levels", "passed": True, "detail": "All signals OK"})
            except Exception as e:
                result.warnings.append(f"CPE connectivity check failed: {e}")
                result.checks.append({"check": "cpe_connectivity", "passed": False, "detail": str(e)})

        if result.warnings:
            result.passed = False

        return result

    async def apply_config(self, config: dict, dry_run: bool = False) -> dict:
        """Apply full config to device via POST /cgi.lua/apiv1/config.

        Args:
            config: Full device configuration dict.
            dry_run: If True, validate without applying.

        Returns:
            Dict with 'success' bool and response details.
        """
        endpoint = "/cgi.lua/apiv1/config"
        if dry_run:
            endpoint += "?dry_run=true"
        status, body = await self._curl("POST", endpoint, data=config)
        result = {"success": status == 200, "status_code": status}
        try:
            result.update(json.loads(body))
        except json.JSONDecodeError:
            result["raw_response"] = body
        return result

    def get_hardware_id(self, model: str) -> str:
        """Get the CONTROL file hardware ID for a device model."""
        if model:
            return self.MODEL_HARDWARE_IDS.get(model.lower(), "tn-110-prs")
        return "tn-110-prs"
