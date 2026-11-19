import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_settings_save_preserves_scope_and_zero_temperature():
    source = (ROOT / "updater/templates/monitor.html").read_text()
    drawer = source.split("async function saveDrawerSettings()", 1)[1].split("// ── Drawer Firmware", 1)[0]
    assert "schedule_scope: 'all'" not in drawer
    assert "Number.isFinite(parseFloat" in drawer


def test_cooldown_is_writable_and_validated():
    source = (ROOT / "updater/app.py").read_text()
    assert '"firmware_update_cooldown_days"' in source
    assert "firmware_update_cooldown_days must be between 0 and 365" in source


def test_webhook_secret_is_not_loaded_or_sent_when_unchanged():
    source = (ROOT / "updater/templates/monitor.html").read_text()
    assert "document.getElementById('webhookSecret').value = '';" in source
    assert "if (secret) settings.webhook_secret = secret;" in source


def test_settings_saves_check_http_status_and_stop_tests_on_failure():
    source = (ROOT / "updater/templates/monitor.html").read_text()
    assert source.count("if (!resp.ok) throw new Error(await readSaveError(resp));") >= 3
    assert "if (!await saveWebhookSettings()) return;" in source
    assert "return false;" in source


@pytest.mark.parametrize(
    ("handler", "functions"),
    [
        ("testSnmpTrap", ["saveSnmpSettings"]),
        ("saveReleaseChannel", ["setAppUpdateStatusMessage"]),
    ],
)
@pytest.mark.parametrize("save_ok", [False, True])
def test_settings_follow_up_requires_successful_save(handler, functions, save_ok):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to run the settings handlers")
    source = (ROOT / "updater/templates/monitor.html").read_text()
    definitions = []
    for name in [handler, "readSaveError", "setStatusTone", *functions]:
        match = re.search(
            rf"^        (?:async )?function {name}\([^\n]*\) \{{\n.*?^        \}}",
            source,
            re.M | re.S,
        )
        assert match, name
        definitions.append(match.group())

    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const [source, handler, saveOk] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const calls = [];
const toneClasses = new Set(['msg-warn']);
const status = {
    style: {color: '#10b981'},
    classList: {
        remove(...names) { names.forEach(name => toneClasses.delete(name)); },
        add(name) { toneClasses.add(name); },
    },
};
let refreshes = 0;
const context = {
    document: {
        getElementById(id) {
            if (id === 'snmpStatus' || id === 'appUpdateStatus') return status;
            if (id === 'appReleaseChannel') return {value: 'beta'};
            return {checked: true, value: ''};
        },
    },
    async fetch(url, options) {
        calls.push([url, options.method]);
        if (url === '/api/settings') {
            return {ok: saveOk, status: saveOk ? 200 : 400,
                json: async () => ({detail: 'Settings rejected'})};
        }
        return {ok: true, json: async () => ({success: true})};
    },
    setTimeout(callback) { callback(); },
    loadAppUpdateStatus() { refreshes++; },
};
vm.createContext(context);
vm.runInContext(source, context);
(async () => {
    await context[handler]();
    const snmp = handler === 'testSnmpTrap';
    const expectedCalls = [['/api/settings', 'PUT']];
    if (snmp && saveOk) expectedCalls.push(['/api/snmp/test', 'POST']);
    assert.deepEqual(calls, expectedCalls);
    assert.equal(refreshes, !snmp && saveOk ? 1 : 0);
    const successMessage = snmp ? 'Test trap sent!' : 'Channel updated to beta';
    assert.equal(status.textContent, saveOk ? successMessage : 'Settings rejected');
    assert.deepEqual([...toneClasses], [saveOk ? 'msg-ok' : 'msg-err']);
    assert.equal(status.style.color, '');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps(["\n".join(definitions), handler, save_ok]),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
