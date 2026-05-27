# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Changed
- External time validation no longer consults `worldtimeapi.org`. That
  endpoint started timing out on roughly every request (>1 fail per minute
  observed on sixtyops-dev in 2026-05), and `timeapi.io` — the existing
  fallback — was always picking up the work anyway. `get_external_time`
  now goes straight to `timeapi.io`. No setting change required (#163).
- `config_enforce_hour` is now explicitly seeded to `"4"` on fresh
  installs. The poller already defaulted to 4 AM local when the row was
  absent (`poller.py:801`), but a missing row made the value invisible
  in the settings table to anyone inspecting the DB directly. Behaviour
  unchanged; just visibility (#166).

### Fixed
- Config templates that enable `ping_watchdog` with a reboot trigger
  shorter than 30 minutes are now rejected by the template-save and
  config-push paths. The previously-seeded `Watchdog Standard` default
  of `interval=300, failure=3, addresses=[8.8.8.8, 1.1.1.1]` rebooted
  every device on a bench in lockstep when a brief upstream blip made
  both public IPs unreachable for 15 minutes (issue #162). The template
  form default for "Failures" is now `6` (30-min trigger). Operators
  can still use any combination of `interval` and `failure` that
  clears the 1800-second floor.
- Device portal: rebuilt the cert-untrusted fallback as a single
  "Sign in" button that opens a small popup, POSTs the login form into
  it (top-level navigation, which Firefox honours per the existing
  per-origin cert exception), then closes the popup as soon as the
  user does — or after 5s — and redirects the main window onto the
  device's now-logged-in home. Replaces the previous two-button
  cert-accept-then-poll flow that left Firefox users stuck because
  Firefox doesn't extend a manually-accepted self-signed cert
  exception to cross-origin subresource requests (the favicon probe
  and iframe login POST added in #110 both stayed silently blocked).
  Chrome's fast path is unchanged: when the probe succeeds, the
  iframe POST + redirect runs with no popup at all.
- Auto-update scheduler no longer spawns duplicate no-op rollouts when the
  per-tick eligibility check disagrees with the per-phase assignment check
  (cooldown days defaulted to `0` in the dedup path while the assignment
  step applied the configured cooldown). Both paths now use the same
  cooldown value, and `db.create_rollout` refuses to insert a second
  rollout for the same firmware set within 60s as a belt-and-suspenders
  guard. On `sixtyops-dev` this bug had produced 265 identical rollouts
  for one firmware filename, created in clustered 3-minute bursts during
  Tue/Wed/Thu maintenance windows whenever CPE versions drifted relative
  to the AP.

### Changed
- "Firmware quarantine" is now "Canary hold," and the hold no longer blocks
  the whole rollout — it only gates the canary→pct10 advance. The canary
  phase itself now runs immediately, so a newly-uploaded firmware gets
  soaked on the configured canary AP instead of sitting unused for a week.
  Setting key renamed from `firmware_quarantine_days` (default 7, min 0)
  to `firmware_canary_hold_days` (default 6, min 6); operator-set values
  are migrated automatically on startup and clamped to the new minimum.
  WebSocket scheduler status field renamed from `quarantine` to
  `canary_hold`; firmware-files API renamed `quarantine_*` fields to
  `hold_*`. UI state label is "Running" with a per-rollout countdown
  ("Holding 3d 12h more") instead of a top-level "Scheduled" badge.

### Added
- Post-deploy operator checklist at `docs/post-deploy-checklist.md`: ten-item, five-minute smoke test the operator runs after install (admin login, site, AP, poll, config compliance, notification test, audit log via API, backups decision, HTTPS, update channel). Audit-log step uses `curl` against `GET /api/audit-log` and links forward to #136 for the in-UI panel. Linked from the README's Quick Start pointer and Documentation section (#122)
- Operator quickstart at `docs/quickstart.md`: ~10-minute install-to-first-device walkthrough (install → first login → setup wizard → first tower site → first AP → first poll). Screenshot placeholders only this pass; real captures land in a follow-up. Linked from the README's Quick Start and Documentation sections (#123)
- Troubleshooting one-pager at `docs/troubleshooting.md` covering the top five operator failure modes (device unreachable, RADIUS auth, hung update jobs, SSL renewal, SFTP backup) with symptom → diagnose → recover for each. Linked from README's Documentation section and the in-app About panel footer (#124)
- Switch → AP topology cascade: APs are now nested under their upstream switch in the device tree, with a port badge showing the switch port they're connected to (ordered by port number)
- OIDC admin group mapping: configure an "Admin Group" in SSO settings to auto-promote members to admin role on login
- Role badge in the header shows the current user's role; write-operation UI (Add Devices, delete, bulk actions) is hidden from viewer accounts
- Viewer UI is now consistently read-only across the Updates/Config drawers and the Settings modal: toggles, schedule inputs, firmware selectors, config policies, notifications, RADIUS, backup/restore, and HTTPS controls are visibly locked rather than appearing editable. Action buttons (Save, Upload, Push, Resume/Cancel, Check Compliance) are hidden. When a write does land on the server (e.g. via a stale tab), the inline status now reads "Read-only access — ask an admin" instead of the generic "Save failed"
- Initial config priming: devices without a cached config are polled on the next poll cycle instead of waiting until the 4 AM daily run, so compliance works from day one
- Check Compliance now triggers a fresh config poll (`?refresh=true`) with visible "Polling devices…" feedback instead of reading stale cache
- NTP server defaults (132.163.97.1 and 129.6.15.28) in the config template editor; toggle stays off by default
- Toast notification CSS (fixed top-right, typed color borders, slide-in) — previously toasts rendered as unstyled plaintext in the page body
- Last-admin / self-delete guards on the Local Users table (disabled button + tooltip)
- Config snapshot recycle bin: deleting a device now soft-deletes its config history rather than orphaning rows. Deleting an AP also cascades to its CPEs' snapshots. A new "Config Snapshot Recycle Bin" panel in the Config drawer lets admins restore or permanently purge entries
- MAC-based config-history auto-rebind: when a managed device's IP changes (DHCP renumber, replacement at the same MAC), its prior config history is automatically re-linked to the new IP. The UI surfaces a toast and refreshes when this happens
- Manager backup export now includes device config snapshots and the recycle bin (Fernet-encrypted with the same passphrase). Re-import is idempotent on `(ip, fetched_at)` so DR no longer resets config history
- `device_configs.config_json` is now Fernet-encrypted at rest with the same key (`data/.encryption_key`) used for device passwords (#35). Configs typically contain RADIUS shared secrets, WPA PSKs, SNMP communities and 802.1X creds; previously these sat in plaintext in SQLite, so anyone with read access to the DB file could lift them. Existing rows are migrated in-place on next startup (idempotent — checks for the Fernet `gAAAAA` prefix). The `config_hash` column stays plaintext so change-detection queries (`get_latest_config_hash`) remain cheap. Backup tar export still re-wraps with the export passphrase so DR works across managers with different storage keys

### Changed
- Updates tab clarifies "when next" for both the fleet and individual devices. The Auto-Update status badge collapses to four user-facing labels — **Off**, **Scheduled**, **Updating**, **Paused**, **Up to date** — replacing the eight internal terms (Idle, Waiting, Blocked, On Hold, No firmware, All current, …) the badge used to surface. Each Scheduled/Paused state shows a concrete subtitle ("Next: tomorrow at 3:00 AM" or the block reason) instead of the abbreviated `3:00-4:00 on tue,wed,thu` window string. Hovering the orange ↑ on a device row now shows that device's next expected attempt ("Next attempt tomorrow at 3:00 AM — click to update now"), computed from its rollout phase, the maintenance window, and any firmware-quarantine hold. Internal scheduler state names are unchanged, so logs and `schedule_log` history are unaffected
- Config auto-enforce can now auto-rollback the last enforce phase when the post-enforce re-poll shows mass failure. New setting `config_enforce_auto_rollback_threshold_pct` (default `0` = off, range `0-100`). When non-zero, after the post-enforce re-poll the percent of last-phase devices still non-compliant is compared against the threshold; if exceeded, each affected device's pre-enforce snapshot is pushed back and the action is logged with `phase=rollback`. A `config_enforce_status` event with `status="rolled_back"` is broadcast so operators see what happened. The pre-enforce snapshot already existed for manual `/api/config-push/rollback/{ip}` — this just closes the loop automatically (#50)
- Config auto-enforce now classifies canary failures and retries transient ones (login failed, fetch-config failed) up to `config_enforce_canary_retry_count` times (default 1, range 0-3) before declaring the canary failed. Policy failures (dry-run rejected, apply failed) still stop the run immediately — they're the signal canary is meant to catch. The `config_enforce_log.error` column now prefixes the message with `"transient: "` or `"policy: "`, and exhausted retries append a `(N/N retries)` suffix so the audit trail makes sense (#49)
- Config auto-enforce now defers when an immediate `/api/config-push` job is in flight, in addition to the existing rollout check. Closes the race where a 4 AM enforce run could overwrite an operator's manual change by acting on cached compliance data. The skip is broadcast as a `config_enforce_status` event with `status="skipped"` so it shows up in the audit trail (#48)
- API documentation routes (`/docs`, `/redoc`, `/openapi.json`) are now auth-gated and serve locally-vendored Swagger UI / ReDoc assets instead of `cdn.jsdelivr.net`. Two issues addressed in one change: (1) browser content blockers were silently dropping the default FastAPI HTML's CDN script tags, leaving the doc pages blank — same root cause as the Chart.js vendoring fix. (2) Anonymously-accessible docs were leaking the full API surface (every route, request/response schema, validator constraints) to anyone who could reach the manager. New `static/vendor/swagger-ui.css` (155 KB), `static/vendor/swagger-ui-bundle.js` (1.4 MB, swagger-ui-dist 5.21.0), `static/vendor/redoc.standalone.js` (911 KB, redoc 2.5.0). The schema content itself is unchanged
- Config push rollouts now put the "advance" affordance on the **next** phase pill instead of the just-completed one. After canary finishes, the 10% pill lights up with a pulsing blue background and explicit "Tap to push" copy — instead of the previous design where the canary pill became clickable with only a 1-pixel inset shadow and a hover-tooltip nudge. Operators were missing the affordance entirely and assuming the next phase would auto-run. For 1-device rollouts (no later phase has any devices), the current pill remains clickable with "Tap to finish" copy and walks straight through the empty phases via PR #88's empty-phase logic
- Signal health classification tightened to match the network's actual operating envelope: Strong (≥ −60 dBm), Low (−61 to −65 dBm), Marginal (< −65 dBm). Was Strong (> −65), Low (−65 to −75), Critical (< −75) — much more lenient than the network actually performs at. The Signal vs Distance chart's reference dotted lines now sit at −60 (Strong/Low boundary) and −65 (Low/Marginal boundary) instead of −65 and −75. The "Critical" tier was renamed to "Marginal" everywhere it surfaces (chart legend, per-row signal coloring, CPE status tooltip — internal CSS class `signal-critical` kept for diff size). `SignalHealth.GREEN/YELLOW/RED` API values are unchanged so external consumers keep working
- Auto-update default `allow_downgrade` is now `true` (was `false`) and default `min_temperature_c` is now `-4` (was `-10`, i.e. ≈14°F → 25°F). New installs will pre-fill the System > Updates drawer with the safer "Weather Guard < 25°F" cutoff and downgrades enabled by default. Existing installs are not affected — these defaults only seed on a fresh DB via `INSERT OR IGNORE`
- Config tab "Last Backup" column renamed to "Last Checked" and now reads from `devices.last_config_poll_at` / `cpe_cache.last_config_poll_at` (the per-device poll-outcome timestamp added in PR #67) instead of `device_configs.fetched_at`. The old column was misleading: `fetched_at` only advances when a *new snapshot row is inserted*, which the poller skips when the config hash is unchanged — so a successfully-polled fleet whose configs hadn't drifted showed dates weeks behind the present. The cell tooltip still surfaces the last config-change date, and a `⚠` glyph appears when the most recent poll outcome was non-`ok`. `/api/configs` gains `last_polled_at`, `last_poll_status`, `last_poll_error` fields per device
- `GET /api/config-compliance` per-device `checked_at` now reflects the last successful config poll (`devices.last_config_poll_at` / `cpe_cache.last_config_poll_at`) instead of the snapshot's `fetched_at`. Same root cause as the "Last Checked" column rename above — `fetched_at` only advances when the config drifts, so the compliance summary's "last checked" date on a successfully-polled fleet stayed frozen at whenever each device's config last changed. Falls back to `fetched_at` for legacy rows that pre-date per-device poll-outcome tracking
- Chart.js (Signal vs Distance dashboard chart) is now vendored at `static/vendor/chart.umd.js` (pinned to v4.5.1) instead of loaded from `cdn.jsdelivr.net`. Some browser content blockers were silently blocking the CDN request, leaving the chart blank with `ReferenceError: Chart is not defined` in the console. Vendoring eliminates the dashboard's only third-party runtime asset dependency
- `docker-compose.yml` now publishes ports through env-overridable defaults: `${BIND_IP:-0.0.0.0}:${HOST_PORT:-8000}:8000` and `${BIND_IP:-0.0.0.0}:${RADIUS_HOST_PORT:-1812}:1812/udp`. Default behavior is unchanged (binds `0.0.0.0:8000` and `0.0.0.0:1812/udp`). Operators on multi-tenant hosts can now set `BIND_IP=<host-ip>` / `HOST_PORT=<port>` / `RADIUS_HOST_PORT=<port>` in their environment instead of hand-deleting the upstream `ports:` block — which would leave the working tree dirty and break the in-app self-update path on every release
- Manual config push (`/api/config-push` and `/api/config-push/preview`, including phased rollouts) now honors each template's `device_types` filter — an AP-only template targeted at a switch is reported as "skipped" in preview and silently bypassed at apply, instead of being merged into a config it doesn't belong in. The push job and rollout state expose a new `skipped` counter alongside `success`/`failed`.
- Config tar download (`/api/configs/{ip}/download/{config_id}`) now writes the CONTROL file as a `key=value` manifest (`hardware_id`, `fetched_at`, `config_hash`, `manager_version`) instead of just the bare hardware id, so a future re-import path can verify the snapshot's origin and integrity
- Bridge/FDB table polled from Tachyon switches on each poll cycle to maintain AP-to-port mapping
- Chassis connector replaced with inline `eth[n]` port badge on nested AP rows (cleaner, no orphaned line art)
- System > Updates panel normalized into a label/control grid; RADIUS Server stat cards removed; RADIUS Clients & Logs rewritten for clarity; About panel redesigned with inline version chip

### Removed
- Settings > Backup & Restore "Remote Backups (SFTP)" list-and-restore panel hidden until the feature is finished. The placeholder "DANGEROUS" badge and "Loading backups…" spinner were the only thing visible to operators on instances without an SFTP server configured. The Run Now / Open SFTP Setup buttons remain so configuration still works; the JS (`loadRemoteBackups`, `restoreRemoteBackup`) is left in place so reviving the panel is a one-line markup change
- Appliance build infrastructure (OVA/QCOW2 image generation, Packer configs, build-appliance workflow)

### Fixed
- Users-template compliance check incorrectly flagged every fleet as non-compliant immediately after a successful push. Each push generates a fresh random salt for `$1$<salt>$<hash>` MD5-crypt values, so the template-side hash and the device-side hash encoded the same plaintext but never compared equal as strings — `fragment_matches`'s naïve string-equal returned False forever. Now the `system.users` path is special-cased: per-user comparison is keyed on `username` (not list index, since devices return users in factory order), and the `password` field tolerates differing hashes when both sides are valid 34-char `$1$<salt>$<hash>` strings. Plaintext or empty `password` on either side still falls back to exact-match — that's how factory-default credentials and post-reset states surface as drift, which is the actual goal of the Users compliance check
- Config-push rollouts no longer require a final "Advance" click after the pct100 phase finishes with no failures. `_run_config_push_phase` checks for any remaining `pending` devices across all phases at the end of each phase; if none are left and no failures occurred, the rollout transitions straight to `completed`. The post-pct100 `(Tap to finish)` step on the phase pill bar is gone for the happy path. Failures still pause for manual `Resume` as before
- Dashboard `failure_count` chip kept showing yesterday's resolved failure indefinitely. `get_enforce_failures` now suppresses any failed row that has a subsequent successful enforce row for the same IP — operators already saw the resolution in the log; pinning the failure to the chip just adds noise. Tie-breaks on `(enforced_at, id)` so rapid successive rows with identical 1-second-resolution timestamps are still ordered correctly. Failed rows stay in `config_enforce_log` for diagnostics; we just stop counting them as live drift once a success lands
- Users-category `config_templates` rows stored device-user passwords as **plaintext** in `config_fragment.system.users[*].password` and the matching `form_data.users[*].password`. SQL dumps, CSV backups, and anyone with `config_templates` read access could read the credentials directly — a soft-secret leak we should never have shipped. Plaintext is now hashed in place to `$1$<salt>$<hash>` MD5-crypt (the same format the device stores) at template-save time, and a one-shot startup migration (`_migrate_users_template_password_hashing`) re-hashes any pre-existing plaintext rows once on first boot of an upgraded build. The GET response also scrubs the stored hash and flags affected users with `has_stored_password: true` so the form can render a `(unchanged — leave blank)` placeholder instead of echoing even the hashed value to the browser. Operator UX: leaving the password field blank preserves the prior hash; typing a new value re-hashes; and the existing `_normalize_user_passwords` push-time hashing keeps the door closed even if a stray plaintext slips past
- "Update Available" banner in System > Updates rendered the "Update Now" button as faint white text on a near-invisible faded grey background. `.updates-banner-btn` only set `background` and `color`; with the global `* { padding:0; margin:0 }` reset removing browser defaults, the button kept the user-agent default border + grey gradient fill, and the green background never showed through. Filled in the missing `border`, `padding`, `font-size`, `font-weight`, and `border-radius` plus explicit `:hover` / `:active` / `:disabled` states. Same banner's release-notes bullets had wrap text flush left under the marker instead of indented under the previous line's text — `.release-notes-content ul` had `list-style: disc` but no `padding-left`, so default `list-style-position: outside` markers landed in the parent's left edge. Added `padding-left: 1.5em` and per-`li` line-height
- Auto-enforce was failing the canary push every day at 4 AM whenever the Users template was enforced, with `Configuration validation failed: Length of value for system.users.0.password_hash must be between 34 and 34 characters`. The Tachyon write validator only knows the JSON key `password` — `password_hash` is silently dropped as an unknown property. Within `password`, the validator accepts plaintext OR a 34-char `$1$<salt>$<hash>` MD5-crypt string for *existing* users, but for new users (when a Users template merge expands the user list) it strictly requires the 34-char hash. Manager auto-enforce had been sending plaintext, which works for the device's existing users but fails the moment the merge adds one. The Tachyon vendor client (`apply_config`) now hashes any plaintext `system.users[*].password` value through `passlib.hash.md5_crypt` before sending; values that already start with `$1$` (re-pushed configs, hand-pasted `openssl passwd -1` output) pass through untouched. Verified live against tw35-ap-test that the previously-rejected merged payload now passes the dry-run cleanly. New `passlib>=1.7.4` requirement (small pure-Python dep — `crypt` was deprecated in 3.12 and removed in 3.13)
- Job History "By Device" and "By Job" tabs displayed timestamps off by the operator's TZ offset. The manager container runs in UTC and writes `device_update_history.completed_at` / `job_history.completed_at` via `datetime.now().isoformat()`, which produces a naive ISO string with no `Z` suffix. The frontend's `new Date(naiveStr).toLocaleString()` then parses those strings as the *browser's* local time, so a UTC 21:21 event displayed as "9:21 PM" in a CDT browser instead of the correct "4:21 PM". Both views now normalize through a new `parseTimestampToDate(s)` helper that appends `Z` when no TZ marker is present, so `.toLocaleString()` converts to the operator's wall clock correctly. `loadAppUpdateStatus` was already doing this inline; the helper centralizes the pattern for future timestamp displays
- Config-push rollout phase pills (Canary / 10% / 50% / 100%) stayed visible in the Config tab status bar after a rollout finished, so a fully-green bar lingered until the next rollout started or the page reloaded — looking like the live status of whatever the operator did next (e.g. Check Compliance). The render function only cleared pills for `cancelled` rollouts; `completed` rollouts fell through and rendered four green dots indefinitely. Now the bar only shows pills while a rollout is `active` or `paused`; the toast on completion remains the success indicator
- Config tab "Auto-Enforce" toggle didn't visually flip when the operator confirmed enabling it. The PUT to `/api/settings` succeeded and the server-side state changed, but the toggle's `active` CSS class was never added — so the toggle stayed visually off until the next `loadEnforceStatus` run. Now the toggle flips immediately on click (matching every other toggle in the file, e.g. `toggleAppAutoUpdate`) and rolls back if the PUT fails
- TNS-100 switches showed an "update available" arrow on the Updates tab even when both firmware banks already ran the selected target version. Tachyon switch firmware filenames use a bare `tns-` prefix (e.g. `tns-1.12.8-r54729-...-tns-100-...bin`) where APs use `tna-30x-` / `tna-303l-`. The version-extraction regex in `_extract_version_from_filename` only knew the AP-style prefixes and the explicit `tns-100-` form, so it missed the actual filename, fell through to a fallback that captured only `1.12.8` (no `-r54729` build revision), and `_compare_versions` then saw the device's `1.12.8.54729` as *ahead* of target `1.12.8`. With `allow_downgrade=true` (the new default in PR #92), `_device_version_status` interpreted "ahead" as "needs an update" and lit the up-arrow. Added `tns` to the regex alternates and made the fallback capture the `-rN` revision when present, so future unrecognised filenames don't silently lose their build numbers either. Also keeps the duplicate copy in `scheduler.py` in sync (parameterized tests cover both).
- Config-push rollouts of 1–3 devices got stuck after canary because `_compute_phase_batch_size` allocates a per-phase floor of 1 device and the canary always consumes the first slot, leaving `pct10`/`pct50`/`pct100` with zero assigned devices. The advance endpoint's "at least one device succeeded" guard then 400-ed on every advance attempt past the first empty phase, with no path to completion short of cancelling the rollout. Advance now treats empty phases as auto-skippable (the safety guard only fires when the current phase actually has assigned devices) and walks past consecutive empty phases in a single call so a 1-device rollout completes with one click instead of being stranded at `pct10`. Discovered while smoke-testing PR #87 against a single-AP target on the dev instance
- AP poll cycle was destroying CPE per-poll outcome columns every minute. The pattern was `db.clear_cpes_for_ap(ip)` followed by `db.upsert_cpe(...)` — the DELETE removed every row, and the subsequent INSERT brought the row back with NULL `last_config_poll_at/status/error` because `upsert_cpe`'s column list (correctly) doesn't include those. Replaced with upsert-then-prune: a new `prune_stale_cpes(ap_ip, current_ips)` deletes only the CPE rows whose IPs are no longer attached to the AP, preserving outcome columns for CPEs that are still there. This was the actual reason the dev CPE outcome columns kept reverting to NULL even after PRs #81 and #83 shipped
- Config-poll exceptions raised by `client.connect()` (network unreachable, ECONNREFUSED, SSL handshake failure, timeout) used to bypass every per-device status write — the outer `except Exception` in `_fetch_and_store_config` only logged the error at debug level. Operators saw an indefinite gap that looked indistinguishable from "config unchanged". Now the outer except records `last_config_poll_status='unknown'` with the exception message so the failure has a visible reason. This is what was hiding the polling state of the 3 dev CPEs that were silently throwing connect errors after PR #52 shipped
- Per-device config-poll outcome (PR #52) now also tracked for CPEs. `cpe_cache` gains `last_config_poll_at`, `last_config_poll_status`, `last_config_poll_error` columns; `update_device_config_poll_status()` writes both `devices` and `cpe_cache` so the existing AP/switch/CPE shared poll path records an outcome regardless of role. Previously CPE config polling was fire-and-forget — a successful no-op (config unchanged, hash-matches) and a silent failure looked identical, hiding genuine polling problems behind unchanged `device_configs.fetched_at` timestamps. Now operators get the same `ok`/`timeout`/`http_status`/`json_decode`/`auth`/`unknown` taxonomy on CPEs as on APs and switches
- Daily config-poll catch-up now fires after a manager upgrade from a build that predated the `last_config_poll_at` persistence (PR #67). Previously, hydration found no setting, treated the manager like a fresh install, and silently waited until the next 04:00 — even when cached configs were days stale. Hydration now falls back to `MAX(fetched_at)` from `device_configs` when the setting is missing, with separator-aware UTC/local parsing and a clamp against future-dated rows. Real fresh installs (no setting AND no rows) keep the previous behavior
- Self-update now detects uncommitted local changes to tracked files in the manager repo *before* attempting `git checkout` and returns a structured `{success: false, dirty_tree: true, dirty_files: [...], suggested_command}` response instead of the opaque "Your local changes would be overwritten by checkout" error. Untracked files are not flagged because they don't block checkout. Operators get a clear message and a copy-pasteable `git stash` command instead of having to read the raw git error
- OIDC user roles edited in the Local Users table reverted to the `oidc_default_role` (or `viewer`) on the user's next login. Now the IdP is the source of truth only when an `admin_group` is configured; with no `admin_group`, manual UI changes persist across logins. The Local Users table also disables the role dropdown for OIDC users when an `admin_group` is configured (with a tooltip explaining the role is IdP-managed), and shows a toast confirmation when a role/enable change is saved
- Config tab summary chips ("0 Devices / Compliant / Non-Compliant / Unchecked") stayed at zero on initial page load when the WebSocket topology arrived after `loadConfigData()`; `updateUI()` now re-runs `updateConfigStats()` whenever topology updates so the chips reflect the live fleet
- Signal vs Distance chart silently swallowed CPEs that hadn't been polled yet — the `-100` fallback fell below the `-75` y-axis minimum, hiding the dot. CPEs without signal data now floor at `-88` and the y-axis extends to `-90` so Critical-tier and unpolled CPEs are visible; tooltip distinguishes "Signal: not yet polled" from real readings (also switched to `??` so a real `0` reading isn't dropped)
- "Update Available" banner in Settings > Updates stayed hidden even when an update was detected (inline `display:none` overrode the `.hidden` class toggle)
- Switch → AP topology cascade wasn't populating because `TachyonDriver` didn't expose `get_bridge_table()`; added passthrough so bridge entries are stored and APs render under their upstream switch
- Tachyon config GET/POST hit the wrong endpoint (`/cgi.lua/apiv1/config`) and returned HTTP 401 "Authorization Failed"; corrected to `/cgi.lua/config` so config backup, compliance, and push all work
- Signal vs Distance chart was empty whenever every AP at a site sat behind a managed switch — `updateChart()` only walked `site.aps[]` and missed `site.switches[].aps[]`; now iterates both, and selecting a switch in the topology scopes the chart to its nested APs
- Topology index (`rebuildTopologyIndex`) skipped APs and CPEs nested under managed switches, so `findAP`/`findCPE` returned null for those rows — broke "Edit notes", AP/CPE checkbox selection, and chart point highlight for switch-nested devices
- Site-wide iterators (Config tab badge, model/firmware filter dropdowns, "X Devices / Y Compliant" bar, site/all checkbox toggles + indeterminate state, CPE preservation across polls) walked only `site.aps[]` and undercounted or skipped switch-nested APs/CPEs; now traverse both branches via a shared `walkSiteAPs` helper, so site-row checkboxes also toggle every nested device row
- "Update site" firmware action and the config-push paths (rollout target builder, preview device picker — which also had a `site.access_points` typo — and "Push to selected") missed APs and CPEs nested under managed switches; switch-nested devices now flash with their site, count toward the confirmation summary, and resolve correctly for config push. "Push to selected" now drops unknown IPs with a toast instead of silently sending them as `type: 'ap'`
- Config-push rollout `all_aps` scope was sending `{type: 'site', id}` per site, which the backend resolves to APs + CPEs + switches — so picking "all APs" silently pushed to CPEs and switches too. The rollout target builder now enumerates per role for `all_aps`/`all_switches`/`all_cpes` (matching the existing pattern for the unassigned-site bucket) and only uses the site shortcut for `all` scope
- Crashed scheduled jobs no longer leave the rollout stuck "active" — `_finalize_crashed_job` was passing `learned_version=` instead of `learned_versions=`, so the scheduler call raised `TypeError` and the rollout never progressed
- Per-device window cutoff was off-by-one: at exactly `end_hour` the deferral fell into the overnight branch and computed ~24 hours remaining, so devices kept updating past the window
- Freeze windows configured with a date-only `end_date` (e.g., "2026-05-05") now cover the entire end-day inclusively; previously the lexical string compare excluded the entire end-day
- Scheduler now recovers orphaned active rollouts after a restart: pending devices whose update job died with the previous process are flipped to `deferred` so they retry next window instead of being silently skipped
- `_ran_today` startup recovery uses the configured timezone for the date key, preventing a same-day double-run when the container's system TZ differs from the configured TZ
- `trigger_canary_now` no longer raises `ValueError` when settings like `parallel_updates` or `min_temperature_c` are malformed; uses the safe `_as_int` / `_as_float` helpers
- Time-source drift validation samples the system clock after the external HTTP response so request latency cannot register as drift
- Stale job-completion events post-restart are now logged and reconciled if the active rollout still tracks the job_id, instead of silent drop
- Pre-rollback safety snapshot is now mandatory: if `/api/config-push/rollback/{ip}` can't capture the current config (device unreachable for fetch, empty response), the rollback is refused with HTTP 409 instead of proceeding silently. Operators can override with `force=true` in the request body, which logs a warning and writes a `config.rollback.force` audit-log entry. The response now includes `safety_snapshot_saved`, and the UI re-prompts the operator before forcing
- Deleting a device now also purges its rows in `config_enforce_log`, `device_update_history`, and `device_uptime_events`. Previously these audit/history tables accumulated orphaned rows keyed by stale IPs, and reusing an IP for a different device blended the histories. New `scripts/cleanup_orphaned_device_data.py` (with `--dry-run`) cleans up rows that orphaned before this fix shipped. `device_configs` is intentionally still soft-deleted via the recycle bin
- Daily config-poll window catch-up: the last successful poll time is now persisted to `settings.last_config_poll_at`, and on the next poll tick the manager checks whether that timestamp is older than 25h. If so it runs a catch-up poll instead of silently skipping the day. Previously the in-memory `_last_config_poll` was lost on restart, so a manager that was down during the configured poll hour would miss an entire day of compliance data with no signal to the operator
- Per-device config-poll outcome is now persisted to `devices.last_config_poll_at/_status/_error` (status one of `ok`, `timeout`, `http_status`, `json_decode`, `auth`, `unknown`). Previously a failed `get_config()` returned `None`, the device disappeared from `device_configs`, and the operator had no signal that polling was broken on a specific device. Tachyon driver now exposes `fetch_config()` that returns `(config, status, error)` for granular classification
- Signal vs Distance chart rendered fully blank (no axes, grid, or threshold lines) when every CPE in scope had a null `link_distance` — Chart.js v4's auto-scale collapsed the x-axis to a degenerate `[0, 0]` range, throwing in the user-supplied tick callback before the render could complete. The x-axis now anchors at `min: 0` with `suggestedMax: 100` and the tick callback guards non-numeric values; `initChart` is wrapped in try/catch so future Chart.js construction failures surface in the console instead of silently blanking the canvas

## 1.3.0 - 2026-04-08

### Added
- Release validation script (`scripts/validate_release.py`) for automated API-level smoke testing against live deployments
- "Dangerous" feature classification: 6 features that make sweeping network/auth changes are labeled with amber badges in the UI (config backup/restore, config templates, config compliance, config push, RADIUS, SSO/OIDC)
- `/api/features` endpoint returning feature map with enabled/dangerous status
- About panel in Settings (replaces License panel) showing version, instance ID, and GitHub link
- Config auto-enforce: automatically detect config drift and push corrections in phases (canary → 10% → 50% → 100%)
- Site-scoped config templates: site templates override global per category
- Config enforce log: audit trail of all auto-enforcement actions
- Syslog and Watchdog config template categories (replacing Discovery)
- Config push confirmation dialog and "All Switches" scope option
- SLA/uptime tracking with automatic state transition detection in poller
- Per-device and fleet-wide availability percentage calculations
- Uptime API endpoints: /api/uptime/device, /api/uptime/fleet, /api/uptime/events
- Device notes field for APs and switches
- Bulk device operations: enable, disable, delete, move to site
- OpenAPI documentation with tagged endpoints, Swagger UI at /docs, ReDoc at /redoc
- Bandwidth throttling for firmware uploads (configurable KB/s limit per device)
- Update analytics dashboard with summary stats, daily trends, model breakdown, error analysis, and device reliability
- SNMP trap notifications for firmware update job completion (SNMPv2c)
- SNMP trap configuration UI in Settings > Notifications panel
- Test trap button for verifying SNMP configuration
- Inline release notes display in Settings > Updates panel
- GitHub release notes categorization via `.github/release.yml`
- SHA256 integrity verification for firmware files before device upload
- Overall update timeout safety net (30 min APs/CPEs, 45 min switches)
- Concurrency limit (10) for RADIUS rollout device pushes
- Self-update safety gate: block app updates while firmware jobs are running
- Device offline/recovered email notifications
- RADIUS open client mode (accept any device with correct secret, default)
- HTTPS/SSL tab in App Settings
- Setup wizard replaced with App Settings auto-open on first run
- Weather temperature display on startup (no longer waits for first scheduler tick)

### Changed
- **Open-source conversion**: all features are now free and unlocked with no license key required
- Removed all billing/licensing infrastructure (license server, Stripe, activation, validation, grace periods, device counting, free-tier limits, nag banners)
- `updater/license.py` replaced by `updater/features.py`; `license.py` is now a thin re-export shim
- `require_feature()` and `require_pro()` are no-ops (kept in endpoint signatures for minimal diff)
- Repo references updated from `isolson/firmware-updater` to `sixtyops/manager`
- Docker image updated from `ghcr.io/isolson/firmware-updater` to `ghcr.io/sixtyops/manager`
- Website pricing section replaced with open-source feature list
- Privacy policy updated to remove license validation references
- Config push rollout controls (advance, resume, cancel) now require admin or operator role
- Complete rebrand from Tachyon to SixtyOps across codebase, Docker, appliance, and CI
- App Settings modal uses fixed height to prevent layout jumping between tabs
- Email notification subjects changed from `[Tachyon]` to `[SixtyOps]`
- Simplified to single-branch (`main`) workflow — no more `dev` staging branch

### Removed
- License key activation, deactivation, and validation endpoints
- License validator background task and grace period logic
- Free-tier device limits and nag banner
- `SIXTYOPS_FORCE_PRO` environment variable
- `website/billing.html`
- Stripe and billing references throughout codebase
- In-app subscription checkout with Stripe and auto-activation via instance_id
- Contextual license status banners (cancelled, over limit, expired, grace period)

### Fixed
- Crashed update jobs now properly clear active job state
- Website deploy pipeline (AWS OIDC credentials + S3/CloudFront)
- Logo alignment (icon sits on text baseline)
- Local Users tab not loading on initial Auth tab open
- Border radius normalization (5px → 6px)
- Appliance now boots on both Proxmox (virtio) and ESXi (SCSI) hypervisors via UUID-based fstab/bootloader and SCSI initramfs drivers
- Appliance SSL cert generation failure now stops the service instead of silently continuing
- Appliance boot disk detection is now automatic instead of hardcoded to /dev/vda
- Proxmox installation instructions corrected to use virtio disk controller

## 1.1.1-dev1 - 2026-02-19

### Added
- Release workflow protections (dev/stable split, manual approval for stable)
- Development documentation (CLAUDE.md, contributing section)
- System update overlay with progress tracking
- Settings notification dot for available updates

### Changed
- Updates panel layout and label clarity improvements

## 1.1.0 - 2026-02-19

### Added
- SSO/OIDC authentication save fix
- Updates panel layout improvements

## 1.0.5 - 2026-02-18

### Fixed
- Data directory permissions for Docker volumes

## 1.0.0 - 2026-02-17

### Added
- Gradual rollout for scheduled updates: canary (1 AP) -> 10% -> 50% -> 100%
- Rollout status card in Auto-Update tab with phase indicator and progress bar
- Rollout pause on failure with manual resume/cancel controls
- Target firmware version auto-detection after canary phase
- API endpoints: `GET /api/rollout/current`, `POST /api/rollout/{id}/resume`, `POST /api/rollout/{id}/cancel`
- `rollouts` and `rollout_devices` database tables
- CPE authentication probing - detects CPEs with OK/failed auth status
- Login retries for failed device connections
- System time validation against NTP sources before running updates
- Rebranded UI titles to "Unofficial Tachyon Networks Bulk Updater"
- CSV backup/restore for device lists
- Real-time status broadcasts
- Pre-rollout predictions
- Single-page monitor with settings drawer
- Firmware fetcher and UI polish

### Changed
- Device-level phase ordering replaces AP-group concurrency model
- Updates run in phases: CPEs pass 1 -> APs pass 1 -> APs pass 2 -> CPEs pass 2
- Split single Firmware tab into separate Firmware (file management) and Update (manual) tabs
- IP addresses in UI are now clickable links

### Security
- RADIUS authentication with local username/password fallback
- Session-based auth with 24-hour TTL and HTTPOnly cookies
- Resource cleanup and security hardening
- API security hardening

### Infrastructure
- Dockerfile and docker-compose.yml with persistent volumes
- Docker Compose split into base + standalone overlay
- Background network poller discovering APs and CPEs every 60 seconds
- SQLite persistence for devices, sessions, settings, and job history
