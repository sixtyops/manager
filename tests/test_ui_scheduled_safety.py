"""Execute the schedule status renderer with safety readiness changes."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from updater.rollout_gate import SCHEDULED_ROLLOUT_SAFETY_REASON

TEMPLATE = Path(__file__).resolve().parents[1] / "updater/templates/monitor.html"


@pytest.mark.parametrize("state", ["disabled", "blocked_safety"])
def test_schedule_safety_reason_is_visible_and_clears_after_readiness(state):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to execute the schedule status renderer")
    html = TEMPLATE.read_text()
    source = re.search(r"        function updateSchedulerUI\(status\) \{\n.*?\n        \}", html, re.S)
    assert source
    assert 'aria-describedby="barBlockReason"' in html
    script = r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
const [source, state, reason] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const toggleStates = [];
const elements = Object.fromEntries(['barAutoToggle', 'barStateBadge', 'barBlockReason',
    'barDeviceCountSection', 'barRolloutSection'].map(id => [id, {
        textContent: '', title: '', innerHTML: '', classList: {add(){}}, hidden: false
    }]));
const context = {
    document: {getElementById: id => elements[id] || null},
    autoUpdateEnabled: false, topology: null, lastFleetTotal: 1, lastBehindCount: 0,
    lastAutoState: 'disabled', setToggleActive() {},
    setHidden(element, hidden) {if (element) element.hidden = hidden;},
    updateBarDeviceCount() {}, renderQueueStatus() {},
    renderScheduledUpdatesToggle(on) {toggleStates.push(on);},
};
vm.createContext(context);
vm.runInContext(source, context);
context.updateSchedulerUI({state, safety_block_reason: reason, block_reason: reason});
assert.equal(elements.barBlockReason.textContent, reason);
assert.equal(elements.barBlockReason.title, reason);
assert.equal(elements.barBlockReason.hidden, false);
assert.ok(elements.barStateBadge.innerHTML.includes(state === 'disabled' ? 'Off' : 'Paused'));
context.updateSchedulerUI({state: 'idle', safety_block_reason: null});
assert.equal(elements.barBlockReason.textContent.includes(reason), false);
assert.equal(elements.barBlockReason.title.includes(reason), false);
assert.deepEqual(toggleStates, [false, true]);
'''
    result = subprocess.run(
        [node, "-e", script], input=json.dumps([source.group(), state, SCHEDULED_ROLLOUT_SAFETY_REASON]),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def _functions(*names):
    html = TEMPLATE.read_text()
    found = []
    for name in names:
        match = re.search(r"        (?:async )?function " + name + r"\([^\n]*\) \{\n.*?\n        \}", html, re.S)
        assert match, name
        found.append(match.group())
    return "\n".join(found)


def _run_node(script, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to execute the confirmation helpers")
    result = subprocess.run([node, "-e", script], input=json.dumps(payload),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_WINDOW_SCRIPT = r'''
const vm = require('node:vm');
const [source, cases] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
const out = cases.map(([settings, zone, iso]) => context.nextWindowText(settings, zone, new Date(iso)));
process.stdout.write(JSON.stringify(out));
'''


@pytest.mark.parametrize("settings,zone,now,expected", [
    # An overnight window is open in its evening and its morning portion.
    ({"schedule_days": "tue", "schedule_start_hour": "20", "schedule_end_hour": "4"}, "UTC",
     "2026-11-10T22:00:00Z", "open now, until 4 AM Wed, Nov 11 (UTC)"),
    ({"schedule_days": "tue", "schedule_start_hour": "20", "schedule_end_hour": "4"}, "UTC",
     "2026-11-11T02:00:00Z", "open now, until 4 AM Wed, Nov 11 (UTC)"),
    ({"schedule_days": "tue", "schedule_start_hour": "20", "schedule_end_hour": "4"}, "UTC",
     "2026-11-10T12:00:00Z", "Tue, Nov 10, 8 PM to 4 AM Wed, Nov 11 (UTC)"),
    # Saturday 23:30 in Chicago, before the spring DST change.
    ({"schedule_days": "sun", "schedule_start_hour": "3", "schedule_end_hour": "4"}, "America/Chicago",
     "2026-03-08T05:30:00Z", "Sun, Mar 8, 3 AM to 4 AM (America/Chicago)"),
    ({"schedule_days": "tue", "schedule_start_hour": "3", "schedule_end_hour": "4"}, "UTC",
     "2026-11-10T05:00:00Z", "Tue, Nov 17, 3 AM to 4 AM (UTC)"),
    ({"schedule_days": "", "schedule_start_hour": "3", "schedule_end_hour": "4"}, "UTC",
     "2026-11-10T05:00:00Z", "none (no maintenance days are set)"),
])
def test_next_window_text_matches_review_cases(settings, zone, now, expected):
    source = _functions("formatHour", "nextWindowText")
    assert _run_node(_WINDOW_SCRIPT, [source, [[settings, zone, now]]]) == [expected]


@pytest.mark.parametrize("zone,first_utc", [
    ("America/Chicago", "2026-03-01T00:00:00Z"),  # spring DST change
    ("America/Chicago", "2026-10-25T00:00:00Z"),  # fall DST change
    ("Pacific/Auckland", "2026-09-20T00:00:00Z"),
])
@pytest.mark.parametrize("days,start,end", [
    ("tue,sun", 3, 4),
    ("tue,sat", 20, 4),
    ("mon,wed,fri", 22, 6),
])
def test_next_window_text_agrees_with_scheduler_every_hour(zone, first_utc, days, start, end):
    """Compare each hour of two weeks with the scheduler's own window check."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from updater.services import is_in_schedule_window

    tz = ZoneInfo(zone)
    day_list = days.split(",")
    first = datetime.fromisoformat(first_utc.replace("Z", "+00:00"))
    hours = [first + timedelta(hours=h) for h in range(24 * 14)]

    def is_open(moment):
        local = moment.astimezone(tz)
        return is_in_schedule_window(local.hour, local.strftime("%a").lower(), day_list, start, end)

    settings = {"schedule_days": days, "schedule_start_hour": str(start), "schedule_end_hour": str(end)}
    texts = _run_node(_WINDOW_SCRIPT, [_functions("formatHour", "nextWindowText"),
                                       [[settings, zone, h.isoformat()] for h in hours]])
    for moment, text in zip(hours, texts):
        assert text.startswith("open now") == is_open(moment), (moment, text)
        if is_open(moment):
            continue
        following = next(m for m in (moment + timedelta(hours=k) for k in range(1, 24 * 9)) if is_open(m))
        local = following.astimezone(tz)
        assert local.hour == start, (moment, text)
        assert text.startswith(f"{local:%a, %b} {local.day}, "), (moment, text)
        assert following.astimezone(timezone.utc) > moment


