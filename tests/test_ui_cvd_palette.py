from pathlib import Path
import colorsys
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'updater/templates/monitor.html'
COMMON = ROOT / 'static/common.css'
COLOR = r'var\(--[\w-]+(?:,\s*#[\da-fA-F]+)?\)|#[\da-fA-F]{3,8}\b|\bwhite\b|\bblack\b'
STATE = re.compile(
    r'status|badge|pill|toast|feedback|compliance|signal|sig-margin|saved|'
    r'\.msg-|\.dot|row-status|opt-dot|device-icon|cpop|fleet-chip|'
    r'cell-error|cell-failed|history-error|update-action|update-check|'
    r'\.green|\.yellow|\.red|\.online|\.offline|\.current|\.behind'
)


def _luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    return sum(c * w for c, w in zip(linear, (.2126, .7152, .0722)))


def _contrast(foreground, background):
    a, b = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (a + .05) / (b + .05)


def _resolve(value, tokens):
    if value.startswith('var('):
        value = tokens[re.search(r'--([\w-]+)', value)[1]]
    value = {'white': '#ffffff', 'black': '#000000'}.get(value.lower(), value)
    if len(value) == 4:
        value = '#' + ''.join(c * 2 for c in value[1:])
    assert re.fullmatch(r'#[\da-fA-F]{6}', value), value
    return value


def _green(color):
    hue, saturation, _ = colorsys.rgb_to_hsv(*(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)))
    return 75 <= hue * 360 <= 170 and saturation > .05


def _rules(source):
    css = source.split('<style>', 1)[-1].split('</style>', 1)[0]
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    return [(m[1].strip(), dict(re.findall(r'([\w-]+)\s*:\s*([^;{}]+)', m[2])))
            for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css)]


def _scan(source):
    tokens = dict(re.findall(r'--([\w-]+):\s*(#[\da-fA-F]{3,8})', source))
    errors = []

    def check(label, foreground, background, state=True):
        fg, bg = _resolve(foreground, tokens), _resolve(background, tokens)
        if state and (_green(fg) or _green(bg)):
            errors.append(f'{label}: green state {fg} on {bg}')
        ratio = _contrast(fg, bg)
        if ratio < 4.5:
            errors.append(f'{label}: {fg} on {bg} = {ratio:.2f}:1')

    rules = _rules(source)
    for selector, declarations in rules:
        if not STATE.search(selector):
            continue
        background = declarations.get('background', declarations.get('background-color', ''))
        foreground = declarations.get('color', '')
        for prop, value in declarations.items():
            if prop not in ('color', 'background', 'background-color', 'border', 'border-color', 'border-left-color'):
                continue
            for color in re.findall(COLOR, value):
                if _green(_resolve(color, tokens)):
                    errors.append(f'{selector} {prop}: green state {color}')
        if not foreground and not re.search(r'\.dot$|\.opt-dot|row-status-dot|bar-fill', selector):
            candidates = [(len(base), props['color']) for base, props in rules
                          if base.startswith('.') and ',' not in base and base != selector
                          and (selector.startswith(base) or selector.endswith(' ' + base))
                          and 'color' in props]
            if candidates:
                foreground = max(candidates, key=lambda item: item[0])[1]
        if not foreground or foreground.strip() in ('inherit', 'transparent'):
            continue
        if not re.findall(COLOR, background):
            # Recover the base surface for variant and descendant selectors.
            candidates = [(len(base), props) for base, props in rules
                          if base.startswith('.') and ',' not in base and base != selector
                          and selector.startswith(base) and 'background' in props]
            for _, props in sorted(candidates, key=lambda item: item[0], reverse=True):
                if re.findall(COLOR, props['background']):
                    background = props['background']
                    break
        backgrounds = re.findall(COLOR, background) or ['#ffffff', tokens.get('bg', '#f5f5f5')]
        for fg in re.findall(COLOR, foreground):
            for bg in backgrounds:
                check(selector, fg, bg)

    # Scan all inline text colors, including generated HTML and ternary branches.
    for match in re.finditer(r'\bstyle="([^"]*)"', source):
        style = match[1]
        color_match = re.search(r'(?<![-\w])color\s*:\s*([^;]+)', style)
        if not color_match:
            continue
        expression = color_match[1]
        background_match = re.search(r'background\s*:\s*([^;]+)', style)
        backgrounds = re.findall(COLOR, background_match[1]) if background_match else []
        foregrounds = re.findall(COLOR, expression)
        for i, fg in enumerate(foregrounds):
            bg = backgrounds[min(i, len(backgrounds) - 1)] if backgrounds else '#ffffff'
            check(f'inline {expression}', fg, bg)

    # Both legacy assignments and the shared tone setter must remain covered.
    for match in re.finditer(r'(?:[^\s;=]+\.style\.color\s*=|(?<!function )setStatusTone\([^,]+,)\s*([^;]+);', source):
        foregrounds = re.findall(COLOR, match[1])
        if not foregrounds and match[1].strip().removesuffix(')') not in ("''", '""', "'msg-ok'", "'msg-warn'", "'msg-err'", "'msg-info'"):
            errors.append(f'unresolved status color: {match[0]}')
        background = '#ffffff'
        if '.style.color' in match[0]:
            target = match[0].split('.style.color', 1)[0]
            branch = source[max(0, source.rfind('{', 0, match.start())):match.start()]
            assigned = re.findall(re.escape(target) + r'\.style\.background(?:Color)?\s*=\s*([^;]+);', branch)
            if assigned and (colors := re.findall(COLOR, assigned[-1])):
                background = colors[0]
        for fg in foregrounds:
            check(f'assignment {match[0]}', fg, background)
    for match in re.finditer(r'''(['"])color:\s*([^;'" ]+);\1''', source):
        for fg in re.findall(COLOR, match[2]):
            check(f'generated style {match[0]}', fg, '#ffffff')
    return errors


