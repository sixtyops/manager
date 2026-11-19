"""Check that shipped Manager JavaScript parses. Skips when node is missing."""

import json
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


@pytest.mark.parametrize("close_path", ["handle", "outside", "escape", "updates tab"])
def test_dirty_drawer_confirms_before_close(tmp_path, close_path):
    source = (ROOT / 'updater/templates/monitor.html').read_text()
    start = source.index('        function requestDrawerClose()')
    end = source.index('        let drawerSettingsLoaded', start)
    _run_monitor_js(tmp_path, """
let drawerDirty = false, drawerCloseRequested = false, drawerSettingsLoaded = true;
let drawerDragJustEnded = false, activeColumnMode = 'firmware';
let reloadSucceeds = false;
const loadDrawerSettings = async () => reloadSucceeds;
""" + source[start:end] + _monitor_function('handleTabClick') +
        _monitor_function('markDrawerDirty') + _monitor_function('clearDrawerDirty') +
        _monitor_function('discardDrawerSettings') + f"\nconst path = {json.dumps(close_path)};\n" + """
(async () => {
    const drawer = element('bottomDrawer');
    drawer.classList.add('open');
    listeners.click({composedPath: () => [drawer]});
    assert(drawer.classList.contains('open'));

    markDrawerDirty();
    const close = {
        handle: () => toggleDrawer({target: {closest: () => null}}),
        outside: () => listeners.click({composedPath: () => []}),
        escape: () => listeners.keydown({key: 'Escape', composedPath: () => [drawer]}),
        'updates tab': () => handleTabClick('firmware'),
    };
    close[path]();
    assert(drawer.classList.contains('open'));
    assert(!element('barSaveCluster').classList.contains('hidden'));
    assert.equal(element('drawerSaveStatus').textContent, 'Save or discard your changes before closing.');
    assert(element('drawerSaveBtn').focused);

    await discardDrawerSettings();
    assert(drawer.classList.contains('open'));
    assert(drawerDirty);
    reloadSucceeds = true;
    await discardDrawerSettings();
    assert(!drawer.classList.contains('open'));
    assert(!drawerDirty);
    assert(element('drawerHandle').focused);
})().catch(e => { console.error(e); process.exitCode = 1; });
""")


