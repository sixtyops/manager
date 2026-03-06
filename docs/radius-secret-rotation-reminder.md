# Radius Secret Rotation Reminder Plan

This document defines the planned behavior for a built-in Radius shared-secret rotation reminder.

## Goal

Encourage operators to review and manually rotate the built-in Radius shared secret on a reasonable schedule without ever changing it automatically.

Default policy:

- Recommend review after `365` days
- Do not auto-rotate
- Do not auto-push a new secret to devices
- Do not block Radius auth when the reminder is overdue

## Non-Goals

- Automatic shared-secret rotation
- Automatic downstream device cutover
- Mandatory secret expiry
- Forced lockout of overdue deployments

## Operator Experience

The Authentication UI should show the current secret state as one of:

- `No Secret` - Radius enabled but no secret configured
- `Healthy` - Secret tracked and younger than 365 days
- `Review Due` - Secret age is 365 days or older
- `Age Unknown` - Secret exists but predates tracking metadata

UI behavior:

- Show `last rotated` date when known
- Show `days since rotation` when known
- Show a soft reminder banner when review is due
- Keep the existing secret field behavior: blank means unchanged, entering a new value is an explicit manual update
- For legacy secrets with no tracked date, show `Age Unknown` and offer a one-time explicit action such as `Mark current secret as reviewed today`

Important: the reminder is advisory only. The operator must still choose when to update the secret and when to push that change to devices.

## Backend Data Model

Add new settings keys:

- `builtin_radius_secret_updated_at`
- `builtin_radius_secret_review_acknowledged_at`

Recommendation logic:

- `builtin_radius_secret_updated_at` is set only when the stored secret value actually changes
- Saving Radius config without providing a new secret must not change the timestamp
- `builtin_radius_secret_review_acknowledged_at` is set when the operator explicitly marks an overdue or unknown-age secret as reviewed without changing it

Derived fields:

- `secret_tracked` - whether `builtin_radius_secret_updated_at` exists
- `secret_last_rotated_at`
- `secret_age_days`
- `rotation_recommended`
- `rotation_status` - one of `missing`, `healthy`, `due`, `unknown`

## Existing Deployments

Backward compatibility matters here.

For existing installs:

- If no Radius secret is configured, state remains `No Secret`
- If a secret exists but `builtin_radius_secret_updated_at` is empty, state is `Age Unknown`
- Do not backfill the timestamp automatically to `now`, because that would hide the real uncertainty

This means legacy systems will not get a false sense of freshness.

## API Changes

Extend `GET /api/auth/radius` and `GET /api/auth/radius/stats` with:

- `secret_last_rotated_at`
- `secret_age_days`
- `rotation_recommended`
- `rotation_status`
- `rotation_recommend_after_days`

Add a small explicit action endpoint:

- `POST /api/auth/radius/secret-review`

Request body:

```json
{
  "action": "acknowledge" | "mark_reviewed"
}
```

Initial implementation can keep this simple:

- `mark_reviewed` sets both `builtin_radius_secret_updated_at` and `builtin_radius_secret_review_acknowledged_at` to now, but only if a secret already exists and tracking is currently unknown
- `acknowledge` only updates `builtin_radius_secret_review_acknowledged_at` so the UI can stop highlighting the reminder aggressively for a short period if we later add snoozing

If we want a smaller first version, omit `acknowledge` and support only `mark_reviewed`.

## UI Changes

Add a small secret-health block in the built-in Radius card:

- `Last rotated`
- `Age`
- `Recommendation`
- `Status badge`

When `rotation_status = due`:

- Show a warning banner with copy like `Radius shared secret review recommended. Last rotated 382 days ago.`
- Include a secondary note that rotation is manual and must be coordinated with downstream devices

When `rotation_status = unknown`:

- Show `This secret predates rotation tracking`
- Include a button: `Mark current secret as reviewed today`

When the operator manually enters a new secret and saves:

- Update the secret
- Set `builtin_radius_secret_updated_at = now`
- Clear the overdue warning state

## Documentation Changes

Update these docs when the feature is implemented:

- `docs/radius.md`
  - Add the yearly review policy
  - Explain that rotation is manual, not automatic
  - Explain `Age Unknown` for older installs
- `docs/api.md`
  - Document the new rotation metadata fields
  - Document `POST /api/auth/radius/secret-review` if added
- `docs/deployment.md`
  - Add operational guidance for yearly review and coordinated device rollout

## Test Plan

Backend tests:

- New secret save sets `builtin_radius_secret_updated_at`
- Saving config without a new secret preserves the existing timestamp
- Re-saving the same secret does not reset age unless explicitly intended
- Legacy secret with no timestamp reports `rotation_status = unknown`
- Secret older than 365 days reports `rotation_status = due`
- `mark_reviewed` transitions unknown-age secrets into tracked state

UI tests:

- `Healthy` badge for tracked secrets under 365 days
- `Review Due` badge and message at 365+ days
- `Age Unknown` badge and review button for legacy secrets
- No reminder when no secret exists

## Suggested Implementation Order

1. Add settings keys and backend helper logic in `builtin_radius.py`
2. Extend Radius API responses with derived rotation metadata
3. Add the review action endpoint if we keep the explicit `mark reviewed` flow
4. Update the Authentication UI to display status and reminder copy
5. Update docs
6. Add tests for tracked, due, and unknown-age states

## Recommendation

Ship the first version with:

- Fixed `365` day recommendation
- No automatic rotation
- No automatic snoozing
- Manual `mark reviewed today` only for legacy unknown-age secrets

That gives operators a clear yearly reminder without creating any risk of unexpected Radius auth breakage.
