"""Single source of truth for rollout phase gating (fail-closed).

A fleet rollout widens in waves (pct10 -> pct50 -> pct100). There is no separate
canary phase: the first wave (pct10) is the de-facto canary, and any device that
fails to come back online halts the whole job (halt-on-first-failure). Two timing
rules protect uptime:

  1. **One wave per maintenance window.** After a rollout runs a wave's job in a
     window, the next wave waits for the *next* window. Without this, all waves
     cascade through a single window and the whole fleet updates at once (the
     "all devices in one night" incident).

  2. **Firmware hold on the first wave.** The first wave (pct10) of newly-released
     firmware waits out the **Firmware Hold** — N days after the firmware's Tachyon
     release date — before any fleet device updates. The hold clears early, per
     model family, once a device of that family is confirmed working on the new
     firmware (the operator's manual canary). The caller computes that and passes
     `first_wave_held`; this function only enforces it. The hold gates pct10 only —
     pct50/pct100 never re-consult it, so a rollout that has begun always finishes
     on schedule.

Both rules live in this one function so they are defined and tested exactly once
and cannot drift between the execution path and any future caller (e.g. the UI's
"next attempt" prediction). The function is **fail-closed**: any unexpected
rollout state returns "do not run", so a future refactor that introduces a new
phase or status holds instead of cascading.
"""

from typing import Optional


def phase_run_decision(
    rollout: dict,
    window_key: str,
    *,
    first_wave_held: bool = False,
) -> tuple[bool, Optional[str]]:
    """Decide whether `rollout` may START its current wave's job right now.

    `window_key` identifies the current maintenance window (its date).
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
