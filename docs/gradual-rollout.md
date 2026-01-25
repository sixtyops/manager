# Gradual Rollout Auto-Update System

## Overview

Replaces the "update all devices at once" scheduler with an intelligent gradual rollout that:
1. Auto-detects which devices need updates (compares DB firmware versions to target)
2. Rolls out in phases: **canary -> 10% -> 50% -> 100%**, one phase per schedule night
3. Lets operators pin dedicated AP and switch canaries in the firmware drawer
4. Lets the pending `Canary` pill run the canary phase immediately, outside the maintenance window
5. Pauses on failure; resumes manually

---

## Database Changes (`database.py`)

### New tables in `init_db()`

```sql
CREATE TABLE IF NOT EXISTS rollouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firmware_file TEXT NOT NULL,
    firmware_file_303l TEXT,
    target_version TEXT,               -- learned / fallback target for tna-30x
    target_version_303l TEXT,
    target_version_tns100 TEXT,
    phase TEXT NOT NULL DEFAULT 'canary',  -- canary | pct10 | pct50 | pct100
    status TEXT NOT NULL DEFAULT 'active', -- active | paused | completed | cancelled
    pause_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_phase_completed_at TEXT,
    last_job_id TEXT
);

CREATE TABLE IF NOT EXISTS rollout_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rollout_id INTEGER NOT NULL,
    ip TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'ap',
    phase_assigned TEXT,
    status TEXT DEFAULT 'pending',     -- pending | updated | failed | skipped
    updated_at TEXT,
    FOREIGN KEY (rollout_id) REFERENCES rollouts(id),
    UNIQUE(rollout_id, ip)
);
```

### Database functions

- `get_active_rollout() -> Optional[dict]` -- get rollout with `status='active'` or `status='paused'`
- `get_rollout(rollout_id) -> Optional[dict]`
- `create_rollout(firmware_file, firmware_file_303l) -> int`
- `get_last_rollout_for_firmware(firmware_file) -> Optional[dict]` -- most recent rollout for this firmware
- `advance_rollout_phase(rollout_id)` -- canary->pct10->pct50->pct100->completed
- `complete_rollout_phase(rollout_id)` -- set `last_phase_completed_at` + advance
- `pause_rollout(rollout_id, reason)`
- `resume_rollout(rollout_id)`
- `cancel_rollout(rollout_id)`
- `set_rollout_target_versions(rollout_id, versions)`
- `set_rollout_job_id(rollout_id, job_id)`
- `assign_device_to_rollout(rollout_id, ip, device_type, phase)`
- `mark_rollout_device(rollout_id, ip, status)` -- mark a single device
- `mark_rollout_phase_devices(rollout_id, phase, status)` -- bulk mark updated/failed
- `get_rollout_devices(rollout_id, phase) -> list[dict]`
- `get_rollout_progress(rollout_id) -> dict` -- counts by status

---

## Scheduler Logic (`scheduler.py`)

### Rollout-aware `_check_and_run()`

Steps 1-8 remain unchanged (schedule enabled check, job running check, timezone, time validation, schedule window, ran_today, cutoff, weather).

Steps 9+ (replaced):

1. **Check firmware selection** -- if no `selected_firmware_30x`, block
2. **Get or create rollout** -- `db.get_active_rollout()`. If none exists:
   - Check if a previous rollout used the same firmware file (already done). If so, check if any in-scope devices still have old firmware. If none, set `blocked_all_current` + "All devices up to date".
   - Otherwise create a new rollout for this firmware.
3. **Firmware change detection** -- if active rollout uses different firmware file, cancel it and create new one
4. **If rollout is paused** -- show state `waiting` with the pause reason, return
5. **Determine phase batch** -- call `_get_devices_for_phase()` and `_get_switches_for_phase()`:
   - Get all in-scope AP IPs via `_resolve_scope()`
   - Get only in-scope switches via `_resolve_switch_scope()`
   - Build per-family target versions for `tna-30x`, `tna-303l`, and `tns-100`
   - An AP is considered pending when the AP itself or any attached manageable CPE is behind its family target
   - A switch is considered pending only when it is behind the selected switch target and inside scope
   - For canary:
     - If `rollout_canary_aps` / `rollout_canary_switches` are configured, use those devices first
     - Otherwise fall back to `1` AP and `1` switch when available
   - For later phases, select `10%`, `50%`, or `100%` of the remaining APs and switches independently
   - If no candidates remain, advance phase (or complete rollout)
   - Record selected APs and switches in `rollout_devices`
6. **Launch job** -- call `start_update_func()` with the phase APs and phase switches, store `job_id` on rollout

### Manual canary trigger

- `POST /api/rollout/canary/trigger` starts only the canary phase immediately
- It still respects:
  - time-source validation
  - firmware hold / quarantine
  - weather guardrails
  - saved canary validation and current rollout scope
  - the configured canary AP / switch selection
- It intentionally skips:
  - the maintenance-window check
  - the daily `_ran_today` lockout
  - the per-device maintenance-window cutoff used by normal scheduled jobs
- Result:
  - the test canary can run during the day
  - the later `10%`, `50%`, and `100%` phases still wait for the next maintenance window

