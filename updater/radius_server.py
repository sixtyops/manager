"""Built-in RADIUS authentication server for Tachyon device management.

Runs a pyrad-based RADIUS server in a background thread alongside the
FastAPI application.  Supports PAP authentication against either a local
user database (radius_users table) or an LDAP/AD directory.

The server is gated behind Feature.RADIUS_AUTH (PRO license).
"""

import asyncio
import io
import logging
import select
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from pyrad import dictionary, packet, server

from . import database as db
from .crypto import decrypt_password, encrypt_password, is_encrypted
from .radius_users import verify_radius_user

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal RADIUS dictionary (embedded)
# ---------------------------------------------------------------------------

_RADIUS_DICT = """\
ATTRIBUTE\tUser-Name\t1\tstring
ATTRIBUTE\tUser-Password\t2\toctets
ATTRIBUTE\tNAS-IP-Address\t4\tipaddr
ATTRIBUTE\tNAS-Port\t5\tinteger
ATTRIBUTE\tService-Type\t6\tinteger
ATTRIBUTE\tNAS-Identifier\t32\tstring
ATTRIBUTE\tNAS-Port-Type\t61\tinteger
"""


def _load_dictionary() -> dictionary.Dictionary:
    return dictionary.Dictionary(io.StringIO(_RADIUS_DICT))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RadiusServerConfig:
    """RADIUS server configuration."""
    enabled: bool = False
    auth_port: int = 1812
    shared_secret: str = ""
    auth_mode: str = "local"  # "local" or "ldap"
    advertised_address: str = ""
    # LDAP proxy settings
    ldap_url: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_base_dn: str = ""
    ldap_user_filter: str = "(&(objectClass=user)(sAMAccountName={username}))"


def get_radius_server_config() -> RadiusServerConfig:
    """Load RADIUS server config from database settings."""
    enabled = db.get_setting("radius_server_enabled", "false")
    port_str = db.get_setting("radius_server_port", "1812")
    secret_raw = db.get_setting("radius_server_secret", "")
    ldap_pw_raw = db.get_setting("radius_server_ldap_bind_password", "")

    # Decrypt secrets if stored encrypted
    secret = ""
    if secret_raw:
        try:
            secret = decrypt_password(secret_raw) if is_encrypted(secret_raw) else secret_raw
        except Exception:
            logger.warning("Failed to decrypt RADIUS shared secret")

    ldap_pw = ""
    if ldap_pw_raw:
        try:
            ldap_pw = decrypt_password(ldap_pw_raw) if is_encrypted(ldap_pw_raw) else ldap_pw_raw
        except Exception:
            logger.warning("Failed to decrypt LDAP bind password")

    return RadiusServerConfig(
        enabled=enabled.lower() == "true",
        auth_port=int(port_str) if port_str.isdigit() else 1812,
        shared_secret=secret,
        auth_mode=db.get_setting("radius_server_auth_mode", "local"),
        advertised_address=db.get_setting("radius_server_advertised_address", ""),
        ldap_url=db.get_setting("radius_server_ldap_url", ""),
        ldap_bind_dn=db.get_setting("radius_server_ldap_bind_dn", ""),
        ldap_bind_password=ldap_pw,
        ldap_base_dn=db.get_setting("radius_server_ldap_base_dn", ""),
        ldap_user_filter=db.get_setting(
            "radius_server_ldap_user_filter",
            "(&(objectClass=user)(sAMAccountName={username}))",
        ),
    )


def set_radius_server_config(config: RadiusServerConfig):
    """Save RADIUS server config to database. Encrypts secrets."""
    settings = {
        "radius_server_enabled": str(config.enabled).lower(),
        "radius_server_port": str(config.auth_port),
        "radius_server_auth_mode": config.auth_mode,
        "radius_server_advertised_address": config.advertised_address,
        "radius_server_ldap_url": config.ldap_url,
        "radius_server_ldap_bind_dn": config.ldap_bind_dn,
        "radius_server_ldap_base_dn": config.ldap_base_dn,
        "radius_server_ldap_user_filter": config.ldap_user_filter,
    }
    settings["radius_server_secret"] = (
        encrypt_password(config.shared_secret) if config.shared_secret else ""
    )
    settings["radius_server_ldap_bind_password"] = (
        encrypt_password(config.ldap_bind_password) if config.ldap_bind_password else ""
    )
    db.set_settings(settings)
    logger.info("RADIUS server config updated: enabled=%s, port=%s, mode=%s",
                config.enabled, config.auth_port, config.auth_mode)


