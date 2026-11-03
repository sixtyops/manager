"""Auto-update scheduler with gradual rollout support."""

import asyncio
import ipaddress
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import database as db
from .core import engine
from .firmware_policy import (
    classify_device_version,
    extract_version_from_filename as policy_extract_version_from_filename,
    parse_version as policy_parse_version,
)
from . import rollout_gate
from . import services
from .core.gates import MaintenanceSchedule, firmware_window_cutoff
from .services import format_temperature

logger = logging.getLogger(__name__)

# Do not start new scheduled jobs when the window has this many minutes or less remaining.
SCHEDULE_END_BUFFER_MINUTES = 15

# A confirmed-working device only counts toward clearing its family's Firmware
# Hold if it was seen this recently. One generous poll-day — a stale device that
# hasn't reported in proves nothing about the firmware's current health.
CONFIRMED_SEEN_WITHIN = timedelta(hours=24)

_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

_UNSET = object()


def _resolved_failure_recovery_ip(window_key) -> Optional[str]:
    """Return the latest prior resolved failure that has no later success record."""
    if not isinstance(window_key, (list, tuple)) or len(window_key) != 2:
        return None
    try:
        current_day = datetime.fromisoformat(window_key[1]).date()
        with db.get_db() as conn:
            stops = conn.execute(
                "SELECT window_key, device_ip, resolved_at FROM firmware_window_stops "
                "WHERE resolved_at IS NOT NULL ORDER BY resolved_at DESC, stopped_at DESC"
            ).fetchall()
            for stop in stops:
                try:
                    prior_key = json.loads(stop["window_key"])
                    prior_day = (
                        datetime.fromisoformat(prior_key[1]).date()
                        if isinstance(prior_key, list) and len(prior_key) == 2 else None
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if prior_day is None or prior_key[0] != window_key[0] or prior_day >= current_day:
                    continue
                verified = conn.execute(
                    "SELECT 1 FROM device_update_history "
                    "WHERE ip = ? AND status = 'success' AND completed_at > ? LIMIT 1",
                    (stop["device_ip"], stop["resolved_at"]),
                ).fetchone()
                return None if verified else stop["device_ip"]
    except Exception:
        raise
    return None


def _missing_serial_recovery_reason(ip: str, entry: dict, window_key) -> Optional[str]:
    """Explain why a current target cannot use read-only recovery without a saved serial."""
    if not entry.get("verify_recovery_if_current"):
        return None
    stop = db.get_latest_resolved_firmware_window_stop(ip, window_key)
    if stop and not stop.get("expected_serial"):
        return (
            f"Cannot verify {ip}: the prior failure has no saved serial, "
            "and the device already reports the target firmware"
        )
    return None


def _parse_schedule_days(schedule_days) -> set[int]:
    """Convert schedule_days (csv string or list) to a set of weekday integers."""
    if not schedule_days:
        return set()
    if isinstance(schedule_days, str):
        parts = [d.strip().lower() for d in schedule_days.split(",")]
    else:
        parts = [str(d).strip().lower() for d in schedule_days]
    return {_DAY_INDEX[p] for p in parts if p in _DAY_INDEX}


def upcoming_window_starts(
    now: datetime,
    schedule_days,
    start_hour: int,
    end_hour: int,
    count: int = 1,
    earliest_after: Optional[datetime] = None,
) -> list[datetime]:
    """Return the next `count` maintenance-window start datetimes after `now`.

    A window "starts" at `start_hour` on any day whose weekday is in
    `schedule_days`. Today's window counts if it has not yet ended (i.e.
    `now.hour < end_hour`); the start it returns is then clamped to `now`
    when we're already inside the window. Overnight windows
    (start_hour > end_hour) are not modelled here — the scheduler treats
    them by day-of-start, so we match.

    `earliest_after` lets callers push the answer past a hold/freeze clear
    date (e.g. the new-firmware Firmware Hold).
    """
    active_days = _parse_schedule_days(schedule_days)
    if not active_days:
        return []

    floor = max(now, earliest_after) if earliest_after else now
    results: list[datetime] = []
    for offset in range(0, 60):
        candidate_date = floor.date() + timedelta(days=offset)
        if candidate_date.weekday() not in active_days:
            continue
        window_start = datetime.combine(
            candidate_date,
            datetime.min.time().replace(hour=start_hour),
            tzinfo=floor.tzinfo,
        )
        window_end = datetime.combine(
            candidate_date,
            datetime.min.time().replace(hour=end_hour),
            tzinfo=floor.tzinfo,
        )
        if window_end <= floor:
            continue
        results.append(max(window_start, floor))
        if len(results) >= count:
            break
    return results


def _parse_version(version: str) -> tuple:
    """Parse version string into tuple for comparison.

    Handles formats like '1.12.3.54970' or '1.12.3.r54970'.
    Returns tuple of integers for comparison.
    """
    return policy_parse_version(version)


def _extract_version_from_filename(filename: str) -> str:
    """Extract normalized version from a firmware filename.

    Mirrors `app._extract_version_from_filename`; keep the two in sync.
    See that copy for why the regex must match the bare `tns-` prefix.
    """
    return policy_extract_version_from_filename(filename)


def _firmware_type_for_model(model: Optional[str]) -> str:
    """Map a device model to its firmware family."""
    if not model:
        return "tna-30x"
    model_lower = model.lower()
    if model_lower.startswith("tns-100"):
        return "tns-100"
    if model_lower.startswith("tna-303l"):
        return "tna-303l"
    return "tna-30x"


def _seen_within(last_seen: Optional[str], cutoff: datetime) -> bool:
    """True if `last_seen` (ISO) is at or after `cutoff` (an aware-UTC datetime).

    Stored timestamps may be naive UTC (container clock) — treat them as UTC so
    the comparison is absolute. Missing/unparseable -> False (fail-closed)."""
    if not last_seen:
        return False
    try:
        dt = datetime.fromisoformat(last_seen)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _device_needs_update(
    current_version: str,
    target_version: str,
    allow_downgrade: bool,
    last_update_iso: Optional[str] = None,
    cooldown_days: int = 0,
) -> bool:
    """Return True when a device should be included in a rollout."""
    if not target_version:
        return False
    if target_version == "__unknown__":
        return True

    # Cooldown check: skip if recently updated
    if cooldown_days > 0 and last_update_iso:
        try:
            last_upd = datetime.fromisoformat(last_update_iso)
            if datetime.now() - last_upd < timedelta(days=cooldown_days):
                return False
        except (ValueError, TypeError):
            pass

    return classify_device_version(current_version, target_version, allow_downgrade).needs_update


def _known_firmware_family(model: Optional[str]) -> Optional[str]:
    """Return the supported firmware family, or None for an unknown model."""
    if not model:
        return None
    normalized = model.strip().lower()
    if normalized.startswith("tns-100"):
        return "tns-100"
    if normalized.startswith("tna-303l"):
        return "tna-303l"
    if normalized.startswith(("tna-301", "tna-302", "tna-303x")):
        return "tna-30x"
    return None


def _normalized_ip(ip: str) -> tuple[int, int]:
    """Sort IPv4 and IPv6 addresses by numeric value, independent of spelling."""
    address = ipaddress.ip_address(ip.strip())
    return address.version, int(address)


def _as_int(value, default: int) -> int:
    """Parse int with fallback for malformed persisted settings."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    """Parse float with fallback for malformed persisted settings."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _cpe_auth_block_reason(parent_ip: str, cpe_ip: str, status: str) -> str:
    """Build the operator-visible hold reason from structured CPE details."""
    return (
        f"CPE {cpe_ip} under AP {parent_ip} failed the fresh login check "
        f"({status}); this wave is held"
    )


_scheduler: Optional["AutoUpdateScheduler"] = None


class AutoUpdateScheduler:
    """Background service that checks schedule and triggers firmware updates."""

    STATES = (
        "disabled",
        "idle",
        "waiting",
        "running",
        "blocked_weather",
        "blocked_time",
        "blocked_no_firmware",
        "blocked_all_current",
        "blocked_firmware_hold",
    )

    def __init__(self, broadcast_func: Callable, start_update_func: Callable, check_interval: int = 60):
        self.broadcast_func = broadcast_func
        self.start_update_func = start_update_func
        self.check_interval = check_interval

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._weather_task: Optional[asyncio.Task] = None
        self._state = "disabled"
        self._block_reason: Optional[str] = None
        self._weather_info: Optional[dict] = None
        self._weather_checked_today: Optional[str] = None
        self._weather_ok: Optional[bool] = None
        self._last_run_result: Optional[str] = None
        self._last_run_time: Optional[str] = None
        self._current_job_id: Optional[str] = None
        self._check_lock = asyncio.Lock()
        self._queue_job_context: dict[str, dict] = {}
        # Human-readable note when a family's Firmware Hold was cleared early
        # because a device is confirmed working on the new firmware (None when no
        # early clear is in effect).
        self._hold_clear_reason: Optional[str] = None

    @staticmethod
    def _firmware_changed(rollout: dict, fw_30x: str, fw_303l: str, fw_tns100: str) -> bool:
        """Return True if any selected firmware differs from the rollout's stored firmware."""
        if rollout["firmware_file"] != fw_30x:
            return True
        if (fw_303l or "") != (rollout.get("firmware_file_303l") or ""):
            return True
        if (fw_tns100 or "") != (rollout.get("firmware_file_tns100") or ""):
            return True
        return False

    async def start(self):
        """Start the scheduler check loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"Auto-update scheduler started (interval: {self.check_interval}s)")
        # Fetch weather eagerly so the header shows temperature immediately. Keep
        # the handle so stop() can cancel it — otherwise the fire-and-forget task
        # can outlive the scheduler (e.g. in tests, leaving "Task was destroyed but
        # it is pending" warnings at loop teardown).
        self._weather_task = asyncio.create_task(self._fetch_weather_initial())

    async def _fetch_weather_initial(self):
        """Fetch weather on startup so the UI shows temperature immediately."""
        try:
            settings = db.get_all_settings()
            if settings.get("weather_check_enabled") != "true":
                return
            zip_code = settings.get("zip_code", "")
            min_temp_c = _as_float(settings.get("min_temperature_c", "-10"), -10.0)
            weather_ok, weather_data = await services.check_weather_ok(
                zip_code if zip_code else None, min_temp_c
            )
            self._weather_info = weather_data
            self._weather_ok = weather_ok
            self._weather_checked_today = datetime.now().strftime("%Y-%m-%d")
            if weather_data:
                logger.info("Scheduler: initial weather fetch: %.1f°C", weather_data.get("temperature_c", 0))
        except Exception as e:
            logger.warning("Scheduler: initial weather fetch failed: %s", e)

    async def stop(self):
        """Stop the scheduler."""
        self._running = False
        for task in (self._task, self._weather_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._weather_task = None
        logger.info("Auto-update scheduler stopped")

    async def force_check(self):
        """Force immediate re-evaluation of scheduler state."""
        self._weather_checked_today = None
        try:
            await self._check_and_run()
        except Exception as e:
            logger.exception(f"Forced scheduler check error: {e}")
        await self._broadcast_status()

    async def _check_loop(self):
        """Main loop: check every interval."""
        while self._running:
            try:
                await self._check_and_run()
            except Exception as e:
                logger.exception(f"Scheduler check error: {e}")
            await asyncio.sleep(self.check_interval)

    async def _check_and_run(self):
        """Serialize scheduler ticks so one window cannot admit duplicate jobs."""
        async with self._check_lock:
            await self._check_and_run_locked()

    async def _check_and_run_locked(self):
        """Main decision logic each tick."""
        settings = db.get_all_settings()
        schedule_enabled = settings.get("schedule_enabled")

        if schedule_enabled != "true":
            if self._state != "disabled":
                logger.info(f"Scheduler disabled (schedule_enabled={schedule_enabled!r})")
                self._state = "disabled"
                self._block_reason = None
                await self._broadcast_status()
            return

        if self._current_job_id is not None:
            return

        if not self._scheduled_admission_allowed():
            await self._broadcast_status()
            return

        tz_str = settings.get("timezone", "auto")
        if tz_str == "auto":
            tz_str = await services.get_timezone()

        time_ok, time_result = await services.validate_time_sources(tz_str)
        if not time_ok:
            # Fall back to system clock with a warning instead of hard-blocking
            from zoneinfo import ZoneInfo
            try:
                tz = ZoneInfo(tz_str)
            except Exception:
                tz = ZoneInfo("America/Chicago")
            now = datetime.now(tz)
            # Only hard-block if the error is clock drift (not just unreachable)
            if "drift" in str(time_result).lower():
                self._state = "blocked_time"
                self._block_reason = str(time_result)
                db.log_schedule_event("blocked_time", self._block_reason)
                logger.warning(f"Scheduler blocked: {self._block_reason}")
                await self._broadcast_status()
                return
            # External time unreachable — use system clock with degraded confidence
            logger.warning(f"Time source unavailable ({time_result}), falling back to system clock")
            if self._state == "blocked_time":
                db.log_schedule_event("time_fallback", "Using system clock — external time source unavailable")
        else:
            now = time_result
        logger.info(f"Time validation: {'passed' if time_ok else 'degraded (system clock)'}")

        # Check freeze windows
        freeze = db.is_in_freeze_window(now.isoformat())
        if freeze:
            if self._state != "frozen":
                self._state = "frozen"
                self._block_reason = f"Maintenance freeze: {freeze['name']}"
                db.log_schedule_event("frozen", self._block_reason)
                logger.info(f"Scheduler frozen: {freeze['name']} (until {freeze['end_date']})")
                await self._broadcast_status()
            return

        fw_30x = settings.get("selected_firmware_30x", "")
        allow_downgrade = settings.get("allow_downgrade", "false") == "true"
        if fw_30x:
            fw_303l_early = settings.get("selected_firmware_303l", "")
            fw_tns100_early = settings.get("selected_firmware_tns100", "")
            last_rollout = db.get_last_rollout_for_firmware_set(fw_30x, fw_303l_early, fw_tns100_early)
            if last_rollout and last_rollout["status"] == "completed":
                scope_ips = self._resolve_scope(settings)
                switch_scope = self._resolve_switch_scope(settings)

                target_versions = self._target_versions(settings, None)
                cooldown_days = _as_int(settings.get("firmware_update_cooldown_days", "30"), 30)
                needs_update = self._filter_devices_needing_update(
                    scope_ips, target_versions, allow_downgrade, cooldown_days=cooldown_days
                )
                needs_switch_update = self._filter_switches_needing_update(
                    switch_scope, target_versions, allow_downgrade, cooldown_days=cooldown_days
                )
                retry_key = rollout_gate.firmware_window_key(settings, now)
                if (not needs_update and not needs_switch_update
                        and not _resolved_failure_recovery_ip(retry_key)):
                    if self._state != "blocked_all_current":
                        self._state = "blocked_all_current"
                        self._block_reason = "All devices up to date"
                        await self._broadcast_status()
                    return

        schedule_days = [d.strip() for d in settings.get("schedule_days", "").split(",") if d.strip()]
        start_hour = _as_int(settings.get("schedule_start_hour", "3"), 3)
        end_hour = _as_int(settings.get("schedule_end_hour", "4"), 4)
        current_day = now.strftime("%a").lower()
        current_hour = now.hour

        logger.info(
            f"Schedule check: day={current_day} (in {schedule_days}?), hour={current_hour} (in {start_hour}-{end_hour}?)"
        )

        if not services.is_in_schedule_window(current_hour, current_day, schedule_days, start_hour, end_hour):
            if self._state != "idle":
                logger.info(
                    f"Outside maintenance window (day={current_day}, hour={current_hour}), setting state to idle"
                )
                self._state = "idle"
                self._block_reason = None
                self._weather_checked_today = None
                self._weather_ok = None
                await self._broadcast_status()
            return

        logger.info(f"Inside maintenance window! day={current_day}, hour={current_hour}, window={start_hour}-{end_hour}")

        minutes_until_end = services.minutes_until_window_end(now, start_hour, end_hour)
        if minutes_until_end <= SCHEDULE_END_BUFFER_MINUTES:
            self._state = "waiting"
            self._block_reason = "Too close to maintenance window end"
            await self._broadcast_status()
            return

        if settings.get("weather_check_enabled") == "true":
            today_key = now.strftime("%Y-%m-%d")
            if self._weather_checked_today != today_key:
                zip_code = settings.get("zip_code", "")
                min_temp_c = _as_float(settings.get("min_temperature_c", "-10"), -10.0)
                weather_ok, weather_data = await services.check_weather_ok(
                    zip_code if zip_code else None, min_temp_c
                )
                self._weather_info = weather_data
                self._weather_ok = weather_ok
                self._weather_checked_today = today_key
            else:
                weather_ok = self._weather_ok

            if not weather_ok:
                self._state = "blocked_weather"
                temp_c = self._weather_info.get("temperature_c") if self._weather_info else None
                min_temp_c = _as_float(settings.get("min_temperature_c", "-10"), -10.0)
                temp_unit = await services.resolve_temperature_unit(settings.get("temperature_unit", "auto"))
                temp_str = format_temperature(temp_c, temp_unit) if temp_c is not None else "?"
                min_temp_str = format_temperature(min_temp_c, temp_unit)
                self._block_reason = f"Temperature {temp_str} is below minimum {min_temp_str}"
                db.log_schedule_event("blocked_weather", self._block_reason)
                logger.warning(f"Scheduler blocked by weather: {self._block_reason}")
                await self._broadcast_status()
                return

        fw_30x = settings.get("selected_firmware_30x", "")
        if not fw_30x:
            self._state = "blocked_no_firmware"
            self._block_reason = "No firmware selected"
            await self._broadcast_status()
            return

        fw_303l = settings.get("selected_firmware_303l", "")
        fw_tns100 = settings.get("selected_firmware_tns100", "")

        rollout = db.get_active_rollout()

        if rollout and self._firmware_changed(rollout, fw_30x, fw_303l, fw_tns100):
            db.cancel_rollout(rollout["id"])
            db.log_schedule_event("rollout_cancelled", "Firmware selection changed")
            logger.info(f"Cancelled rollout {rollout['id']} due to firmware change")
            rollout = None

        if rollout is None:
            last = db.get_last_rollout_for_firmware_set(fw_30x, fw_303l, fw_tns100)
            if last and last["status"] == "completed":
                scope_ips = self._resolve_scope(settings)
                switch_scope = self._resolve_switch_scope(settings)

                target_versions = self._target_versions(settings, None)
                cooldown_days = _as_int(settings.get("firmware_update_cooldown_days", "30"), 30)
                needs_update = self._filter_devices_needing_update(
                    scope_ips, target_versions, allow_downgrade, cooldown_days=cooldown_days
                )
                needs_switch_update = self._filter_switches_needing_update(
                    switch_scope, target_versions, allow_downgrade, cooldown_days=cooldown_days
                )
                retry_key = rollout_gate.firmware_window_key(settings, now)
                if (not needs_update and not needs_switch_update
                        and not _resolved_failure_recovery_ip(retry_key)):
                    self._state = "blocked_all_current"
                    self._block_reason = "All devices up to date"
                    await self._broadcast_status()
                    return

            if not self._scheduled_admission_allowed():
                await self._broadcast_status()
                return
            snapshot_keys = ["bank_mode", "parallel_updates", "allow_downgrade", "pre_update_reboot",
                             "bandwidth_limit_kbps", "schedule_start_hour", "schedule_end_hour",
                             "schedule_days", "schedule_timezone"]
            snapshot = {k: settings.get(k, "") for k in snapshot_keys}
            try:
                rollout_id = db.create_rollout(
                    fw_30x, fw_303l if fw_303l else None, fw_tns100 if fw_tns100 else None,
                    settings_snapshot=snapshot, now=now,
                )
            except Exception as e:
                logger.warning(f"Rollout could not be created; no job started: {e}")
                self._state = "waiting"
                self._block_reason = "Could not create rollout; retry next tick"
                await self._broadcast_status()
                return
            rollout = db.get_rollout(rollout_id)
            db.log_schedule_event("rollout_created", f"Rollout {rollout_id} for {fw_30x}")
            logger.info(f"Created rollout {rollout_id} for firmware {fw_30x}")

        return await self._run_derived_queue(
            settings, rollout, now, schedule_days, start_hour, end_hour, tz_str,
            fw_30x, fw_303l, fw_tns100, allow_downgrade,
        )

    async def _run_derived_queue(
        self, settings, rollout, now, schedule_days, start_hour, end_hour,
        timezone_name, fw_30x, fw_303l, fw_tns100, allow_downgrade,
    ):
        """Admit the current derived queue under its persisted family cutoffs."""
        window_key = rollout_gate.firmware_window_key({
            "schedule_days": ",".join(schedule_days),
            "schedule_start_hour": start_hour,
            "schedule_end_hour": end_hour,
            "schedule_timezone": timezone_name,
        }, now)
        if window_key is None:
            self._state = "waiting"
            self._block_reason = "Maintenance window identity is invalid"
            await self._broadcast_status()
            return

        try:
            recovery_ip = _resolved_failure_recovery_ip(window_key)
        except Exception as exc:
            logger.warning("Could not read resolved firmware stop; holding queue: %s", exc)
            self._state = "blocked_safety"
            self._block_reason = "Could not verify the previously failed device"
            await self._broadcast_status()
            return

        try:
            if db.get_firmware_window_stop(window_key):
                self._state = "blocked_safety"
                self._block_reason = "This maintenance window stopped after a device failure"
                await self._broadcast_status()
                return
            unresolved = db.get_unresolved_firmware_window_stops()
        except Exception as exc:
            logger.warning("Could not read firmware window stops; holding queue: %s", exc)
            self._state = "blocked_safety"
            self._block_reason = "Could not verify firmware failure stops"
            await self._broadcast_status()
            return
        if unresolved:
            stop = unresolved[0]
            self._state = "blocked_safety"
            self._block_reason = (
                f"Firmware queue stopped at {stop.get('device_ip', 'a device')}; "
                "resolve the failure before a later window"
            )
            await self._broadcast_status()
            return

        held_families: set[str] = set()
        family_holds: dict = {}
        try:
            held_families, family_holds = self._held_families(settings, rollout)
            if recovery_ip:
                recovery_queue = self._derive_pending_queue(
                    settings, rollout, allow_downgrade, held_families=set(),
                    recovery_ip=recovery_ip,
                )
                recovery_entry = next(
                    (entry for entry in recovery_queue if entry["ip"] == recovery_ip), None,
                )
                if recovery_entry is None:
                    self._state = "blocked_safety"
                    self._block_reason = "Could not verify the previously failed device"
                    await self._broadcast_status()
                    return
                recovery_block = _missing_serial_recovery_reason(
                    recovery_ip, recovery_entry, window_key,
                )
                if recovery_block:
                    self._state = "blocked_safety"
                    self._block_reason = recovery_block
                    await self._broadcast_status()
                    return
                if recovery_entry["family"] in held_families:
                    self._state = "blocked_firmware_hold"
                    self._block_reason = self._firmware_hold_block_reason(
                        {recovery_entry["family"]}, family_holds,
                    )
                    await self._broadcast_status()
                    return
                queue = [recovery_entry]
            else:
                queue = self._derive_pending_queue(
                    settings, rollout, allow_downgrade, held_families=held_families,
                )
        except Exception as exc:
            logger.warning("Derived firmware queue failed closed: %s", exc)
            if held_families:
                self._state = "blocked_firmware_hold"
                self._block_reason = self._firmware_hold_block_reason(held_families, family_holds)
            else:
                self._state = "waiting"
                self._block_reason = str(exc) or "Could not build firmware queue"
            await self._broadcast_status()
            return

        if not queue:
            try:
                held_queue = self._derive_pending_queue(
                    settings, rollout, allow_downgrade, held_families=set(),
                    recovery_ip=recovery_ip,
                ) if held_families else []
            except Exception as exc:
                logger.warning("Could not verify held queue entries: %s", exc)
                held_queue = [True]
            if held_queue:
                self._state = "blocked_firmware_hold"
                self._block_reason = self._firmware_hold_block_reason(held_families, family_holds)
            else:
                self._state = "blocked_all_current"
                self._block_reason = "All devices up to date"
            await self._broadcast_status()
            return

        if rollout.get("status") in ("paused", "waiting_hold"):
            db.resume_rollout(rollout["id"])
            rollout = db.get_rollout(rollout["id"]) or rollout

        filenames = {
            "tna-30x": rollout.get("firmware_file") or fw_30x,
            "tna-303l": rollout.get("firmware_file_303l") or fw_303l,
            "tns-100": rollout.get("firmware_file_tns100") or fw_tns100,
        }
        schedule = MaintenanceSchedule(
            schedule_id=window_key[0],
            days=frozenset(_parse_schedule_days(schedule_days)),
            start_hour=start_hour,
            end_hour=end_hour,
            tz=timezone_name,
        )
        timing_admissions = []
        try:
            ramp_minutes = json.loads(settings.get("firmware_window_ramp_minutes", "[30, 120]"))
            families = dict.fromkeys(entry["family"] for entry in queue)
            for family in families:
                artifact = filenames.get(family)
                target_sha256 = db.get_firmware_sha256(artifact) if artifact else None
                if not target_sha256:
                    raise ValueError(f"Firmware artifact hash is unavailable for {family}")
                with db.get_db() as conn:
                    prior = conn.execute(
                        "SELECT ordinal, last_window_key, cutoff_utc FROM firmware_window_timing "
                        "WHERE family = ? AND target_sha256 = ?",
                        (family, target_sha256),
                    ).fetchone()
                encoded_key = json.dumps(list(window_key), separators=(",", ":"))
                same_window = bool(prior and prior["last_window_key"] == encoded_key)
                ordinal = int(prior["ordinal"]) if same_window else (int(prior["ordinal"]) + 1 if prior else 1)
                stored_cutoff = prior["cutoff_utc"] if same_window else None
                cutoff = firmware_window_cutoff(
                    schedule, now, ordinal, ramp_minutes, stored_cutoff,
                )
                if cutoff is None:
                    raise ValueError(f"Firmware cutoff is invalid for {family}")
                timing_admissions.append((family, target_sha256, cutoff, encoded_key, ordinal))
        except Exception as exc:
            logger.warning("Could not persist firmware window timing; holding queue: %s", exc)
            self._state = "waiting"
            self._block_reason = "Firmware window timing is unavailable"
            await self._broadcast_status()
            return

        entries = [
            ({"ip": item["ip"], "role": item["device_type"], "parent_ap": item["parent_ip"]}
             | ({"verify_recovery_if_current": True}
                if item.get("verify_recovery_if_current") else {}))
            for item in queue
        ]
        self._state = "running"
        self._block_reason = None
        await self._broadcast_status()
        if not self._scheduled_admission_allowed():
            await self._broadcast_status()
            return
        db.log_schedule_event(
            "job_starting", f"Derived queue: {len(entries)} device(s), concurrency=1",
        )
        try:
            job_id = await self.start_update_func(
                ap_ips=[], switch_ips=[], device_entries=entries,
                firmware_file=fw_30x, firmware_file_303l=fw_303l,
                firmware_file_tns100=fw_tns100, bank_mode="one", concurrency=1,
                start_hour=start_hour, end_hour=end_hour,
                schedule_days=schedule_days, schedule_timezone=timezone_name,
                window_key=window_key,
            )
        except Exception as exc:
            self._state = "waiting"
            self._block_reason = f"Queue admission failed: {exc}"
            db.log_schedule_event("job_start_failed", str(exc))
            logger.error("Scheduler failed to admit derived firmware queue: %s", exc)
            await self._broadcast_status()
            return

        # Count a target window only after the update runner has accepted the
        # queue. A readiness block or rejected job must leave its ordinal alone.
        try:
            for family, target_sha256, cutoff, encoded_key, ordinal in timing_admissions:
                timing = db.record_firmware_window_admission(
                    family, target_sha256, window_key, cutoff.isoformat(),
                )
                if timing["last_window_key"] != encoded_key or int(timing["ordinal"]) != ordinal:
                    raise ValueError(f"Firmware timing changed during admission for {family}")
        except Exception as exc:
            logger.error("Could not persist admitted firmware window timing: %s", exc)
            self._state = "waiting"
            self._block_reason = "Firmware window timing is unavailable"
            await self._broadcast_status()
            return

        self._current_job_id = job_id
        self._queue_job_context[job_id] = {
            "rollout": rollout,
            "window_key": window_key,
            "entries": queue,
        }
        db.log_schedule_event("job_started", f"Job {job_id} for derived firmware queue", job_id=job_id)
        return

    def _scheduled_admission_allowed(self) -> bool:
        """Check current readiness without yielding before admission."""
        admitted, reason = rollout_gate.current_scheduled_admission_decision()
        if not admitted:
            if self._state != "blocked_safety":
                db.log_schedule_event("blocked_safety", reason)
            self._state = "blocked_safety"
            self._block_reason = reason
        return admitted

    async def _precheck_cpe_credentials(
        self, parent_ap_ips: list[str], settings: dict, now: datetime
    ) -> tuple[tuple[str, str, str], ...]:
        """Fresh-check CPE credentials for APs in this candidate wave."""
        if not parent_ap_ips:
            return ()

        from .poller import get_poller

        poller = get_poller()
        failures = []
        for parent_ip in parent_ap_ips:
            generation = poller._cpe_credentials_generation if poller is not None else None
            ap = db.get_access_point(parent_ip)
            cpes = db.get_cpes_for_ap(parent_ip)
            for cpe in cpes:
                cpe_ip = cpe.get("ip") or "unknown"
                status = "precheck_unavailable"
                if cpe_ip == "unknown":
                    status = "missing_ip"
                elif ap and poller is not None:
                    try:
                        status = await poller.check_cpe_auth_for_period(
                            cpe_ip, ap["username"], ap["password"], settings, now,
                            force_fresh_login=True,
                            validate_cached_session=False,
                            credential_generation=generation,
                        )
                        db.update_cpe_auth_status(parent_ip, cpe_ip, status)
                    except Exception:
                        logger.exception("CPE credential precheck failed for %s", cpe_ip)
                        status = "precheck_unavailable"
                if status != "ok":
                    failures.append((parent_ip, cpe_ip, status))
        return tuple(failures)

    def _target_versions(self, settings: dict, rollout: Optional[dict]) -> dict[str, str]:
        """Build target versions for each firmware family."""
        rollout = rollout or {}
        file_names = {
            "tna-30x": rollout.get("firmware_file") or settings.get("selected_firmware_30x", ""),
            "tna-303l": rollout.get("firmware_file_303l") or settings.get("selected_firmware_303l", ""),
            "tns-100": rollout.get("firmware_file_tns100") or settings.get("selected_firmware_tns100", ""),
        }
        targets = {
            "tna-30x": _extract_version_from_filename(file_names["tna-30x"]) or rollout.get("target_version") or "",
            "tna-303l": _extract_version_from_filename(file_names["tna-303l"]) or rollout.get("target_version_303l") or "",
            "tns-100": _extract_version_from_filename(file_names["tns-100"]) or rollout.get("target_version_tns100") or "",
        }
        for fw_type, file_name in file_names.items():
            if file_name and not targets[fw_type]:
                targets[fw_type] = "__unknown__"
        return targets

    def _confirmed_family_devices(
        self, settings: dict, rollout: dict, targets: dict[str, str]
    ) -> dict[str, str]:
        """For each firmware family, return the IP of an in-scope managed device
        (AP/switch) CONFIRMED WORKING on the target version, or omit the family.

        Confirmed = a recorded confirmation event (updated to the version + passed
        post-update smoke tests) AND currently healthy on it (on-version, no
        `last_error`, seen within CONFIRMED_SEEN_WITHIN). This is the operator's
        manual canary: one confirmed device clears its family's Firmware Hold.
        Per-family — a confirmed tna-30x never clears tna-303l. Proof is restricted
        to the rollout's resolved scope. Fail-closed: an unreadable fleet yields no
        confirmations, so families stay held.
        """
        try:
            scope_ips = set(self._resolve_scope(settings))
            switch_scope = set(self._resolve_switch_scope(settings))
            ap_dict = db.get_all_access_points_dict(enabled_only=True)
            switch_dict = db.get_all_switches_dict(enabled_only=True)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Confirmed-family check could not read fleet, holding: {e}")
            return {}

        devices = (
            [ap_dict[ip] for ip in scope_ips if ip in ap_dict]
            + [switch_dict[ip] for ip in switch_scope if ip in switch_dict]
        )
        cutoff = datetime.now(timezone.utc) - CONFIRMED_SEEN_WITHIN
        confirmed_ips_cache: dict[str, set] = {}
        result: dict[str, str] = {}
        for dev in devices:
            fam = _firmware_type_for_model(dev.get("model"))
            if fam in result:
                continue
            target_v = targets.get(fam, "")
            if not target_v or target_v == "__unknown__":
                continue
            if dev.get("firmware_version") != target_v or dev.get("last_error"):
                continue
            if not _seen_within(dev.get("last_seen"), cutoff):
                continue
            if target_v not in confirmed_ips_cache:
                confirmed_ips_cache[target_v] = db.get_confirmed_ips_for_version(target_v)
            if dev["ip"] in confirmed_ips_cache[target_v]:
                result[fam] = dev["ip"]
        return result

    def _held_families(self, settings: dict, rollout: Optional[dict]) -> tuple[set, dict]:
        """Per-family Firmware Hold state for the firmware this rollout is rolling.

        A family is HELD when its target firmware's release-date hold has not
        elapsed AND no in-scope healthy device of that family is confirmed working
        on it. Returns (held_families, family_holds); family_holds maps each family
        with a selected firmware to {held, clears_at, confirmed_by, version,
        cleared_by_device}. `cleared_by_device` is True when the release-date hold
        has NOT elapsed but a confirmed device cleared the family early (for the
        honest UI/log note). Fail-closed via _confirmed_family_devices.
        """
        rollout = rollout or {}
        targets = self._target_versions(settings, rollout)
        files = {
            "tna-30x": rollout.get("firmware_file") or settings.get("selected_firmware_30x", ""),
            "tna-303l": rollout.get("firmware_file_303l") or settings.get("selected_firmware_303l", ""),
            "tns-100": rollout.get("firmware_file_tns100") or settings.get("selected_firmware_tns100", ""),
        }
        hold_days = _as_int(settings.get("firmware_canary_hold_days", "6"), 6)
        confirmed_by = (
            self._confirmed_family_devices(settings, rollout, targets) if hold_days > 0 else {}
        )

        held_families: set = set()
        family_holds: dict = {}
        for fam, fname in files.items():
            if not fname:
                continue
            target_v = targets.get(fam, "")
            if not target_v or target_v == "__unknown__":
                continue
            if hold_days <= 0:
                family_holds[fam] = {"held": False, "clears_at": None, "confirmed_by": None,
                                     "version": target_v, "cleared_by_device": False}
                continue
            info = db.get_firmware_hold_info(fname, hold_days)
            cleared_by_time = bool(info.get("cleared", True))
            prover = confirmed_by.get(fam)
            is_held = (not cleared_by_time) and (prover is None)
            family_holds[fam] = {
                "held": is_held,
                "clears_at": info.get("clears_at"),
                "confirmed_by": prover,
                "version": target_v,
                "cleared_by_device": (not cleared_by_time) and (prover is not None),
            }
            if is_held:
                held_families.add(fam)
        return held_families, family_holds

    @staticmethod
    def _firmware_hold_block_reason(held_families: set, family_holds: dict) -> str:
        """Human reason for a blocked_firmware_hold state."""
        soonest = None
        for fam in held_families:
            ca = (family_holds.get(fam) or {}).get("clears_at")
            if ca and (soonest is None or ca < soonest):
                soonest = ca
        models = ", ".join(sorted(held_families)) or "new firmware"
        if soonest:
            try:
                day = datetime.fromisoformat(soonest).date().isoformat()
                return (
                    f"Firmware hold — soaking {models} until {day} "
                    f"(or until a device is confirmed working on it)"
                )
            except (TypeError, ValueError):
                pass
        return f"Firmware hold — soaking {models} before the first fleet wave"

    def _ap_or_children_need_update(self, ap: dict, target_versions: dict[str, str], allow_downgrade: bool,
                                     cpes_by_ap: Optional[dict[str, list[dict]]] = None, cooldown_days: int = 0) -> bool:
        """Return True when an AP or any attached manageable CPE needs an update."""
        ap_target = target_versions.get(_firmware_type_for_model(ap.get("model")), "")
        if _device_needs_update(
            ap.get("firmware_version", ""),
            ap_target,
            allow_downgrade,
            last_update_iso=ap.get("last_firmware_update"),
            cooldown_days=cooldown_days
        ):
            return True

        cpes = cpes_by_ap.get(ap["ip"], []) if cpes_by_ap is not None else db.get_cpes_for_ap(ap["ip"])
        for cpe in cpes:
            if cpe.get("auth_status") != "ok":
                continue
            cpe_target = target_versions.get(_firmware_type_for_model(cpe.get("model")), "")
            if _device_needs_update(
                cpe.get("firmware_version", ""),
                cpe_target,
                allow_downgrade,
                last_update_iso=cpe.get("last_firmware_update"),
                cooldown_days=cooldown_days
            ):
                return True
        return False

    def _filter_devices_needing_update(self, scope_ips: list[str], target_versions: dict[str, str], allow_downgrade: bool = False, cooldown_days: int = 0) -> list[str]:
        """Filter scope APs to those whose firmware differs from target."""
        ap_dict = db.get_all_access_points_dict(enabled_only=False)
        cpes_by_ap = db.get_all_cpes_grouped()
        needs_update = []

        for ip in scope_ips:
            ap = ap_dict.get(ip)
            if not ap:
                continue
            if self._ap_or_children_need_update(ap, target_versions, allow_downgrade, cpes_by_ap=cpes_by_ap, cooldown_days=cooldown_days):
                needs_update.append(ip)
        return needs_update

    def _filter_switches_needing_update(self, scope_ips: list[str], target_versions: dict[str, str], allow_downgrade: bool = False, cooldown_days: int = 0) -> list[str]:
        """Filter scope switches to those whose firmware differs from target."""
        switch_dict = db.get_all_switches_dict(enabled_only=False)
        needs_update = []

        for ip in scope_ips:
            switch = switch_dict.get(ip)
            if not switch:
                continue
            target_version = target_versions.get(_firmware_type_for_model(switch.get("model")), "")
            if _device_needs_update(
                switch.get("firmware_version", ""),
                target_version,
                allow_downgrade,
                last_update_iso=switch.get("last_firmware_update"),
                cooldown_days=cooldown_days
            ):
                needs_update.append(ip)
        return needs_update

    def _derive_pending_queue(
        self,
        settings: dict,
        rollout: Optional[dict] = None,
        allow_downgrade: bool = False,
        held_families: Optional[set[str]] = None,
        recovery_ip: Optional[str] = None,
    ) -> list[dict]:
        """Build the current ordered queue from enabled Manager inventory.

        This is a selector only. The existing wave dispatcher remains the
        runtime admission path until the queue cutover.
        """
        target_versions = self._target_versions(settings, rollout or {})
        cooldown_days = _as_int(settings.get("firmware_update_cooldown_days", "30"), 30)
        if held_families is None:
            held_families, _ = self._held_families(settings, rollout)
        ap_scope = set(self._resolve_scope(settings))
        switch_scope = set(self._resolve_switch_scope(settings))
        aps = db.get_all_access_points_dict(enabled_only=True)
        switches = db.get_all_switches_dict(enabled_only=True)
        cpes_by_ap = db.get_all_cpes_grouped()
        entries: list[dict] = []
        address_owners: set[tuple[int, int]] = set()

        def add_device(device: dict, role: str, parent_ip: Optional[str] = None) -> None:
            ip = device.get("ip")
            family = _known_firmware_family(device.get("model"))
            if not family:
                if not any(target_versions.values()):
                    return
                raise ValueError(f"Pending queue held: unknown model for {role} {ip or 'without address'}")
            target = target_versions.get(family, "")
            if not target:
                return
            if target == "__unknown__":
                raise ValueError(f"Pending queue held: target version is unreadable for {family}")
            if not ip:
                raise ValueError(f"Pending queue held: {role} has no management address")
            try:
                address_key = _normalized_ip(ip)
            except ValueError as exc:
                raise ValueError(f"Pending queue held: invalid management address for {role} {ip}") from exc
            if address_key in address_owners:
                raise ValueError(f"Pending queue held: duplicate management address {ip}")
            address_owners.add(address_key)

            version = device.get("firmware_version")
            if not version:
                raise ValueError(f"Pending queue held: reported version is unreadable for {role} {ip}")
            if family in held_families:
                return
            needs_update = _device_needs_update(
                version, target, allow_downgrade,
                last_update_iso=device.get("last_firmware_update"),
                cooldown_days=cooldown_days,
            )
            if not needs_update and ip != recovery_ip:
                return
            entries.append({
                "ip": ip,
                "device_type": role,
                "parent_ip": parent_ip,
                "model": device.get("model"),
                "family": family,
                "target_version": target,
                "verify_recovery_if_current": ip == recovery_ip and not needs_update,
            })

        for parent_ip in sorted(ap_scope):
            ap = aps.get(parent_ip)
            if not ap:
                continue
            add_device(ap, "ap")
            for cpe in cpes_by_ap.get(parent_ip, []):
                # The cache is populated by the AP peer report. Keep the same
                # authenticated-peer eligibility used by existing scheduling.
                if cpe.get("auth_status") == "ok":
                    add_device(cpe, "cpe", parent_ip)
        for ip in sorted(switch_scope):
            switch = switches.get(ip)
            if switch:
                add_device(switch, "switch")

        entries.sort(key=lambda item: (
            0 if item["ip"] == recovery_ip else 1,
            {"cpe": 0, "ap": 1, "switch": 2}[item["device_type"]],
            _normalized_ip(item["ip"]),
            _normalized_ip(item["parent_ip"]) if item["parent_ip"] else (0, 0),
        ))
        return entries

    def on_job_completed(
        self,
        job_id: str,
        success_count: int,
        failed_count: int,
        learned_versions: Optional[dict[str, str]] = None,
        device_statuses: Optional[dict[str, str]] = None,
        cancel_reason: Optional[str] = None,
    ):
        """Record completion for an admitted derived-queue job."""
        queue_context = self._queue_job_context.pop(job_id, None)
        if queue_context is None:
            logger.warning("Scheduler: ignoring completion for unknown job %s", job_id)
            return

        if self._current_job_id == job_id:
            self._current_job_id = None
        self._last_run_time = datetime.now().isoformat()
        window_key = queue_context["window_key"]
        entries = queue_context["entries"]
        if failed_count > 0:
            failed_ip = next(
                (ip for ip, status in (device_statuses or {}).items()
                 if status not in ("success", "updated", "deferred", "skipped")),
                entries[0]["ip"] if entries else "unknown",
            )
            stop = db.stop_firmware_window(
                window_key, job_id, failed_ip, "scheduled_update",
                f"{failed_count} device(s) failed during the derived firmware queue",
            )
            reason = (stop or {}).get("reason") or "A device failed during the firmware queue"
            self._last_run_result = f"Firmware queue stopped: {reason}"
            db.log_schedule_event("firmware_window_stopped", reason, job_id=job_id)
            logger.warning("Firmware queue stopped for job %s: %s", job_id, reason)
        elif cancel_reason:
            self._last_run_result = cancel_reason
            db.log_schedule_event("job_deferred", cancel_reason, job_id=job_id)
        else:
            self._last_run_result = f"Queue job completed ({success_count} device(s))"
            db.log_schedule_event("job_completed", self._last_run_result, job_id=job_id)
        if self._state != "blocked_safety":
            self._state = "waiting"
            self._block_reason = self._last_run_result
        task = asyncio.create_task(self._broadcast_status())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

    def _resolve_scope(self, settings: dict) -> list[str]:
        """Resolve which AP IPs to update based on scope settings."""
        scope = settings.get("schedule_scope", "all")
        scope_data = settings.get("schedule_scope_data", "")

        if scope == "all":
            aps = db.get_access_points(enabled_only=True)
            return [ap["ip"] for ap in aps]

        if scope == "sites":
            site_ids = [int(s.strip()) for s in scope_data.split(",") if s.strip().isdigit()]
            ips = []
            for site_id in site_ids:
                aps = db.get_access_points(tower_site_id=site_id, enabled_only=True)
                ips.extend(ap["ip"] for ap in aps)
            return ips

        if scope == "aps":
            return [ip.strip() for ip in scope_data.split(",") if ip.strip()]

        return []

    def _resolve_switch_scope(self, settings: dict) -> list[str]:
        """Resolve which switches participate in rollout phases."""
        scope = settings.get("schedule_scope", "all")
        scope_data = settings.get("schedule_scope_data", "")

        if scope == "all":
            return [sw["ip"] for sw in db.get_switches(enabled_only=True)]

        if scope == "sites":
            site_ids = [int(s.strip()) for s in scope_data.split(",") if s.strip().isdigit()]
            ips = []
            for site_id in site_ids:
                ips.extend(sw["ip"] for sw in db.get_switches(tower_site_id=site_id, enabled_only=True))
            return ips

        if scope == "aps":
            ap_ips = [ip.strip() for ip in scope_data.split(",") if ip.strip()]
            site_ids = {
                ap.get("tower_site_id")
                for ap_ip in ap_ips
                for ap in [db.get_access_point(ap_ip)]
                if ap and ap.get("tower_site_id") is not None
            }
            ips = []
            for site_id in site_ids:
                ips.extend(sw["ip"] for sw in db.get_switches(tower_site_id=site_id, enabled_only=True))
            return ips

        return []

    def compute_next_attempt(
        self,
        ip: str,
        role: str,
        parent_ap_ip: Optional[str] = None,
        settings: Optional[dict] = None,
        rollout: Optional[dict] = _UNSET,
        rollout_devices_by_ip: Optional[dict] = None,
        scope_ips: Optional[set] = None,
        switch_scope: Optional[set] = None,
        hold_clears_at_iso: Optional[str] = None,
    ) -> dict:
        """Return the next scheduled window when this device can be considered.

        Returns a dict with:
          - auto_update_eligible: bool — whether the scheduler will ever pick
            this device up given current scope/settings
          - next_attempt_iso: ISO datetime string or None
          - reason: short user-facing string when eligible is False or the
            attempt is blocked (e.g. waiting on the firmware hold to clear)

        `hold_clears_at_iso` is the device's Firmware Hold clear time (ISO), or
        None when its model family's hold is already clear. When set, the hold
        pushes the next eligible window past that date. Callers iterating over
        many devices (e.g. /api/fleet-status) can pre-resolve `settings`,
        `scope_ips`, and `switch_scope` to avoid repeated scope queries.
        """
        settings = settings or db.get_all_settings()

        if settings.get("schedule_enabled") != "true":
            return {"auto_update_eligible": False, "next_attempt_iso": None,
                    "reason": "Auto-update is off"}

        admitted, reason = rollout_gate.scheduled_admission_decision(settings)
        if not admitted:
            return {"auto_update_eligible": False, "next_attempt_iso": None,
                    "reason": reason}

        schedule_days = settings.get("schedule_days", "")
        start_hour = _as_int(settings.get("schedule_start_hour", "3"), 3)
        end_hour = _as_int(settings.get("schedule_end_hour", "4"), 4)
        if not _parse_schedule_days(schedule_days):
            return {"auto_update_eligible": False, "next_attempt_iso": None,
                    "reason": "No maintenance days configured"}

        # Resolve scope membership. CPEs piggy-back on their parent AP.
        if role == "switch":
            if switch_scope is None:
                switch_scope = set(self._resolve_switch_scope(settings))
            in_scope = ip in switch_scope
        elif role == "cpe":
            if scope_ips is None:
                scope_ips = set(self._resolve_scope(settings))
            in_scope = bool(parent_ap_ip) and parent_ap_ip in scope_ips
        else:
            if scope_ips is None:
                scope_ips = set(self._resolve_scope(settings))
            in_scope = ip in scope_ips
        if not in_scope:
            return {"auto_update_eligible": False, "next_attempt_iso": None,
                    "reason": "Not in auto-update scope"}

        fw_30x = settings.get("selected_firmware_30x", "")
        if not fw_30x:
            return {"auto_update_eligible": True, "next_attempt_iso": None,
                    "reason": "Waiting for firmware to be selected"}

        # The Firmware Hold (computed per family by the caller) delays this
        # device until its family clears.
        hold_clears_at: Optional[datetime] = None
        if hold_clears_at_iso:
            try:
                hold_clears_at = datetime.fromisoformat(hold_clears_at_iso)
            except (TypeError, ValueError):
                hold_clears_at = None

        # Firmware Hold delays a device until its family is clear. The queue
        # does not assign devices to later percentage waves.
        earliest = hold_clears_at

        windows = upcoming_window_starts(
            datetime.now(), schedule_days, start_hour, end_hour,
            count=1, earliest_after=earliest,
        )
        if not windows:
            return {"auto_update_eligible": True, "next_attempt_iso": None,
                    "reason": None}
        target = windows[0]
        reason = None
        if earliest and target.date() == earliest.date():
            reason = "Waiting for the firmware hold to clear"
        return {"auto_update_eligible": True,
                "next_attempt_iso": target.isoformat(),
                "reason": reason}

    def get_status(self) -> dict:
        """Return current scheduler status for UI."""
        settings = db.get_all_settings()
        start_hour = _as_int(settings.get("schedule_start_hour", "3"), 3)
        end_hour = _as_int(settings.get("schedule_end_hour", "4"), 4)
        schedule_days = settings.get("schedule_days", "")

        rollout_info = None
        rollout = db.get_current_rollout()
        if rollout:
            progress = db.get_rollout_progress(rollout["id"])
            remaining_devices = None
            cutoff_utc = None
            try:
                pending = self._derive_pending_queue(
                    settings, rollout, settings.get("allow_downgrade", "false") == "true",
                    held_families=set(),
                )
                remaining_devices = len(pending)
                if pending:
                    family = pending[0]["family"]
                    artifact = {
                        "tna-30x": rollout.get("firmware_file") or settings.get("selected_firmware_30x", ""),
                        "tna-303l": rollout.get("firmware_file_303l") or settings.get("selected_firmware_303l", ""),
                        "tns-100": rollout.get("firmware_file_tns100") or settings.get("selected_firmware_tns100", ""),
                    }.get(family)
                    target_sha256 = db.get_firmware_sha256(artifact) if artifact else None
                    window_key = rollout_gate.firmware_window_key(settings, datetime.now())
                    if target_sha256 and window_key:
                        encoded_key = json.dumps(list(window_key), separators=(",", ":"))
                        with db.get_db() as conn:
                            timing = conn.execute(
                                "SELECT last_window_key, cutoff_utc FROM firmware_window_timing "
                                "WHERE family = ? AND target_sha256 = ?",
                                (family, target_sha256),
                            ).fetchone()
                        if timing and timing["last_window_key"] == encoded_key:
                            cutoff_utc = timing["cutoff_utc"]
            except Exception as exc:
                logger.debug("Could not calculate current queue status: %s", exc)
            rollout_info = {
                "id": rollout["id"],
                "phase": rollout["phase"],
                "status": rollout["status"],
                "target_version": rollout.get("target_version"),
                "firmware_file": rollout["firmware_file"],
                "firmware_file_tns100": rollout.get("firmware_file_tns100"),
                "progress": progress,
                "remaining_devices": remaining_devices,
                "pause_reason": rollout.get("pause_reason") or self._block_reason,
                "cutoff_utc": cutoff_utc,
            }

        # Per-family Firmware Hold state for the selected firmware (or the active
        # rollout's): the release-date soak, clearable early by a confirmed device.
        hold_days = _as_int(settings.get("firmware_canary_hold_days", "6"), 6)
        firmware_hold = None
        try:
            held_families, family_holds = self._held_families(
                settings, rollout if rollout and rollout.get("status") in ("active", "paused") else None
            )
            if family_holds:
                firmware_hold = {
                    "hold_days": hold_days,
                    "any_held": bool(held_families),
                    "families": family_holds,
                }
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"Firmware-hold status computation failed: {e}")

        next_windows = upcoming_window_starts(
            datetime.now(), schedule_days, start_hour, end_hour, count=1
        )
        next_window_iso = next_windows[0].isoformat() if next_windows else None

        return {
            "state": self._state,
            "safety_block_reason": rollout_gate.scheduled_admission_decision(settings)[1],
            "block_reason": self._block_reason,
            "weather": self._weather_info,
            "last_run_result": self._last_run_result,
            "last_run_time": self._last_run_time,
            "current_job_id": self._current_job_id,
            "next_window": f"{start_hour}:00-{end_hour}:00 on {schedule_days}",
            "next_window_iso": next_window_iso,
            "schedule_start_hour": start_hour,
            "schedule_end_hour": end_hour,
            "rollout": rollout_info,
            "firmware_hold": firmware_hold,
            "hold_clear_reason": self._hold_clear_reason,
        }

    async def _broadcast_status(self):
        """Send scheduler_status WebSocket message."""
        if self.broadcast_func:
            await self.broadcast_func({"type": "scheduler_status", **self.get_status()})


def get_scheduler() -> Optional[AutoUpdateScheduler]:
    """Get the global scheduler instance."""
    return _scheduler


def init_scheduler(broadcast_func: Callable, start_update_func: Callable, check_interval: int = 60) -> AutoUpdateScheduler:
    """Initialize the global scheduler instance."""
    global _scheduler
    _scheduler = AutoUpdateScheduler(broadcast_func, start_update_func, check_interval)
    return _scheduler
