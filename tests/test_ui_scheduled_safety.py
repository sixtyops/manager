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
    assert 'aria-describedby="barBlockReason"' in html
    script = r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
const [source, state, reason] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
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
};
vm.createContext(context);
vm.runInContext(source, context);
context.updateSchedulerUI({state, block_reason: reason});
assert.equal(elements.barBlockReason.textContent, state === 'disabled' ? '' : reason);
assert.equal(elements.barBlockReason.title, state === 'disabled' ? '' : reason);
assert.equal(elements.barBlockReason.hidden, false);
assert.ok(elements.barStateBadge.innerHTML.includes(state === 'disabled' ? 'Off' : 'Paused'));
context.updateSchedulerUI({state: 'idle'});
assert.equal(elements.barBlockReason.textContent.includes(reason), false);
assert.equal(elements.barBlockReason.title.includes(reason), false);
'''
    result = subprocess.run(
        [node, "-e", script], input=json.dumps([source.group(), state, "Resolve the failed device before a later window"]),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