def test_drawer_dirty_state_controls_save_visibility(tmp_path):
    _run_monitor_js(tmp_path, 'let drawerDirty = false, drawerCloseRequested = false;\n' +
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


@pytest.mark.parametrize(("status", "expected"), [
    ({"state": "blocked_safety"}, ["Scheduled updates are paused.", "Device monitoring continues."]),
    ({"state": "blocked_weather", "block_reason": "Temperature -10 °C is below minimum -4 °C"},
     ["Scheduled updates are paused for weather.", "Temperature -10 °C is below minimum -4 °C",
      "The scheduler retries at the next check."]),
    ({"state": "blocked_weather", "weather_status": "Weather or location data unavailable. Weather Guard did not run."},
     ["Scheduled updates are paused for weather.", "Weather or location data unavailable.", "The scheduler retries at the next check."]),
    ({"state": "waiting", "block_reason": "Deferred: firmware admission cutoff reached or unavailable; device will retry next window"},
     ["Scheduled updates are paused at the window cutoff.", "The next maintenance window starts Tuesday 03:00."]),
    ({"state": "blocked_firmware_hold", "firmware_hold": {"families": {
        "tna-30x": {"held": True, "clears_at": "2026-11-25T00:00:00Z"},
        "tns-100": {"held": False, "clears_at": "2026-11-29T00:00:00Z"},
    }}}, ["Firmware Hold for tna-30x clears on 2026-11-25.", "A confirmed device can clear Firmware Hold early."]),
    ({"state": "frozen"}, ["Scheduled updates are paused during a freeze."]),
    ({"state": "blocked_time"}, ["Scheduled updates are paused because the clock check failed."]),
    ({"state": "blocked_no_firmware"}, ["Select firmware in the Updates drawer."]),
])
def test_paused_bar_full_reason_and_single_queue_summary(tmp_path, status, expected):
    _run_monitor_js(tmp_path, """
let lastSchedulerStatus, lastAutoState, lastNextWindow, lastNextWindowIso;
let autoUpdateEnabled = false, topology = null, lastFleetTotal = 10, lastBehindCount = 3;
const tempUnit = 'f', STAGE_LABELS = {};
const setToggleActive = () => {};
const refreshFirmwareStop = () => {};
const formatWhen = x => x;
""" + _monitor_function('renderQueueStatus') + _monitor_function('updateSchedulerUI') +
        f"\nconst status = {json.dumps(status)}, expected = {json.dumps(expected)};\n" + """
const detail = status.block_reason ||= 'Full reason with device and recovery details that must remain visible.';
status.next_window = 'Tuesday 03:00';
for (const live of [false, true]) {
    if (live) status.rollout = {remaining_devices: 3, pause_reason: detail};
    updateSchedulerUI(status);
    const notice = element('barNotice');
    assert(!notice.classList.contains('hidden'));
    assert(!notice.title);
    for (const text of expected) assert(notice.textContent.includes(text), notice.textContent);
    assert(notice.textContent.includes(detail));
    assert(notice.textContent.includes('Device monitoring continues.'));
    assert(!notice.textContent.includes('2026-11-29'));
    assert.equal(element('barQueueRemaining').textContent, '3 devices to update');
    assert.equal(element('barQueueNextWindow').textContent,
        status.state === 'waiting' ? '' : 'Next window: Tuesday 03:00');
    assert.equal(element('barQueueReason').textContent, '');
}
updateSchedulerUI({state: 'waiting', next_window: 'Tuesday 03:00'});
assert(element('barNotice').classList.contains('hidden'));
assert.equal(element('barQueueNextWindow').textContent, 'Next window: Tuesday 03:00');
""")


def test_unresolved_stop_shows_reason_earliest_day_and_resolution_action(tmp_path):
    _run_monitor_js(tmp_path, """
let firmwareStop = null, firmwareStopsLoading = null;
const lastSchedulerStatus = {state: 'blocked_safety'};
let stops = [{
    id: 42, device_ip: '192.0.2.1', window_key: '["firmware","2026-12-31"]',
    reason: 'The device did not pass its recovery checks.',
}];
element('firmwareStopDialog').showModal = function () { this.open = true; };
element('firmwareStopDialog').close = function () { this.open = false; };
const calls = [];
const fetch = async (url, options) => {
    calls.push(url);
    if (options) {
        assert.equal(options.method, 'POST');
        assert.equal(options.body.get('resolution'), 'Verified device recovery');
        stops = [];
    }
    return {ok: true, json: async () => stops};
};
const FormData = function (form) { this.get = key => form[key]; };
""" + ''.join(_monitor_function(name) for name in (
        'firmwareStopDetail', 'refreshFirmwareStop', 'openFirmwareStopResolution', 'resolveFirmwareStop',
    )) + """
(async () => {
    await refreshFirmwareStop();
    const detail = element('barStopDetail').textContent;
    assert.match(detail, /Firmware updates stopped at 192.0.2.1/);
    assert.match(detail, /The device did not pass its recovery checks/);
    assert.match(detail, /Device monitoring continues/);
    assert.match(detail, /earliest resolution day is 2027-01-01 in the schedule time zone/);
    assert(!element('barStopNotice').classList.contains('hidden'));
    openFirmwareStopResolution();
    const dialog = element('firmwareStopDialog');
    assert(dialog.open);
    assert.equal(element('firmwareStopDialogDetail').textContent, detail);
    await resolveFirmwareStop({
        preventDefault() {},
        currentTarget: {
            closest: () => dialog,
            querySelector: () => element('submit'),
            resolution: 'Verified device recovery',
        },
    });
    assert.deepEqual(calls, [
        '/api/firmware-window-stops', '/api/firmware-window-stops/42/resolve', '/api/firmware-window-stops',
    ]);
    assert(!dialog.open);
    assert(element('barStopNotice').classList.contains('hidden'));
})().catch(e => { console.error(e); process.exitCode = 1; });
""")


@pytest.mark.parametrize('succeeds', [False, True])
@pytest.mark.parametrize('close_requested', [False, True])
def test_drawer_save_result_keeps_or_clears_unsaved_changes(tmp_path, succeeds, close_requested):
    _run_monitor_js(tmp_path, '''
let drawerDirty = true, drawerSettingsLoaded = true;
const activeColumnMode = "firmware";
const drawerFwSelected = {}, drawerBetaEnabled = false;
const storageTemp = x => x;
const readSaveError = async () => 'Rejected';
let saved = false;
const showSavedIndicator = () => { saved = true; };
const loadDrawerFirmware = () => {};
const loadFleetStatus = () => {};
''' + f'let drawerCloseRequested = {json.dumps(close_requested)};\n' +
        f'const fetch = async () => ({{ok: {json.dumps(succeeds)}}});\n' +
        _monitor_function('requestDrawerClose') +
        _monitor_function('markDrawerDirty') + _monitor_function('clearDrawerDirty') +
        _monitor_function('saveDrawerSettings') + f'''
(async () => {{
    const drawer = element('bottomDrawer');
    drawer.classList.add('open');
    markDrawerDirty();
    await saveDrawerSettings();
    assert.equal(drawer.classList.contains('open'), {json.dumps(not (close_requested and succeeds))});
    assert.equal(drawerDirty, {str(not succeeds).lower()});
    assert.equal(element('barSaveCluster').classList.contains('hidden'), {str(succeeds).lower()});
    assert.equal(element('drawerSaveBtn').disabled, {str(succeeds).lower()});
    assert.equal(saved, {str(succeeds).lower()});
    if (!saved) assert.match(element('drawerSaveStatus').textContent, /Try (Save )?again/i);
    assert(!element('drawerDiscardBtn').disabled);
}})().catch(e => {{ console.error(e); process.exitCode = 1; }});
''')
