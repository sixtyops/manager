"""Background poller for refreshing AP/CPE data."""

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import database as db
from . import radius_config
from .config_utils import (
    deep_merge,
    fragment_matches,
    check_config_compliance,
    validate_fragment_safety,
    filter_templates_by_device_type,
)
from .tachyon import TachyonClient  # kept for fallback/type compat
from .vendors import get_driver
from .vendors.base import VendorDriver
from .models import SignalHealth

PHASE_ORDER = ["canary", "pct10", "pct50", "pct100"]

logger = logging.getLogger(__name__)

_MAX_CLIENT_CACHE = 500
_CLIENT_TTL_SECONDS = 600  # 10 minutes
_BACKOFF_THRESHOLD = 3  # consecutive failures before backoff
_MAX_BACKOFF_CYCLES = 16

# Global poller instance
_poller: Optional["NetworkPoller"] = None


def _port_sort_key(ap: dict) -> tuple:
    """Natural sort key for AP port strings like 'eth1', 'eth12', 'Port 3'."""
    port = ap.get("switch_port") or ""
    match = re.search(r"(\d+)", port)
    num = int(match.group(1)) if match else 10**9
    return (num, port)


class NetworkPoller:
    """Background service that polls APs for CPE data."""

    def __init__(self, broadcast_func: Callable = None, poll_interval: int = 60):
        self.poll_interval = poll_interval
        self.broadcast_func = broadcast_func
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._clients: dict[str, tuple[VendorDriver, float]] = {}  # IP -> (client, last_used)
        self._last_config_poll: Optional[datetime] = None
        self._last_config_poll_hydrated = False
        self._poll_in_progress = False
        # Circuit breaker state
        self._failure_counts: dict[str, int] = {}
        self._backoff_until: dict[str, float] = {}
        # Alert cooldown tracking
        self._last_alert_time: dict[str, float] = {}
        self._enforce_running = False

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

                # Prime configs for any device that has none yet, so the
                # compliance view isn't empty until the daily 4 AM poll runs.
                await self._poll_missing_configs()

                # Check if it's time for the scheduled daily config poll.
                await self._maybe_poll_configs()
            except Exception as e:
                logger.exception(f"Error in poll loop: {e}")

            await asyncio.sleep(self.poll_interval)

    def _evict_stale_clients(self):
        """Remove cached clients for devices no longer in the database, disabled, or expired by TTL."""
        known_ips = db.get_enabled_device_ips()
        now = time.time()
        stale = []
        for ip, (client, last_used) in self._clients.items():
            if ip not in known_ips or (now - last_used) > _CLIENT_TTL_SECONDS:
                stale.append(ip)
        for ip in stale:
            del self._clients[ip]
        # Cap cache size by evicting oldest entries
        if len(self._clients) > _MAX_CLIENT_CACHE:
            sorted_by_age = sorted(self._clients.items(), key=lambda x: x[1][1])
            excess = len(self._clients) - _MAX_CLIENT_CACHE
            for ip, _ in sorted_by_age[:excess]:
                del self._clients[ip]
                stale.append(ip)
        if stale:
            logger.info(f"Evicted {len(stale)} stale client(s) from cache")

    def _get_cached_client(self, ip: str) -> Optional[VendorDriver]:
        """Get a cached client, updating its last-used timestamp."""
        entry = self._clients.get(ip)
        if entry:
            client, _ = entry
            self._clients[ip] = (client, time.time())
            return client
        return None

    def _cache_client(self, ip: str, client: VendorDriver):
        """Cache an authenticated client."""
        self._clients[ip] = (client, time.time())

    def _remove_cached_client(self, ip: str):
        """Remove a client from the cache."""
        self._clients.pop(ip, None)

    def _is_backed_off(self, ip: str) -> bool:
        """Check if a device is in backoff due to consecutive failures."""
        until = self._backoff_until.get(ip, 0)
        if time.time() < until:
            return True
        return False

    def _record_poll_success(self, ip: str):
        """Reset circuit breaker on successful poll."""
        self._failure_counts.pop(ip, None)
        self._backoff_until.pop(ip, None)

    def _record_poll_failure(self, ip: str):
        """Increment failure count and set backoff if threshold exceeded."""
        count = self._failure_counts.get(ip, 0) + 1
        self._failure_counts[ip] = count
        if count >= _BACKOFF_THRESHOLD:
            cycles = min(2 ** (count - _BACKOFF_THRESHOLD), _MAX_BACKOFF_CYCLES)
            self._backoff_until[ip] = time.time() + (cycles * self.poll_interval)
            logger.debug(f"Device {ip} backed off for {cycles} cycles after {count} consecutive failures")

    async def _poll_all_aps(self):
        """Poll all enabled APs."""
        if self._poll_in_progress:
            logger.warning("Previous poll cycle still running, skipping AP poll")
            return

        self._poll_in_progress = True
        try:
            self._evict_stale_clients()
            aps = db.get_access_points(enabled_only=True)

            if not aps:
                return

            logger.debug(f"Polling {len(aps)} APs")

            concurrency = int(db.get_setting("poller_concurrency", "10"))
            semaphore = asyncio.Semaphore(concurrency)

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
        finally:
            self._poll_in_progress = False

    def _check_uptime_transition(self, ip: str, device_type: str, was_error, now_error):
        """Record uptime event if device state changed."""
        was_down = bool(was_error)
        is_down = bool(now_error)
        if was_down and not is_down:
            db.record_uptime_event(ip, device_type, "up")
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._notify_device_recovered(ip, device_type))
            except RuntimeError:
                pass  # No event loop (e.g., in sync test context)
        elif not was_down and is_down:
            db.record_uptime_event(ip, device_type, "down", details=str(now_error)[:200] if now_error else None)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._notify_device_offline(ip, device_type, str(now_error)[:200] if now_error else "Unknown error"))
            except RuntimeError:
                pass  # No event loop (e.g., in sync test context)

    async def _notify_device_offline(self, ip: str, device_type: str, error: str):
        """Send notifications when a device goes offline."""
        try:
            if db.get_setting("alert_device_offline_enabled", "true") != "true":
                return
            cooldown = int(db.get_setting("alert_device_offline_cooldown_minutes", "60")) * 60
            last_alert = self._last_alert_time.get(ip, 0)
            if time.time() - last_alert < cooldown:
                return
            self._last_alert_time[ip] = time.time()

            from . import slack, snmp, email_notifier
            try:
                await slack.notify_device_offline(ip, device_type, error)
            except Exception as e:
                logger.debug(f"Slack device offline notification failed: {e}")
            try:
                await snmp.notify_device_offline(ip, device_type, error)
            except Exception as e:
                logger.debug(f"SNMP device offline notification failed: {e}")
            try:
                from . import webhooks
                await webhooks.notify_device_offline(ip, device_type, error)
            except Exception as e:
                logger.debug(f"Webhook device offline notification failed: {e}")
            try:
                await email_notifier.notify_device_offline(ip, device_type, error)
            except Exception as e:
                logger.debug(f"Email device offline notification failed: {e}")
        except Exception as e:
            logger.error(f"Device offline alert dispatch error: {e}")

    async def _notify_device_recovered(self, ip: str, device_type: str):
        """Send notifications when a device recovers."""
        try:
            if db.get_setting("alert_device_offline_enabled", "true") != "true":
                return
            # Only send recovery if we previously sent an offline alert
            if ip not in self._last_alert_time:
                return
            self._last_alert_time.pop(ip, None)

            from . import slack, snmp, email_notifier
            try:
                await slack.notify_device_recovered(ip, device_type)
            except Exception as e:
                logger.debug(f"Slack device recovered notification failed: {e}")
            try:
                await snmp.notify_device_recovered(ip, device_type)
            except Exception as e:
                logger.debug(f"SNMP device recovered notification failed: {e}")
            try:
                from . import webhooks
                await webhooks.notify_device_recovered(ip, device_type)
            except Exception as e:
                logger.debug(f"Webhook device recovered notification failed: {e}")
            try:
                await email_notifier.notify_device_recovered(ip, device_type)
            except Exception as e:
                logger.debug(f"Email device recovered notification failed: {e}")
        except Exception as e:
            logger.error(f"Device recovered alert dispatch error: {e}")

    async def _poll_ap(self, ap: dict):
        """Poll a single AP for CPE data."""
        ip = ap["ip"]
        prev_error = ap.get("last_error")

        if self._is_backed_off(ip):
            logger.debug(f"Skipping backed-off device {ip}")
            return

        try:
            # Get or create authenticated client
            client, error = await self._get_client(ip, ap["username"], ap["password"])

            if not client:
                db.update_ap_status(ip, last_error=error)
                self._check_uptime_transition(ip, "ap", prev_error, error)
                return

            # Get AP info
            ap_info = await client.get_ap_info()

            # Detect stale session: if login succeeded but API returned no data,
            # the token likely expired on the device side.  Invalidate the cached
            # client, re-authenticate, and retry once.
            if not ap_info.get("model") and not ap_info.get("firmware_version") and not ap_info.get("system_name"):
                logger.info(f"Stale session detected for {ip}, re-authenticating")
                self._remove_cached_client(ip)
                client, error = await self._get_client(ip, ap["username"], ap["password"])
                if not client:
                    db.update_ap_status(ip, last_error=error)
                    self._check_uptime_transition(ip, "ap", prev_error, error)
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
            self._check_uptime_transition(ip, "ap", prev_error, None)
            self._record_poll_success(ip)

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
            self._check_uptime_transition(ip, "ap", prev_error, str(e))
            self._record_poll_failure(ip)
            self._remove_cached_client(ip)

    async def _get_client(self, ip: str, username: str, password: str, vendor: str = "tachyon") -> tuple:
        """Get authenticated client, reusing existing session if possible.

        Returns (client, None) on success or (None, error_string) on failure.
        """
        cached = self._get_cached_client(ip)
        if cached:
            return cached, None

        try:
            driver_cls = get_driver(vendor)
        except KeyError:
            return None, f"Unknown vendor: {vendor}"

        client = driver_cls(ip, username, password)

        result = await client.connect()
        if result is True:
            self._cache_client(ip, client)
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
            client = get_driver("tachyon")(cpe_ip, effective_user, effective_pass, timeout=10)
            result = await client.connect()
            if result is True:
                return "ok"
            if isinstance(result, str) and "not reachable" in result.lower():
                return "unreachable"

            # If AP credentials failed, try global defaults as fallback
            if effective_user == username and radius_config.is_device_auth_enabled():
                default_config = radius_config.get_device_auth_config()
                if default_config.username != username:  # Don't retry same creds
                    logger.debug(f"Trying global default credentials for CPE {cpe_ip}")
                    client = get_driver("tachyon")(cpe_ip, default_config.username, default_config.password, timeout=10)
                    result = await client.connect()
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

            client = get_driver("tachyon")(cpe_ip, username, password, timeout=15)
            result = await client.connect()
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
        self._remove_cached_client(ip)

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

        concurrency = int(db.get_setting("poller_concurrency", "10"))
        semaphore = asyncio.Semaphore(concurrency)

        async def poll_with_limit(sw):
            async with semaphore:
                await self._poll_switch(sw)

        await asyncio.gather(*[poll_with_limit(sw) for sw in switches], return_exceptions=True)

    async def _poll_switch(self, sw: dict):
        """Poll a single switch for status info."""
        ip = sw["ip"]
        prev_error = sw.get("last_error")

        if self._is_backed_off(ip):
            logger.debug(f"Skipping backed-off switch {ip}")
            return

        try:
            client, error = await self._get_client(ip, sw["username"], sw["password"])

            if not client:
                db.update_switch_status(ip, last_error=error)
                self._check_uptime_transition(ip, "switch", prev_error, error)
                return

            ap_info = await client.get_ap_info()

            # Detect stale session (same logic as _poll_ap)
            if not ap_info.get("model") and not ap_info.get("firmware_version") and not ap_info.get("system_name"):
                logger.info(f"Stale session detected for switch {ip}, re-authenticating")
                self._remove_cached_client(ip)
                client, error = await self._get_client(ip, sw["username"], sw["password"])
                if not client:
                    db.update_switch_status(ip, last_error=error)
                    self._check_uptime_transition(ip, "switch", prev_error, error)
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

            self._check_uptime_transition(ip, "switch", prev_error, None)
            self._record_poll_success(ip)
            logger.debug(f"Polled switch {ip}")

            try:
                bridge_entries = await client.get_bridge_table()
                db.replace_switch_bridge_entries(ip, bridge_entries)
            except Exception as e:
                logger.debug(f"Failed to fetch bridge table for switch {ip}: {e}")

        except Exception as e:
            logger.error(f"Error polling switch {ip}: {e}")
            db.update_switch_status(ip, last_error=str(e))
            self._check_uptime_transition(ip, "switch", prev_error, str(e))
            self._record_poll_failure(ip)
            self._remove_cached_client(ip)

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

    async def _poll_missing_configs(self):
        """Fetch configs for enabled devices that have none cached yet.

        Runs every poll cycle but only polls devices without any stored config,
        so the work is O(new devices) after the first successful poll per device.
        Failures fall through quietly — they'll be retried next cycle.
        """
        try:
            if db.get_setting("config_poll_enabled", "true") != "true":
                return

            existing = set(db.get_all_latest_configs().keys())
            aps = db.get_all_access_points_dict(enabled_only=True)
            switches = db.get_all_switches_dict(enabled_only=True)
            missing = [ip for ip in list(aps) + list(switches) if ip not in existing]
            if not missing:
                return

            logger.info(f"Priming initial configs for {len(missing)} device(s)")
            await self.poll_configs_for_ips(missing)
        except Exception as e:
            logger.debug(f"_poll_missing_configs error: {e}")

    def _hydrate_last_config_poll(self) -> None:
        """Load the persisted last-poll timestamp from settings (one-time per process).

        Without this, a manager that restarts after the daily window would lose
        the in-memory _last_config_poll and skip the day silently.
        """
        if self._last_config_poll_hydrated:
            return
        self._last_config_poll_hydrated = True
        raw = db.get_setting("last_config_poll_at")
        if not raw:
            return
        try:
            self._last_config_poll = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            logger.warning("Stored last_config_poll_at is unparseable: %r", raw)

    def _record_last_config_poll(self) -> None:
        """Stamp _last_config_poll and persist it so a restart after the daily
        window doesn't silently skip a day."""
        ts = datetime.now()
        self._last_config_poll = ts
        self._last_config_poll_hydrated = True
        try:
            db.set_setting("last_config_poll_at", ts.isoformat())
        except Exception as e:
            logger.warning("Failed to persist last_config_poll_at: %s", e)

    async def _maybe_poll_configs(self):
        """Run config poll + auto-enforce daily at 4 AM local time, with
        catch-up if the manager was down during the configured window."""
        try:
            if db.get_setting("config_poll_enabled", "true") != "true":
                return

            self._hydrate_last_config_poll()

            # Determine local time using timezone setting
            import zoneinfo
            tz_name = db.get_setting("timezone", "auto")
            try:
                if tz_name and tz_name != "auto":
                    tz = zoneinfo.ZoneInfo(tz_name)
                else:
                    tz = None  # fall back to system local time
            except Exception:
                tz = None

            now_local = datetime.now(tz)
            try:
                target_hour = int(db.get_setting("config_enforce_hour", "4"))
            except (TypeError, ValueError):
                target_hour = 4

            # Catch-up: if we have a previous poll and it's been >25h since
            # then, the daily window was missed (manager was down). Run now,
            # outside the hour gate, so the day isn't silently skipped.
            # The 1-hour grace prevents triggering right before the next
            # scheduled window would fire on its own.
            if self._last_config_poll is not None:
                stale_for = datetime.now() - self._last_config_poll
                if stale_for > timedelta(hours=25):
                    logger.info(
                        "Config poll catch-up: last successful poll was %s ago "
                        "(threshold 25h); running now",
                        stale_for,
                    )
                    await self.poll_all_configs()
                    if db.get_setting("config_auto_enforce", "false") == "true":
                        await self._auto_enforce_compliance()
                    return

            # Only run during the target hour
            if now_local.hour != target_hour:
                return

            # Don't run again if we already polled today
            if self._last_config_poll:
                last_local = self._last_config_poll
                if tz and not last_local.tzinfo:
                    last_local = last_local.replace(tzinfo=tz)
                if last_local.date() == now_local.date():
                    return

            logger.info(f"Starting scheduled config poll (daily at {target_hour}:00)")
            await self.poll_all_configs()

            # After polling, check if auto-enforce is enabled
            if db.get_setting("config_auto_enforce", "false") == "true":
                await self._auto_enforce_compliance()
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
        ap_dict = db.get_all_access_points_dict(enabled_only=False)
        for cpe in all_cpes:
            if cpe.get("auth_status") == "ok" and cpe.get("ip"):
                # Use parent AP credentials for CPE (batch lookup)
                ap = ap_dict.get(cpe["ap_ip"])
                if ap:
                    devices.append((cpe["ip"], ap["username"], ap["password"], cpe.get("model"), "cpe"))

        if not devices:
            self._record_last_config_poll()
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

        self._record_last_config_poll()
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
            client = self._get_cached_client(ip)
            if not client:
                client = get_driver("tachyon")(ip, username, password, timeout=15)
                login_result = await client.connect()
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

            # Determine hardware_id via vendor driver if available
            try:
                driver_cls = get_driver("tachyon")
                driver_inst = object.__new__(driver_cls)
                hardware_id = driver_inst.get_hardware_id((model or "").lower())
            except Exception:
                hardware_id = TachyonClient.MODEL_HARDWARE_IDS.get(
                    (model or "").lower(), "tn-110-prs"
                )

            # Look up MAC for per-unit identity (used for auto-rebind on IP change).
            # Falls through to None when the device's MAC hasn't been discovered yet,
            # which simply disables rebind for this snapshot — no false matches.
            mac = None
            try:
                dev_row = db.get_access_point(ip) or db.get_switch(ip)
                if dev_row and dev_row.get("mac"):
                    mac = dev_row["mac"]
                else:
                    cpe_row = db.get_cpe_by_ip(ip)
                    if cpe_row and cpe_row.get("mac"):
                        mac = cpe_row["mac"]
            except Exception:
                mac = None

            # Auto-rebind: if a different IP has live snapshots with this MAC
            # and is no longer a managed device, the device most likely changed IP
            # (DHCP renumber, replacement). Re-link the history to this IP. MAC is
            # a per-unit identifier so a single match is a strong identity signal.
            # Only rebind on the first poll for this IP — ambiguous cases stay
            # in the recycle bin for manual resolution.
            if existing_hash is None and mac:
                orphans = db.find_orphan_snapshots_by_mac(mac, ip)
                if len(orphans) == 1:
                    old_ip = orphans[0]
                    moved = db.rebind_snapshots(old_ip, ip, mac)
                    if moved > 0:
                        # Concurrent polls on the same orphan can race; only the
                        # first UPDATE moves rows. Skip broadcast/refresh on a
                        # zero-row move to avoid misleading "rebound 0" toasts.
                        logger.info(
                            f"Config poll: rebound {moved} snapshot(s) from {old_ip} → {ip} "
                            f"(mac={mac})"
                        )
                        if self.broadcast_func:
                            await self.broadcast_func({
                                "type": "config_history_rebound",
                                "old_ip": old_ip,
                                "new_ip": ip,
                                "mac": mac,
                                "snapshots_moved": moved,
                            })
                        # Refresh existing_hash since rebind brought history under this IP.
                        existing_hash = db.get_latest_config_hash(ip)
                        if existing_hash == config_hash:
                            logger.debug(f"Config poll: {ip} matches rebound history, skipping save")
                            return
                elif len(orphans) > 1:
                    logger.warning(
                        f"Config poll: ambiguous MAC rebind for {ip} "
                        f"(mac={mac}) — {len(orphans)} candidate IPs: {orphans}. "
                        f"Skipping rebind; manual reconciliation needed."
                    )

            db.save_device_config(ip, config_json, config_hash, model, hardware_id, mac)
            logger.info(f"Config poll: saved new config for {ip} (hash: {config_hash[:12]})")

        except Exception as e:
            logger.debug(f"Config poll: error fetching config from {ip}: {e}")

    # ------------------------------------------------------------------
    # Config auto-enforce
    # ------------------------------------------------------------------

    async def _auto_enforce_compliance(self):
        """Detect non-compliant devices and push corrections in phases."""
        if self._enforce_running:
            logger.debug("Config enforce: already running, skipping")
            return

        self._enforce_running = True
        try:
            await self._run_enforce_phases()
        except Exception as e:
            logger.error(f"Config enforce error: {e}")
        finally:
            self._enforce_running = False

    async def _run_enforce_phases(self):
        """Core enforce loop: find non-compliant devices, push in phases."""
        # Skip if a manual config push rollout is active
        active_rollout = db.get_active_config_push_rollout()
        if active_rollout:
            logger.info("Config enforce: skipping — a config push rollout is active")
            return

        # Get effective templates per device (global + site overrides resolved)
        effective = db.get_all_effective_templates()
        if not effective:
            return

        # Get latest configs for all devices
        all_configs = db.get_all_latest_configs()

        # Find non-compliant devices
        non_compliant = []  # [(ip, device_type, templates)]
        for ip, templates in effective.items():
            if not templates:
                continue
            config_row = all_configs.get(ip)
            if not config_row:
                continue  # No config snapshot yet — skip
            try:
                config_data = json.loads(config_row["config_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            # Determine device type
            ap = db.get_access_point(ip)
            device_type = "ap" if ap else "switch"
            # Filter templates by device_types (custom category may target specific types)
            applicable, _excluded = filter_templates_by_device_type(templates, device_type)
            if not applicable:
                continue
            if not check_config_compliance(config_data, applicable):
                non_compliant.append((ip, device_type, applicable))

        if not non_compliant:
            logger.info("Config enforce: all devices compliant")
            if self.broadcast_func:
                await self.broadcast_func({
                    "type": "config_enforce_status",
                    "status": "idle",
                    "message": "All devices compliant",
                })
            return

        logger.info(f"Config enforce: {len(non_compliant)} non-compliant device(s)")

        try:
            cooldown_minutes = int(db.get_setting("config_enforce_cooldown_minutes", "10"))
        except (TypeError, ValueError):
            cooldown_minutes = 10

        remaining = list(non_compliant)
        for phase in PHASE_ORDER:
            if not remaining:
                break

            # Check if auto-enforce was toggled off
            if db.get_setting("config_auto_enforce", "false") != "true":
                logger.info("Config enforce: disabled mid-run, stopping")
                if self.broadcast_func:
                    await self.broadcast_func({
                        "type": "config_enforce_status",
                        "status": "stopped",
                        "message": "Auto-enforce disabled",
                    })
                return

            # Select batch for this phase (use remaining count for correct percentages)
            batch_size = self._phase_batch_size(phase, len(remaining))
            batch = remaining[:batch_size]

            logger.info(f"Config enforce phase {phase}: {len(batch)} device(s)")
            if self.broadcast_func:
                await self.broadcast_func({
                    "type": "config_enforce_status",
                    "status": "running",
                    "phase": phase,
                    "total": len(non_compliant),
                    "batch_size": len(batch),
                    "completed": len(non_compliant) - len(remaining),
                })

            # Push to batch concurrently
            sem = asyncio.Semaphore(5)
            results = await asyncio.gather(
                *[self._enforce_device(ip, dtype, templates, phase, sem)
                  for ip, dtype, templates in batch],
                return_exceptions=True,
            )

            # Count successes and failures in this batch
            batch_successes = 0
            batch_failures = 0
            for i, result in enumerate(results):
                if result is True:
                    batch_successes += 1
                else:
                    batch_failures += 1

            # Remove all batch devices from remaining (don't retry failures)
            batch_ips = {b[0] for b in batch}
            remaining = [(ip, dt, t) for ip, dt, t in remaining
                         if ip not in batch_ips]

            # If canary phase failed, stop the enforce run to protect the fleet
            if phase == "canary" and batch_failures > 0 and batch_successes == 0:
                logger.warning(
                    "Config enforce: canary device failed — stopping enforce run"
                )
                if self.broadcast_func:
                    await self.broadcast_func({
                        "type": "config_enforce_status",
                        "status": "idle",
                        "phase": phase,
                        "total": len(non_compliant),
                        "completed": len(non_compliant) - len(remaining),
                        "message": "Canary failed — enforce stopped. Investigate before next cycle.",
                        "canary_failed": True,
                    })
                return

            # Cooldown between phases (skip after last phase)
            if remaining and phase != PHASE_ORDER[-1]:
                logger.info(f"Config enforce: cooldown {cooldown_minutes}m before next phase")
                await asyncio.sleep(cooldown_minutes * 60)

        logger.info("Config enforce: completed all phases")

        # Re-poll configs for enforced devices to verify changes
        enforced_ips = [ip for ip, _, _ in non_compliant]
        if enforced_ips:
            await self.poll_configs_for_ips(enforced_ips)

        if self.broadcast_func:
            await self.broadcast_func({
                "type": "config_enforce_status",
                "status": "idle",
                "message": "Enforce completed",
            })

    def _phase_batch_size(self, phase: str, total: int) -> int:
        """Return batch size for a phase."""
        if phase == "canary":
            return 1
        elif phase == "pct10":
            return max(1, math.ceil(total * 0.1))
        elif phase == "pct50":
            return max(1, math.ceil(total * 0.5))
        else:
            return total

    async def _enforce_device(self, ip: str, device_type: str,
                              templates: list[dict], phase: str,
                              sem: asyncio.Semaphore) -> bool:
        """Push templates to a single device. Returns True on success."""
        async with sem:
            try:
                # Get device credentials
                device = db.get_access_point(ip)
                if not device:
                    device = db.get_switch(ip)
                if not device:
                    raise RuntimeError("Device not found in database")

                vendor = device.get("vendor", "tachyon")
                driver_cls = get_driver(vendor)
                client = driver_cls(ip, device["username"], device["password"], timeout=15)
                login_result = await client.connect()
                if login_result is not True:
                    raise RuntimeError(f"Login failed: {login_result}")

                current_config = await client.get_config()
                if current_config is None:
                    raise RuntimeError("Failed to fetch current config")

                # Save pre-enforce config snapshot (backup)
                import hashlib
                pre_json = json.dumps(current_config, sort_keys=True, separators=(",", ":"))
                pre_hash = hashlib.sha256(pre_json.encode()).hexdigest()
                model = device.get("model")
                hardware_id = client.get_hardware_id((model or "").lower())
                db.save_device_config(ip, pre_json, pre_hash, model, hardware_id)

                # Merge all template fragments into current config
                merged = current_config
                for t in templates:
                    frag = json.loads(t["config_fragment"]) if isinstance(t["config_fragment"], str) else t["config_fragment"]
                    validate_fragment_safety(frag)
                    merged = deep_merge(merged, frag)

                # Dry-run validation
                dry_result = await client.apply_config(merged, dry_run=True)
                if not dry_result.get("success"):
                    error_msg = dry_result.get("error", dry_result.get("raw_response", "Dry run failed"))
                    raise RuntimeError(f"Dry run rejected: {error_msg}")

                # Apply
                result = await client.apply_config(merged)
                if not result.get("success"):
                    error_msg = result.get("error", result.get("raw_response", "Apply failed"))
                    raise RuntimeError(f"Apply failed: {error_msg}")

                template_ids = [t["id"] for t in templates]
                db.save_config_enforce_log(ip, device_type, phase, "success",
                                           template_ids=template_ids)
                logger.info(f"Config enforce: {ip} corrected successfully ({phase})")
                return True

            except Exception as e:
                template_ids = [t["id"] for t in templates]
                db.save_config_enforce_log(ip, device_type, phase, "failed",
                                           error=str(e), template_ids=template_ids)
                logger.warning(f"Config enforce: {ip} failed ({phase}): {e}")
                return False

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

        # Build MAC -> (switch_ip, port) map from bridge entries of known switches
        switches = db.get_switches(enabled_only=False)
        known_switch_ips = {sw["ip"] for sw in switches}
        mac_to_switch_port = {}
        for sw in switches:
            for entry in db.get_switch_downstream_aps(sw["ip"]):
                mac = (entry.get("mac") or "").upper()
                if not mac:
                    continue
                mac_to_switch_port.setdefault(mac, {
                    "switch_ip": sw["ip"],
                    "port": entry.get("port"),
                })

        # Group APs by site
        site_aps = {}
        unassigned_aps = []
        aps_by_switch = {}

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
                "notes": ap.get("notes"),
                "cpes": [],
                "cpe_count": 0,
                "health_summary": {"green": 0, "yellow": 0, "red": 0},
                "switch_ip": None,
                "switch_port": None,
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

            # Check if this AP is seen on a known switch's port
            ap_mac = (ap.get("mac") or "").upper()
            upstream = mac_to_switch_port.get(ap_mac) if ap_mac else None
            if upstream and upstream["switch_ip"] in known_switch_ips:
                ap_data["switch_ip"] = upstream["switch_ip"]
                ap_data["switch_port"] = upstream["port"]
                aps_by_switch.setdefault(upstream["switch_ip"], []).append(ap_data)
                continue

            if ap["tower_site_id"]:
                site_id = ap["tower_site_id"]
                if site_id not in site_aps:
                    site_aps[site_id] = []
                site_aps[site_id].append(ap_data)
            else:
                unassigned_aps.append(ap_data)

        # Group switches by site
        site_switches = {}
        unassigned_switches = []

        for sw in switches:
            nested = aps_by_switch.get(sw["ip"], [])
            nested.sort(key=_port_sort_key)
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
                "notes": sw.get("notes"),
                "aps": nested,
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
        total_cpes = 0
        for site in sites_data:
            for ap_entry in site.get("aps", []):
                total_cpes += len(ap_entry.get("cpes", []))
            for sw_entry in site.get("switches", []):
                for ap_entry in sw_entry.get("aps", []):
                    total_cpes += len(ap_entry.get("cpes", []))
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


def set_poller(poller_instance) -> None:
    """Set the global poller to a custom instance (used by dev mode)."""
    global _poller
    _poller = poller_instance
