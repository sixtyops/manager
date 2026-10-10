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

The function now lives in `updater/core/gates.py`, where the composed
`evaluate_wave_start` also calls it. This module re-exports it so existing
callers keep one definition.
"""

from .core.gates import phase_run_decision

__all__ = ["phase_run_decision"]
