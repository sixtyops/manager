# UI Principles

**Bottom line: minimize what the operator sees by default, never duplicate signals, never expose individual device management, and never block the dashboard on something we could've cached.** Component patterns (filters, popovers, density, performance) live in `docs/ui-components.md`.

The frontend is a single server-rendered template (`updater/templates/monitor.html`) — vanilla JS, CSS custom properties on `:root`, no framework, no build step. New UI must reuse existing tokens, helpers, and design language.

## Page structure

Three fixed layers, always in this order:

1. **Dashboard** (permanent) — add-devices card, signal/rain-fade chart, fleet-status counts, config-status counts, active-rollout strip when a rollout is in flight. Never changes based on active tab.
2. **Tabs** (below dashboard) — default open: **Updates**:
   - **Updates** — current shipped UI has update policies and a safety-defaults
     override panel. There is no Parallel setting. Weather availability and
     sample time are shown. Saving or discarding edits must be confirmed before
     the drawer closes. Live rollout status lives in the status bar.
   - **Configuration** — configuration policies + drift resolution.
   - **Devices** — conformity ledger only. Intentional afterthought; one click away, never in the way.
3. **Persistent status bar** (fixed bottom) — always visible. It shows update status and config conformity. Unsaved Updates changes expose Save and Discard controls and save-result feedback. Paused states show the full reason in a separate notice row, with recovery instructions; manual updates remain available. The queue summary shows its count and next window.

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

- **Three buckets, three colors, three labels** — `green/yellow/red` → **Survives local storms / Survives heavy rain / May drop in heavy rain**. Labels are climate-relative: green = survives the worst storm the auto-detected region sees (the 99.99% rain rate), yellow = survives heavy rain (99.9%) but not that storm, red = below the local heavy-rain line. When a link lacks the inputs for a rain estimate (missing distance/channel), the tier falls back to raw signal strength and **never claims rain resilience**: the popover badge, the Signal filter menu and chips, the AP/switch row-dot titles (worst-of-children), and the chart-legend tally all surface those links under a single **"Signal only — no rain estimate"** label instead of a rain word. The dot/color rule splits by surface: on the *category* surfaces (Signal filter menu, chart-legend row) the entry carries a neutral grey dot; on the *per-device* surfaces (popover badge, AP/switch row dots) the dot keeps its severity color and only the wording drops the rain claim. The label self-hides on the category surfaces when no such links exist, so a normal fleet never sees it. Tokens: `--success`, `--warning`, `--danger-bright` with `*-bg` and `*-text` variants.
- **Info-blue (`--info*`) is the fourth semantic color, reserved for "scheduled or in progress"** — never for interactive controls. The accent for controls is teal (`--primary*`); see `UI_STYLE_GUIDE.md`.
- **Color is never the sole signal.** Pair with glyph (`▲`/`▼`), label, or shape.
- **Humans first, numbers as evidence.** Status pills carry English; numbers live in popovers.

## Scheduled queue status

The scheduled queue processes CPEs, APs, then switches, one device at a time.
Show queue progress and the current device status. The configured ramp applies
to the first used windows for a new firmware family or artifact. The persisted
cutoff stops new starts and allows active recovery to finish. A failed check
stops the queue. After resume, Manager checks the failed device first. Do not
show deleted wave selection, saved membership, predictions, or phase controls.
The status bar and drawer show the reason for each paused state. The device
table keeps values on one line, shortens long names, and scrolls horizontally
when needed.


## Don't ask the user for input

- **"There are no power users."** Zero-config bias. Climate auto-detects from IP geolocation. Antenna kit reads from the AP.
- **Allow override, never require it.** Climate override dropdown marks the auto-detected zone ("YOUR AREA" with a location pin).
- **Sensible defaults log once, never nag.** Missing antenna kit → assume AK-150, log it, move on.
- **Shipped settings.** Operators can set the maintenance window and safety defaults. The current backend default for Weather Guard is −4 °C / 25 °F (`min_temperature_c`). Weather data availability and sample time appear in the Updates drawer. Each save changes only the settings the operator edited.

## Target operator contract

**Key points:** The target has no safety-gate override. A failed gate cannot be
approved around. A failed rollout cannot resume in the same maintenance
window. A pre-start block is a hold, not an outage. Only one device write and
recovery sequence can run at a time.

**Detail:** Operators can set timing and pacing within the exposure limits.
They get a Stop action before and during a device sequence. Stop prevents new writes but
does not interrupt a flash. Approval is required only for no rollback, the
first run of a new firmware family, a sole path to other devices, or a policy
exception such as a named Hold exception. Approval covers that plan only. It
cannot bypass a failed gate. A queue shows its risk statement, gate
inputs and results, device results, and operator actions. These are target
requirements. The current override panel and controls above remain shipped
behavior until implementation changes them.

---

**Doc check:** covers page structure (dashboard/tabs/status bar), fleet-scope rules, disclosure progression, color/copy, scheduled queue status, and the shipped settings pattern including the correct Weather Guard default.
