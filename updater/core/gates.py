"""Runtime phase checks and maintenance-window identity."""

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..database import PHASE_ORDER


@dataclass(frozen=True, kw_only=True)
class MaintenanceSchedule:
    """One recurring maintenance window.

    `days` holds weekday numbers (Monday=0). A window belongs to the day it
    starts on. `end_hour <= start_hour` means the window crosses midnight;
    `end_hour == start_hour` is a 24-hour window, as in
    `services.is_in_schedule_window`. `tz` is an IANA zone name; None means
    `now` is already in the schedule's local time.
    """

    schedule_id: Any
    days: frozenset[int]
    start_hour: int
    end_hour: int
    tz: Optional[str] = None


WindowKey = tuple[int | str, str]


@dataclass(frozen=True)
class _WindowInstance:
    key: WindowKey
    end_utc: datetime


def _valid_hour(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 23


def _valid_schedule_id(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and value > 0
    ) or (isinstance(value, str) and bool(value.strip()))


def _current_window(schedule: Any, now: Any) -> Optional[_WindowInstance]:
    """Return the window instance that contains `now`, or None.

    Returns None for a missing or malformed schedule and for a naive `now`.
    """
    if not isinstance(schedule, MaintenanceSchedule) or not isinstance(now, datetime):
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        return None
    if not _valid_schedule_id(schedule.schedule_id):
        return None
    if not (_valid_hour(schedule.start_hour) and _valid_hour(schedule.end_hour)):
        return None
    days = schedule.days
    if not isinstance(days, (set, frozenset)) or not days:
        return None
    if not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
        return None
    if schedule.tz is not None:
        try:
            now = now.astimezone(ZoneInfo(schedule.tz))
        except Exception:
            return None

    tzinfo = now.tzinfo
    crosses_midnight = schedule.end_hour <= schedule.start_hour
    now_utc = now.astimezone(timezone.utc)
    # A window that crosses midnight can still be open from yesterday.
    for offset in (0, 1):
        day = now.date() - timedelta(days=offset)
        if day.weekday() not in days:
            continue
        start = datetime(day.year, day.month, day.day, schedule.start_hour, tzinfo=tzinfo)
        end_day = day + timedelta(days=1) if crosses_midnight else day
        end = datetime(end_day.year, end_day.month, end_day.day, schedule.end_hour, tzinfo=tzinfo)
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        if start_utc <= now_utc < end_utc:
            return _WindowInstance((schedule.schedule_id, day.isoformat()), end_utc)
    return None


def window_instance_key(schedule: Any, now: Any) -> Optional[WindowKey]:
    """Return `(schedule_id, local_start_date)` for the window open at `now`.

    Two windows on one day give two keys. One window that crosses midnight
    gives one key for its whole length, including DST offset changes. The date
    is an ISO date string, so the key survives JSON storage. Returns None
    when no window is open or the input is not valid.
    """
    window = _current_window(schedule, now)
    return window.key if window else None


def firmware_window_cutoff(
    schedule: Any,
    now: Any,
    ordinal: Any,
    ramp_minutes: Any,
    stored_cutoff: Any = None,
) -> Optional[datetime]:
    """Return the UTC cutoff for the active firmware window, or None if invalid.

    Ramp limits are anchored to the scheduled start. A stored cutoff can only
    shorten the result, so a restart or settings edit cannot extend a window.
    """
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        return None
    if not isinstance(ramp_minutes, (list, tuple)) or any(
        not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0
        for minutes in ramp_minutes
    ):
        return None
    if any(later < earlier for earlier, later in zip(ramp_minutes, ramp_minutes[1:])):
        return None

    window = _current_window(schedule, now)
    if window is None:
        return None

    local_now = now.astimezone(ZoneInfo(schedule.tz)) if schedule.tz else now
    try:
        start_date = date.fromisoformat(window.key[1])
        scheduled_start = datetime.combine(
            start_date, time(schedule.start_hour), tzinfo=local_now.tzinfo,
        ).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None

    cutoff = window.end_utc - timedelta(minutes=15)
    if ordinal <= len(ramp_minutes):
        cutoff = min(cutoff, scheduled_start + timedelta(minutes=ramp_minutes[ordinal - 1]))

    if stored_cutoff is not None:
        if isinstance(stored_cutoff, str):
            try:
                stored_cutoff = datetime.fromisoformat(stored_cutoff.replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(stored_cutoff, datetime) or stored_cutoff.tzinfo is None:
            return None
        try:
            stored_utc = stored_cutoff.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None
        cutoff = min(cutoff, stored_utc)
    return cutoff


def _is_window_key(value: Any) -> bool:
    if not (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and _valid_schedule_id(value[0])
        and isinstance(value[1], str)
    ):
        return False
    try:
        return date.fromisoformat(value[1]).isoformat() == value[1]
    except ValueError:
        return False


def _is_local_window_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def phase_run_decision(
    rollout: dict,
    window_key: Any,
    *,
    first_wave_held: bool = False,
) -> tuple[bool, Optional[str]]:
    """Decide whether `rollout` may START its current wave's job right now.

    `window_key` identifies the current maintenance window. A legacy date-only
    `last_phase_window` may refer to a window starting on that date or the
    previous date. Admit only a window starting after the marker date.
    `first_wave_held` (computed by the caller) is True when the pct10 wave must
    still wait out the Firmware Hold — i.e. the firmware's release-date hold has
    not elapsed AND no in-scope device of any pending family is confirmed working
    on it yet. It is meaningful only at pct10.

    Returns (may_run, reason). When may_run is False, `reason` is a short
    machine-readable tag ("status_<x>", "already_ran_this_window", "firmware_hold").
    When may_run is True, `reason` is None.

    `first_wave_held` gates pct10 only — it never bypasses Rule 1, so waves still
    advance at most one per maintenance window and never cascade. Fail-closed.
    """
    status = rollout.get("status")
    if status != "active":
        return False, f"status_{status}"

    # Rule 1: one wave-job per maintenance window. Always enforced and checked
    # first, so an early-cleared firmware hold can never let a second wave run in
    # the same window.
    last_window = rollout.get("last_phase_window")
    if last_window is not None:
        stored_key = last_window
        if isinstance(last_window, str):
            try:
                stored_key = json.loads(last_window)
            except (TypeError, ValueError):
                # Older installs stored the local maintenance date as plain text.
                stored_key = last_window
        if _is_local_window_date(stored_key):
            if _is_window_key(window_key):
                current_date = window_key[1]
            elif _is_local_window_date(window_key):
                current_date = window_key
            else:
                # Without a valid current date, the legacy marker cannot prove
                # that this is a different maintenance window.
                return False, "already_ran_this_window"
            if stored_key == current_date:
                return False, "already_ran_this_window"
            # The old marker used the clock date, not the window start date.
            # After midnight it can describe the previous day's window. With
            # no time or schedule ID, only a later start date proves separation.
            if current_date < stored_key:
                return False, "legacy_window_ambiguous"
            last_window = None
        elif _is_window_key(stored_key):
            last_window = tuple(stored_key)
        else:
            return False, "already_ran_this_window"
    if last_window and last_window == window_key:
        return False, "already_ran_this_window"

    # Rule 2: the firmware hold gates the first fleet wave (pct10) only.
    if rollout.get("phase") == "pct10" and first_wave_held:
        return False, "firmware_hold"

    # Scheduled admission also carries the CPE fresh-login results for the
    # candidate AP wave. Keep this in the shared gate so every caller uses the
    # same fail-closed admission path.
    cpe_auth_failures = rollout.get("cpe_auth_failures", ())
    if cpe_auth_failures:
        parent_ip, cpe_ip, status = cpe_auth_failures[0]
        return False, f"cpe_auth_failed:{parent_ip}:{cpe_ip}:{status}"

    return True, None
