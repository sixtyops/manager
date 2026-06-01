# UI Components

**Patterns for specific UI building blocks in `updater/templates/monitor.html`.** Design decisions and page structure live in `docs/ui-principles.md`.

## Filtering & sorting

- **Excel-style column filters.** Funnel icon on each filterable header → dropdown with sort (asc/desc) + checkbox list + per-value counts + colored dots for categorical types. Plumbed through `columnFilters` + `getDeviceColumnValue` + `handleHeaderClick`.
- **Active filters show as removable chips** below the toolbar. Per-chip × and a Clear all. Never bury filter state inside the dropdown only.
- **Compose, don't replace.** Search + every column filter narrow the same row set. Filters skip empty values so parent rows (APs, switches) pass through CPE-only filters like Signal.
- **Adding a filterable column = one entry in the column metadata.** Don't re-implement filter UI per column.

## Density & responsive

- Tables sit in an `overflow-x:auto` scroll wrapper.
- At `max-width:720px` hide low-priority columns (Version, Update, Last updated, Actions) — every value is still reachable via the hover popover.
- At `max-width:420px` also hide Model.
- The chart card switches from inline popover to bottom-sheet at the same breakpoints.

## Reuse over rebuild

- The popover (`.cpop`), filter dropdown (`.col-filter-dropdown`), and filter chip strip (`#activeFilterChips`) are defined **once** and reused everywhere. New views extend the existing element/CSS, not create parallels.
- New columns thread through `columnFilters` / `getColumnValues` / `matchesDeviceFilters`.
- New per-device data extends the topology payload from `poller.get_topology()` — frontend reads from one place.
- New math goes in `updater/*.py` as pure functions (e.g. `link_budget.py`, `rain_zones.py`). Pure means testable.

## Action affordances

- Multiple action links in one cell use a thin separator: `Notes · Refresh`, never run-on.
- Click targets are scoped — row `onclick` selects; cell hover shows popover; column funnel opens filter. None overlap.
- Cursors signal intent: `pointer` for clickable, `help` for hover-info, default for static.

## Performance & background work

- **`/api/topology` is the dashboard heartbeat — never block it on an external call.** External fetches go through `asyncio.create_task()` (fire-and-forget) with a cached fallback on the request path.
- Hover-driven state changes (Chart.js setActiveElements, plugin redraws) must memoize on "same hovered item" — mousemove fires 60 Hz and 100+ scatter points stop being fluid without it.
- Chart datasets with `clip: false` + small `layout.padding` let edge dots render without clipping axis labels.

---

**Doc check:** covers filtering/sorting, density/responsive, reuse mandate, action affordances, and performance rules. Nothing here duplicates `docs/ui-principles.md`.
