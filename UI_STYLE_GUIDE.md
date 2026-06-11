# UI Style Guide — Tachyon Firmware Updater

**One teal accent for everything interactive, four semantic state colors, one
type scale, one radius scale, one motion scale. If a new element needs a color
or size that isn't a token, question the design before adding a value.**

Design-system reference for `updater/templates/monitor.html` (login/setup pages
mirror a small token subset in `static/common.css`). The app is an
industrial/utilitarian network tool: clean, compact, high information density.
Every color, size, radius, and duration flows through the `:root` tokens at the
top of `monitor.html` — never hardcode a hex or pixel value a token covers.

## Color Tokens

### Accent vs state

- **`--primary` (teal `#0D9488`)**: ALL interactive elements — buttons, toggles,
  active tabs, links, focus rings, selection tints. The only accent.
- **`--info` (blue `#3b82f6`)**: the fourth semantic state color — *scheduled or
  in progress*. Status pills, device-update spinners, the active phase in the
  rollout stepper, info toasts and banners.
- Never color a control blue; never use teal to express state. An operator must
  be able to tell "this is a control" from "this is happening" by hue alone.

### Semantic colors

| Family | Base | Usage |
|--------|------|-------|
| `--primary*` | `#0D9488` | Interactive controls (`-hover`, `-text`, `-bg`, `-bg-hover`, `-bg-subtle`, `--focus-ring`) |
| `--info*` | `#3b82f6` | Scheduled / running / informational state (`-strong`, `-text`, `-bg`, `-bg-subtle`, `-border`) |
| `--success*` | `#10b981` | Online, healthy, complete (`-hover`, `-light`, `-text`, `-bg`) |
| `--warning*` | `#f59e0b` | Caution, canary, paused (`-hover`, `-light`, `-text`, `-bg`) |
| `--danger*` | `#dc2626` | Destructive, errors, offline (`-hover`, `-bright`, `-text`, `-bg`) |

### Neutrals

`--bg`, `--bg-card`, `--bg-subtle`, `--text`, `--text-muted`, `--text-dim`,
`--text-subtle`, `--border`, `--border-light`, `--input-bg`, `--toggle-off`,
`--overlay` (the one modal scrim), `--shadow-pop`.

## Typography

IBM Plex Sans 400/500/600 and IBM Plex Mono 400/500, vendored at
`static/vendor/fonts/` (SIL OFL 1.1) and declared in `static/fonts.css` with
the system stack as fallback. Use `var(--font-sans)` / `var(--font-mono)`.

| Token | Size | Usage |
|-------|------|-------|
| `--text-2xs` | 0.625rem (10px) | Micro labels, phase detail |
| `--text-xs` | 0.6875rem (11px) | Badges, pills, secondary table headers |
| `--text-sm` | 0.75rem (12px) | Table headers, form labels, small controls |
| `--text-base` | 0.8125rem (13px) | Table body, inputs, standard UI text |
| `--text-md` | 0.875rem (14px) | Nav links, dropdown items, emphasis text |
| `--text-lg` | 1rem (16px) | Card titles, modal headings |
| `--text-xl` | 1.125rem (18px) | Page title |

Data surfaces (tables, stat values, version cells, counts, the clock) carry
`font-variant-numeric: tabular-nums` so numbers don't wiggle as they update.
No em-dashes in rendered copy — rewrite with a period, colon, or `·`.

## Spacing

`--space-1` 2px · `--space-2` 4px · `--space-3` 6px · `--space-4` 8px ·
`--space-5` 12px · `--space-6` 16px · `--space-7` 20px · `--space-8` 24px

## Radius

| Token | Size | Usage |
|-------|------|-------|
| `--radius-sm` | 4px | Chips, tiny boxes, progress tracks |
| `--radius-md` | 6px | Buttons, inputs, dropdown items |
| `--radius-lg` | 8px | Cards, menus, banners |
| `--radius-xl` | 12px | Modals, large cards |
| `--radius-full` | 999px | Pills, badges, toggle capsules |

`50%` only for true circles (dots, spinners, knobs).

## Motion, focus, press

- Durations: `--tr-fast` 0.15s (hover color/background), `--tr-base` 0.2s,
  `--tr-slow` 0.3s (panel slides). `prefers-reduced-motion` disables all.
- Keyboard focus: global `:focus-visible` ring (`--focus-ring`), 2px, offset 1px.
- Press feedback: buttons and chips translate down 1px on `:active`.

## Buttons

| Class | Appearance | Usage |
|-------|-----------|-------|
| `.btn` | White bg, gray border | Default/neutral — Cancel, Close |
| `.btn-primary` | Teal bg, white text | Primary actions — Save, Submit, Confirm |
| `.btn-danger` | Red bg, white text | Destructive — Delete, Remove |
| `.btn-success` | Green bg, white text | Positive — Create, Enable |
| `.btn-secondary` | Gray bg, muted text | Alternative actions |
| `.btn-ghost` | Transparent, no border | De-emphasized — Cancel in dialogs |
| `.btn-warning` | Amber bg, **dark amber text** | Caution — Set Canary. The one dark-text solid: amber can't carry white at AA contrast |
| `.btn-outline-danger` | Red border, red text | Soft danger |

Sizes: `.btn` 8px 16px, `.btn-sm` 4px 12px. Base `.btn` is always neutral;
color intent requires an explicit variant class.

## Toggles

Two sizes only: `.toggle` (36×20, drawer handle/settings) and `.toggle-sm`
(28×16, compact rows). Active state: `var(--primary)`.

## Modals / Confirm Dialogs

Widths come in three tiers: `--modal-sm` 440px (confirm, backup),
`--modal-md` 640px (settings), `--modal-lg` 960px (history). Scrim is
`var(--overlay)`. Button order: **Cancel (left)** — **Action (right)**;
Cancel is `.btn-ghost`, the action is `.btn-primary` or `.btn-danger`
(`showConfirmDialog(msg, {danger})` picks for you).

## Tree Table Indentation

| Level | Class | Padding |
|-------|-------|---------|
| Site | `.tree-indent-0` | 4px |
| AP/Switch | `.tree-indent-1` | 28px |
| CPE | `.tree-indent-2` | 52px |

## Responsive Breakpoints

| Width | Behavior |
|-------|----------|
| > 1024px | Full layout, drawer grid side-by-side |
| 900px | Top section stacks to single column |
| 1024px | Drawer settings grid stacks vertically |
| 768px | Status bar wraps, table toolbar full-width |