@pytest.mark.parametrize('path', [TEMPLATE, COMMON], ids=['monitor', 'common'])
def test_status_colors_have_contrast_and_no_green(path):
    assert not (errors := _scan(path.read_text())), '\n'.join(errors)


@pytest.mark.parametrize('path', [TEMPLATE, COMMON], ids=['monitor', 'common'])
def test_cvd_palette_token_pairs_meet_text_contrast(path):
    tokens = dict(re.findall(r'--([\w-]+):\s*(#[\da-fA-F]{6})', path.read_text()))
    pairs = [('state-neutral-text', 'state-neutral-bg'), ('info-text', 'info-bg'),
             ('primary-fill-text', 'primary-fill')]
    if path == TEMPLATE:
        pairs += [('warning-text', 'warning-bg'), ('danger-fill-text', 'danger-fill')]
    for fg, bg in pairs:
        assert _contrast(tokens[fg], tokens[bg]) >= 4.5


def test_old_push_status_is_rejected():
    old = subprocess.run(['git', 'show', '10ee812:updater/templates/monitor.html'],
                         cwd=ROOT, check=True, text=True, capture_output=True).stdout
    errors = _scan(old)
    assert any('assignment el.style.color' in error and '#10b981' in error for error in errors)


def test_history_classes_and_state_shapes():
    source = TEMPLATE.read_text()
    assert not re.search(r'<[^>]*\bclass="[^"]*"[^>]*\bclass=', source)
    assert 'class="u-history-cell msg-ok"' in source
    for selector in ('.history-status-badge.success', '.history-stage-pill.ok', '.user-status-active'):
        assert re.search(re.escape(selector) + r'::before\s*\{\s*content:\s*"✓ ', source)
    assert '${entry.status}</span>' in source
    assert '${s.stage}${dur}</span>' in source
    assert "u.enabled ? 'Active' : 'Disabled'" in source


def test_toggle_outline_and_selected_fill():
    rules = dict(_rules(TEMPLATE.read_text()))
    for selector in ('.toggle', '.toggle-sm'):
        off = rules[selector]
        assert off['background'] == 'var(--toggle-off)'
        assert off['box-shadow'] == 'inset 0 0 0 1.5px #6B7280'
        assert _contrast('#6b7280', '#ffffff') >= 3
        assert rules[selector + '.active']['background'] == 'var(--primary-fill)'
        assert rules[selector + '.active::after']['background'] == '#FFFFFF'


@pytest.mark.parametrize('mutation', [
    "<script>document.getElementById('pushStatus').style.color = '#10b981';</script>",
    "<script>status.style.color = unknownColor;</script>",
    "<script>const renewalClass = 'color:#22c55e;';</script>",
    "<script>statusEl.style.color = '#10b981';</script>",
    '<span style="color:#f59e0b;">Paused</span>',
    '<span style="color:${ok ? \'#16a34a\' : \'#ef4444\'};">Result</span>',
    '<style>.new-status { color:#9ca3af; background:#ffffff; }</style>',
    '<style>.new-status { color:#374151; background:#dcfce7; }</style>',
    '<style>.new-status { color:#374151; background:#333333; }</style>',
])
def test_scan_rejects_new_unsafe_status_colors(mutation):
    assert _scan(mutation)


def test_status_tones_override_component_colors():
    rules = dict(_rules(TEMPLATE.read_text()))
    for tone in ('ok', 'err', 'warn', 'info'):
        assert rules[f'.msg-{tone}']['color'].endswith('!important')
    source = TEMPLATE.read_text()
    helper = source.split('function setStatusTone(', 1)[1].split('function inlineFormat(', 1)[0]
    assert "classList.remove('msg-ok', 'msg-warn', 'msg-err', 'msg-info')" in helper
    assert "element.style.color = '';" in helper


def test_script_status_uses_assigned_background():
    assert not _scan("<script>badge.style.background = '#b91c1c'; badge.style.color = '#ffffff';</script>")
