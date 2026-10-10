"""Wave-start gates as pure functions (fail-closed).

`evaluate_wave_start(ctx)` runs the named gates in a fixed order and returns
the first failure. Execution and the UI "next attempt" prediction must both
call it, so neither keeps its own copy of the rules.

Every gate fails closed. Missing, unknown, or malformed input blocks the wave.
The one documented exception is NTP: an unreachable time source
(`ntp_drift_seconds=None`) allows the wave, and the caller logs it.

`phase_run_decision` holds the timing rules for one rollout: status, one wave
per maintenance window, and the Firmware Hold on the first wave (pct10). The
`already_ran_this_window` gate calls it, so the hold reason
(`firmware_hold`) comes from that gate.

Nothing calls this module yet. The scheduler still calls
`rollout_gate.phase_run_decision` with a date key.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from ..database import _parse_freeze_boundary

# Do not start a wave with this many minutes or less left in the window.
# Same value as scheduler.SCHEDULE_END_BUFFER_MINUTES.
WINDOW_END_BUFFER_MINUTES = 15

# Clock drift above this many seconds blocks the wave.
# Same value as services.validate_time_sources(max_drift=300).
MAX_CLOCK_DRIFT_SECONDS = 300


@dataclass(frozen=True)
class Decision:
    """Result of a gate. `reason` is a machine-readable code, None when ok."""

    ok: bool
    reason: Optional[str] = None


OK = Decision(True)


def _block(reason: str) -> Decision:
    return Decision(False, reason)


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


WindowKey = tuple[Any, datetime]


@dataclass(frozen=True)
class _WindowInstance:
    key: WindowKey
    end_utc: datetime


def _valid_hour(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 23


def _current_window(schedule: Any, now: Any) -> Optional[_WindowInstance]:
    """Return the window instance that contains `now`, or None.

    Returns None for a missing or malformed schedule and for a naive `now`.
    """
    if not isinstance(schedule, MaintenanceSchedule) or not isinstance(now, datetime):
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        return None
    if schedule.schedule_id is None:
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
            return _WindowInstance((schedule.schedule_id, start_utc), end_utc)
    return None


def window_instance_key(schedule: Any, now: Any) -> Optional[WindowKey]:
    """Return `(schedule_id, window_start_utc)` for the window open at `now`.

    Two windows on one day give two keys. One window that crosses midnight
    gives one key for its whole length. Returns None when no window is open
    or the input is not valid.
    """
    window = _current_window(schedule, now)
    return window.key if window else None


def _is_window_key(value: Any) -> bool:
    return (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and isinstance(value[1], datetime)
        and value[1].tzinfo is not None
    )


def phase_run_decision(
    rollout: dict,
    window_key: Any,
    *,
    first_wave_held: bool = False,
) -> tuple[bool, Optional[str]]:
    """Decide whether `rollout` may START its current wave's job right now.

    `window_key` identifies the current maintenance window.
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
    if last_window and last_window == window_key:
        return False, "already_ran_this_window"

    # Rule 2: the firmware hold gates the first fleet wave (pct10) only.
    if rollout.get("phase") == "pct10" and first_wave_held:
        return False, "firmware_hold"

    return True, None


@dataclass(frozen=True, kw_only=True)
class WaveStartContext:
    """Everything the gates read. Every field is required.

    Pass None for a value you do not know. The gate that reads it then blocks,
    except `ntp_drift_seconds`, where None means NTP is unreachable.

    - `rollout`: rollout row with `status`, `phase`, `last_phase_window`.
      `last_phase_window` is None or a key from `window_instance_key`.
    - `freeze_windows`: freeze rows with `start_date`, `end_date`, `enabled`.
    - `weather`: True when the weather guard passes, False when it blocks.
    - `artifacts_ok`: True when every artifact the wave needs is on disk and
      its sha256 matches.
    - `pending_units`: count of safe, schedulable units in the wave.
    - `job_in_flight`: False when no other job is running.
    - `first_wave_held`: False when the Firmware Hold is clear for pct10.
    """

    rollout: Optional[dict]
    now: Optional[datetime]
    schedule: Optional[MaintenanceSchedule]
    freeze_windows: Optional[list]
    ntp_drift_seconds: Optional[float]
    weather: Optional[bool]
    artifacts_ok: Optional[bool]
    pending_units: Optional[int]
    job_in_flight: Optional[bool]
    first_wave_held: Optional[bool]


