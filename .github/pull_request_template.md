## Result
<!-- State the result first. Use one to three short sentences. -->

## Issue
<!-- Add the full GitHub issue URL. -->
Issue URL:

## Validation
- CI lane and run URL:
- Tests added or removed:

## Device test plan
<!-- Keep these five labels at the start of each bullet. Give the Bench line the exact head SHA, lane names, run IDs, and two read-backs. Name the canary site and result, maintenance window, rollback trigger and action, and wave order. -->
- Bench: <exact head SHA>; lanes: <lane names>; run IDs: <run IDs>; two read-backs: <device status and firmware version>
- Canary: <site and result>
- Window: <maintenance window>
- Rollback: <trigger and rollback action>
- Waves: <10%, 50%, 100% order and window for each wave>

## Data recovery plan
<!-- Keep this section for schema changes, data-only migrations, and changes to a migration path. Give each bullet a specific action and read-back. Delete this section when none apply. -->
- Migration: <data change and migration check>
- Writes: <which actions write data and how to check them>
- Revert safety: <safe revert steps or recovery action>
- Downgrade: <downgrade steps and data check>
- Published dev rollback: <rollback steps for published development data>

## Documentation
- Docs changed:
- CHANGELOG: add a line for a user-visible change, or state why none is needed.
