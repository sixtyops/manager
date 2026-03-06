"""Background poller for refreshing AP/CPE data."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Callable, Optional

from . import database as db
from . import radius_config
from .tachyon import TachyonClient
from .models import SignalHealth

logger = logging.getLogger(__name__)

# Global poller instance
_poller: Optional["NetworkPoller"] = None


class NetworkPoller:
    """Background service that polls APs for CPE data."""

    def __init__(self, broadcast_func: Callable = None, poll_interval: int = 60):
        self.poll_interval = poll_interval
        self.broadcast_func = broadcast_func
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._clients: dict[str, TachyonClient] = {}  # IP -> authenticated client
        self._last_config_poll: Optional[datetime] = None

    async def start(self):
        """Start the background polling loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Network poller started (interval: {self.poll_interval}s)")

    async def stop(self):
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Network poller stopped")

    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_all_aps()
                await self._poll_all_switches()

                # Check if it's time for a config poll
                await self._maybe_poll_configs()
            except Exception as e:
                logger.exception(f"Error in poll loop: {e}")

            await asyncio.sleep(self.poll_interval)

    def _evict_stale_clients(self):
        """Remove cached clients for devices no longer in the database."""
        known_ips = db.get_all_device_ips()
        stale = [ip for ip in self._clients if ip not in known_ips]
        for ip in stale:
            del self._clients[ip]
        if stale:
            logger.info(f"Evicted {len(stale)} stale client(s) from cache")

    async def _poll_all_aps(self):
        """Poll all enabled APs."""
        self._evict_stale_clients()
        aps = db.get_access_points(enabled_only=True)

        if not aps:
            return

        logger.debug(f"Polling {len(aps)} APs")

        # Poll in parallel with concurrency limit
        semaphore = asyncio.Semaphore(5)

        async def poll_with_limit(ap):
            async with semaphore:
                await self._poll_ap(ap)

        await asyncio.gather(*[poll_with_limit(ap) for ap in aps], return_exceptions=True)

        # Broadcast updated topology
        if self.broadcast_func:
            topology = self.get_topology()
            await self.broadcast_func({
                "type": "topology_update",
                "topology": topology,
            })

    async def _poll_ap(self, ap: dict):
        """Poll a single AP for CPE data."""
        ip = ap["ip"]

        try:
            # Get or create authenticated client
            client, error = await self._get_client(ip, ap["username"], ap["password"])

            if not client:
                db.update_ap_status(ip, last_error=error)
                return

            # Get AP info
            ap_info = await client.get_ap_info()

            # Detect stale session: if login succeeded but API returned no data,
            # the token likely expired on the device side.  Invalidate the cached
            # client, re-authenticate, and retry once.
            if not ap_info.get("model") and not ap_info.get("firmware_version") and not ap_info.get("system_name"):
                logger.info(f"Stale session detected for {ip}, re-authenticating")
                self._clients.pop(ip, None)
                client, error = await self._get_client(ip, ap["username"], ap["password"])
                if not client:
                    db.update_ap_status(ip, last_error=error)
                    return
                ap_info = await client.get_ap_info()
            location = ap_info.get("location")  # Don't fall back to zone

            # Auto-assign to site based on location
            site_id = ap.get("tower_site_id")
            if location:
                # Check if we need to create/update site assignment
                current_site = None
                if site_id:
                    current_site = db.get_tower_site(site_id)

                # Update site if location changed or no site assigned
                if not site_id or (current_site and current_site["name"].lower() != location.lower()):
                    new_site_id = self._get_or_create_site(location)
                    if new_site_id:
                        site_id = new_site_id
                        db.upsert_access_point(
                            ip, ap["username"], ap["password"], site_id
                        )

            # Fetch bank info for AP
            bank_kwargs = {}
            try:
                device_info = await client.get_device_info()
                bank_kwargs = {
                    "bank1_version": device_info.bank1_version,
                    "bank2_version": device_info.bank2_version,
                    "active_bank": device_info.active_bank,
                }
            except Exception as e:
                logger.debug(f"Failed to get bank info for AP {ip}: {e}")

            db.update_ap_status(
                ip,
                last_seen=datetime.now().isoformat(),
                last_error=None,
                system_name=ap_info.get("system_name"),
                model=ap_info.get("model"),
                mac=ap_info.get("mac"),
                firmware_version=ap_info.get("firmware_version"),
                location=location,
                **bank_kwargs,
            )

            # Get connected CPEs
            cpes = await client.get_connected_cpes()

            # Clear old CPEs and insert new ones
            db.clear_cpes_for_ap(ip)

            for cpe in cpes:
                cpe_data = {
                    "ip": cpe.ip,
                    "mac": cpe.mac,
                    "system_name": cpe.system_name,
                    "model": cpe.model,
                    "firmware_version": cpe.firmware_version,
                    "link_distance": cpe.link_distance,
                    "rx_power": cpe.rx_power,
                    "combined_signal": cpe.combined_signal,
                    "last_local_rssi": cpe.last_local_rssi,
                    "tx_rate": cpe.tx_rate,
                    "rx_rate": cpe.rx_rate,
                    "mcs": cpe.mcs,
                    "link_uptime": cpe.link_uptime,
                    "signal_health": cpe.signal_health.value,
                }
                db.upsert_cpe(ip, cpe_data)

            logger.debug(f"Polled {ip}: {len(cpes)} CPEs")

            # Probe CPE auth and fetch bank info concurrently
            cpe_ips_to_probe = [cpe.ip for cpe in cpes if cpe.ip]
            if cpe_ips_to_probe:
                cpe_sem = asyncio.Semaphore(3)

                async def probe_cpe(cpe_ip):
                    async with cpe_sem:
                        status = await self._check_cpe_auth(
                            cpe_ip, ap["username"], ap["password"]
                        )
                        db.update_cpe_auth_status(ip, cpe_ip, status)

                        # Fetch bank info if auth OK and not fetched recently
                        if status == "ok":
                            await self._maybe_fetch_cpe_banks(
                                ip, cpe_ip, ap["username"], ap["password"]
                            )

                await asyncio.gather(
                    *[probe_cpe(cpe_ip) for cpe_ip in cpe_ips_to_probe],
                    return_exceptions=True,
                )

        except Exception as e:
            logger.error(f"Error polling {ip}: {e}")
            db.update_ap_status(ip, last_error=str(e))
            # Remove cached client on error
            self._clients.pop(ip, None)

    async def _get_client(self, ip: str, username: str, password: str) -> tuple:
        """Get authenticated client, reusing existing session if possible.

        Returns (client, None) on success or (None, error_string) on failure.
        """
        if ip in self._clients:
            return self._clients[ip], None

        client = TachyonClient(ip, username, password)

        result = await client.login()
        if result is True:
            self._clients[ip] = client
            return client, None

        return None, result if isinstance(result, str) else "Login failed"

    def _get_or_create_site(self, location: str) -> Optional[int]:
        """Get or create a tower site by location name."""
        if not location:
            return None

        # Check if site exists
        existing = db.get_tower_site_by_name(location)
        if existing:
            return existing["id"]

        # Create new site
        try:
            site_id = db.create_tower_site(location)
            logger.info(f"Auto-created tower site: {location}")
            return site_id
        except Exception as e:
            logger.error(f"Failed to create site {location}: {e}")
            return None

    async def _check_cpe_auth(self, cpe_ip: str, username: str, password: str) -> str:
        """Check if we can authenticate to a CPE.

        Tries authentication in order:
        1. Parent AP's credentials
        2. Global default device credentials (if enabled)

        Returns "ok", "failed", or "unreachable".
        """
        # Get effective credentials (device-specific or global defaults)
        effective_user, effective_pass = radius_config.get_device_credentials(username, password)

        try:
            client = TachyonClient(cpe_ip, effective_user, effective_pass, timeout=10)
            result = await client.login()
            if result is True:
                return "ok"
            if isinstance(result, str) and "not reachable" in result.lower():
                return "unreachable"

            # If AP credentials failed, try global defaults as fallback
            if effective_user == username and radius_config.is_device_auth_enabled():
                default_config = radius_config.get_device_auth_config()
                if default_config.username != username:  # Don't retry same creds
                    logger.debug(f"Trying global default credentials for CPE {cpe_ip}")
                    client = TachyonClient(cpe_ip, default_config.username, default_config.password, timeout=10)
                    result = await client.login()
                    if result is True:
                        return "ok"

            return "failed"
        except Exception as e:
            logger.debug(f"CPE auth probe failed for {cpe_ip}: {e}")
            return "unreachable"

    async def _maybe_fetch_cpe_banks(self, ap_ip: str, cpe_ip: str,
                                      username: str, password: str):
        """Fetch CPE bank info if newly discovered or >24h since last fetch."""
        try:
            last_fetched = db.get_cpe_bank_last_fetched(ap_ip, cpe_ip)
            if last_fetched:
                age = (datetime.now() - datetime.fromisoformat(last_fetched)).total_seconds()
                if age < 86400:  # 24 hours
                    return

            client = TachyonClient(cpe_ip, username, password, timeout=15)
            result = await client.login()
            if result is not True:
                return

            info = await client.get_device_info()
            if info.bank1_version or info.bank2_version:
                db.update_cpe_bank_info(
                    ap_ip, cpe_ip,
                    info.bank1_version, info.bank2_version, info.active_bank
                )
                logger.debug(f"Fetched bank info for CPE {cpe_ip}: "
                           f"B1={info.bank1_version} B2={info.bank2_version} "
                           f"active={info.active_bank}")
        except Exception as e:
            logger.debug(f"Failed to get bank info for CPE {cpe_ip}: {e}")

    def invalidate_client(self, ip: str):
        """Remove cached client (e.g., when credentials change)."""
        self._clients.pop(ip, None)

    async def poll_ap_now(self, ip: str) -> bool:
        """Trigger immediate poll of a specific AP."""
        ap = db.get_access_point(ip)
        if not ap:
            return False

        await self._poll_ap(ap)
        refreshed = db.get_access_point(ip)

        # Broadcast update
        if self.broadcast_func:
            topology = self.get_topology()
            await self.broadcast_func({
                "type": "topology_update",
                "topology": topology,
            })

        return bool(refreshed and not refreshed.get("last_error"))

    async def _poll_all_switches(self):
        """Poll all enabled switches."""
        switches = db.get_switches(enabled_only=True)
        if not switches:
            return

        logger.debug(f"Polling {len(switches)} switches")

        semaphore = asyncio.Semaphore(5)

        async def poll_with_limit(sw):
            async with semaphore:
                await self._poll_switch(sw)

        await asyncio.gather(*[poll_with_limit(sw) for sw in switches], return_exceptions=True)

    async def _poll_switch(self, sw: dict):
        """Poll a single switch for status info."""
        ip = sw["ip"]

        try:
            client, error = await self._get_client(ip, sw["username"], sw["password"])

            if not client:
                db.update_switch_status(ip, last_error=error)
                return

            ap_info = await client.get_ap_info()

            # Detect stale session (same logic as _poll_ap)
            if not ap_info.get("model") and not ap_info.get("firmware_version") and not ap_info.get("system_name"):
                logger.info(f"Stale session detected for switch {ip}, re-authenticating")
                self._clients.pop(ip, None)
                client, error = await self._get_client(ip, sw["username"], sw["password"])
                if not client:
                    db.update_switch_status(ip, last_error=error)
                    return
                ap_info = await client.get_ap_info()

            location = ap_info.get("location")

            # Auto-assign to site based on location
            site_id = sw.get("tower_site_id")
            if location:
                current_site = None
                if site_id:
                    current_site = db.get_tower_site(site_id)

                if not site_id or (current_site and current_site["name"].lower() != location.lower()):
                    new_site_id = self._get_or_create_site(location)
                    if new_site_id:
                        site_id = new_site_id
                        db.upsert_switch(ip, sw["username"], sw["password"], site_id)

            bank_kwargs = {}
            try:
                device_info = await client.get_device_info()
                bank_kwargs = {
                    "bank1_version": device_info.bank1_version,
                    "bank2_version": device_info.bank2_version,
                    "active_bank": device_info.active_bank,
                }
            except Exception as e:
                logger.debug(f"Failed to get bank info for switch {ip}: {e}")

            db.update_switch_status(
                ip,
                last_seen=datetime.now().isoformat(),
                last_error=None,
                system_name=ap_info.get("system_name"),
                model=ap_info.get("model"),
                mac=ap_info.get("mac"),
                firmware_version=ap_info.get("firmware_version"),
                location=location,
                **bank_kwargs,
            )

            logger.debug(f"Polled switch {ip}")

        except Exception as e:
            logger.error(f"Error polling switch {ip}: {e}")
            db.update_switch_status(ip, last_error=str(e))
            self._clients.pop(ip, None)

    async def poll_switch_now(self, ip: str) -> bool:
        """Trigger immediate poll of a specific switch."""
        sw = db.get_switch(ip)
        if not sw:
            return False

        await self._poll_switch(sw)

        if self.broadcast_func:
            topology = self.get_topology()
            await self.broadcast_func({
                "type": "topology_update",
                "topology": topology,
            })

        return True

    # ------------------------------------------------------------------
    # Config polling
    # ------------------------------------------------------------------

    async def _maybe_poll_configs(self):
        """Check if it's time for a config poll based on settings."""
        try:
            if db.get_setting("config_poll_enabled", "true") != "true":
                return

            interval_hours = int(db.get_setting("config_poll_interval_hours", "24"))
            if self._last_config_poll:
                elapsed = (datetime.now() - self._last_config_poll).total_seconds()
                if elapsed < interval_hours * 3600:
                    return

            logger.info("Starting scheduled config poll")
            await self.poll_all_configs()
        except Exception as e:
            logger.error(f"Error checking config poll schedule: {e}")

    async def poll_all_configs(self):
        """Fetch configs from all managed devices."""
        aps = db.get_access_points(enabled_only=True)
        switches = db.get_switches(enabled_only=True)
        all_cpes = db.get_all_cpes()

        # Build list of devices to poll: (ip, username, password, model, role)
        devices = []
        for ap in aps:
            devices.append((ap["ip"], ap["username"], ap["password"], ap.get("model"), "ap"))
        for sw in switches:
            devices.append((sw["ip"], sw["username"], sw["password"], sw.get("model"), "switch"))
        for cpe in all_cpes:
            if cpe.get("auth_status") == "ok" and cpe.get("ip"):
                # Use parent AP credentials for CPE
                ap = db.get_access_point(cpe["ap_ip"])
                if ap:
                    devices.append((cpe["ip"], ap["username"], ap["password"], cpe.get("model"), "cpe"))

        if not devices:
            self._last_config_poll = datetime.now()
            return

        logger.info(f"Config poll: fetching configs from {len(devices)} devices")
        sem = asyncio.Semaphore(5)

        async def fetch_config(ip, username, password, model, role):
            async with sem:
                await self._fetch_and_store_config(ip, username, password, model)

        await asyncio.gather(
            *[fetch_config(ip, u, p, m, r) for ip, u, p, m, r in devices],
            return_exceptions=True,
        )

        self._last_config_poll = datetime.now()
        logger.info("Config poll completed")

        if self.broadcast_func:
            await self.broadcast_func({"type": "config_poll_complete"})

    async def poll_configs_for_ips(self, ips: list[str]):
        """Fetch configs for specific device IPs (e.g., after a config push)."""
        sem = asyncio.Semaphore(5)

        async def fetch_one(ip):
            async with sem:
                # Find device credentials
                device = db.get_access_point(ip)
                if device:
                    await self._fetch_and_store_config(ip, device["username"], device["password"], device.get("model"))
                    return
                device = db.get_switch(ip)
                if device:
                    await self._fetch_and_store_config(ip, device["username"], device["password"], device.get("model"))
                    return
                cpe = db.get_cpe_by_ip(ip)
                if cpe:
                    ap = db.get_access_point(cpe["ap_ip"])
                    if ap:
                        await self._fetch_and_store_config(ip, ap["username"], ap["password"], cpe.get("model"))

        await asyncio.gather(*[fetch_one(ip) for ip in ips], return_exceptions=True)

        if self.broadcast_func:
            await self.broadcast_func({"type": "config_poll_complete"})

    async def _fetch_and_store_config(self, ip: str, username: str, password: str, model: str = None):
        """Fetch config from a device and store if changed."""
        try:
            # Reuse cached client if available, otherwise create new
            client = self._clients.get(ip)
            if not client:
                client = TachyonClient(ip, username, password, timeout=15)
                login_result = await client.login()
                if login_result is not True:
                    logger.debug(f"Config poll: login failed for {ip}: {login_result}")
                    return

            config = await client.get_config()
            if config is None:
                logger.debug(f"Config poll: failed to get config from {ip}")
                return

            import hashlib
            # Use compact separators to match _compute_config_hash in app.py
            config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
            config_hash = hashlib.sha256(config_json.encode()).hexdigest()

            existing_hash = db.get_latest_config_hash(ip)
            if existing_hash == config_hash:
                logger.debug(f"Config poll: {ip} config unchanged")
                return

            hardware_id = TachyonClient.MODEL_HARDWARE_IDS.get(
                (model or "").lower(), "tn-110-prs"
            )
            db.save_device_config(ip, config_json, config_hash, model, hardware_id)
            logger.info(f"Config poll: saved new config for {ip} (hash: {config_hash[:12]})")

        except Exception as e:
            logger.debug(f"Config poll: error fetching config from {ip}: {e}")

    def get_topology(self) -> dict:
        """Build topology dict from database."""
        sites = db.get_tower_sites()
        aps = db.get_access_points(enabled_only=False)
        health = db.get_health_summary()

        # Batch-load all CPEs in one query, grouped by AP IP
        all_cpes = db.get_all_cpes()
        cpes_by_ap = {}
        for cpe in all_cpes:
            cpes_by_ap.setdefault(cpe["ap_ip"], []).append(cpe)

        # Build site lookup
        site_lookup = {s["id"]: s for s in sites}

        # Group APs by site
        site_aps = {}
        unassigned_aps = []

        for ap in aps:
            ap_data = {
                "ip": ap["ip"],
                "system_name": ap["system_name"],
                "model": ap["model"],
                "mac": ap["mac"],
                "firmware_version": ap["firmware_version"],
                "bank1_version": ap.get("bank1_version"),
                "bank2_version": ap.get("bank2_version"),
                "active_bank": ap.get("active_bank"),
                "location": ap["location"],
                "last_seen": ap["last_seen"],
                "last_error": ap["last_error"],
                "enabled": bool(ap["enabled"]),
                "last_firmware_update": ap.get("last_firmware_update"),
                "cpes": [],
                "cpe_count": 0,
                "health_summary": {"green": 0, "yellow": 0, "red": 0},
            }

            # Get CPEs for this AP from pre-loaded data
            for cpe in cpes_by_ap.get(ap["ip"], []):
                cpe_data = {
                    "ip": cpe["ip"],
                    "mac": cpe["mac"],
                    "system_name": cpe["system_name"],
                    "model": cpe["model"],
                    "firmware_version": cpe["firmware_version"],
                    "bank1_version": cpe.get("bank1_version"),
                    "bank2_version": cpe.get("bank2_version"),
                    "active_bank": cpe.get("active_bank"),
                    "link_distance": cpe["link_distance"],
                    "rx_power": cpe["rx_power"],
                    "combined_signal": cpe["combined_signal"],
                    "last_local_rssi": cpe["last_local_rssi"],
                    "tx_rate": cpe["tx_rate"],
                    "rx_rate": cpe["rx_rate"],
                    "mcs": cpe["mcs"],
                    "link_uptime": cpe["link_uptime"],
                    "signal_health": cpe["signal_health"],
                    "auth_status": cpe["auth_status"],
                    "primary_signal": cpe["combined_signal"] or cpe["rx_power"] or cpe["last_local_rssi"],
                }
                ap_data["cpes"].append(cpe_data)

                # Update health summary
                sh = cpe["signal_health"] or "red"
                ap_data["health_summary"][sh] = ap_data["health_summary"].get(sh, 0) + 1

            ap_data["cpe_count"] = len(ap_data["cpes"])

            if ap["tower_site_id"]:
                site_id = ap["tower_site_id"]
                if site_id not in site_aps:
                    site_aps[site_id] = []
                site_aps[site_id].append(ap_data)
            else:
                unassigned_aps.append(ap_data)

        # Group switches by site
        switches = db.get_switches(enabled_only=False)
        site_switches = {}
        unassigned_switches = []

        for sw in switches:
            sw_data = {
                "ip": sw["ip"],
                "system_name": sw.get("system_name"),
                "model": sw.get("model"),
                "mac": sw.get("mac"),
                "firmware_version": sw.get("firmware_version"),
                "bank1_version": sw.get("bank1_version"),
                "bank2_version": sw.get("bank2_version"),
                "active_bank": sw.get("active_bank"),
                "location": sw.get("location"),
                "last_seen": sw.get("last_seen"),
                "last_error": sw.get("last_error"),
                "enabled": bool(sw.get("enabled", 1)),
                "last_firmware_update": sw.get("last_firmware_update"),
            }

            if sw.get("tower_site_id"):
                site_id = sw["tower_site_id"]
                if site_id not in site_switches:
                    site_switches[site_id] = []
                site_switches[site_id].append(sw_data)
            else:
                unassigned_switches.append(sw_data)

        # Build sites list
        sites_data = []
        for site in sites:
            site_data = {
                "id": site["id"],
                "name": site["name"],
                "location": site["location"],
                "latitude": site["latitude"],
                "longitude": site["longitude"],
                "aps": site_aps.get(site["id"], []),
                "switches": site_switches.get(site["id"], []),
            }
            sites_data.append(site_data)

        # Add unassigned as a virtual site
        if unassigned_aps or unassigned_switches:
            sites_data.append({
                "id": None,
                "name": "Unassigned",
                "location": None,
                "aps": unassigned_aps,
                "switches": unassigned_switches,
            })

        total_aps = len(aps)
        total_cpes = sum(len(ap_entry["cpes"]) for site in sites_data for ap_entry in site.get("aps", []))
        total_switches = len(switches)

        return {
            "sites": sites_data,
            "total_aps": total_aps,
            "total_cpes": total_cpes,
            "total_switches": total_switches,
            "overall_health": health,
            "last_updated": datetime.now().isoformat(),
        }


def get_poller() -> Optional[NetworkPoller]:
    """Get the global poller instance."""
    return _poller


def init_poller(broadcast_func: Callable, poll_interval: int = 60) -> NetworkPoller:
    """Initialize the global poller instance."""
    global _poller
    _poller = NetworkPoller(broadcast_func, poll_interval)
    return _poller
