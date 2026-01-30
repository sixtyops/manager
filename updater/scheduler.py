"""Auto-update scheduler with gradual rollout support."""

import asyncio
import logging
import math
from datetime import datetime
from typing import Callable, Optional, Set

from zoneinfo import ZoneInfo

from . import database as db
from . import services

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional["AutoUpdateScheduler"] = None


class AutoUpdateScheduler:
    """Background service that checks schedule and triggers firmware updates."""

    # Possible states
    STATES = ("disabled", "idle", "waiting", "running",
              "blocked_weather", "blocked_time", "blocked_no_firmware",
              "blocked_all_current")

    def __init__(self, broadcast_func: Callable, start_update_func: Callable,
                 check_interval: int = 60):
        self.broadcast_func = broadcast_func
        self.start_update_func = start_update_func
        self.check_interval = check_interval

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._state = "disabled"
        self._block_reason: Optional[str] = None
        self._weather_info: Optional[dict] = None
        self._last_run_result: Optional[str] = None
        self._last_run_time: Optional[str] = None
        self._current_job_id: Optional[str] = None
        self._ran_today: Set[str] = set()  # date strings we've already run on

    async def start(self):
        """Start the scheduler check loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"Auto-update scheduler started (interval: {self.check_interval}s)")

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

        # 1. Check if schedule is enabled
        if settings.get("schedule_enabled") != "true":
            if self._state != "disabled":
                self._state = "disabled"
                self._block_reason = None
                await self._broadcast_status()
            return

        # 2. If already running a job, skip
        if self._current_job_id is not None:
            return

        # 3. Resolve timezone
        tz_str = settings.get("timezone", "auto")
        if tz_str == "auto":
            tz_str = await services.get_timezone()

        # 4. Validate time against 2 sources
        time_ok, time_result = await services.validate_time_sources(tz_str)
        if not time_ok:
            self._state = "blocked_time"
            self._block_reason = str(time_result)
            db.log_schedule_event("blocked_time", self._block_reason)
            logger.warning(f"Scheduler blocked: {self._block_reason}")
            await self._broadcast_status()
            return

        now = time_result  # datetime object
        logger.info("Time validation passed")

        # 5. Check if in schedule window
        schedule_days = [d.strip() for d in settings.get("schedule_days", "").split(",") if d.strip()]
        start_hour = int(settings.get("schedule_start_hour", "3"))
        end_hour = int(settings.get("schedule_end_hour", "4"))
        current_day = now.strftime("%a").lower()
        current_hour = now.hour

        if not services.is_in_schedule_window(current_hour, current_day, schedule_days, start_hour, end_hour):
            # Outside window - clear ran_today if we've moved past the window
            today_key = now.strftime("%Y-%m-%d")
            if current_hour >= end_hour and today_key in self._ran_today:
                pass  # Keep it until next day
            elif current_hour < start_hour:
                # New day, clear previous ran_today entries
                self._ran_today.clear()

            if self._state != "idle":
                self._state = "idle"
                self._block_reason = None
                await self._broadcast_status()
            return

        # 6. Track ran_today to avoid re-running
        today_key = now.strftime("%Y-%m-%d")
        if today_key in self._ran_today:
            if self._state != "waiting":
                self._state = "waiting"
                self._block_reason = "Already ran today"
                await self._broadcast_status()
            return

        # 7. Check 10-minute cutoff before window end
        minutes_until_end = (end_hour - current_hour) * 60 - now.minute
        if minutes_until_end < 10:
            self._state = "waiting"
            self._block_reason = "Too close to maintenance window end"
            await self._broadcast_status()
            return

        # 8. Check weather if enabled
        if settings.get("weather_check_enabled") == "true":
            zip_code = settings.get("zip_code", "")
            min_temp_c = float(settings.get("min_temperature_c", "-10"))
            weather_ok, weather_data = await services.check_weather_ok(
                zip_code if zip_code else None, min_temp_c
            )
            self._weather_info = weather_data
            if not weather_ok:
                self._state = "blocked_weather"
                temp = weather_data.get("temperature_c", "?") if weather_data else "?"
                self._block_reason = f"Temperature {temp}C is below minimum {min_temp_c}C"
                db.log_schedule_event("blocked_weather", self._block_reason)
                logger.warning(f"Scheduler blocked by weather: {self._block_reason}")
                await self._broadcast_status()
                return

        # 9. Verify firmware is selected
        fw_30x = settings.get("selected_firmware_30x", "")
        if not fw_30x:
            self._state = "blocked_no_firmware"
            self._block_reason = "No firmware selected"
            await self._broadcast_status()
            return

        fw_303l = settings.get("selected_firmware_303l", "")

        # 10. Get or create rollout
        rollout = db.get_active_rollout()

        if rollout and rollout["firmware_file"] != fw_30x:
            # Firmware changed - cancel existing rollout and start fresh
            db.cancel_rollout(rollout["id"])
            db.log_schedule_event("rollout_cancelled", f"Firmware changed to {fw_30x}")
            logger.info(f"Cancelled rollout {rollout['id']} due to firmware change")
            rollout = None

        if rollout is None:
            # Check if previous rollout already covered this firmware
            last = db.get_last_rollout_for_firmware(fw_30x)
            if last and last["status"] == "completed":
                # Check if any in-scope devices still need updating
                scope_ips = self._resolve_scope(settings)
                if last.get("target_version"):
                    needs_update = self._filter_devices_needing_update(scope_ips, last["target_version"])
                    if not needs_update:
                        self._state = "blocked_all_current"
                        self._block_reason = "All devices up to date"
                        await self._broadcast_status()
                        return

            # Create new rollout
            rollout_id = db.create_rollout(fw_30x, fw_303l if fw_303l else None)
            rollout = db.get_rollout(rollout_id)
            db.log_schedule_event("rollout_created", f"Rollout {rollout_id} for {fw_30x}")
            logger.info(f"Created rollout {rollout_id} for firmware {fw_30x}")

        # 11. If rollout is paused, show state and return
        if rollout["status"] == "paused":
            self._state = "waiting"
            self._block_reason = f"Rollout paused: {rollout.get('pause_reason', 'Unknown reason')}"
            await self._broadcast_status()
            return

        # 12. Determine phase batch
        scope_ips = self._resolve_scope(settings)
        if not scope_ips:
            self._state = "idle"
            self._block_reason = "No devices in scope"
            await self._broadcast_status()
            return

        batch_ips = self._get_devices_for_phase(rollout, scope_ips)

        if not batch_ips:
            # No candidates for this phase - auto-advance
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

            # Try next phase immediately (recursive but bounded by 4 phases)
            logger.info(f"Phase {current_phase} has no candidates, advanced to {refreshed['phase']}")
            batch_ips = self._get_devices_for_phase(refreshed, scope_ips)
            rollout = refreshed

            if not batch_ips:
                # Still nothing - complete the rollout
                db.complete_rollout_phase(rollout["id"])
                self._state = "blocked_all_current"
                self._block_reason = "All devices up to date"
                await self._broadcast_status()
                return

        # Record devices in rollout_devices table
        for ip in batch_ips:
            db.assign_device_to_rollout(rollout["id"], ip, "ap", rollout["phase"])

        # 13. Launch the job
        self._state = "running"
        self._block_reason = None
        await self._broadcast_status()

        bank_mode = settings.get("bank_mode", "both")
        concurrency = int(settings.get("parallel_updates", "2"))

        phase = rollout["phase"]
        db.log_schedule_event("job_starting",
                              f"Rollout {rollout['id']} phase={phase}, {len(batch_ips)} APs, bank_mode={bank_mode}")
        logger.info(f"Scheduler starting rollout phase {phase}: {len(batch_ips)} APs")

        try:
            job_id = await self.start_update_func(
                ap_ips=batch_ips,
                firmware_file=fw_30x,
                firmware_file_303l=fw_303l,
                bank_mode=bank_mode,
                concurrency=concurrency,
                end_hour=end_hour,
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

    def _filter_devices_needing_update(self, scope_ips: list[str], target_version: str) -> list[str]:
        """Filter scope IPs to those whose firmware differs from target."""
        needs_update = []
        for ip in scope_ips:
            ap = db.get_access_point(ip)
            if ap and ap.get("firmware_version") != target_version:
                needs_update.append(ip)
        return needs_update

    def _get_devices_for_phase(self, rollout: dict, scope_ips: list[str]) -> list[str]:
        """Determine which APs to update in the current phase."""
        phase = rollout["phase"]
        rollout_id = rollout["id"]
        target_version = rollout.get("target_version")

        # Get already-processed devices in this rollout
        existing_devices = db.get_rollout_devices(rollout_id)
        already_done = {d["ip"] for d in existing_devices if d["status"] in ("updated", "pending")}

        # Filter to candidates needing update
        candidates = []
        for ip in scope_ips:
            if ip in already_done:
                continue
            if target_version:
                ap = db.get_access_point(ip)
                if ap and ap.get("firmware_version") == target_version:
                    continue
            candidates.append(ip)

        if not candidates:
            return []

        # Determine batch size
        if phase == "canary":
            batch_size = 1
        elif phase == "pct10":
            batch_size = max(1, math.ceil(len(candidates) * 0.1))
        elif phase == "pct50":
            batch_size = max(1, math.ceil(len(candidates) * 0.5))
        else:  # pct100
            batch_size = len(candidates)

        return candidates[:batch_size]

    def on_job_completed(self, job_id: str, success_count: int, failed_count: int,
                         learned_version: Optional[str] = None):
        """Called when a scheduled job finishes."""
        if self._current_job_id != job_id:
            return

        self._current_job_id = None
        self._last_run_time = datetime.now().isoformat()

        rollout = db.get_active_rollout()
        if rollout and rollout.get("last_job_id") == job_id:
            if failed_count > 0:
                # Pause rollout on failure
                reason = f"{failed_count} device(s) failed during {rollout['phase']} phase"
                db.pause_rollout(rollout["id"], reason)
                # Mark phase devices as failed
                db.mark_rollout_phase_devices(rollout["id"], rollout["phase"], "failed")
                self._last_run_result = f"Rollout paused: {reason}"
                db.log_schedule_event("rollout_paused", reason, job_id=job_id)
                logger.warning(f"Rollout {rollout['id']} paused: {reason}")
            else:
                # Success
                if rollout.get("target_version") is None and learned_version:
                    db.set_rollout_target_version(rollout["id"], learned_version)
                    logger.info(f"Rollout {rollout['id']} learned target version: {learned_version}")

                # Mark phase devices as updated
                db.mark_rollout_phase_devices(rollout["id"], rollout["phase"], "updated")

                # Advance phase
                db.complete_rollout_phase(rollout["id"])

                refreshed = db.get_rollout(rollout["id"])
                if refreshed and refreshed["status"] == "completed":
                    self._last_run_result = f"Rollout completed ({success_count} devices this phase)"
                    db.log_schedule_event("rollout_completed",
                                          f"Rollout {rollout['id']} completed", job_id=job_id)
                else:
                    self._last_run_result = (
                        f"Phase {rollout['phase']} done ({success_count} devices), "
                        f"next: {refreshed['phase'] if refreshed else '?'}"
                    )
                    db.log_schedule_event("phase_completed",
                                          f"Phase {rollout['phase']} -> {refreshed['phase'] if refreshed else '?'}",
                                          job_id=job_id)
        else:
            if failed_count > 0:
                self._last_run_result = f"Completed with {failed_count} failure(s)"
                db.log_schedule_event("job_completed_with_failures",
                                      f"success={success_count}, failed={failed_count}",
                                      job_id=job_id)
            else:
                self._last_run_result = f"Success ({success_count} devices)"
                db.log_schedule_event("job_completed",
                                      f"success={success_count}",
                                      job_id=job_id)

        self._state = "waiting"
        self._block_reason = "Already ran today"
        logger.info(f"Scheduler job {job_id} completed: {self._last_run_result}")

        # Fire-and-forget broadcast
        asyncio.ensure_future(self._broadcast_status())

    def _resolve_scope(self, settings: dict) -> list[str]:
        """Resolve which AP IPs to update based on scope settings."""
        scope = settings.get("schedule_scope", "all")
        scope_data = settings.get("schedule_scope_data", "")

        if scope == "all":
            aps = db.get_access_points(enabled_only=True)
            return [ap["ip"] for ap in aps]

        if scope == "sites":
            # scope_data is comma-separated site IDs
            site_ids = [int(s.strip()) for s in scope_data.split(",") if s.strip().isdigit()]
            ips = []
            for site_id in site_ids:
                aps = db.get_access_points(tower_site_id=site_id, enabled_only=True)
                ips.extend(ap["ip"] for ap in aps)
            return ips

        if scope == "aps":
            # scope_data is comma-separated AP IPs
            return [ip.strip() for ip in scope_data.split(",") if ip.strip()]

        return []

    def get_status(self) -> dict:
        """Return current scheduler status for UI."""
        settings = db.get_all_settings()
        start_hour = int(settings.get("schedule_start_hour", "3"))
        end_hour = int(settings.get("schedule_end_hour", "4"))
        schedule_days = settings.get("schedule_days", "")

        # Include rollout info
        rollout_info = None
        rollout = db.get_active_rollout()
        if rollout:
            progress = db.get_rollout_progress(rollout["id"])
            rollout_info = {
                "id": rollout["id"],
                "phase": rollout["phase"],
                "status": rollout["status"],
                "target_version": rollout.get("target_version"),
                "firmware_file": rollout["firmware_file"],
                "progress": progress,
                "pause_reason": rollout.get("pause_reason"),
            }

        return {
            "state": self._state,
            "block_reason": self._block_reason,
            "weather": self._weather_info,
            "last_run_result": self._last_run_result,
            "last_run_time": self._last_run_time,
            "current_job_id": self._current_job_id,
            "next_window": f"{start_hour}:00-{end_hour}:00 on {schedule_days}",
            "rollout": rollout_info,
        }

    async def _broadcast_status(self):
        """Send scheduler_status WebSocket message."""
        if self.broadcast_func:
            await self.broadcast_func({
                "type": "scheduler_status",
                **self.get_status(),
            })


def get_scheduler() -> Optional[AutoUpdateScheduler]:
    """Get the global scheduler instance."""
    return _scheduler


def init_scheduler(broadcast_func: Callable, start_update_func: Callable,
                   check_interval: int = 60) -> AutoUpdateScheduler:
    """Initialize the global scheduler instance."""
    global _scheduler
    _scheduler = AutoUpdateScheduler(broadcast_func, start_update_func, check_interval)
    return _scheduler
