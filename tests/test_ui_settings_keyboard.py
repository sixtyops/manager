"""Execute the shipped Settings handlers with synthetic DOM and keyboard events."""

import json
from pathlib import Path
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / 'updater/templates/monitor.html'


class SettingsMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.elements[attrs['id']] = (tag, attrs)


def test_settings_dialog_wiring():
    parser = SettingsMarkup()
    parser.feed(TEMPLATE.read_text())
    elements = parser.elements
    dialog = elements['appSettingsDialog'][1]
    assert dialog['role'] == 'dialog'
    assert dialog['aria-modal'] == 'true'
    assert dialog['aria-labelledby'] in elements
    assert dialog['tabindex'] == '-1'
    close = elements['appSettingsClose'][1]
    assert close['aria-label'] == 'Close App Settings'
    assert close['onclick'] == 'closeAppSettingsModal()'
    assert elements['appSettingsOverlay'][1]['onkeydown'] == 'handleAppSettingsKeydown(event)'
    assert 'event.target===this' in elements['appSettingsOverlay'][1]['onclick']
    assert elements['settingsMenuTrigger'][0] == 'button'


@pytest.mark.parametrize('scenario', ['open', 'wrap', 'excluded', 'empty', 'escape', 'confirm', 'closed', 'close'])
def test_settings_keyboard_behavior(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed to execute the shipped Settings functions')
    source = TEMPLATE.read_text()
    functions = []
    for name in ('openAppSettingsModal', 'handleAppSettingsKeydown', 'closeAppSettingsModal'):
        match = re.search(r'        (?:async )?function ' + name + r'\([^\n]*\) \{\n.*?\n        \}', source, re.S)
        assert match, name
        functions.append(match.group())
    script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const [source, scenario] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const document = {activeElement: null, getElementById: id => elements[id] || null};
function element(id, options = {}) {
    const classes = new Set();
    return {id, visibility: options.visibility, tabIndex: options.tabIndex ?? 0, textContent: 'old status',
        classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c)},
        focus() {document.activeElement = this;},
        matches: () => !!options.disabled,
        closest: () => options.inert ? {} : null,
        getClientRects: () => options.hidden ? [] : [{}]};
}
const elements = Object.fromEntries(['appSettingsOverlay', 'appSettingsDialog', 'appSettingsClose',
    'settingsMenuTrigger', 'userDropdown', 'backupStatus', 'confirmOverlay'].map(id => [id, element(id)]));
const first = elements.appSettingsClose, middle = element('middle'), last = element('last');
let controls = [first, middle, last];
elements.appSettingsDialog.querySelectorAll = () => controls;
const calls = [];
const context = {document, window: {}, getComputedStyle: el => ({visibility: el.visibility || 'visible'}),
    resolveSettingsTarget: (tab, subtab) => ({tab, subtab}),
    switchSettingsTab: (tab, subtab) => calls.push(['tab', tab, subtab]),
    fetch: (url, opts) => {calls.push([url, opts]); return Promise.resolve({});}};
for (const name of ['loadAppUpdateStatus','loadNotificationsData','loadAuthData','loadAboutPanel','loadSslStatus','loadUsers'])
    context[name] = () => calls.push(name);
vm.createContext(context); vm.runInContext(source, context);
function key(key, shiftKey = false) {
    const event = {key, shiftKey, prevented: false, stopped: false,
        preventDefault() {this.prevented = true;}, stopPropagation() {this.stopped = true;}};
    context.handleAppSettingsKeydown(event); return event;
}
(async () => {
    elements.userDropdown.classList.add('open');
    await context.openAppSettingsModal('access', 'users');
    if (scenario === 'open') {
        assert.equal(document.activeElement, first);
        assert.equal(elements.userDropdown.classList.contains('open'), false);
        assert.deepEqual(calls[0], ['tab', 'access', 'users']);
        assert.ok(calls.includes('loadUsers')); assert.ok(calls.includes('loadAboutPanel'));
    } else if (scenario === 'wrap' || scenario === 'excluded') {
        if (scenario === 'excluded') controls = [element('hidden', {hidden:true}),
            element('disabled', {disabled:true}), element('cssHidden', {visibility:'hidden'}),
            element('collapsed', {visibility:'collapse'}), first, middle, last,
            element('negative', {tabIndex:-1}), element('inert', {inert:true})];
        first.focus(); assert.equal(key('Tab', true).prevented, true); assert.equal(document.activeElement, last);
        assert.equal(key('Tab').prevented, true); assert.equal(document.activeElement, first);
        middle.focus(); assert.equal(key('Tab').prevented, false); assert.equal(document.activeElement, middle);
        element('removed').focus(); key('Tab'); assert.equal(document.activeElement, first);
        element('removed').focus(); key('Tab', true); assert.equal(document.activeElement, last);
    } else if (scenario === 'empty') {
        controls = [element('hidden', {hidden:true}), element('disabled', {disabled:true})];
        assert.equal(key('Tab').prevented, true); assert.equal(document.activeElement, elements.appSettingsDialog);
    } else if (scenario === 'escape') {
        const event = key('Escape'); assert.equal(event.prevented, true); assert.equal(event.stopped, true);
        assert.equal(elements.appSettingsOverlay.classList.contains('open'), false);
        assert.equal(document.activeElement, elements.settingsMenuTrigger);
    } else if (scenario === 'confirm') {
        elements.confirmOverlay.classList.add('open'); last.focus();
        const escape = key('Escape'); assert.equal(escape.prevented, false); assert.equal(escape.stopped, false);
        assert.equal(elements.appSettingsOverlay.classList.contains('open'), true);
        assert.equal(key('Tab').prevented, false); assert.equal(document.activeElement, last);
        elements.confirmOverlay.classList.remove('open');
        assert.equal(key('Tab').prevented, true); assert.equal(document.activeElement, first);
    } else if (scenario === 'closed') {
        context.closeAppSettingsModal(); const event = key('Tab');
        assert.equal(event.prevented, false); assert.equal(document.activeElement, elements.settingsMenuTrigger);
        assert.equal(key('Enter').prevented, false);
    } else if (scenario === 'close') {
        context.window._firstRunSetup = true;
        context.closeAppSettingsModal();
        assert.equal(elements.backupStatus.textContent, '');
        assert.equal(context.window._firstRunSetup, false);
        assert.equal(calls.filter(c => c[0] === '/api/setup-wizard/complete').length, 1);
        context.closeAppSettingsModal();
        assert.equal(calls.filter(c => c[0] === '/api/setup-wizard/complete').length, 1);
        assert.equal(document.activeElement, elements.settingsMenuTrigger);
    }
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    subprocess.run([node, '-e', script], input=json.dumps(['\n'.join(functions), scenario]), text=True, check=True, capture_output=True)