# ---------------------------------------------------------------------------
# Auth logging
# ---------------------------------------------------------------------------

def _log_auth_attempt(
    username: str, nas_ip: str, result: str,
    reject_reason: str = "", auth_mode: str = "local",
):
    """Record an authentication attempt in the radius_auth_log table."""
    try:
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO radius_auth_log "
                "(username, client_ip, outcome, reason, occurred_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, nas_ip, result, reject_reason,
                 datetime.now().isoformat()),
            )
    except Exception as e:
        logger.warning("Failed to log RADIUS auth attempt: %s", e)


# ---------------------------------------------------------------------------
# LDAP authentication
# ---------------------------------------------------------------------------

def _authenticate_ldap(username: str, password: str, config: RadiusServerConfig) -> tuple[bool, str]:
    """Authenticate via LDAP bind. Returns (success, reject_reason)."""
    try:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        return False, "ldap3_not_installed"

    safe_username = escape_filter_chars(username)
    search_filter = config.ldap_user_filter.replace("{username}", safe_username)

    try:
        # Validate TLS
        url = config.ldap_url.strip()
        if not url:
            return False, "ldap_not_configured"
        if url.startswith("ldap://") and not url.startswith("ldaps://"):
            # Allow ldap:// only with STARTTLS
            tls = ldap3.Tls(validate=2)  # ssl.CERT_REQUIRED
            srv = ldap3.Server(url, use_ssl=False, tls=tls, connect_timeout=10)
        elif url.startswith("ldaps://"):
            srv = ldap3.Server(url, use_ssl=True, connect_timeout=10)
        else:
            return False, "ldap_invalid_url"

        # Service account bind to search for the user.
        # For ldap:// we negotiate STARTTLS before bind so credentials
        # are never sent in cleartext.
        conn = ldap3.Connection(
            srv, user=config.ldap_bind_dn,
            password=config.ldap_bind_password,
            auto_bind=False, receive_timeout=5, raise_exceptions=True,
        )
        conn.open()
        if url.startswith("ldap://"):
            conn.start_tls()
        conn.bind()

        conn.search(
            config.ldap_base_dn, search_filter,
            attributes=["dn"],
        )
        if not conn.entries:
            conn.unbind()
            return False, "user_not_found"

        user_dn = conn.entries[0].entry_dn
        conn.unbind()

        # Bind as the user to verify password
        user_conn = ldap3.Connection(
            srv, user=user_dn, password=password,
            auto_bind=False, receive_timeout=5, raise_exceptions=True,
        )
        user_conn.open()
        if url.startswith("ldap://"):
            user_conn.start_tls()
        user_conn.bind()
        user_conn.unbind()
        return True, ""

    except ldap3.core.exceptions.LDAPBindError:
        return False, "bad_password"
    except ldap3.core.exceptions.LDAPSocketOpenError:
        return False, "ldap_unreachable"
    except Exception as e:
        logger.warning("LDAP auth error: %s", e)
        return False, "ldap_error"


# ---------------------------------------------------------------------------
# pyrad Server subclass
# ---------------------------------------------------------------------------

