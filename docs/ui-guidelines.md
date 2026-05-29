# UI Guidelines

**Bottom line: minimize what the operator sees by default, never duplicate signals, and never block the dashboard on something we could've cached.** Patterns below were extracted from the signal-health work in PR #186; they apply across `monitor.html` and any new view we add.

The repo's frontend is a single server-rendered template (`updater/templates/monitor.html`) with vanilla JS and CSS custom properties on `:root`. There is no framework, no build step. New UI must reuse the existing tokens, helpers, and design language — not introduce a parallel one.

## Information hierarchy

- **One canonical place per signal.** Don't duplicate the same status indicator in two columns. If the Name dot already encodes health, the Status column shouldn't.
- **Show contextual info only when it adds something.** The signal-margin badge (▲/▼ vs target) only renders when an AP-reported target exists. The standby firmware bank only fades in on hover.
- **Aggregate at parent rows.** When a child status implies the parent's status, roll it up (e.g. AP leading dot = worst-CPE health). Don't make the operator dig.

## Disclosure progression: default → hover → click

- **Default view = the minimum needed for quick scanning.** One dot, one number, one icon. No raw dB / mm / % values.
- **Hover = the full story.** A single reusable `.cpop` popover (defined once, repositioned per trigger) shows the breakdown — status pill, gauges with reference ticks, delta arrows. Same element on chart dots, table cells, and any future hover surface.
- **Click = navigation or action**, never disclosure. Row click selects; hover shows detail. These must not compete (scope hover to the relevant cell, not the whole row).
- **Touch parity.** `matchMedia('(hover: none)')` switches popovers to tap-to-toggle; narrow viewports snap them to a bottom sheet via `@media (max-width:720px)` so positioning math never has to handle edge cases.

## Status, color, and copy

- **Three buckets, three colors, three labels** — `green` / `yellow` / `red` mapped to **Reliable / Mostly reliable / Unstable in rain**. Same triplet everywhere (chart dots, table dots, popover, summaries). Tokens: `--success`, `--warning`, `--danger-bright`, with `*-bg` and `*-text` variants for tinted surfaces.
- **Color is never the sole signal.** Pair with glyph (`▲`/`▼`), label ("Reliable"), or shape (rotated-square for offline APs) so color-blind operators read the same story.
- **Humans first, numbers as evidence.** "Update available" tooltip on a `↑` arrow beats "● avail" text. Status pills carry English; numbers live in tooltips/popovers.

## Filtering & sorting

- **Excel-style column filters.** Funnel icon on each filterable header → dropdown with sort (asc/desc) + checkbox list of values + per-value counts + colored dots for categorical types. Plumbed through `columnFilters` + `getDeviceColumnValue` + `handleHeaderClick` in `monitor.html`.
- **Active filters show as removable chips** below the toolbar with per-chip × and a Clear all. Don't bury the filter state in the dropdown only.
- **Compose, don't replace.** Search + every column filter narrow the same row set. Filters skip empty values so parent rows (APs, switches) pass through CPE-only filters like Signal.
- **Adding a filterable column = one entry in the column metadata.** Don't re-implement filter UI per column.

## Density & responsive

- Tables sit in an `overflow-x:auto` scroll wrapper.
- At `max-width:720px` hide low-priority columns (Version, Update, Last updated, Actions) — every value is still reachable via the hover/tap popover, so hiding columns loses zero data.
- At `max-width:420px` also hide Model.
- The chart card switches from inline popover to bottom-sheet on the same breakpoints.

## Don't ask the user for input

- **"There are no power users."** Bias toward zero-config. Climate auto-detects from IP geolocation. Antenna kit reads from the AP. Frontend bucketing happens against the auto-detected zone by default.
- **Allow override, never require it.** A dropdown in the toolbar (`.climate-pill`) lets an operator simulate other regions, but the auto-detected one is clearly marked ("YOUR AREA" pill with a location pin).
- **Sensible defaults log once, never nag.** Missing antenna kit on a 303X/305X → assume AK-150 (middle of range), log it, move on.

## Reuse over rebuild

- The popover (`.cpop`), filter dropdown (`.col-filter-dropdown`), and filter chip strip (`#activeFilterChips`) are each defined **once** and reused. New views must add to the existing element/CSS, not create parallels.
- New columns thread through `columnFilters` / `getColumnValues` / `matchesDeviceFilters`. New per-device data extends the topology payload from `poller.get_topology()` — frontend reads from one place.
- New math modules go in `updater/*.py` as pure functions (e.g. `link_budget.py`, `rain_zones.py`). Pure means testable means rev-able.

## Action affordances

- Multiple action links in one cell use a thin separator: `Notes · Refresh`, never run-on.
- Click targets are scoped — row `onclick` selects the device; cell hover shows the popover; column funnel-icon opens the filter. None overlap.
- Cursors signal intent: `pointer` for clickable, `help` for hover-info, default for static cells.

## Performance & background work

- **`/api/topology` is the dashboard heartbeat — never block it on an external call.** External fetches go through `asyncio.create_task()` (fire-and-forget) and a synchronous cached read on the request path with a safe default fallback.
- Hover-driven state changes (Chart.js setActiveElements, plugin redraws) must memoize on "same hovered item" — mousemove fires 60 Hz and 100+ scatter points stop being fluid without it.
- Chart datasets with `clip: false` + small `layout.padding` let edge dots render without clipping, without polluting axis tick labels.

---

**Doc check:** captures the disclosure progression, color/copy rules, Excel-style filter pattern, zero-config bias, reuse mandate, and the topology-can't-block rule — the patterns that drove every visual and architectural decision in the signal-health work. Within the 1-page guideline (deliberately dense bullets, single read-through). If we add a second large UI surface, split this into `ui-principles.md` + `ui-components.md`.

How it went: written to `docs/ui-guidelines.md`, ~110 lines, follows the docs/ folder's BLUF style. No new code; pure documentation. Ready to commit.
