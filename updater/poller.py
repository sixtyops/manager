"""Background poller for refreshing AP/CPE data."""

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from . import database as db
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
            except Exception as e:
                logger.exception(f"Error in poll loop: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _poll_all_aps(self):
        """Poll all enabled APs."""
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

            db.update_ap_status(
                ip,
                last_seen=datetime.now().isoformat(),
                last_error=None,
                system_name=ap_info.get("system_name"),
                model=ap_info.get("model"),
                mac=ap_info.get("mac"),
                firmware_version=ap_info.get("firmware_version"),
                location=location,
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

            # Probe CPE auth concurrently (don't block poll cycle)
            cpe_ips_to_probe = [cpe.ip for cpe in cpes if cpe.ip]
            if cpe_ips_to_probe:
                cpe_sem = asyncio.Semaphore(3)

                async def probe_cpe(cpe_ip):
                    async with cpe_sem:
                        status = await self._check_cpe_auth(
                            cpe_ip, ap["username"], ap["password"]
                        )
                        db.update_cpe_auth_status(ip, cpe_ip, status)

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
        sites = db.get_tower_sites()
        for site in sites:
            if site["name"].lower() == location.lower():
                return site["id"]

        # Create new site
        try:
            site_id = db.create_tower_site(location)
            logger.info(f"Auto-created tower site: {location}")
            return site_id
        except Exception as e:
            logger.error(f"Failed to create site {location}: {e}")
            return None

    async def _check_cpe_auth(self, cpe_ip: str, username: str, password: str) -> str:
        """Check if we can authenticate to a CPE using the parent AP's credentials.

        Returns "ok", "failed", or "unreachable".
        """
        try:
            client = TachyonClient(cpe_ip, username, password, timeout=10)
            result = await client.login()
            if result is True:
                return "ok"
            if isinstance(result, str) and "not reachable" in result.lower():
                return "unreachable"
            return "failed"
        except Exception as e:
            logger.debug(f"CPE auth probe failed for {cpe_ip}: {e}")
            return "unreachable"

    def invalidate_client(self, ip: str):
        """Remove cached client (e.g., when credentials change)."""
        self._clients.pop(ip, None)

    async def poll_ap_now(self, ip: str) -> bool:
        """Trigger immediate poll of a specific AP."""
        ap = db.get_access_point(ip)
        if not ap:
            return False

        await self._poll_ap(ap)

        # Broadcast update
        if self.broadcast_func:
            topology = self.get_topology()
            await self.broadcast_func({
                "type": "topology_update",
                "topology": topology,
            })

        return True

    def get_topology(self) -> dict:
        """Build topology dict from database."""
        sites = db.get_tower_sites()
        aps = db.get_access_points(enabled_only=False)
        health = db.get_health_summary()

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
                "location": ap["location"],
                "last_seen": ap["last_seen"],
                "last_error": ap["last_error"],
                "enabled": bool(ap["enabled"]),
                "cpes": [],
                "cpe_count": 0,
                "health_summary": {"green": 0, "yellow": 0, "red": 0},
            }

            # Get CPEs for this AP
            cpes = db.get_cpes_for_ap(ap["ip"])
            for cpe in cpes:
                cpe_data = {
                    "ip": cpe["ip"],
                    "mac": cpe["mac"],
                    "system_name": cpe["system_name"],
                    "model": cpe["model"],
                    "firmware_version": cpe["firmware_version"],
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
            }
            sites_data.append(site_data)

        # Add unassigned as a virtual site
        if unassigned_aps:
            sites_data.append({
                "id": None,
                "name": "Unassigned",
                "location": None,
                "aps": unassigned_aps,
            })

        total_aps = len(aps)
        total_cpes = sum(len(db.get_cpes_for_ap(ap["ip"])) for ap in aps)

        return {
            "sites": sites_data,
            "total_aps": total_aps,
            "total_cpes": total_cpes,
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
