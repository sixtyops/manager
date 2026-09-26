"""Check session expiry handling without a server or live devices."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


TEMPLATE = Path(__file__).resolve().parents[1] / "updater/templates/monitor.html"


def test_session_expiry_wiring():
    source = TEMPLATE.read_text()
    assert source.count("function handleSessionExpiry()") == 1
    wrapper = source.split("window.fetch =", 1)[1].split("// Utility:", 1)[0]
    assert "response.status === 401" in wrapper
    assert "handleSessionExpiry();" in wrapper
    close = source.split("ws.onclose =", 1)[1].split("ws.onmessage =", 1)[0]
    assert "event.code === 4001" in close
    assert "handleSessionExpiry();\n                    return;" in close


def test_session_expiry_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to execute the browser handler")
    source = TEMPLATE.read_text()
    handler = source.split("    <script>\n", 1)[1].split("// Utility:", 1)[0]
    close = "ws.onclose =" + source.split("ws.onclose =", 1)[1].split("ws.onmessage =", 1)[0]
    script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const [handler, close] = JSON.parse(fs.readFileSync(0, 'utf8'));
function setup(path = '/') {
    const redirects = [], timers = [], calls = [];
    const context = {
        URL, Request, ws: {},
        window: {
            location: {
                href: `https://manager.test${path}`, origin: 'https://manager.test',
                pathname: path, replace: url => redirects.push(url),
            },
            fetch: async (...args) => { calls.push(args); return context.response; },
        },
        response: {status: 401},
        document: {getElementById: () => ({classList: {add() {}}})},
        localStorage: {getItem: () => null},
        setTimeout: (...args) => timers.push(args),
        connectWebSocket() {},
    };
    vm.createContext(context);
    vm.runInContext(handler + close, context);
    return {context, redirects, timers, calls};
}
(async () => {
    for (const input of ['/api/updates', new URL('https://manager.test/api/settings'),
                         new Request('https://manager.test/api/users/me')]) {
        const {context, redirects, timers, calls} = setup();
        const init = {method: 'GET'};
        assert.equal(await context.window.fetch(input, init), context.response);
        assert.deepEqual(calls[0], [input, init]);
        await context.window.fetch('/api/settings');
        context.ws.onclose({code: 4001});
        assert.deepEqual(redirects, ['/login']);
        assert.equal(timers.length, 0);
    }
    for (const input of ['/login', '/auth/oidc/login', '/api/auth/config',
                         '/api/auth', '/api/login', '/apiary/test',
                         'https://other.test/api/updates']) {
        const {context, redirects} = setup();
        await context.window.fetch(input);
        assert.deepEqual(redirects, []);
    }
    for (const status of [200, 403, 500]) {
        const {context, redirects} = setup();
        context.response.status = status;
        assert.equal(await context.window.fetch('/api/settings'), context.response);
        assert.deepEqual(redirects, []);
    }
    for (const path of ['/login', '/login/', '/auth', '/auth/oidc/callback']) {
        const {context, redirects, timers} = setup(path);
        await context.window.fetch('/api/settings');
        context.ws.onclose({code: 4001});
        assert.deepEqual(redirects, []);
        assert.equal(timers.length, 0);
    }
    const {context, redirects, timers} = setup();
    context.ws.onclose({code: 4001});
    assert.deepEqual(redirects, ['/login']);
    assert.equal(timers.length, 0);
    const normal = setup();
    normal.context.ws.onclose({code: 1006});
    assert.deepEqual(normal.redirects, []);
    assert.equal(normal.timers.length, 1);
    assert.equal(normal.timers[0][1], 2000);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run(
        [node, "-e", script], input=json.dumps([handler, close]),
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
