"""Network discovery and topology building for Tachyon Management System."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from .models import APWithCPEs, CPEInfo, DeviceType, NetworkTopology
from .tachyon import TachyonClient

logger = logging.getLogger(__name__)


class NetworkDiscovery:
    """Discovers APs and their connected CPEs to build network topology."""

    def __init__(self):
        self._cached_topology: Optional[NetworkTopology] = None
        self._cached_credentials: dict[str, tuple[str, str]] = {}  # ip -> (username, password)

    async def discover_ap(
        self, ip: str, username: str, password: str
    ) -> APWithCPEs:
        """Discover a single AP and its connected CPEs.

        Args:
            ip: AP IP address
            username: Login username
            password: Login password

        Returns:
            APWithCPEs with discovered CPE data, or error info if failed.
        """
        client = TachyonClient(ip, username, password)

        ap = APWithCPEs(ip=ip, device_type=DeviceType.AP)

        try:
            # Login
            if not await client.login():
                ap.error = "Login failed"
                logger.error(f"Failed to login to AP {ip}")
                return ap

            # Cache credentials for refresh
            self._cached_credentials[ip] = (username, password)

            # Get AP info
            ap_info = await client.get_ap_info()
            ap.mac = ap_info.get("mac")
            ap.system_name = ap_info.get("system_name")
            ap.model = ap_info.get("model")
            ap.firmware_version = ap_info.get("firmware_version")

            # Get connected CPEs
            cpes = await client.get_connected_cpes()
            ap.cpes = cpes

            logger.info(
                f"Discovered AP {ip}: {ap.system_name or 'Unknown'} "
                f"with {len(cpes)} CPEs"
            )

        except Exception as e:
            ap.error = str(e)
            logger.exception(f"Error discovering AP {ip}")

        return ap

    async def discover_network(
        self,
        ap_ips: list[str],
        username: str,
        password: str,
        concurrency: int = 5,
    ) -> NetworkTopology:
        """Discover multiple APs and build network topology.

        Args:
            ap_ips: List of AP IP addresses
            username: Login username (same for all APs)
            password: Login password (same for all APs)
            concurrency: Max concurrent discoveries

        Returns:
            NetworkTopology with all discovered APs and CPEs.
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def discover_with_limit(ip: str) -> APWithCPEs:
            async with semaphore:
                return await self.discover_ap(ip, username, password)

        # Discover all APs in parallel (limited by semaphore)
        tasks = [discover_with_limit(ip) for ip in ap_ips]
        aps = await asyncio.gather(*tasks)

        topology = NetworkTopology(
            aps=list(aps),
            discovered_at=datetime.now().isoformat(),
        )

        # Cache the topology
        self._cached_topology = topology

        logger.info(
            f"Network discovery complete: {topology.total_aps} APs, "
            f"{topology.total_cpes} CPEs"
        )

        return topology

    async def refresh_topology(self) -> Optional[NetworkTopology]:
        """Refresh topology using cached AP list and credentials.

        Returns:
            Updated NetworkTopology, or None if no cached data.
        """
        if not self._cached_topology or not self._cached_credentials:
            logger.warning("No cached topology to refresh")
            return None

        # Get all AP IPs from cache
        ap_ips = list(self._cached_credentials.keys())

        # For simplicity, use the first cached credential
        # In a real system, you might store per-AP credentials
        if not ap_ips:
            return None

        first_ip = ap_ips[0]
        username, password = self._cached_credentials[first_ip]

        return await self.discover_network(ap_ips, username, password)

    def get_cached_topology(self) -> Optional[NetworkTopology]:
        """Get the cached topology without re-discovering.

        Returns:
            Cached NetworkTopology, or None if not discovered yet.
        """
        return self._cached_topology

    def get_cpe_by_ip(self, ip: str) -> Optional[CPEInfo]:
        """Find a CPE by IP address in the cached topology.

        Args:
            ip: CPE IP address

        Returns:
            CPEInfo if found, None otherwise.
        """
        if not self._cached_topology:
            return None

        for ap in self._cached_topology.aps:
            for cpe in ap.cpes:
                if cpe.ip == ip:
                    return cpe

        return None

    def get_all_cpes(self) -> list[CPEInfo]:
        """Get all CPEs from the cached topology.

        Returns:
            List of all CPEInfo objects.
        """
        if not self._cached_topology:
            return []

        cpes = []
        for ap in self._cached_topology.aps:
            cpes.extend(ap.cpes)

        return cpes


# Global discovery instance
network_discovery = NetworkDiscovery()
