"""Check that shipped Manager JavaScript parses. Skips when node is missing."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "updater/templates").glob("*.html"))
JS_FILES = sorted((ROOT / "static/js").glob("*.js"))
INLINE_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is not installed; JavaScript syntax checks need `node --check`"
)


def _check(path: Path):
    result = subprocess.run(
        [NODE, "--check", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"{path.name}: {result.stderr.strip()}"


def _scripts(html: str):
    """Yield the body of each inline JavaScript block in `html`."""
    for match in INLINE_SCRIPT.finditer(html):
        attrs = match.group("attrs")
        body = match.group("body").strip()
        if "src=" in attrs or not body:
            continue
        if "type=" in attrs and "javascript" not in attrs and "module" not in attrs:
            continue
        yield body


def _write_and_check(tmp_path, name, bodies):
    for index, body in enumerate(bodies):
        script = tmp_path / f"{name}.{index}.js"
        script.write_text(body, encoding="utf-8")
        _check(script)


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_static_js_parses(path):
    _check(path)


def test_monitor_inline_scripts_parse(authed_client, tmp_path):
    """Check the rendered page, because the template mixes Jinja into its script."""
    bodies = list(_scripts(authed_client.get("/").text))
    assert bodies, "monitor page has no inline script"
    _write_and_check(tmp_path, "monitor", bodies)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_plain_template_scripts_parse(template, tmp_path):
    """Check templates with no Jinja syntax inside their scripts."""
    bodies = [b for b in _scripts(template.read_text(encoding="utf-8")) if "{%" not in b and "{{" not in b]
    _write_and_check(tmp_path, template.name, bodies)


def _monitor_function(name):
    source = (ROOT / 'updater/templates/monitor.html').read_text()
    return re.search(rf'^        (?:async )?function {name}\(.*?^        }}', source, re.M | re.S)[0]


def _run_monitor_js(tmp_path, body):
    script = tmp_path / 'bar-behavior.js'
    script.write_text('''
const assert = require('node:assert/strict');
const elements = new Map();
function element(id) {
    if (!elements.has(id)) {
        const classes = new Set();
        elements.set(id, {textContent: '', innerHTML: '', style: {}, disabled: false,
            value: '3', dataset: {}, focus() { this.focused = true; },
            classList: {contains: c => classes.has(c), add: c => classes.add(c),
                remove: c => classes.delete(c), toggle(c, on) {
                    if (on === undefined) on = !classes.has(c);
                    on ? classes.add(c) : classes.delete(c);
                }}, setAttribute() {}, removeAttribute() {}});
    }
    return elements.get(id);
}
const listeners = {};
const document = {getElementById: element, body: {style: {}},
    querySelectorAll: () => [], addEventListener: (type, fn) => listeners[type] = fn};
const setHidden = (el, hidden) => el.classList.toggle('hidden', hidden);
const syncDrawerPadding = () => {};
const requestAnimationFrame = fn => fn();
const setTimeout = () => 1;
const clearTimeout = () => {};
''' + body)
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_drawer_click_path_and_dirty_escape(tmp_path):
    source = (ROOT / 'updater/templates/monitor.html').read_text()
    start = source.index('        // Collapse drawer when clicking outside it')
    end = source.index('        let drawerSettingsLoaded', start)
    _run_monitor_js(tmp_path, '''let drawerDirty = false;
''' + source[start:end] + '''
const drawer = element('bottomDrawer');
drawer.classList.add('open');
for (const chip of ['Latest', 'version', 'Latest']) {
    listeners.click({target: {}, composedPath: () => [chip, drawer]});
    assert(drawer.classList.contains('open'));
}
drawerDirty = true;
listeners.click({composedPath: () => []});
listeners.keydown({key: 'Escape', composedPath: () => [drawer]});
assert(drawer.classList.contains('open'));
drawerDirty = false;
listeners.keydown({key: 'Escape', composedPath: () => [drawer]});
assert(!drawer.classList.contains('open'));
assert(element('drawerHandle').focused);
drawer.classList.add('open');
listeners.click({composedPath: () => []});
assert(!drawer.classList.contains('open'));
''')


def test_drawer_dirty_state_controls_save_visibility(tmp_path):
    _run_monitor_js(tmp_path, 'let drawerDirty = false;\n' +
        _monitor_function('markDrawerDirty') + _monitor_function('clearDrawerDirty') + '''
clearDrawerDirty();
assert(element('barSaveCluster').classList.contains('hidden'));
markDrawerDirty();
assert(!element('barSaveCluster').classList.contains('hidden'));
assert(!element('drawerSaveBtn').disabled);
clearDrawerDirty();
assert(element('barSaveCluster').classList.contains('hidden'));
assert(element('drawerSaveBtn').disabled);
''')


@pytest.mark.parametrize('state', ['blocked_safety', 'blocked_weather', 'blocked_time', 'blocked_firmware_hold', 'frozen'])
def test_paused_bar_full_reason_and_single_queue_summary(tmp_path, state):
    _run_monitor_js(tmp_path, '''
let lastSchedulerStatus, lastAutoState, lastNextWindow, lastNextWindowIso;
let autoUpdateEnabled = false, topology = null, lastFleetTotal = 10, lastBehindCount = 3;
const tempUnit = 'f', STAGE_LABELS = {};
const setToggleActive = () => {};
const formatWhen = x => x;
''' + _monitor_function('renderQueueStatus') + _monitor_function('updateSchedulerUI') + f'''
const detail = 'Full reason with device and recovery details that must remain visible.';
const status = {{state: '{state}', block_reason: detail, next_window: 'Tuesday 03:00'}};
for (const live of [false, true]) {{
    if (live) status.rollout = {{remaining_devices: 3, pause_reason: detail}};
    updateSchedulerUI(status);
    assert(!element('barNotice').classList.contains('hidden'));
    assert(element('barNotice').textContent.includes(detail));
    assert(element('barNotice').textContent.includes('Manual updates still work.'));
    assert.equal(element('barQueueRemaining').textContent, '3 devices to update');
    assert.equal(element('barQueueNextWindow').textContent, 'Next window: Tuesday 03:00');
    assert.equal(element('barQueueReason').textContent, '');
}}
updateSchedulerUI({{state: 'waiting', next_window: 'Tuesday 03:00'}});
assert(element('barNotice').classList.contains('hidden'));
assert.equal(element('barQueueNextWindow').textContent, 'Next window: Tuesday 03:00');
''')


@pytest.mark.parametrize('succeeds', [False, True])
def test_drawer_save_result_keeps_or_clears_unsaved_changes(tmp_path, succeeds):
    _run_monitor_js(tmp_path, '''
let drawerDirty = true, drawerSettingsLoaded = true;
const drawerFwSelected = {}, drawerBetaEnabled = false;
const storageTemp = x => x;
const readSaveError = async () => 'Rejected';
let saved = false;
const showSavedIndicator = () => { saved = true; };
const loadDrawerFirmware = () => {};
const loadFleetStatus = () => {};
''' + f'const fetch = async () => ({{ok: {str(succeeds).lower()}}});\n' +
        _monitor_function('markDrawerDirty') + _monitor_function('clearDrawerDirty') +
        _monitor_function('saveDrawerSettings') + f'''
(async () => {{
    markDrawerDirty();
    await saveDrawerSettings();
    assert.equal(drawerDirty, {str(not succeeds).lower()});
    assert.equal(element('barSaveCluster').classList.contains('hidden'), {str(succeeds).lower()});
    assert.equal(element('drawerSaveBtn').disabled, {str(succeeds).lower()});
    assert.equal(saved, {str(succeeds).lower()});
    if (!saved) assert.equal(element('drawerSaveStatus').textContent, '✕ Not saved. Try again.');
    assert(!element('drawerDiscardBtn').disabled);
}})().catch(e => {{ console.error(e); process.exitCode = 1; }});
''')