def _gate_status(ctx: WaveStartContext) -> Decision:
    if not isinstance(ctx.rollout, dict):
        return _block("status_None")
    status = ctx.rollout.get("status")
    return OK if status == "active" else _block(f"status_{status}")


def _gate_job_in_flight(ctx: WaveStartContext) -> Decision:
    return OK if ctx.job_in_flight is False else _block("job_in_flight")


def _gate_already_ran_this_window(ctx: WaveStartContext) -> Decision:
    rollout = ctx.rollout
    last = rollout.get("last_phase_window")
    if last is not None:
        # A legacy date key or other unknown format cannot prove this window
        # is new.
        if not _is_window_key(last):
            return _block("already_ran_this_window")
        rollout = {**rollout, "last_phase_window": tuple(last)}
    key = window_instance_key(ctx.schedule, ctx.now)
    # Unknown hold state blocks pct10.
    held = ctx.first_wave_held is not False
    may_run, reason = phase_run_decision(rollout, key, first_wave_held=held)
    return OK if may_run else _block(reason)


def _gate_frozen(ctx: WaveStartContext) -> Decision:
    if not isinstance(ctx.now, datetime) or ctx.now.tzinfo is None:
        return _block("frozen")
    if not isinstance(ctx.freeze_windows, (list, tuple)):
        return _block("frozen")
    for row in ctx.freeze_windows:
        if not isinstance(row, dict):
            return _block("frozen")
        if row.get("enabled", 1) in (0, False):
            continue
        start = _parse_freeze_boundary(row.get("start_date"), end_of_day=False, default_tz=ctx.now.tzinfo)
        end = _parse_freeze_boundary(row.get("end_date"), end_of_day=True, default_tz=ctx.now.tzinfo)
        # An enabled row we cannot read may cover now.
        if start is None or end is None:
            return _block("frozen")
        if start <= ctx.now <= end:
            return _block("frozen")
    return OK


def _gate_clock_invalid(ctx: WaveStartContext) -> Decision:
    drift = ctx.ntp_drift_seconds
    if drift is None:
        # NTP unreachable: allow on the system clock. The caller logs it.
        return OK
    if isinstance(drift, bool) or not isinstance(drift, (int, float)) or math.isnan(drift):
        return _block("clock_invalid")
    return OK if abs(drift) <= MAX_CLOCK_DRIFT_SECONDS else _block("clock_invalid")


def _gate_outside_window(ctx: WaveStartContext) -> Decision:
    return OK if _current_window(ctx.schedule, ctx.now) else _block("outside_window")


def _gate_window_ending(ctx: WaveStartContext) -> Decision:
    window = _current_window(ctx.schedule, ctx.now)
    if window is None:
        return _block("window_ending")
    left = window.end_utc - ctx.now.astimezone(timezone.utc)
    return OK if left > timedelta(minutes=WINDOW_END_BUFFER_MINUTES) else _block("window_ending")


def _gate_blocked_weather(ctx: WaveStartContext) -> Decision:
    return OK if ctx.weather is True else _block("blocked_weather")


def _gate_artifact_missing(ctx: WaveStartContext) -> Decision:
    return OK if ctx.artifacts_ok is True else _block("artifact_missing")


def _gate_nothing_schedulable(ctx: WaveStartContext) -> Decision:
    units = ctx.pending_units
    if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
        return _block("nothing_schedulable")
    return OK


Gate = Callable[[WaveStartContext], Decision]

# The order is the contract (docs/rollout-logic.md, "Wave gate").
GATES: tuple[tuple[str, Gate], ...] = (
    ("status", _gate_status),
    ("job_in_flight", _gate_job_in_flight),
    ("already_ran_this_window", _gate_already_ran_this_window),
    ("frozen", _gate_frozen),
    ("clock_invalid", _gate_clock_invalid),
    ("outside_window", _gate_outside_window),
    ("window_ending", _gate_window_ending),
    ("blocked_weather", _gate_blocked_weather),
    ("artifact_missing", _gate_artifact_missing),
    ("nothing_schedulable", _gate_nothing_schedulable),
)


def evaluate_wave_start(ctx: WaveStartContext) -> Decision:
    """Return the first failing gate's Decision, or `Decision(True)`.

    A gate that raises blocks the wave with its own name as the reason.
    """
    if not isinstance(ctx, WaveStartContext):
        return _block("status_None")
    for name, gate in GATES:
        try:
            decision = gate(ctx)
        except Exception:
            return _block(name)
        if not decision.ok:
            return decision
    return OK
