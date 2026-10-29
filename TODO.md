# SixtyOps Manager — Task List

## Firmware Updates
- [x] **Implement 30-day update cooldown**
    - [x] Update `scheduler.py` to respect `last_firmware_update` timestamp.
    - [x] Add `firmware_update_cooldown_days` setting (default: 30).
    - [x] Add UI control in Settings > Auto-Update for the cooldown period.
- [x] **Pre-update config backup**
    - [x] Trigger a configuration snapshot before starting a firmware update.
    - [x] Ensure snapshots are visible in the device config history.

## UI/UX
- [x] Remove legacy `TODO` comments from `monitor.html` once implemented.
- [x] Standardize tooltip wording for "New firmware update delay" vs "Update cooldown".
