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
     firmware's release date.

Both rules live in this one function so they are defined and tested exactly once
and cannot drift between the execution path and any future caller (e.g. the UI's
"next attempt" prediction). The function is **fail-closed**: any unexpected
rollout state returns "do not run", so a future refactor that introduces a new
phase or status holds instead of cascading.
"""

from datetime import datetime, timedelta
from typing import Optional


def canary_soak_cleared(
    rollout: dict, now: datetime, canary_soak: timedelta
) -> tuple[bool, Optional[timedelta]]:
    """Return (cleared, remaining) for the canary soak.

    Cleared when `now >= canary_completed_at + canary_soak`. If the rollout has
    no recorded canary completion, there is nothing to soak, so it is cleared.
    """
    if not canary_soak or canary_soak.total_seconds() <= 0:
        return True, None
    ran_at = rollout.get("canary_completed_at")
    if not ran_at:
        return True, None  # canary never recorded a completion — nothing to soak
    try:
        ran_dt = datetime.fromisoformat(ran_at)
    except (ValueError, TypeError):
        return True, None
    # Reconcile tz-awareness so naive (stored) and aware (live) clocks compare.
    if now.tzinfo is not None and ran_dt.tzinfo is None:
        ran_dt = ran_dt.replace(tzinfo=now.tzinfo)
    elif now.tzinfo is None and ran_dt.tzinfo is not None:
        now = now.replace(tzinfo=ran_dt.tzinfo)
    clears_at = ran_dt + canary_soak
    if now >= clears_at:
        return True, None
    return False, clears_at - now


def phase_run_decision(
    rollout: dict,
    window_key: str,
    now: datetime,
    canary_soak: timedelta,
) -> tuple[bool, Optional[str]]:
    """Decide whether `rollout` may START its current phase's job right now.

    `window_key` identifies the current maintenance window (its date). Returns
    (may_run, reason); `reason` is None when may_run is True, otherwise a short
    machine-readable tag ("status_<x>", "already_ran_this_window",
    "canary_soak"). Fail-closed.
    """
    status = rollout.get("status")
    if status != "active":
        return False, f"status_{status}"

    # Rule 1: one phase-job per maintenance window.
    last_window = rollout.get("last_phase_window")
    if last_window and last_window == window_key:
        return False, "already_ran_this_window"

    # Rule 2: canary soak gates the first widening past canary.
    if rollout.get("phase") == "pct10":
        cleared, _remaining = canary_soak_cleared(rollout, now, canary_soak)
        if not cleared:
            return False, "canary_soak"

    return True, None