def test_scope_lists_every_target_and_the_switches_at_ap_sites():
    script = r'''
const vm = require('node:vm');
const [source, cases] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
process.stdout.write(JSON.stringify(cases.map(args => context.scheduleScope(...args))));
'''
    aps = [{"ip": f"10.0.1.{i}", "system_name": f"AP {i}", "tower_site_id": 1 if i < 20 else 2}
           for i in range(1, 26)]
    aps.append({"ip": "10.0.3.1", "system_name": "Other AP", "tower_site_id": 3})
    switches = [
        {"ip": "10.0.2.1", "system_name": "SW north", "tower_site_id": 1, "enabled": 1},
        {"ip": "10.0.2.2", "system_name": None, "tower_site_id": 2, "enabled": 1},
        {"ip": "10.0.2.3", "system_name": "SW off", "tower_site_id": 1, "enabled": 0},
        {"ip": "10.0.2.4", "system_name": "SW other", "tower_site_id": 3, "enabled": 1},
    ]
    sites = [{"id": i, "name": f"Site {i}"} for i in range(1, 31)]
    ap_scope = {"schedule_scope": "aps", "schedule_scope_data": ",".join(ap["ip"] for ap in aps[:25])}
    site_scope = {"schedule_scope": "sites", "schedule_scope_data": ",".join(str(i) for i in range(1, 31)) + ",99"}
    ap_result, site_result = _run_node(script, [_functions("scheduleScope"), [
        [ap_scope, [], aps, switches],
        [site_scope, sites, [], []],
    ]])
    assert ap_result["summary"] == "25 APs and their CPEs, and 2 switches at their sites"
    assert ap_result["groups"] == [
        {"heading": "APs and their CPEs", "items": [f"AP {i} (10.0.1.{i})" for i in range(1, 26)]},
        {"heading": "Switches at these APs' sites", "items": ["SW north (10.0.2.1)", "10.0.2.2"]},
    ]
    assert site_result["summary"] == "31 sites"
    assert site_result["groups"][0]["items"] == [f"Site {i}" for i in range(1, 31)] + ["site 99 (not found)"]


