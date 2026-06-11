# UI Principles

**Bottom line: minimize what the operator sees by default, never duplicate signals, never expose individual device management, and never block the dashboard on something we could've cached.** Component patterns (filters, popovers, density, performance) live in `docs/ui-components.md`.

The frontend is a single server-rendered template (`updater/templates/monitor.html`) — vanilla JS, CSS custom properties on `:root`, no framework, no build step. New UI must reuse existing tokens, helpers, and design language.

## Page structure

Three fixed layers, always in this order:

1. **Dashboard** (permanent) — add-devices card, signal/rain-fade chart, fleet-status counts, config-status counts, active-rollout strip when a rollout is in flight. Never changes based on active tab.
2. **Tabs** (below dashboard) — default open: **Updates**:
   - **Updates** — update policies (maintenance window, parallelism) + safety-defaults override panel. Live rollout status lives in the status bar, not here.
   - **Configuration** — configuration policies + drift resolution.
   - **Devices** — conformity ledger only. Intentional afterthought; one click away, never in the way.
3. **Persistent status bar** (fixed bottom) — always visible. Two clusters: update status + config conformity. Each cluster is clickable when something is active, expanding a panel upward with full detail (rollout progress or drift card). Abort button appears during active rollouts.

## Information hierarchy

- **One canonical place per signal.** If the Name dot encodes health, the Status column shouldn't.
- **Show contextual info only when it adds something.** Signal-margin badge only renders when AP-reported target exists.
- **Aggregate at parent rows.** AP leading dot = worst-CPE health. Don't make the operator dig.
- **Fleet scope, not device scope.** The Devices tab is a conformity ledger. Allowed columns: Name/IP, Model, Firmware (version + state merged), Config (Compliant/Drift), Signal (aggregate per AP), Last seen. Not allowed: per-CPE rows, serial numbers, per-device activity logs, SSH/reboot actions.

## Disclosure progression: default → hover → click

- **Default = minimum for scanning.** One dot, one number, one icon. No raw dB/mm/% values.
- **Hover = the full story.** Single reusable `.cpop` popover (defined once, repositioned per trigger) used on chart dots, table cells, and any future hover surface.
- **Click = navigation or action**, never disclosure. These must not compete — scope hover to the relevant cell, not the whole row.
- **Touch parity.** `matchMedia('(hover: none)')` switches popovers to tap-to-toggle; `@media (max-width:720px)` snaps to a bottom sheet.

## Status, color, and copy

- **Three buckets, three colors, three labels** — `green/yellow/red` → **Reliable / Mostly reliable / Unstable in rain**. Tokens: `--success`, `--warning`, `--danger-bright` with `*-bg` and `*-text` variants.
- **Info-blue (`--info*`) is the fourth semantic color, reserved for "scheduled or in progress"** — never for interactive controls. The accent for controls is teal (`--primary*`); see `UI_STYLE_GUIDE.md`.
- **Color is never the sole signal.** Pair with glyph (`▲`/`▼`), label, or shape.
- **Humans first, numbers as evidence.** Status pills carry English; numbers live in popovers.

## Rollout phases

Both firmware and config-push rollouts follow four phases: **Canary (1 device) → 10% → 50% → 100%**. Display a horizontal phase stepper (dot + label + per-phase count) in the expanded status-bar panel. Advancement differs by type:

- **Firmware rollouts** — the scheduler auto-advances phases after each phase completes (`scheduler.py`, `database.complete_rollout_phase`). Operator controls: canary trigger, resume after failure, cancel. The stepper is informational only — no manual-advance action.
- **Config-push rollouts** — advancement is always manual. Operators confirm each phase via `POST /api/config-push/rollout/{id}/advance`. The active-phase panel includes **Skip & advance to N% →** as the gate action.

In both: completed phases fill green; active phase reflects current state (yellow if paused/failed, blue if running); locked phases are grey outlines. Show device rows only for current and completed phases; queued phases show a count only.

## Don't ask the user for input

- **"There are no power users."** Zero-config bias. Climate auto-detects from IP geolocation. Antenna kit reads from the AP.
- **Allow override, never require it.** Climate override dropdown marks the auto-detected zone ("YOUR AREA" with a location pin).
- **Sensible defaults log once, never nag.** Missing antenna kit → assume AK-150, log it, move on.
- **We own the safety defaults.** Operators configure two things: maintenance window (default Tue–Thu 03:00–04:00 local) and parallelism. Everything else — canary hold, Weather Guard (backend default: −4 °C / 25 °F, stored in `min_temperature_c`), pre-reboot, auto-pause on failure, single-bank updates — is always on. Expose as an expandable "Safety defaults" section; operators may override but are never prompted to configure them on first use. Show server local time next to the maintenance window so the operator can confirm timezone without asking.

---

**Doc check:** covers page structure (dashboard/tabs/status bar), fleet-scope rules, disclosure progression, color/copy, firmware vs. config-push rollout phase distinction, and safety-defaults pattern including the correct Weather Guard default.
