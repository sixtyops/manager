# UI Style Guide — Tachyon Firmware Updater

Design system reference for `updater/templates/monitor.html`.

## Design Direction

Industrial/utilitarian network management tool. Clean, functional, compact.
High information density with clear visual hierarchy. System font stack.

## Color Tokens

### Semantic Colors

| Token | Hex | Usage |
|-------|-----|-------|
| `--primary` | `#3b82f6` | Primary actions, active states, links, focus rings |
| `--primary-hover` | `#2563eb` | Hover state for primary |
| `--primary-bg` | `#dbeafe` | Selection backgrounds, active tab bg |
| `--primary-text` | `#1e40af` | Primary text on light bg |
| `--danger` | `#dc2626` | Destructive actions, errors |
| `--danger-hover` | `#b91c1c` | Hover for danger |
| `--danger-bg` | `#fee2e2` | Error backgrounds |
| `--warning` | `#f59e0b` | Caution states, canary indicators |
| `--warning-bg` | `#fef3c7` | Warning backgrounds |
| `--success` | `#10b981` | Online, healthy, complete |
| `--success-bg` | `#d1fae5` | Success backgrounds |

### Brand vs Primary

- **`--brand` (teal `#0D9488`)**: Brand identity only — day-picker active, config status badges, teal accents
- **`--primary` (blue `#3b82f6`)**: ALL interactive elements — buttons, toggles, tabs, links, focus states

Never use `--brand` for action buttons or interactive controls.

### Neutral Colors

| Token | Usage |
|-------|-------|
| `--bg` | Page background |
| `--bg-card` | Card/panel surfaces |
| `--text` | Primary text |
| `--text-muted` | Secondary text, labels |
| `--text-dim` | Tertiary text, hints |
| `--border` | Standard borders |
| `--border-light` | Subtle dividers, table rows |
| `--input-bg` | Form field backgrounds |
| `--toggle-off` | Inactive toggle, disabled states |

## Typography

System font stack: `-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, sans-serif`

### Size Scale

| Token | Size | Usage |
|-------|------|-------|
| `--text-2xs` | 0.625rem (10px) | Micro labels, pro badges |
| `--text-xs` | 0.6875rem (11px) | Badges, pills, section titles |
| `--text-sm` | 0.75rem (12px) | Table headers, form labels, small controls |
| `--text-base` | 0.8125rem (13px) | Table body, inputs, standard UI text |
| `--text-md` | 0.875rem (14px) | Nav links, dropdown items |
| `--text-lg` | 1rem (16px) | Card titles, modal headings |
| `--text-xl` | 1.125rem (18px) | Page title |

## Spacing

| Token | Size | Usage |
|-------|------|-------|
| `--space-1` | 2px | Tight gaps |
| `--space-2` | 4px | Icon gaps, compact padding |
| `--space-3` | 6px | Small element padding |
| `--space-4` | 8px | Standard gap, button padding |
| `--space-5` | 12px | Section gaps, table cell padding |
| `--space-6` | 16px | Card padding, group gaps |
| `--space-7` | 20px | Large section padding |
| `--space-8` | 24px | Panel padding |

## Buttons

### Hierarchy

| Class | Appearance | Usage |
|-------|-----------|-------|
| `.btn` | White bg, gray border | Default/neutral — Cancel, Close, secondary actions |
| `.btn-primary` | Blue bg, white text | Primary actions — Save, Submit, Confirm |
| `.btn-danger` | Red bg, white text | Destructive — Delete, Remove, Start Update |
| `.btn-success` | Green bg, white text | Positive — Create, Enable |
| `.btn-secondary` | Gray bg, muted text | Alternative actions |
| `.btn-ghost` | Transparent, no border | De-emphasized — Cancel in dialogs, Clear |
| `.btn-warning` | Amber bg, dark text | Caution actions — Set Canary |
| `.btn-outline-danger` | Red border, red text | Soft danger — Delete with confirmation pending |

### Sizes

| Class | Padding |
|-------|---------|
| `.btn` (default) | 8px 16px |
| `.btn-sm` | 4px 12px |

### Rules

- Base `.btn` is always neutral (never colored by default)
- Every button with color intent must have an explicit variant class
- In config sections, use `.btn-primary` explicitly — do not rely on parent overrides

## Toggles

Two sizes only:

| Class | Size | Usage |
|-------|------|-------|
| `.toggle` | 36x20px | Drawer handle, settings page |
| `.toggle-sm` | 28x16px | Compact option rows in drawer |

Active state: `var(--primary)` (blue). Never teal.

## Modals / Confirm Dialogs

### Button Order

Always: **Cancel (left)** — **Action (right)**

### Button Styling

- Cancel: `.btn .btn-sm .btn-ghost` (transparent, de-emphasized)
- Non-destructive action: `.btn .btn-sm .btn-primary` (blue)
- Destructive action: `.btn .btn-sm .btn-danger` (red)

### Pattern

```javascript
showConfirmDialog('Message', {
    title: 'Dialog Title',
    confirmText: 'Action Label',
    danger: true,  // true = red btn-danger, false = blue btn-primary
});
```

## Tree Table Indentation

| Level | Class | Padding | Elements |
|-------|-------|---------|----------|
| Site | `.tree-indent-0` | 4px | Checkbox, expand toggle, site icon |
| AP/Switch | `.tree-indent-1` | 28px | Checkbox, expand toggle, device icon |
| CPE | `.tree-indent-2` | 52px | Checkbox, placeholder toggle, health dot |

Checkboxes live inside `.name-cell` div, before the expand toggle.

## Responsive Breakpoints

| Width | Behavior |
|-------|----------|
| > 1024px | Full layout, drawer grid side-by-side |
| 900px | Top section stacks to single column |
| 1024px | Drawer settings grid stacks vertically |
| 768px | Status bar wraps, table toolbar full-width |
