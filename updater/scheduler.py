"""Auto-update scheduler with gradual rollout support."""

import asyncio
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Set

from . import database as db
from .firmware_policy import (
    classify_device_version,
    extract_version_from_filename as policy_extract_version_from_filename,
    parse_version as policy_parse_version,
)
from . import rollout_gate
from . import services
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
        self._ran_today: Set[str] = set()
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
        self._recover_ran_today()
        self._recover_orphaned_rollouts()
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"Auto-update scheduler started (interval: {self.check_interval}s)")
        # Fetch weather eagerly so the header shows temperature immediately. Keep
        # the handle so stop() can cancel it — otherwise the fire-and-forget task
        # can outlive the scheduler (e.g. in tests, leaving "Task was destroyed but
        # it is pending" warnings at loop teardown).
        self._weather_task = asyncio.create_task(self._fetch_weather_initial())

    def _recover_ran_today(self):
        """Recover the per-day run guard so a restart doesn't trigger a second job.

        The schedule_log timestamp column is naive container-local time, so
        `_ran_today` is keyed by the configured timezone's calendar date and
        we look back 23 hours instead of relying on a DATE() match across
        differing zones.
        """
        settings = db.get_all_settings()
        tz_str = settings.get("timezone", "auto")
        try:
            from zoneinfo import ZoneInfo
            if tz_str == "auto":
                tz = datetime.now().astimezone().tzinfo
            else:
                tz = ZoneInfo(tz_str)
        except Exception:
            tz = datetime.now().astimezone().tzinfo
        today_key = datetime.now(tz).strftime("%Y-%m-%d")

        cutoff = (datetime.now() - timedelta(hours=23)).isoformat()
        try:
            with db.get_db() as conn:
                row = conn.execute(
                    "SELECT 1 FROM schedule_log WHERE event = 'job_started' "
                    "AND timestamp >= ? LIMIT 1",
                    (cutoff,),
                ).fetchone()
                if row:
                    self._ran_today.add(today_key)
                    logger.info(f"Scheduler: recovered _ran_today for {today_key} from DB")
        except Exception as e:
            logger.warning(f"Scheduler: failed to recover _ran_today: {e}")

    def _recover_orphaned_rollouts(self):
        """Reconcile rollouts whose update job died with the previous process.

        An active rollout with `last_job_id` set but no completion event for
        that job is orphaned — the async update task did not survive the
        restart. Flip its pending devices to `deferred` so they retry next
        window, and clear `last_job_id` so the next window starts a fresh
        job.
        """
        completion_events = (
            "job_completed",
            "job_completed_with_failures",
            "rollout_paused",
            "rollout_completed",
            "rollout_cancelled",
            "job_deferred",
            "phase_completed",
            "phase_deferred",
        )
        placeholders = ",".join("?" for _ in completion_events)
        recovered = []
        try:
            with db.get_db() as conn:
                rollouts = conn.execute(
                    "SELECT id, last_job_id FROM rollouts "
                    "WHERE status = 'active' AND last_job_id IS NOT NULL"
                ).fetchall()
                for row in rollouts:
                    rollout_id = row["id"]
                    job_id = row["last_job_id"]
                    completion = conn.execute(
                        f"SELECT 1 FROM schedule_log WHERE job_id = ? "
                        f"AND event IN ({placeholders}) LIMIT 1",
                        (job_id, *completion_events),
                    ).fetchone()
                    if completion:
                        continue
                    now_iso = datetime.now().isoformat()
                    cur = conn.execute(
                        "UPDATE rollout_devices SET status = 'deferred', updated_at = ? "
                        "WHERE rollout_id = ? AND status = 'pending'",
                        (now_iso, rollout_id),
                    )
                    affected = cur.rowcount
                    conn.execute(
                        "UPDATE rollouts SET last_job_id = NULL, updated_at = ? WHERE id = ?",
                        (now_iso, rollout_id),
                    )
                    recovered.append((rollout_id, job_id, affected))
        except Exception as e:
            logger.warning(f"Scheduler: orphaned rollout recovery failed: {e}")
            return

        for rollout_id, job_id, affected in recovered:
            db.log_schedule_event(
                "startup_recovery",
                f"Rollout {rollout_id}: orphaned job {job_id}, {affected} device(s) deferred",
                job_id=job_id,
            )
            logger.warning(
                f"Scheduler: recovered orphaned rollout {rollout_id} (job {job_id}); "
                f"{affected} pending device(s) marked deferred"
            )

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

                target_versions = self._target_versions(settings, None)
                cooldown_days = _as_int(settings.get("firmware_update_cooldown_days", "30"), 30)
                needs_update = self._filter_devices_needing_update(
                    scope_ips, target_versions, allow_downgrade, cooldown_days=cooldown_days
                )
                needs_switch_update = self._filter_switches_needing_update(
                    switch_scope, target_versions, allow_downgrade, cooldown_days=cooldown_days
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

        # ── Wave gate: one wave per maintenance window + firmware hold ──
        # Single source of truth in rollout_gate (fail-closed). Rule 1 stops all
        # waves cascading through one window. Rule 2 holds the first fleet wave
        # (pct10) of newly-released firmware until the Firmware Hold clears — the
        # release-date soak, or earlier once a device of that model family is
        # confirmed working on it (the operator's manual canary). The hold is
        # evaluated per family but enforced all-or-nothing at the wave: while ANY
        # in-scope family with pending work is still held, the first wave waits.
        # Once it runs, nothing is held, so the whole fleet (all families, incl.
        # attached CPEs) rolls together 10% -> 50% -> 100% with no held firmware
        # slipping out — and the rollout never advances/completes with held work
        # still outstanding.
        window_key = now.strftime("%Y-%m-%d")
        held_families, family_holds = self._held_families(settings, rollout)
        first_wave_held = False
        if rollout.get("phase") == "pct10" and held_families:
            _, has_held = self._pending_split_by_hold(
                settings, rollout, allow_downgrade, held_families
            )
            first_wave_held = has_held

        may_run, gate_reason = rollout_gate.phase_run_decision(
            rollout, window_key, first_wave_held=first_wave_held
        )
        if not may_run:
            self._hold_clear_reason = None
            if gate_reason == "firmware_hold":
                new_reason = self._firmware_hold_block_reason(held_families, family_holds)
                if self._block_reason != new_reason:
                    db.log_schedule_event("blocked_firmware_hold", new_reason)
                self._state = "blocked_firmware_hold"
                self._block_reason = new_reason
            else:
                new_reason = "One wave per maintenance window — next wave next window"
                if self._block_reason != new_reason:
                    db.log_schedule_event("phase_held", f"Rollout {rollout['id']}: {gate_reason}")
                self._state = "waiting"
                self._block_reason = new_reason
            await self._broadcast_status()
            return

        # Past the gate. If a family's hold was cleared early by a confirmed
        # device (rather than the days elapsing), surface it honestly (logged
        # once) — never a silent advance.
        clear_note = self._early_clear_note(rollout, family_holds)
        if clear_note and clear_note != self._hold_clear_reason:
            db.log_schedule_event("firmware_hold_cleared", f"Rollout {rollout['id']}: {clear_note}")
        self._hold_clear_reason = clear_note

        scope_ips = self._resolve_scope(settings)
        switch_scope = self._resolve_switch_scope(settings)


        if not scope_ips and not switch_scope:
            self._state = "idle"
            self._block_reason = "No devices in scope"
            await self._broadcast_status()
            return

        batch_ips = self._get_devices_for_phase(rollout, scope_ips, allow_downgrade, settings)
        batch_switches = self._get_switches_for_phase(rollout, switch_scope, allow_downgrade, settings)

        if not batch_ips and not batch_switches:
            current_phase = rollout["phase"]

            # The current wave has no devices left to update. Advance to find
            # the next wave with work. The firmware hold and one-wave-per-window
            # rules are enforced by the gate above (rollout_gate), so this path is
            # pure "skip an empty wave" bookkeeping — it touches no devices and
            # therefore does not consume the window. Because the hold is enforced
            # all-or-nothing at the gate (the first wave doesn't run while any
            # family is held), an empty wave here means the work is genuinely done,
            # not held — so completing is correct.
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
            # Stamp the window so the phase gate holds the next phase until the
            # next maintenance window (one phase per window).
            db.set_rollout_phase_window(rollout["id"], window_key)
            db.log_schedule_event("job_started", f"Job {job_id} for rollout {rollout['id']}", job_id=job_id)
        except Exception as e:
            self._state = "idle"
            self._block_reason = f"Failed to start: {e}"
            db.log_schedule_event("job_start_failed", str(e))
            logger.error(f"Scheduler failed to start update: {e}")
            await self._broadcast_status()

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

    def _pending_split_by_hold(
        self, settings: dict, rollout: dict, allow_downgrade: bool, held_families: set
    ) -> tuple[bool, bool]:
        """Scan in-scope APs/switches/CPEs that still need this update and split by
        whether the device's firmware family hold is cleared. Returns
        (has_cleared_pending, has_held_pending) — used to decide whether the first
        wave is FULLY held (held pending exists, nothing cleared to roll).
        Fail-closed: an unreadable fleet reports held."""
        target_versions = self._target_versions(settings, rollout)
        cooldown_days = _as_int(settings.get("firmware_update_cooldown_days", "30"), 30)
        try:
            scope_ips = self._resolve_scope(settings)
            switch_scope = self._resolve_switch_scope(settings)
            ap_dict = db.get_all_access_points_dict(enabled_only=True)
            switch_dict = db.get_all_switches_dict(enabled_only=True)
            cpes_by_ap = db.get_all_cpes_grouped()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Pending-split check could not read fleet, holding: {e}")
            return False, True

        flags = {"cleared": False, "held": False}

        def _account(dev: dict):
            fam = _firmware_type_for_model(dev.get("model"))
            if _device_needs_update(
                dev.get("firmware_version", ""), target_versions.get(fam, ""), allow_downgrade,
                last_update_iso=dev.get("last_firmware_update"), cooldown_days=cooldown_days,
            ):
                flags["held" if fam in held_families else "cleared"] = True

        for ip in scope_ips:
            ap = ap_dict.get(ip)
            if not ap:
                continue
            _account(ap)
            for cpe in cpes_by_ap.get(ip, []):
                if cpe.get("auth_status") == "ok":
                    _account(cpe)
        for ip in switch_scope:
            sw = switch_dict.get(ip)
            if sw:
                _account(sw)
        return flags["cleared"], flags["held"]

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

    @staticmethod
    def _early_clear_note(rollout: Optional[dict], family_holds: dict) -> Optional[str]:
        """If a family's hold was cleared early by a confirmed device, return a
        short honest note naming it (only meaningful at the first wave)."""
        if not rollout or rollout.get("phase") != "pct10":
            return None
        notes = [
            f"{fam} confirmed working on {d['confirmed_by']}"
            for fam, d in sorted(family_holds.items())
            if d.get("cleared_by_device") and d.get("confirmed_by")
        ]
        if not notes:
            return None
        return "Firmware hold cleared early — " + "; ".join(notes)

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
        """Determine which APs to update in the current wave. The Firmware Hold is
        enforced all-or-nothing at the gate (the first wave doesn't run while any
        family is held), so no per-family filtering is needed here."""
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

        return self._select_phase_batch(phase, candidates)

    def _get_switches_for_phase(
        self,
        rollout: dict,
        scope_ips: list[str],
        allow_downgrade: bool = False,
        settings: Optional[dict] = None,
    ) -> list[str]:
        """Determine which switches to update in the current wave. The Firmware
        Hold is enforced all-or-nothing at the gate, so no per-family filtering is
        needed here."""
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

        return self._select_phase_batch(phase, candidates)

    def _select_phase_batch(self, phase: str, candidates: list[str]) -> list[str]:
        """Select the devices for a rollout wave (pct10 -> pct50 -> pct100)."""
        if not candidates:
            return []
        if phase == "pct10":
            batch_size = max(1, math.ceil(len(candidates) * 0.1))
        elif phase == "pct50":
            batch_size = max(1, math.ceil(len(candidates) * 0.5))
        else:
            batch_size = len(candidates)
        return candidates[:batch_size]

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
            # Stale completion (typically a job that started in a previous
            # process). Only reconcile if the DB still tracks this job_id on
            # an active rollout — otherwise drop with a log line.
            active_rollout = db.get_active_rollout()
            if not (active_rollout and active_rollout.get("last_job_id") == job_id):
                logger.warning(
                    f"Scheduler: ignoring stale completion for job {job_id} "
                    f"(current_job_id={self._current_job_id})"
                )
                return
            logger.warning(
                f"Scheduler: reconciling stale completion for job {job_id} "
                f"(current_job_id={self._current_job_id}, "
                f"matches active rollout {active_rollout['id']})"
            )
        else:
            self._current_job_id = None

        self._last_run_time = datetime.now().isoformat()

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
                reason = f"{failed_count} device(s) failed during the {rollout['phase']} wave"
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

                # Check if any devices were deferred (window cutoff) — don't advance wave
                phase_devices = db.get_rollout_devices(rollout["id"], phase=rollout["phase"])
                deferred_count = sum(1 for d in phase_devices if d["status"] == "deferred")
                if deferred_count > 0:
                    self._last_run_result = (
                        f"Wave {rollout['phase']}: {success_count} updated, "
                        f"{deferred_count} deferred (will retry next window)"
                    )
                    db.log_schedule_event(
                        "phase_deferred",
                        f"Wave {rollout['phase']}: {deferred_count} device(s) deferred",
                        job_id=job_id,
                    )
                    logger.info(f"Rollout {rollout['id']} wave {rollout['phase']} has {deferred_count} deferred device(s)")
                else:
                    db.complete_rollout_phase(rollout["id"])

                    refreshed = db.get_rollout(rollout["id"])
                    if refreshed and refreshed["status"] == "completed":
                        self._last_run_result = f"Rollout completed ({success_count} devices this wave)"
                        db.log_schedule_event("rollout_completed", f"Rollout {rollout['id']} completed", job_id=job_id)
                    else:
                        self._last_run_result = (
                            f"Wave {rollout['phase']} done ({success_count} devices), "
                            f"next: {refreshed['phase'] if refreshed else '?'}"
                        )
                        db.log_schedule_event(
                            "phase_completed",
                            f"Wave {rollout['phase']} -> {refreshed['phase'] if refreshed else '?'}",
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

        self._state = "waiting"
        self._block_reason = (
            rollout.get("pause_reason")
            if rollout and rollout.get("status") == "paused"
            else "Already ran today"
        )
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

        current_ap_batch = self._select_phase_batch(current_phase, ap_candidates)
        current_switch_batch = self._select_phase_batch(current_phase, switch_candidates)
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

            ap_batch = self._select_phase_batch(phase, remaining_ap_candidates)
            switch_batch = self._select_phase_batch(phase, remaining_switch_candidates)
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
        windows = upcoming_window_starts(
            datetime.now(), schedule_days, start_hour, end_hour, count=remaining_windows
        )
        estimated_completion = windows[-1].date().isoformat() if len(windows) >= remaining_windows else None

        return {
            "current_phase": current_phase_info,
            "remaining_phases": remaining_phases,
            "estimated_completion_date": estimated_completion,
            "avg_durations": avg_durations,
            "total_candidates": total_candidates,
            "window_minutes": window_minutes,
        }

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
        """Return when this device is expected to receive its next auto-update.

        Returns a dict with:
          - auto_update_eligible: bool — whether the scheduler will ever pick
            this device up given current scope/settings
          - next_attempt_iso: ISO datetime string or None
          - reason: short user-facing string when eligible is False or the
            attempt is blocked (e.g. waiting on the firmware hold to clear)

        `hold_clears_at_iso` is the device's Firmware Hold clear time (ISO), or
        None when its model family's hold is already clear. The hold delays the
        first fleet wave, so when set it pushes the next attempt past that date.
        Callers iterating over many devices (e.g. /api/fleet-status) pre-resolve
        `settings`, `scope_ips`, `switch_scope`, `rollout`, `rollout_devices_by_ip`,
        and the per-family hold to avoid N×DB queries. Pass `rollout=None`
        explicitly to say "no active rollout"; omit it to let this method look up.
        """
        settings = settings or db.get_all_settings()

        if settings.get("schedule_enabled") != "true":
            return {"auto_update_eligible": False, "next_attempt_iso": None,
                    "reason": "Auto-update is off"}

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

        # The Firmware Hold (computed per family by the caller) delays the first
        # fleet wave for newly-released firmware until it clears.
        hold_clears_at: Optional[datetime] = None
        if hold_clears_at_iso:
            try:
                hold_clears_at = datetime.fromisoformat(hold_clears_at_iso)
            except (TypeError, ValueError):
                hold_clears_at = None

        # If the device is already in an active rollout, use its assigned wave
        # to figure out how many windows away it lands.
        if rollout is _UNSET:
            rollout = db.get_active_rollout()
        phase_idx = 0
        if rollout:
            if rollout_devices_by_ip is None:
                rollout_devices_by_ip = {d["ip"]: d for d in db.get_rollout_devices(rollout["id"])}
            device = rollout_devices_by_ip.get(parent_ap_ip if role == "cpe" else ip)
            current_phase = rollout.get("phase", "pct10")
            current_idx = db.PHASE_ORDER.index(current_phase) if current_phase in db.PHASE_ORDER else 0
            if device:
                if device["status"] == "updated":
                    return {"auto_update_eligible": True, "next_attempt_iso": None,
                            "reason": "Updated"}
                assigned = device.get("phase_assigned")
                assigned_idx = db.PHASE_ORDER.index(assigned) if assigned in db.PHASE_ORDER else current_idx
                phase_idx = max(0, assigned_idx - current_idx)
            else:
                # Unassigned but rollout active — it will land in a later wave.
                phase_idx = 1 if current_idx < len(db.PHASE_ORDER) - 1 else 0

        # The hold gates the first fleet wave, so it applies to this device until
        # its family clears.
        earliest = hold_clears_at

        windows = upcoming_window_starts(
            datetime.now(), schedule_days, start_hour, end_hour,
            count=phase_idx + 1, earliest_after=earliest,
        )
        if not windows or len(windows) <= phase_idx:
            return {"auto_update_eligible": True, "next_attempt_iso": None,
                    "reason": None}
        target = windows[phase_idx]
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
                synthetic_rollout = {"id": 0, "phase": "pct10", "target_version": None}
                pre_rollout_predictions = self._calculate_predictions(synthetic_rollout, settings)
            except Exception as e:
                logger.debug(f"Pre-rollout prediction failed: {e}")

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
            "predictions": pre_rollout_predictions,
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
