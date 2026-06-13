# Gradual Rollout Auto-Update System

**Bottom line:** Auto-update rolls new firmware to the fleet in **10% → 50% → 100%**
waves, **one wave per maintenance window**, and **halts the whole job the moment a
device doesn't come back online**. New firmware is held for a configurable soak
(the **Firmware Hold**) before the first wave; the hold clears early once a device
is confirmed working on it. There is no separate canary phase — the first 10% wave
*is* the canary, protected by halt-on-failure.

The scheduler auto-detects which in-scope devices are behind their per-family target
(`tna-30x` / `tna-303l` / `tns-100`), creates a rollout, and advances it one wave per
window. A single fail-closed gate (`rollout_gate.py`) is the only place that decides
whether a wave may start. Failures pause the rollout for manual review. Manual
per-device updates bypass everything (including the hold) — that is how an operator
tests new firmware before the fleet rolls.

## The gate (`rollout_gate.py`)

`phase_run_decision(rollout, window_key, *, first_wave_held)` — fail-closed, two rules:

1. **One wave per maintenance window.** A wave job stamps `rollouts.last_phase_window`
   when it starts; while that equals the current window the gate holds the next wave,
   so waves never cascade through one night. DB-backed, survives a restart.
2. **Firmware Hold on the first wave.** When `first_wave_held` is true the `pct10`
   wave holds. The caller (`scheduler._held_families` + `_pending_split_by_hold`)
   computes it per firmware family: a family is **held** while its firmware's
   release-date + `firmware_canary_hold_days` hasn't elapsed **and** no in-scope
   device of that family is **confirmed working** on it. `pct50`/`pct100` never
   re-consult the hold, so a started rollout always finishes on schedule.

Every hold logs a `phase_held` or `blocked_firmware_hold` event.
`tests/test_rollout_invariants.py` drives the scheduler across ticks/windows to lock
both rules in.

## Firmware Hold & confirmed-working clear

- **Hold** = `firmware_canary_hold_days` (default/min 6) measured from the firmware's
  Tachyon **release date** (parsed from the filename `-YYYYMMDD-`; registry `added_at`
  fallback). It gates the first fleet wave only.
- **Confirmed working** = a device was updated to the target version, **passed its
  post-update smoke tests**, and is currently healthy (on-version, no `last_error`,
  seen <24h). Recorded in `firmware_confirmations (ip, version, confirmed_at)`; written
  in the post-update path (`app.py`) on smoke-pass.
- **Per family.** A confirmed `tna-30x` device never clears a held `tna-303l` family.
  When some families are cleared and others held, the wave runs for the cleared ones
  and held-family devices are filtered out (shown "on hold"), to ride a later wave.
- No manual bypass exists — the hold clears only by elapsed days or a confirmed device.
  A **manual** per-device update (`/api/start-update`, `/api/update-device`) ignores the
  hold entirely, which is how the operator creates the confirming device.

## Halt-on-first-failure

A device that fails to return online cancels the update job (`app.py`) and the rollout
is paused (`scheduler.on_job_completed` on `failed_count > 0`). A strict-mode smoke
failure (`smoke_test_strict`) marks the device failed and likewise halts the job. With
the default `parallel_updates=2`, the first bad device stops the run before it spreads.

## Per-device update state

`/api/fleet-status` reports `update_state ∈ {up_to_date, needs_update, on_hold}` plus a
`hold` object (`clears_at`, `confirmed_by`). A device is `on_hold` when it's behind, in
scope, auto-update is on, and its family's hold hasn't cleared. The UI shows a distinct
"on hold" glyph; clicking it still updates the device now (skipping the hold).

## Database (`database.py`)

- `rollouts` — `phase` is one of `pct10` / `pct50` / `pct100` (default `pct10`);
  `last_phase_window` drives Rule 1. `create_rollout` inserts `phase='pct10'`. A
  migration remaps any in-flight `canary` rollout to `pct10`.
- `rollout_devices` — per-device phase assignment + status.
- `firmware_confirmations` — confirmed-working records.
- `PHASE_ORDER = ["pct10", "pct50", "pct100"]`; `advance_rollout_phase` /
  `complete_rollout_phase` walk it. `mark_device_firmware_confirmed`,
  `get_confirmed_ips_for_version`, `get_firmware_hold_info` support the hold logic.

## Phase progression

| Window | Wave  | Batch              | After success |
|--------|-------|--------------------|---------------|
| 1      | pct10 | ~10% of remaining  | advance to pct50 |
| 2      | pct50 | ~50% of remaining  | advance to pct100 |
| 3      | pct100| all remaining      | rollout completed |

"Remaining" = in-scope APs whose firmware or manageable CPEs are behind, plus in-scope
switches behind their family target, minus devices already updated this rollout and
held-family devices. An empty wave auto-advances without consuming the window. New
firmware selection cancels the active rollout and starts a fresh one at `pct10`.

## Safety rules

1. **Failure pauses the rollout** — review and resume manually.
2. **One wave per maintenance window** — fail-closed gate via DB-backed `last_phase_window`.
3. **Firmware Hold soaks new firmware** before the first wave; clears early on a
   confirmed-working device, per family.
4. **New firmware = new rollout** — starts at `pct10`.
5. **Manual updates bypass the hold** — the operator's deliberate override / canary.
6. Time validation, scope, and weather checks still gate every wave.
