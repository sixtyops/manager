"""Auto-update scheduler with gradual rollout support."""

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Callable, Optional, Set

from . import database as db
from . import services
from .services import format_temperature
from . import license as lic

logger = logging.getLogger(__name__)

# Do not start new scheduled jobs when the window has this many minutes or less remaining.
SCHEDULE_END_BUFFER_MINUTES = 15


def _parse_version(version: str) -> tuple:
    """Parse version string into tuple for comparison.

    Handles formats like '1.12.3.54970' or '1.12.3.r54970'.
    Returns tuple of integers for comparison.
    """
    if not version:
        return (0,)
    normalized = version.replace(".r", ".")
    parts = []
    for part in normalized.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _extract_version_from_filename(filename: str) -> str:
    """Extract normalized version from a firmware filename."""
    import re

    if not filename:
        return ""
    match = re.search(
        r"(?:tna-30x|tna30x|tna-303l|tna303l|tns-100|tns100)-(\d+\.\d+\.\d+)-r(\d+)",
        filename,
        re.IGNORECASE,
    )
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.search(r"(\d+\.\d+\.\d+)", filename)
    return match.group(1) if match else ""


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

    if current_version == target_version:
        return False
    if not allow_downgrade and _parse_version(current_version) > _parse_version(target_version):
        return False
    return True


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
        "blocked_quarantine",
    )

    def __init__(self, broadcast_func: Callable, start_update_func: Callable, check_interval: int = 60):
        self.broadcast_func = broadcast_func
        self.start_update_func = start_update_func
        self.check_interval = check_interval

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._state = "disabled"
        self._block_reason: Optional[str] = None
        self._weather_info: Optional[dict] = None
        self._weather_checked_today: Optional[str] = None
        self._weather_ok: Optional[bool] = None
        self._last_run_result: Optional[str] = None
        self._last_run_time: Optional[str] = None
        self._current_job_id: Optional[str] = None
        self._ran_today: Set[str] = set()
        self._manual_canary_job_ids: Set[str] = set()

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
        # Recover _ran_today from DB to prevent double-run after restart
        today_key = datetime.now().strftime("%Y-%m-%d")
        try:
            with db.get_db() as conn:
                row = conn.execute(
                    "SELECT 1 FROM schedule_log WHERE event = 'job_started' "
                    "AND DATE(timestamp) = ? LIMIT 1",
                    (today_key,),
                ).fetchone()
                if row:
                    self._ran_today.add(today_key)
                    logger.info(f"Scheduler: recovered _ran_today for {today_key} from DB")
        except Exception as e:
            logger.warning(f"Scheduler: failed to recover _ran_today: {e}")
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"Auto-update scheduler started (interval: {self.check_interval}s)")
        # Fetch weather eagerly so the header shows temperature immediately
        asyncio.create_task(self._fetch_weather_initial())

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
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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
        """Main decision logic each tick."""
        settings = db.get_all_settings()
        schedule_enabled = settings.get("schedule_enabled")
        bank_mode = settings.get("bank_mode", "both")

        if schedule_enabled != "true":
            if self._state != "disabled":
                logger.info(f"Scheduler disabled (schedule_enabled={schedule_enabled!r})")
                self._state = "disabled"
                self._block_reason = None
                await self._broadcast_status()
            return

        if self._current_job_id is not None:
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
                scope_ips, switch_scope = self._apply_free_tier_limits(scope_ips, switch_scope)
                target_versions = self._target_versions(settings, None)
                needs_update = self._filter_devices_needing_update(
                    scope_ips, target_versions, allow_downgrade
                )
                needs_switch_update = self._filter_switches_needing_update(
                    switch_scope, target_versions, allow_downgrade
                )
                if not needs_update and not needs_switch_update:
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
            today_key = now.strftime("%Y-%m-%d")
            if current_hour >= end_hour and today_key in self._ran_today:
                pass
            elif current_hour < start_hour:
                self._ran_today.clear()

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

        today_key = now.strftime("%Y-%m-%d")
        if today_key in self._ran_today:
            active_rollout = db.get_last_rollout_for_firmware(fw_30x) if fw_30x else None
            if not active_rollout or active_rollout["status"] != "active":
                if self._state != "waiting":
                    self._state = "waiting"
                    self._block_reason = "Already ran today"
                    await self._broadcast_status()
                return
            logger.info(f"Continuing active rollout {active_rollout['id']} (phase {active_rollout['phase']})")

        if end_hour > current_hour:
            minutes_until_end = (end_hour - current_hour) * 60 - now.minute
        else:
            minutes_until_end = (24 - current_hour + end_hour) * 60 - now.minute
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

        hold_days = _as_int(settings.get("firmware_quarantine_days", "7"), 7)
        if hold_days > 0:
            for fw_name in (fw_30x, fw_303l, fw_tns100):
                if not fw_name:
                    continue
                hold_info = db.get_firmware_hold_info(fw_name, hold_days)
                if not hold_info["cleared"]:
                    self._state = "blocked_quarantine"
                    remaining_days = hold_info["remaining_days"]
                    remaining_str = f"{remaining_days:.1f} days" if remaining_days >= 1 else f"{remaining_days * 24:.0f} hours"
                    self._block_reason = f"On hold ({remaining_str}) - new firmware waiting period"
                    db.log_schedule_event("blocked_quarantine", self._block_reason)
                    logger.info(f"Scheduler blocked: {self._block_reason}")
                    await self._broadcast_status()
                    return

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
                scope_ips, switch_scope = self._apply_free_tier_limits(scope_ips, switch_scope)
                target_versions = self._target_versions(settings, None)
                needs_update = self._filter_devices_needing_update(
                    scope_ips, target_versions, allow_downgrade
                )
                needs_switch_update = self._filter_switches_needing_update(
                    switch_scope, target_versions, allow_downgrade
                )
                if not needs_update and not needs_switch_update:
                    self._state = "blocked_all_current"
                    self._block_reason = "All devices up to date"
                    await self._broadcast_status()
                    return

            rollout_id = db.create_rollout(fw_30x, fw_303l if fw_303l else None, fw_tns100 if fw_tns100 else None)
            rollout = db.get_rollout(rollout_id)
            # Snapshot update-relevant settings for rollout consistency
            snapshot_keys = ["bank_mode", "parallel_updates", "allow_downgrade", "pre_update_reboot",
                             "bandwidth_limit_kbps", "schedule_start_hour", "schedule_end_hour",
                             "schedule_days", "schedule_timezone"]
            snapshot = {k: settings.get(k, "") for k in snapshot_keys}
            db.set_rollout_settings_snapshot(rollout_id, snapshot)
            db.log_schedule_event("rollout_created", f"Rollout {rollout_id} for {fw_30x}")
            logger.info(f"Created rollout {rollout_id} for firmware {fw_30x}")

        if rollout["status"] == "paused":
            self._state = "waiting"
            self._block_reason = f"Rollout paused: {rollout.get('pause_reason', 'Unknown reason')}"
            await self._broadcast_status()
            return

        scope_ips = self._resolve_scope(settings)
        switch_scope = self._resolve_switch_scope(settings)
        scope_ips, switch_scope = self._apply_free_tier_limits(scope_ips, switch_scope)

        if not scope_ips and not switch_scope:
            self._state = "idle"
            self._block_reason = "No devices in scope"
            await self._broadcast_status()
            return

        batch_ips = self._get_devices_for_phase(rollout, scope_ips, allow_downgrade, settings)
        batch_switches = self._get_switches_for_phase(rollout, switch_scope, allow_downgrade, settings)

        if not batch_ips and not batch_switches:
            current_phase = rollout["phase"]
            db.complete_rollout_phase(rollout["id"])
            refreshed = db.get_rollout(rollout["id"])

            if refreshed["status"] == "completed":
                self._state = "blocked_all_current"
                self._block_reason = "All devices up to date"
                db.log_schedule_event("rollout_completed", f"Rollout {rollout['id']} completed")
                logger.info(f"Rollout {rollout['id']} completed - all devices up to date")
                await self._broadcast_status()
                return

            logger.info(f"Phase {current_phase} has no candidates, advanced to {refreshed['phase']}")
            batch_ips = self._get_devices_for_phase(refreshed, scope_ips, allow_downgrade, settings)
            batch_switches = self._get_switches_for_phase(refreshed, switch_scope, allow_downgrade, settings)
            rollout = refreshed

            if not batch_ips and not batch_switches:
                db.complete_rollout_phase(rollout["id"])
                self._state = "blocked_all_current"
                self._block_reason = "All devices up to date"
                await self._broadcast_status()
                return

        for ip in batch_ips:
            db.assign_device_to_rollout(rollout["id"], ip, "ap", rollout["phase"])
        for ip in batch_switches:
            db.assign_device_to_rollout(rollout["id"], ip, "switch", rollout["phase"])

        self._state = "running"
        self._block_reason = None
        await self._broadcast_status()

        # Use snapshotted settings if available, fall back to current
        rollout_settings = db.get_rollout_settings_snapshot(rollout["id"]) or settings
        bank_mode = rollout_settings.get("bank_mode", settings.get("bank_mode", "both"))
        concurrency = _as_int(rollout_settings.get("parallel_updates", settings.get("parallel_updates", "2")), 2)
        phase = rollout["phase"]
        db.log_schedule_event(
            "job_starting",
            f"Rollout {rollout['id']} phase={phase}, {len(batch_ips)} APs, {len(batch_switches)} switches, bank_mode={bank_mode}",
        )
        logger.info(f"Scheduler starting rollout phase {phase}: {len(batch_ips)} APs, {len(batch_switches)} switches")

        try:
            job_id = await self.start_update_func(
                ap_ips=batch_ips,
                switch_ips=batch_switches,
                firmware_file=fw_30x,
                firmware_file_303l=fw_303l,
                firmware_file_tns100=fw_tns100,
                bank_mode=bank_mode,
                concurrency=concurrency,
                start_hour=start_hour,
                end_hour=end_hour,
                schedule_days=schedule_days,
                schedule_timezone=tz_str,
            )
            self._current_job_id = job_id
            self._ran_today.add(today_key)
            db.set_rollout_job_id(rollout["id"], job_id)
            db.log_schedule_event("job_started", f"Job {job_id} for rollout {rollout['id']}", job_id=job_id)
        except Exception as e:
            self._state = "idle"
            self._block_reason = f"Failed to start: {e}"
            db.log_schedule_event("job_start_failed", str(e))
            logger.error(f"Scheduler failed to start update: {e}")
            await self._broadcast_status()

    async def trigger_canary_now(self):
        """Start the canary phase immediately, outside the maintenance window."""
        settings = db.get_all_settings()
        if settings.get("schedule_enabled") != "true":
            raise RuntimeError("Automatic firmware rollout is disabled")
        if self._current_job_id is not None:
            raise RuntimeError("A rollout job is already running")

        tz_str = settings.get("timezone", "auto")
        if tz_str == "auto":
            tz_str = await services.get_timezone()

        time_ok, time_result = await services.validate_time_sources(tz_str)
        if not time_ok:
            raise RuntimeError(str(time_result))
        now = time_result

        if settings.get("weather_check_enabled") == "true":
            today_key = now.strftime("%Y-%m-%d")
            if self._weather_checked_today != today_key:
                zip_code = settings.get("zip_code", "")
                min_temp_c = float(settings.get("min_temperature_c", "-10"))
                weather_ok, weather_data = await services.check_weather_ok(
                    zip_code if zip_code else None, min_temp_c
                )
                self._weather_info = weather_data
                self._weather_ok = weather_ok
                self._weather_checked_today = today_key
            else:
                weather_ok = self._weather_ok
            if not weather_ok:
                temp_c = self._weather_info.get("temperature_c") if self._weather_info else None
                min_temp_c = float(settings.get("min_temperature_c", "-10"))
                temp_unit = await services.resolve_temperature_unit(settings.get("temperature_unit", "auto"))
                temp_str = format_temperature(temp_c, temp_unit) if temp_c is not None else "?"
                min_temp_str = format_temperature(min_temp_c, temp_unit)
                raise RuntimeError(f"Temperature {temp_str} is below minimum {min_temp_str}")

        fw_30x = settings.get("selected_firmware_30x", "")
        if not fw_30x:
            raise RuntimeError("No firmware selected")
        fw_303l = settings.get("selected_firmware_303l", "")
        fw_tns100 = settings.get("selected_firmware_tns100", "")
        allow_downgrade = settings.get("allow_downgrade", "false") == "true"

        hold_days = int(settings.get("firmware_quarantine_days", "7"))
        if hold_days > 0:
            for fw_name in (fw_30x, fw_303l, fw_tns100):
                if not fw_name:
                    continue
                hold_info = db.get_firmware_hold_info(fw_name, hold_days)
                if not hold_info["cleared"]:
                    remaining_days = hold_info["remaining_days"]
                    remaining_str = f"{remaining_days:.1f} days" if remaining_days >= 1 else f"{remaining_days * 24:.0f} hours"
                    raise RuntimeError(f"On hold ({remaining_str}) - new firmware waiting period")

        rollout = db.get_active_rollout()
        if rollout and self._firmware_changed(rollout, fw_30x, fw_303l, fw_tns100):
            db.cancel_rollout(rollout["id"])
            db.log_schedule_event("rollout_cancelled", "Firmware selection changed")
            rollout = None
        if rollout and rollout["status"] == "paused":
            raise RuntimeError("Current rollout is paused. Resume or reset it first")
        if rollout and rollout["phase"] != "canary":
            raise RuntimeError("Current rollout is already past canary")

        scope_ips = self._resolve_scope(settings)
        switch_scope = self._resolve_switch_scope(settings)
        scope_ips, switch_scope = self._apply_free_tier_limits(scope_ips, switch_scope)

        if rollout is None:
            last = db.get_last_rollout_for_firmware_set(fw_30x, fw_303l, fw_tns100)
            if last and last["status"] == "completed":
                target_versions = self._target_versions(settings, None)
                needs_update = self._filter_devices_needing_update(
                    scope_ips, target_versions, allow_downgrade
                )
                needs_switch_update = self._filter_switches_needing_update(
                    switch_scope, target_versions, allow_downgrade
                )
                if not needs_update and not needs_switch_update:
                    raise RuntimeError("All devices are already current")
            rollout_id = db.create_rollout(fw_30x, fw_303l if fw_303l else None, fw_tns100 if fw_tns100 else None)
            rollout = db.get_rollout(rollout_id)
            db.log_schedule_event("rollout_created", f"Manual canary rollout {rollout_id} for {fw_30x}")

        batch_ips = self._get_devices_for_phase(rollout, scope_ips, allow_downgrade, settings)
        batch_switches = self._get_switches_for_phase(rollout, switch_scope, allow_downgrade, settings)
        if rollout["phase"] != "canary":
            raise RuntimeError("Canary is no longer pending for this rollout")
        if not batch_ips and not batch_switches:
            raise RuntimeError("No pending canary devices need updates")

        for ip in batch_ips:
            db.assign_device_to_rollout(rollout["id"], ip, "ap", rollout["phase"])
        for ip in batch_switches:
            db.assign_device_to_rollout(rollout["id"], ip, "switch", rollout["phase"])

        self._state = "running"
        self._block_reason = None
        await self._broadcast_status()

        bank_mode = settings.get("bank_mode", "both")
        concurrency = int(settings.get("parallel_updates", "2"))
        end_hour = int(settings.get("schedule_end_hour", "4"))
        try:
            job_id = await self.start_update_func(
                ap_ips=batch_ips,
                switch_ips=batch_switches,
                firmware_file=fw_30x,
                firmware_file_303l=fw_303l,
                firmware_file_tns100=fw_tns100,
                bank_mode=bank_mode,
                concurrency=concurrency,
                end_hour=end_hour,
                schedule_timezone=tz_str,
                enforce_window_cutoff=False,
            )
            self._current_job_id = job_id
            self._manual_canary_job_ids.add(job_id)
            db.set_rollout_job_id(rollout["id"], job_id)
            db.log_schedule_event("job_started", f"Manual canary job {job_id} for rollout {rollout['id']}", job_id=job_id)
            await self._broadcast_status()
        except Exception:
            self._state = "idle"
            self._block_reason = "Failed to start canary"
            await self._broadcast_status()
            raise

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

    def _get_devices_for_phase(
        self,
        rollout: dict,
        scope_ips: list[str],
        allow_downgrade: bool = False,
        settings: Optional[dict] = None,
    ) -> list[str]:
        """Determine which APs to update in the current phase."""
        phase = rollout["phase"]
        rollout_id = rollout["id"]
        actual_settings = settings or db.get_all_settings()
        target_versions = self._target_versions(actual_settings, rollout)
        cooldown_days = _as_int(actual_settings.get("firmware_update_cooldown_days", "30"), 30)

        existing_devices = db.get_rollout_devices(rollout_id)
        already_done = {d["ip"] for d in existing_devices if d["status"] in ("updated", "pending", "failed")}

        ap_dict = db.get_all_access_points_dict(enabled_only=False)
        cpes_by_ap = db.get_all_cpes_grouped()
        candidates = []
        for ip in scope_ips:
            if ip in already_done:
                continue
            ap = ap_dict.get(ip)
            if not ap:
                continue
            if self._ap_or_children_need_update(
                ap, target_versions, allow_downgrade,
                cpes_by_ap=cpes_by_ap, cooldown_days=cooldown_days
            ):
                candidates.append(ip)

        preferred_canaries = self._preferred_canary_devices(actual_settings, "rollout_canary_aps")
        return self._select_phase_batch(phase, candidates, preferred_canaries)

    def _get_switches_for_phase(
        self,
        rollout: dict,
        scope_ips: list[str],
        allow_downgrade: bool = False,
        settings: Optional[dict] = None,
    ) -> list[str]:
        """Determine which switches to update in the current phase."""
        phase = rollout["phase"]
        rollout_id = rollout["id"]
        actual_settings = settings or db.get_all_settings()
        target_versions = self._target_versions(actual_settings, rollout)
        cooldown_days = _as_int(actual_settings.get("firmware_update_cooldown_days", "30"), 30)

        existing_devices = db.get_rollout_devices(rollout_id)
        already_done = {d["ip"] for d in existing_devices if d["status"] in ("updated", "pending", "failed")}

        switch_dict = db.get_all_switches_dict(enabled_only=False)
        candidates = []
        for ip in scope_ips:
            if ip in already_done:
                continue
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
                candidates.append(ip)

        preferred_canaries = self._preferred_canary_devices(actual_settings, "rollout_canary_switches")
        return self._select_phase_batch(phase, candidates, preferred_canaries)

    def _select_phase_batch(self, phase: str, candidates: list[str], preferred_canaries: Optional[list[str]] = None) -> list[str]:
        """Select the devices for a rollout phase, preferring configured canaries."""
        if not candidates:
            return []

        if phase == "canary":
            preferred = [ip for ip in (preferred_canaries or []) if ip in candidates]
            if preferred:
                return preferred
            return candidates[:1]
        if phase == "pct10":
            batch_size = max(1, math.ceil(len(candidates) * 0.1))
        elif phase == "pct50":
            batch_size = max(1, math.ceil(len(candidates) * 0.5))
        else:
            batch_size = len(candidates)
        return candidates[:batch_size]

    def _preferred_canary_devices(self, settings: Optional[dict], key: str) -> list[str]:
        """Return unique, ordered canary IPs from settings."""
        if not settings:
            return []
        result = []
        seen = set()
        for raw in settings.get(key, "").split(","):
            ip = raw.strip()
            if not ip or ip in seen:
                continue
            seen.add(ip)
            result.append(ip)
        return result

    def on_job_completed(
        self,
        job_id: str,
        success_count: int,
        failed_count: int,
        learned_versions: Optional[dict[str, str]] = None,
        device_statuses: Optional[dict[str, str]] = None,
        cancel_reason: Optional[str] = None,
    ):
        """Called when a scheduled job finishes."""
        if self._current_job_id != job_id:
            return

        self._current_job_id = None
        self._last_run_time = datetime.now().isoformat()
        manual_canary = job_id in self._manual_canary_job_ids
        if manual_canary:
            self._manual_canary_job_ids.discard(job_id)

        rollout = db.get_active_rollout()
        if rollout and rollout.get("last_job_id") == job_id:
            # Job was cancelled/deferred with no progress — don't advance phase
            if cancel_reason and success_count == 0 and failed_count == 0:
                if device_statuses:
                    for ip, status in device_statuses.items():
                        db.mark_rollout_device(rollout["id"], ip, status)
                db.log_schedule_event("job_deferred", cancel_reason, job_id=job_id)
                self._state = "waiting"
                self._block_reason = cancel_reason
                logger.info(f"Scheduler job {job_id} deferred: {cancel_reason}")
                return

            if failed_count > 0:
                reason = f"{failed_count} device(s) failed during {rollout['phase']} phase"
                db.pause_rollout(rollout["id"], reason)
                if device_statuses:
                    for ip, status in device_statuses.items():
                        rollout_status = "updated" if status == "success" else status
                        db.mark_rollout_device(rollout["id"], ip, rollout_status)
                else:
                    db.mark_rollout_phase_devices(rollout["id"], rollout["phase"], "failed")
                self._last_run_result = f"Rollout paused: {reason}"
                db.log_schedule_event("rollout_paused", reason, job_id=job_id)
                logger.warning(f"Rollout {rollout['id']} paused: {reason}")
            else:
                if learned_versions:
                    db.set_rollout_target_versions(rollout["id"], learned_versions)
                    logger.info(f"Rollout {rollout['id']} learned target versions: {learned_versions}")

                if device_statuses:
                    for ip, status in device_statuses.items():
                        # Map window-cutoff skips to "deferred" so they retry next window
                        rollout_status = "updated" if status == "success" else status
                        db.mark_rollout_device(rollout["id"], ip, rollout_status)
                else:
                    db.mark_rollout_phase_devices(rollout["id"], rollout["phase"], "updated")

                # Canary safety: if canary phase completed with 0 actual updates, pause
                if rollout["phase"] == "canary" and success_count == 0:
                    reason = "Canary phase had no eligible devices — verify canary device availability"
                    db.pause_rollout(rollout["id"], reason)
                    self._last_run_result = f"Rollout paused: {reason}"
                    db.log_schedule_event("rollout_paused", reason, job_id=job_id)
                    logger.warning(f"Rollout {rollout['id']} paused: {reason}")
                else:
                    # Check if any devices were deferred (window cutoff) — don't advance phase
                    phase_devices = db.get_rollout_devices(rollout["id"], phase=rollout["phase"])
                    deferred_count = sum(1 for d in phase_devices if d["status"] == "deferred")
                    if deferred_count > 0:
                        self._last_run_result = (
                            f"Phase {rollout['phase']}: {success_count} updated, "
                            f"{deferred_count} deferred (will retry next window)"
                        )
                        db.log_schedule_event(
                            "phase_deferred",
                            f"Phase {rollout['phase']}: {deferred_count} device(s) deferred",
                            job_id=job_id,
                        )
                        logger.info(f"Rollout {rollout['id']} phase {rollout['phase']} has {deferred_count} deferred device(s)")
                    else:
                        db.complete_rollout_phase(rollout["id"])

                        refreshed = db.get_rollout(rollout["id"])
                        if refreshed and refreshed["status"] == "completed":
                            self._last_run_result = f"Rollout completed ({success_count} devices this phase)"
                            db.log_schedule_event("rollout_completed", f"Rollout {rollout['id']} completed", job_id=job_id)
                        else:
                            self._last_run_result = (
                                f"Phase {rollout['phase']} done ({success_count} devices), "
                                f"next: {refreshed['phase'] if refreshed else '?'}"
                            )
                            db.log_schedule_event(
                                "phase_completed",
                                f"Phase {rollout['phase']} -> {refreshed['phase'] if refreshed else '?'}",
                                job_id=job_id,
                            )
        else:
            if failed_count > 0:
                self._last_run_result = f"Completed with {failed_count} failure(s)"
                db.log_schedule_event(
                    "job_completed_with_failures",
                    f"success={success_count}, failed={failed_count}",
                    job_id=job_id,
                )
            else:
                self._last_run_result = f"Success ({success_count} devices)"
                db.log_schedule_event("job_completed", f"success={success_count}", job_id=job_id)

        if manual_canary and failed_count == 0 and rollout and rollout.get("status") == "active":
            self._state = "idle"
            self._block_reason = "Canary complete; next phase waits for the maintenance window"
        else:
            self._state = "waiting"
            self._block_reason = rollout.get("pause_reason") if rollout and rollout.get("status") == "paused" else "Already ran today"
        logger.info(f"Scheduler job {job_id} completed: {self._last_run_result}")

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

    @staticmethod
    def _apply_free_tier_limits(scope_ips: list[str], switch_scope: list[str]) -> tuple[list[str], list[str]]:
        """Enforce free-tier auto-update device limits."""
        if lic._FORCE_PRO or lic.get_license_state().is_pro():
            return scope_ips, switch_scope
        limit = lic.FREE_AUTO_UPDATE_AP_LIMIT
        if len(scope_ips) > limit:
            logger.info(f"Free tier: limiting auto-update to {limit} APs (had {len(scope_ips)})")
            scope_ips = scope_ips[:limit]
        if switch_scope:
            logger.info("Free tier: switch auto-updates require a Pro license")
            switch_scope = []
        return scope_ips, switch_scope

    def _calculate_predictions(self, rollout: dict, settings: dict) -> dict:
        """Calculate rollout time predictions based on historical device durations."""
        avg_durations = db.get_avg_durations()
        concurrency = _as_int(settings.get("parallel_updates", "2"), 2)
        bank_mode = settings.get("bank_mode", "both")
        allow_downgrade = settings.get("allow_downgrade", "false") == "true"
        start_hour = _as_int(settings.get("schedule_start_hour", "3"), 3)
        end_hour = _as_int(settings.get("schedule_end_hour", "4"), 4)
        schedule_days = settings.get("schedule_days", "")
        cooldown_days = _as_int(settings.get("firmware_update_cooldown_days", "30"), 30)
        window_minutes = (end_hour - start_hour) * 60
        if window_minutes <= 0:
            window_minutes += 24 * 60

        scope_ips = self._resolve_scope(settings)
        switch_scope = self._resolve_switch_scope(settings)
        preferred_ap_canaries = self._preferred_canary_devices(settings, "rollout_canary_aps")
        preferred_switch_canaries = self._preferred_canary_devices(settings, "rollout_canary_switches")

        total_cpes = 0
        for ip in scope_ips:
            cpes = db.get_cpes_for_ap(ip)
            total_cpes += sum(1 for c in cpes if c.get("auth_status") == "ok")

        existing_devices = db.get_rollout_devices(rollout["id"])
        already_done = {d["ip"] for d in existing_devices if d["status"] in ("updated", "pending")}
        target_versions = self._target_versions(settings, rollout)

        ap_candidates = []
        for ip in scope_ips:
            if ip in already_done:
                continue
            ap = db.get_access_point(ip)
            if ap and self._ap_or_children_need_update(
                ap, target_versions, allow_downgrade, cooldown_days=cooldown_days
            ):
                ap_candidates.append(ip)

        switch_candidates = []
        for ip in switch_scope:
            if ip in already_done:
                continue
            switch = db.get_switch(ip)
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
                switch_candidates.append(ip)

        total_candidates = len(ap_candidates) + len(switch_candidates)

        def _estimate_phase_duration(ap_count: int, switch_count: int) -> float:
            avg_cpes_per_ap = total_cpes / len(scope_ips) if scope_ips else 0
            cpe_count = round(ap_count * avg_cpes_per_ap)
            passes = 2 if bank_mode == "both" else 1

            ap_batches = math.ceil(ap_count / concurrency) if ap_count > 0 else 0
            ap_time = ap_batches * avg_durations["ap"] * passes

            cpe_batches = math.ceil(cpe_count / concurrency) if cpe_count > 0 else 0
            cpe_time = cpe_batches * avg_durations["cpe"] * passes

            sw_batches = math.ceil(switch_count / concurrency) if switch_count > 0 else 0
            sw_time = sw_batches * avg_durations["switch"] * passes

            return (ap_time + cpe_time + sw_time) / 60.0

        current_phase = rollout["phase"]
        phase_idx = db.PHASE_ORDER.index(current_phase) if current_phase in db.PHASE_ORDER else 0

        current_ap_batch = self._select_phase_batch(current_phase, ap_candidates, preferred_ap_canaries)
        current_switch_batch = self._select_phase_batch(current_phase, switch_candidates, preferred_switch_canaries)
        current_ap_count = len(current_ap_batch)
        current_switch_count = len(current_switch_batch)
        current_primary_count = current_ap_count + current_switch_count
        current_duration = _estimate_phase_duration(current_ap_count, current_switch_count)

        effective_window = window_minutes - 10
        if current_duration > 0 and effective_window > 0:
            fit_ratio = min(1.0, effective_window / current_duration)
            devices_that_fit = max(1, math.floor(current_primary_count * fit_ratio))
        else:
            devices_that_fit = current_primary_count

        current_phase_info = {
            "device_count": current_primary_count,
            "ap_count": current_ap_count,
            "switch_count": current_switch_count,
            "estimated_duration_minutes": round(current_duration, 1),
            "devices_that_fit_in_window": min(devices_that_fit, current_primary_count),
        }

        remaining_phases = []
        remaining_ap_candidates = list(ap_candidates)
        remaining_switch_candidates = list(switch_candidates)
        for ip in current_ap_batch:
            if ip in remaining_ap_candidates:
                remaining_ap_candidates.remove(ip)
        for ip in current_switch_batch:
            if ip in remaining_switch_candidates:
                remaining_switch_candidates.remove(ip)

        for i in range(phase_idx + 1, len(db.PHASE_ORDER)):
            phase = db.PHASE_ORDER[i]
            if not remaining_ap_candidates and not remaining_switch_candidates:
                remaining_phases.append({
                    "phase": phase,
                    "estimated_devices": 0,
                    "estimated_duration_minutes": 0,
                })
                continue

            ap_batch = self._select_phase_batch(phase, remaining_ap_candidates, preferred_ap_canaries)
            switch_batch = self._select_phase_batch(phase, remaining_switch_candidates, preferred_switch_canaries)
            phase_count = len(ap_batch) + len(switch_batch)
            phase_dur = _estimate_phase_duration(len(ap_batch), len(switch_batch))
            remaining_phases.append({
                "phase": phase,
                "estimated_devices": phase_count,
                "estimated_duration_minutes": round(phase_dur, 1),
            })
            for ip in ap_batch:
                if ip in remaining_ap_candidates:
                    remaining_ap_candidates.remove(ip)
            for ip in switch_batch:
                if ip in remaining_switch_candidates:
                    remaining_switch_candidates.remove(ip)

        remaining_windows = 1 + len([p for p in remaining_phases if p["estimated_devices"] > 0])
        day_abbrs = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        active_days = set()
        for d in schedule_days.split(","):
            d = d.strip().lower()
            if d in day_abbrs:
                active_days.add(day_abbrs[d])

        estimated_completion = None
        if active_days:
            from datetime import date

            current = date.today()
            now = datetime.now()
            start_offset = 1 if now.hour >= end_hour else 0
            windows_counted = 0
            for offset in range(start_offset, 60):
                check = current + timedelta(days=offset)
                if check.weekday() in active_days:
                    windows_counted += 1
                    if windows_counted >= remaining_windows:
                        estimated_completion = check.isoformat()
                        break

        return {
            "current_phase": current_phase_info,
            "remaining_phases": remaining_phases,
            "estimated_completion_date": estimated_completion,
            "avg_durations": avg_durations,
            "total_candidates": total_candidates,
            "window_minutes": window_minutes,
        }

    def get_status(self) -> dict:
        """Return current scheduler status for UI."""
        settings = db.get_all_settings()
        start_hour = _as_int(settings.get("schedule_start_hour", "3"), 3)
        end_hour = _as_int(settings.get("schedule_end_hour", "4"), 4)
        schedule_days = settings.get("schedule_days", "")

        rollout_info = None
        pre_rollout_predictions = None
        rollout = db.get_current_rollout()
        if rollout:
            progress = db.get_rollout_progress(rollout["id"])
            try:
                predictions = self._calculate_predictions(rollout, settings)
            except Exception as e:
                logger.warning(f"Failed to calculate predictions: {e}")
                predictions = None
            rollout_info = {
                "id": rollout["id"],
                "phase": rollout["phase"],
                "status": rollout["status"],
                "target_version": rollout.get("target_version"),
                "firmware_file": rollout["firmware_file"],
                "firmware_file_tns100": rollout.get("firmware_file_tns100"),
                "progress": progress,
                "pause_reason": rollout.get("pause_reason"),
                "predictions": predictions,
            }
        else:
            try:
                synthetic_rollout = {"id": 0, "phase": "canary", "target_version": None}
                pre_rollout_predictions = self._calculate_predictions(synthetic_rollout, settings)
            except Exception as e:
                logger.debug(f"Pre-rollout prediction failed: {e}")

        quarantine_days = _as_int(settings.get("firmware_quarantine_days", "7"), 7)
        quarantine = None
        if quarantine_days > 0:
            fw_30x = settings.get("selected_firmware_30x", "")
            if fw_30x:
                quarantine = db.get_firmware_quarantine_info(fw_30x, quarantine_days)
                quarantine["firmware"] = fw_30x
                quarantine["quarantine_days"] = quarantine_days

        return {
            "state": self._state,
            "block_reason": self._block_reason,
            "weather": self._weather_info,
            "last_run_result": self._last_run_result,
            "last_run_time": self._last_run_time,
            "current_job_id": self._current_job_id,
            "next_window": f"{start_hour}:00-{end_hour}:00 on {schedule_days}",
            "rollout": rollout_info,
            "predictions": pre_rollout_predictions,
            "quarantine": quarantine,
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
