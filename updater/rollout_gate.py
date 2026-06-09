"""Single source of truth for rollout phase gating (fail-closed).

A phased rollout (canary -> pct10 -> pct50 -> pct100) must obey two timing
rules that protect uptime:

  1. **One phase per maintenance window.** After a rollout runs a phase's job in
     a window, the next phase waits for the *next* window. Without this, all four
     phases cascade through a single window and the whole fleet updates at once
     (the "all devices in one night" incident).

  2. **Canary soak.** The first widening past canary (pct10) waits until the
     canary has baked on the fleet for the configured soak period — measured
     from when the canary phase actually completed *here*, not from the
     firmware's release date. This soak is *waived* when the target firmware is
     already proven on the fleet (healthy same-model peers already run it); the
     caller passes that as `soak_proven`. The waiver clears the soak only — Rule
     1 still holds, so phases never cascade through one window.

Both rules live in this one function so they are defined and tested exactly once
and cannot drift between the execution path and any future caller (e.g. the UI's
"next attempt" prediction). The function is **fail-closed**: any unexpected
rollout state returns "do not run", so a future refactor that introduces a new
phase or status holds instead of cascading.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


def canary_soak_cleared(
    rollout: dict, now: datetime, canary_soak: timedelta
) -> tuple[bool, Optional[timedelta]]:
    """Return (cleared, remaining) for the canary soak.

    Cleared when `now >= reference + canary_soak`, where `reference` is when the
    canary finished on this fleet. Prefer the explicit `canary_completed_at`;
    fall back to `last_phase_completed_at`, which for a rollout sitting at pct10
    is the canary -> pct10 advance time (i.e. when canary completed). The
    fallback keeps a rollout that was already past canary when this code shipped
    honest — its `canary_completed_at` is NULL, but the soak still applies from
    the recorded phase-completion time.

    Fail-closed: if no completion timestamp exists at all, hold rather than
    silently skip the soak.
    """
    if not canary_soak or canary_soak.total_seconds() <= 0:
        return True, None
    ran_at = rollout.get("canary_completed_at") or rollout.get("last_phase_completed_at")
    if not ran_at:
        return False, canary_soak  # cannot date the canary — hold (fail-closed)
    try:
        ran_dt = datetime.fromisoformat(ran_at)
    except (ValueError, TypeError):
        return False, canary_soak  # unparseable timestamp — hold (fail-closed)
    # Completion timestamps are UTC (newer rows are tz-aware; older rows are naive
    # UTC, written by the container's UTC wall clock). Normalize BOTH sides to
    # aware UTC so the soak is an absolute duration, independent of the display
    # timezone `now` arrives in. (A prior version relabeled the naive UTC stamp
    # with now's local tz, which made the soak run ~the local UTC offset too long
    # — e.g. ~5h in US Central, enough to miss the window it should have cleared.)
    if ran_dt.tzinfo is None:
        ran_dt = ran_dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    clears_at = ran_dt + canary_soak
    if now >= clears_at:
        return True, None
    return False, clears_at - now


def phase_run_decision(
    rollout: dict,
    window_key: str,
    now: datetime,
    canary_soak: timedelta,
    *,
    soak_proven: bool = False,
) -> tuple[bool, Optional[str]]:
    """Decide whether `rollout` may START its current phase's job right now.

    `window_key` identifies the current maintenance window (its date). Returns
    (may_run, reason). When may_run is False, `reason` is a short machine-readable
    tag ("status_<x>", "already_ran_this_window", "canary_soak"). When may_run is
    True, `reason` is None for a normal run, or "canary_soak_waived" when the soak
    was cleared early because the firmware is already proven on the fleet.

    `soak_proven` (computed by the caller) clears Rule 2 only — it does NOT bypass
    Rule 1, so phases still advance at most one per maintenance window and never
    cascade. Fail-closed.
    """
    status = rollout.get("status")
    if status != "active":
        return False, f"status_{status}"

    # Rule 1: one phase-job per maintenance window. Always enforced — the waiver
    # below never lets a second phase run in the same window.
    last_window = rollout.get("last_phase_window")
    if last_window and last_window == window_key:
        return False, "already_ran_this_window"

    # Rule 2: canary soak gates the first widening past canary — UNLESS the target
    # firmware is already proven on healthy same-model peers (soak_proven), in
    # which case the proof the soak waits for already exists.
    if rollout.get("phase") == "pct10":
        if soak_proven:
            return True, "canary_soak_waived"
        cleared, _remaining = canary_soak_cleared(rollout, now, canary_soak)
        if not cleared:
            return False, "canary_soak"

    return True, None