### Modified `on_job_completed()`

Added `learned_versions: dict[str, str] | None` parameter.

- If rollout is active and `last_job_id` matches:
  - **On failure** (`failed_count > 0`): pause rollout with reason, mark failed devices
  - **On success**:
    - Store learned target versions per firmware family (`tna-30x`, `tna-303l`, `tns-100`) when available
    - Mark phase devices as `updated`
    - Call `complete_rollout_phase()` (advances to next phase)

### `get_status()` additions

Includes rollout info in the status dict:
```python
"rollout": {
    "id": ..., "phase": ..., "status": ..., "target_version": ...,
    "firmware_file": ..., "progress": {"total": N, "updated": N, "pending": N, "failed": N},
    "pause_reason": ...
} or None
```

---

## App Changes (`app.py`)

### Modified `run_update_job()` -- pass learned versions

After job completion, if the job is scheduled, extract one learned version per firmware family from successful devices and pass that map to `scheduler.on_job_completed()`.

### API endpoints

- `GET /api/rollout/current` -- return active/paused rollout + progress
- `POST /api/rollout/canary/trigger` -- start the canary phase immediately
- `POST /api/rollout/{rollout_id}/resume` -- resume paused rollout
- `POST /api/rollout/{rollout_id}/cancel` -- cancel rollout

### WebSocket connect

Sends `rollout_status` message with current rollout info (if any), in addition to `scheduler_status` which now also includes rollout data.

---

## UI Changes (`monitor.html`)

### Rollout status card in Auto-Update tab

Positioned above the scheduler status bar, showing:
- Phase indicator: `Canary -> 10% -> 50% -> 100%` with dots colored by state (completed=green, active=blue, paused=yellow)
- Status badge (active / paused / completed)
- Firmware file name
- Target version (once learned after canary)
- Progress: "12 / 120 devices updated" with progress bar
- If paused: reason text + Resume button + Cancel button
- If active: Cancel button only

### JavaScript

- Handles `rollout_status` and `scheduler_status` (rollout field) WebSocket messages
- `updateRolloutUI(rollout)` -- renders rollout card
- `hideRolloutCard()` -- hides when no active rollout
- `triggerCanaryNow()` -- fires the manual canary endpoint from the pending canary pill
- `resumeRollout()` / `cancelRollout()` -- API calls
- The firmware drawer persists `rollout_canary_aps` and `rollout_canary_switches` from inventory-backed checkbox lists, not just live topology
- Saving canaries validates that they are real enabled devices in the effective rollout scope

---

## Version Comparison Logic

**Problem:** A single learned target is not enough once APs, 303L CPEs, and switches can all be in the same rollout.

**Solution -- per-family targets:**
1. Prefer parsing target versions from the selected rollout firmware filenames
2. If a filename cannot be parsed, learn the version from the first successful device of that firmware family
3. AP eligibility checks the AP plus its attached manageable CPEs against the correct family targets
4. Switch eligibility checks only the switch target for its family

---

## Phase Progression Summary

| Night | Phase | Batch Size | After Success |
|-------|-------|-----------|---------------|
| 1 | canary | Configured canary APs (+ attached CPEs) and configured canary switches, else 1 AP + 1 switch when available | Learn any missing family targets, advance to pct10 |
| 2 | pct10 | ~10% of remaining APs and ~10% of remaining switches | Advance to pct50 |
| 3 | pct50 | ~50% of remaining APs and ~50% of remaining switches | Advance to pct100 |
| 4 | pct100 | All remaining APs and switches | Mark rollout completed |

- "Remaining" = in-scope APs whose own firmware or manageable CPEs are behind, plus in-scope switches behind their family target, excluding devices already `updated` in this rollout
- If a phase has 0 candidates (all already current), auto-advance to next phase immediately
- If all phases are done, rollout status = `completed`

---

## Safety Rules

1. **Failure pauses rollout** -- any device failure in a phase pauses the entire rollout. User must review and resume.
2. **One phase per night** -- existing `_ran_today` set prevents re-running. Phase advances happen logically, not by re-triggering the same night.
3. **New firmware = new rollout** -- if `selected_firmware_30x` changes, any active rollout is cancelled and a fresh one starts from canary.
4. **Canary validates** -- the canary phase always runs the configured canary AP / switch first when available, or falls back to a single AP / switch.
5. **All existing safety rules preserved** -- time validation, switch/AP scope, and weather checks still apply before any phase runs.
6. **Manual canary is isolated** -- clicking the pending `Canary` pill does not consume the nightly rollout window for later phases and does not inherit the scheduled cutoff.

---

## Files Modified

| File | What Changes |
|------|-------------|
| `updater/database.py` | 2 new tables, ~16 new rollout functions |
| `updater/scheduler.py` | Replace steps 9-11 with rollout logic, modify `on_job_completed`, update `get_status` |
| `updater/app.py` | 3 new endpoints, modify `run_update_job` to pass `learned_version`, send rollout status on WS connect |
| `updater/templates/monitor.html` | Rollout status card with phase indicator + progress + resume/cancel buttons |