class TachyonRadiusServer(server.Server):
    """RADIUS authentication server for Tachyon device management."""

    def __init__(
        self,
        config: RadiusServerConfig,
        hosts: dict,
        rad_dict: dictionary.Dictionary,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        super().__init__(
            addresses=["0.0.0.0"],
            authport=config.auth_port,
            hosts=hosts,
            dict=rad_dict,
            auth_enabled=True,
            acct_enabled=False,
            coa_enabled=False,
        )
        self._config = config
        self._running = True
        self._last_heartbeat = time.monotonic()
        # Rate limiting: NAS IP -> list of failed attempt timestamps
        self._rate_attempts: dict[str, list[float]] = {}
        self._rate_limit = 10
        self._rate_window = 60.0
        # LDAP failure tracking
        self._ldap_consecutive_failures = 0
        self._broadcast_func: Optional[Callable] = None
        self._event_loop = event_loop

    def _check_rate_limit(self, nas_ip: str) -> bool:
        """Returns True if the NAS IP is rate-limited."""
        now = time.monotonic()
        attempts = self._rate_attempts.get(nas_ip, [])
        # Prune old entries
        attempts = [t for t in attempts if now - t < self._rate_window]
        self._rate_attempts[nas_ip] = attempts
        return len(attempts) >= self._rate_limit

    def _record_failed_attempt(self, nas_ip: str):
        """Record a failed auth attempt for rate limiting."""
        self._rate_attempts.setdefault(nas_ip, []).append(time.monotonic())

    def cleanup_rate_limiter(self):
        """Remove stale rate limiter entries."""
        now = time.monotonic()
        stale = [
            ip for ip, attempts in self._rate_attempts.items()
            if not attempts or attempts[-1] < now - self._rate_window
        ]
        for ip in stale:
            del self._rate_attempts[ip]

    def HandleAuthPacket(self, pkt):
        """Process an Access-Request packet."""
        nas_ip = pkt.source[0] if pkt.source else "unknown"

        try:
            username = pkt.get("User-Name", [b""])[0]
            if isinstance(username, bytes):
                username = username.decode("utf-8", errors="replace")
        except Exception:
            username = ""

        # Rate limit check
        if self._check_rate_limit(nas_ip):
            _log_auth_attempt(username, nas_ip, "reject", "rate_limited",
                              self._config.auth_mode)
            reply = self.CreateReplyPacket(pkt, **{"code": packet.AccessReject})
            self.SendReplyPacket(pkt.fd, reply)
            return

        # Decrypt PAP password
        try:
            raw_password = pkt.get("User-Password", [b""])[0]
            if isinstance(raw_password, bytes) and raw_password:
                password = pkt.PwDecrypt(raw_password)
            else:
                password = ""
        except Exception:
            password = ""

        if not username or not password:
            self._record_failed_attempt(nas_ip)
            _log_auth_attempt(username or "(empty)", nas_ip, "reject",
                              "empty_credentials", self._config.auth_mode)
            reply = self.CreateReplyPacket(pkt, **{"code": packet.AccessReject})
            self.SendReplyPacket(pkt.fd, reply)
            return

        # Authenticate
        success = False
        reject_reason = ""

        if self._config.auth_mode == "ldap":
            success, reject_reason = _authenticate_ldap(
                username, password, self._config
            )
            if success:
                self._ldap_consecutive_failures = 0
            elif reject_reason in ("ldap_unreachable", "ldap_error"):
                self._ldap_consecutive_failures += 1
                if (
                    self._ldap_consecutive_failures >= 5
                    and self._broadcast_func
                    and self._event_loop
                    and not self._event_loop.is_closed()
                ):
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._broadcast_func({
                                "type": "system_alert",
                                "level": "warning",
                                "message": "RADIUS: LDAP server unreachable. Auth requests are being rejected.",
                            }),
                            self._event_loop,
                        )
                    except Exception:
                        pass
        else:
            # Local auth
            success = verify_radius_user(username, password)
            if not success:
                reject_reason = "invalid_credentials"

        # Send response — uniform rejection (no user enumeration)
        if success:
            _log_auth_attempt(username, nas_ip, "accept", "", self._config.auth_mode)
            reply = self.CreateReplyPacket(pkt, **{"code": packet.AccessAccept})
        else:
            self._record_failed_attempt(nas_ip)
            _log_auth_attempt(username, nas_ip, "reject", reject_reason,
                              self._config.auth_mode)
            reply = self.CreateReplyPacket(pkt, **{"code": packet.AccessReject})

        self.SendReplyPacket(pkt.fd, reply)

    def Run(self):
        """Modified Run() with heartbeat and clean shutdown support."""
        self._poll = select.poll()
        self._fdmap = {}
        self._PrepareSockets()

        while self._running:
            self._last_heartbeat = time.monotonic()
            try:
                events = self._poll.poll(1000)  # 1 second timeout
            except Exception:
                if not self._running:
                    break
                continue

            for (fd, event) in events:
                if event & select.POLLIN:
                    try:
                        fdo = self._fdmap[fd]
                        self._ProcessInput(fdo)
                    except server.ServerPacketError as err:
                        logger.debug("Dropping packet: %s", err)
                    except packet.PacketError as err:
                        logger.debug("Broken packet: %s", err)
                    except Exception:
                        logger.exception("Error processing RADIUS packet")

        # Cleanup sockets
        for fd in list(self._fdmap):
            try:
                self._fdmap[fd].close()
            except Exception:
                pass

    def stop(self):
        """Signal the server to stop."""
        self._running = False


