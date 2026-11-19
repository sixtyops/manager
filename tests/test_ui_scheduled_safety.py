"""Execute the schedule status renderer with Auto-Update and failure stops."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


TEMPLATE = Path(__file__).resolve().parents[1] / "updater/templates/monitor.html"


@pytest.mark.parametrize("state", ["disabled", "blocked_safety"])
def test_schedule_stop_reason_is_visible_and_clears(state):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to execute the schedule status renderer")
    html = TEMPLATE.read_text()
    source = re.search(r"        function updateSchedulerUI\(status\) \{\n.*?\n        \}", html, re.S)
    assert source
    assert 'aria-describedby="barNotice"' in html
    script = r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
const [source, state, reason] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = Object.fromEntries(['barAutoToggle', 'barStateBadge', 'barNotice',
    'barRolloutSection', 'drawerWeatherToggle', 'drawerWeatherStatus',
    'drawerWeatherTemp', 'headerTemp'].map(id => [id, {
        textContent: '', title: '', innerHTML: '', classList: {add(){}, toggle(){}, contains() {return true;}}, hidden: false
    }]));
const context = {
    document: {getElementById: id => elements[id] || null},
    autoUpdateEnabled: false, topology: null, lastFleetTotal: 1, lastBehindCount: 0,
    lastAutoState: 'disabled', setToggleActive() {},
    setHidden(element, hidden) {if (element) element.hidden = hidden;},
    syncDrawerPadding() {}, renderQueueStatus() {},
    displayTemp: value => value, tempUnit: 'c',
};
vm.createContext(context);
vm.runInContext(source, context);
context.updateSchedulerUI({state, block_reason: reason});
assert.equal(elements.barNotice.textContent.includes(reason), state !== 'disabled');
assert.equal(elements.barNotice.hidden, state === 'disabled');
assert.ok(elements.barStateBadge.innerHTML.includes(state === 'disabled' ? 'Off' : 'Paused'));
context.updateSchedulerUI({state: 'idle'});
assert.equal(elements.barNotice.textContent.includes(reason), false);
assert.equal(elements.barNotice.title.includes(reason), false);

const weatherNotice = 'Weather or location data unavailable. Weather Guard did not run.';
context.updateSchedulerUI({state, block_reason: reason, weather_status: weatherNotice});
assert.equal(elements.barNotice.textContent.includes(weatherNotice), state !== 'disabled');
assert.equal(elements.barNotice.textContent.includes(reason), state !== 'disabled');
assert.equal(elements.drawerWeatherStatus.textContent, weatherNotice);

context.updateSchedulerUI({state, weather: {temperature_c: 0, fetched_at: '2026-11-19T12:00:00'}});
assert.match(elements.drawerWeatherTemp.textContent, /^\(sampled .+: 0°C\)$/);
context.updateSchedulerUI({state, weather: {temperature_c: 0}});
assert.equal(elements.drawerWeatherTemp.textContent, '');
assert.equal(elements.drawerWeatherStatus.textContent, '');
assert.equal(elements.headerTemp.textContent, ' · 0°C');
assert.equal(elements.barNotice.textContent.includes(weatherNotice), false);
'''
    result = subprocess.run(
        [node, "-e", script], input=json.dumps([source.group(), state, "Resolve the failed device before a later window"]),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