def test_scope_switches_match_the_scheduler(mock_db):
    """The confirmation names the switches that _resolve_switch_scope selects."""
    from updater import database as db
    from updater.scheduler import AutoUpdateScheduler

    north = db.create_tower_site("North")
    south = db.create_tower_site("South")
    db.upsert_access_point("10.0.1.1", "u", "p", tower_site_id=north)
    db.upsert_access_point("10.0.1.2", "u", "p", tower_site_id=south)
    db.upsert_switch("10.0.2.1", "u", "p", tower_site_id=north)
    db.upsert_switch("10.0.2.2", "u", "p", tower_site_id=south)
    db.upsert_switch("10.0.2.3", "u", "p", tower_site_id=north)
    with db.get_db() as conn:
        conn.execute("UPDATE devices SET enabled = 0 WHERE ip = '10.0.2.3'")
    settings = {"schedule_scope": "aps", "schedule_scope_data": "10.0.1.1"}
    expected = AutoUpdateScheduler._resolve_switch_scope(None, settings)

    script = r'''
const vm = require('node:vm');
const [source, args] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
process.stdout.write(JSON.stringify(context.scheduleScope(...args)));
'''
    aps = db.get_access_points(enabled_only=False)
    switches = db.get_switches(enabled_only=False)
    result = _run_node(script, [_functions("scheduleScope"), [settings, [], aps, switches]])
    assert expected == ["10.0.2.1"]
    assert result["groups"][1]["items"] == ["10.0.2.1"]


def test_confirm_dialog_keeps_focus_inside_and_returns_it():
    node = shutil.which("node")
    module = os.environ.get("SIXTYOPS_UI_PLAYWRIGHT_MODULE", "playwright")
    executable = os.environ.get("SIXTYOPS_UI_CHROMIUM_EXECUTABLE")
    if not node or not executable:
        pytest.skip("An existing Node/Playwright package and cached Chromium are required")
    html = TEMPLATE.read_text()
    page_html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html)
    functions = _functions("showConfirmDialog", "handleConfirmKeydown", "closeConfirmDialog")
    script = r'''
const assert = require('node:assert/strict');
const [modulePath, executablePath, html, functions] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
    const browser = await require(modulePath).chromium.launch({executablePath});
    try {
        const page = await browser.newPage({viewport: {width: 390, height: 844}});
        await page.route('**/*', route => route.abort());
        await page.setContent(html);
        await page.addScriptTag({content: 'let _confirmResolve=null,_confirmKeyHandler=null,_confirmReturnFocus=null;' + functions});
        const open = () => page.evaluate(() => {
            document.getElementById('bottomDrawer').classList.add('open');
            const toggle = document.getElementById('drawerScheduledUpdatesToggle');
            window.result = showConfirmDialog('Scope', {groups: [{heading: 'APs', items: ['a', 'b']}], returnFocus: toggle});
        });
        const focused = () => page.evaluate(() => document.activeElement.id);
        await open();
        assert.equal(await focused(), 'confirmCancelBtn');
        const order = [];
        for (let i = 0; i < 4; i++) { await page.keyboard.press('Tab'); order.push(await focused()); }
        assert.deepEqual(order, ['confirmAcceptBtn', 'confirmList', 'confirmCancelBtn', 'confirmAcceptBtn']);
        await page.keyboard.press('Shift+Tab');
        await page.keyboard.press('Shift+Tab');
        assert.equal(await focused(), 'confirmList');
        await page.keyboard.press('Shift+Tab');
        assert.equal(await focused(), 'confirmAcceptBtn');
        await page.evaluate(() => document.body.focus());
        await page.keyboard.press('Tab');
        assert.ok(await page.evaluate(() => document.activeElement.closest('#confirmOverlay') !== null));
        await page.keyboard.press('Escape');
        assert.equal(await page.evaluate(() => window.result), false);
        assert.equal(await focused(), 'drawerScheduledUpdatesToggle');
        assert.equal(await page.evaluate(() => document.getElementById('confirmOverlay').classList.contains('open')), false);
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], input=json.dumps([module, executable, page_html, functions]),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