# ---------------------------------------------------------------------------
# Background service
# ---------------------------------------------------------------------------

class RadiusService:
    """Manages the RADIUS server lifecycle as a background service."""

    def __init__(self, broadcast_func: Optional[Callable] = None):
        self._broadcast = broadcast_func
        self._server: Optional[TachyonRadiusServer] = None
        self._running = False
        self._bind_error: str = ""
        self._consecutive_bind_failures = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Used to wake run_forever() when it's parked on invalid/disabled config.
        self._restart_signal = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._running and self._server is not None and self._server._running

    @property
    def last_error(self) -> str:
        return self._bind_error

    def get_status(self) -> dict:
        """Get current service status for the API."""
        stats = {}
        if self._server:
            stats["last_heartbeat_age"] = round(
                time.monotonic() - self._server._last_heartbeat, 1
            ) if self._server._last_heartbeat else None
            stats["ldap_consecutive_failures"] = self._server._ldap_consecutive_failures
        return {
            "running": self.is_running,
            "error": self._bind_error,
            "stats": stats,
        }

    def _validate_config(self, config: RadiusServerConfig) -> Optional[str]:
        """Validate config before starting. Returns error message or None."""
        if not config.shared_secret:
            return "No shared secret configured"
        if not (1024 <= config.auth_port <= 65535):
            return f"Invalid port {config.auth_port} (must be 1024-65535)"
        from .license import Feature, is_feature_enabled
        if not is_feature_enabled(Feature.RADIUS_AUTH):
            return "PRO license required"
        return None

    def _build_hosts(self, config: RadiusServerConfig) -> dict:
        """Build pyrad hosts dict from managed devices."""
        hosts = {}
        secret = config.shared_secret.encode()
        try:
            with db.get_db() as conn:
                for table in ("access_points", "switches"):
                    rows = conn.execute(
                        f"SELECT ip, system_name FROM {table} WHERE enabled = 1"
                    ).fetchall()
                    for row in rows:
                        ip = row["ip"]
                        name = row["system_name"] or ip
                        hosts[ip] = server.RemoteHost(
                            address=ip, secret=secret, name=name,
                        )
        except Exception as e:
            logger.warning("Error building NAS client list: %s", e)
        if not hosts:
            # If no managed devices yet, accept from any host with the secret
            hosts["0.0.0.0"] = server.RemoteHost(
                address="0.0.0.0", secret=secret, name="any",
            )
        logger.info("RADIUS NAS clients: %d device(s) registered", len(hosts))
        return hosts

    def refresh_clients(self):
        """Rebuild the NAS client list from the device database."""
        if not self._server:
            return
        config = get_radius_server_config()
        self._server.hosts = self._build_hosts(config)

    async def _sleep_until_restart(self):
        """Sleep without spinning, but allow immediate wake on restart."""
        self._restart_signal.clear()
        sleep_task = asyncio.create_task(asyncio.sleep(3600))
        wake_task = asyncio.create_task(self._restart_signal.wait())
        done, pending = await asyncio.wait(
            {sleep_task, wake_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Propagate sleep cancellation if the sleep task was the one that completed.
        if sleep_task in done:
            await sleep_task

    async def run_forever(self):
        """Run the RADIUS server. Called by _supervised_task for crash recovery.

        When the server can't start (disabled, no license, config error),
        we sleep indefinitely rather than returning — this prevents
        _supervised_task from spinning in a tight loop.  The sleep is
        interrupted by CancelledError on shutdown or restart.
        """
        self._loop = asyncio.get_event_loop()
        config = get_radius_server_config()

        if not config.enabled:
            self._bind_error = "Disabled"
            self._running = False
            await self._broadcast_status()
            await self._sleep_until_restart()
            return

        error = self._validate_config(config)
        if error:
            self._bind_error = error
            self._running = False
            logger.warning("RADIUS server not starting: %s", error)
            await self._broadcast_status()
            await self._sleep_until_restart()
            return

        hosts = self._build_hosts(config)
        rad_dict = _load_dictionary()

        try:
            self._server = TachyonRadiusServer(
                config,
                hosts,
                rad_dict,
                event_loop=self._loop,
            )
            self._server._broadcast_func = self._broadcast
        except OSError as e:
            self._consecutive_bind_failures += 1
            self._bind_error = str(e)
            self._running = False
            logger.error("RADIUS server failed to bind port %d: %s",
                         config.auth_port, e)
            await self._broadcast_status()
            if self._consecutive_bind_failures >= 3:
                # Back off longer after repeated failures
                await asyncio.sleep(50)  # + 10s from supervised_task = 60s total
            raise

        self._bind_error = ""
        self._consecutive_bind_failures = 0
        self._running = True
        logger.info("RADIUS server started on UDP port %d (mode=%s)",
                     config.auth_port, config.auth_mode)
        await self._broadcast_status()

        # Start watchdog and client refresh as concurrent tasks
        watchdog = asyncio.create_task(self._watchdog())
        maintenance = asyncio.create_task(self._maintenance_loop())

        try:
            await asyncio.to_thread(self._server.Run)
        finally:
            self._running = False
            watchdog.cancel()
            maintenance.cancel()
            logger.info("RADIUS server stopped")
            await self._broadcast_status()

    async def _watchdog(self):
        """Monitor the RADIUS server thread for liveness."""
        while self._running:
            await asyncio.sleep(30)
            if self._server and self._running:
                age = time.monotonic() - self._server._last_heartbeat
                if age > 30:
                    logger.error(
                        "RADIUS server thread stuck (heartbeat %.0fs old), forcing restart",
                        age,
                    )
                    self._server.stop()
                    return

    async def _maintenance_loop(self):
        """Periodic maintenance: client refresh, rate limiter cleanup, license check."""
        while self._running:
            await asyncio.sleep(300)  # Every 5 minutes
            if not self._running or not self._server:
                break
            # Refresh NAS clients
            try:
                self.refresh_clients()
            except Exception as e:
                logger.warning("NAS client refresh error: %s", e)
            # Cleanup rate limiter
            try:
                self._server.cleanup_rate_limiter()
            except Exception as e:
                logger.warning("Rate limiter cleanup error: %s", e)
            # Check license
            try:
                from .license import Feature, is_feature_enabled
                if not is_feature_enabled(Feature.RADIUS_AUTH):
                    logger.warning("RADIUS server stopping: PRO license expired")
                    self._bind_error = "PRO license required"
                    self._server.stop()
                    await self._broadcast_status()
                    return
            except Exception:
                pass

    async def stop(self):
        """Stop the RADIUS server."""
        self._restart_signal.set()
        self._running = False
        if self._server:
            self._server.stop()
            self._server = None

    async def restart(self):
        """Stop and restart with new config."""
        self._restart_signal.set()
        await self.stop()
        await asyncio.sleep(0.2)
        # run_forever will be called again by _supervised_task

    async def _broadcast_status(self):
        """Broadcast RADIUS server status over WebSocket."""
        if self._broadcast:
            try:
                await self._broadcast({
                    "type": "radius_status",
                    **self.get_status(),
                })
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: Optional[RadiusService] = None


def init_radius_service(broadcast_func: Optional[Callable] = None) -> RadiusService:
    """Create the singleton RadiusService."""
    global _service
    _service = RadiusService(broadcast_func)
    return _service


def get_radius_service() -> Optional[RadiusService]:
    """Get the singleton RadiusService."""
    return _service
