"""Measure shipped Settings CSS and keyboard behavior in offline Chromium.

Set SIXTYOPS_UI_PLAYWRIGHT_MODULE and SIXTYOPS_UI_CHROMIUM_EXECUTABLE to use
an existing Playwright package and cached Chromium. No packages are installed.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / 'updater/templates/monitor.html'


@pytest.mark.parametrize('width,height', [(390, 844), (1440, 900)])
@pytest.mark.parametrize('long_labels', [False, True])
@pytest.mark.parametrize('reduced_motion', [False, True])
def test_settings_tabs_fit_and_remain_reachable(width, height, long_labels, reduced_motion):
    node = shutil.which('node')
    module = os.environ.get('SIXTYOPS_UI_PLAYWRIGHT_MODULE', 'playwright')
    executable = os.environ.get('SIXTYOPS_UI_CHROMIUM_EXECUTABLE')
    if not node or not executable:
        pytest.skip('An existing Node/Playwright package and cached Chromium are required')
    source = TEMPLATE.read_text()
    style = source.split('<style>', 1)[1].split('</style>', 1)[0]
    modal = source.split('    <!-- App Settings Modal -->', 1)[1].split('            <!-- System Panel -->', 1)[0]
    panels = ''.join(f'<div class="settings-panel" id="settingsPanel-{name}">{name}</div>'
                     for name in ('system', 'notifications', 'access', 'radius', 'about'))
    html = '<style>' + style + '</style><button id="settingsMenuTrigger">Menu</button>' + modal + panels + '</div></div>'
    functions = []
    for name in ('resolveSettingsTarget', 'switchSettingsTab', 'handleAppSettingsKeydown', 'closeAppSettingsModal'):
        match = re.search(r'        function ' + name + r'\([^\n]*\) \{\n.*?\n        \}', source, re.S)
        assert match, name
        functions.append(match.group())
    script = r'''
const assert = require('node:assert/strict');
const [modulePath, executablePath, html, functions, width, height, longLabels, reduced] =
    JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
    const browser = await require(modulePath).chromium.launch({executablePath});
    try {
        const page = await browser.newPage({viewport:{width,height}, reducedMotion:reduced?'reduce':'no-preference'});
        const requests=[]; await page.route('**/*', route => {requests.push(route.request().url());return route.abort();});
        await page.setContent(html);
        await page.addScriptTag({content: `let currentUserRole='admin';
            for(const name of ['switchSystemSubtab','switchNotifSubtab','switchAccessSubtab','switchRadiusSubtab','loadAboutPanel']) window[name]=()=>{};
            ${functions}
            document.addEventListener('keydown',handleAppSettingsKeydown);
            document.body.classList.add('role-resolved');
            document.getElementById('appSettingsOverlay').classList.add('open');`});
        if(longLabels) await page.locator('.settings-tab').evaluateAll(tabs => {
            const labels=['System maintenance','Notification delivery','Access and accounts','RADIUS server','About Manager'];
            tabs.forEach((tab,i)=>tab.textContent=labels[i]);
        });
        const geometry = await page.evaluate(() => {
            const dialog = document.getElementById('appSettingsDialog').getBoundingClientRect();
            const tabs=[...document.querySelectorAll('.settings-tab')].map(tab=>{
                const b=tab.getBoundingClientRect(); return {name:tab.dataset.tab,x:b.x,right:b.right,y:b.y,bottom:b.bottom};
            });
            return {dialog:{x:dialog.x,right:dialog.right,bottom:dialog.bottom},tabs,
                scrollWidth:document.documentElement.scrollWidth,viewport:innerWidth};
        });
        assert.equal(geometry.tabs.length,5);
        assert.ok(geometry.tabs.every(b=>b.x>=geometry.dialog.x && b.right<=geometry.dialog.right && b.bottom<=geometry.dialog.bottom), JSON.stringify(geometry));
        assert.ok(geometry.scrollWidth<=geometry.viewport,JSON.stringify(geometry));
        if(width===1440&&!longLabels) assert.equal(new Set(geometry.tabs.map(b=>b.y)).size,1);
        if(width===390) assert.ok(new Set(geometry.tabs.map(b=>b.y)).size>1);
        for(const name of ['system','notifications','access','radius','about']) {
            await page.locator(`.settings-tab[data-tab="${name}"]`).click();
            assert.equal(await page.locator(`#settingsPanel-${name}`).evaluate(el=>el.classList.contains('active')),true);
        }
        await page.locator('#appSettingsClose').focus();
        for(const name of ['system','notifications','access','radius','about']) {
            await page.keyboard.press('Tab');
            assert.equal(await page.evaluate(()=>document.activeElement.dataset.tab),name);
            assert.ok(await page.evaluate(()=>document.activeElement.matches(':focus-visible')));
            const focus=await page.evaluate(()=>{const s=getComputedStyle(document.activeElement);return {width:parseFloat(s.outlineWidth),style:s.outlineStyle,color:s.outlineColor,transition:s.transitionDuration};});
            assert.ok(focus.width>=2&&focus.style!=='none');
            if(reduced) assert.ok(focus.transition.split(',').every(v=>parseFloat(v)<0.001));
            await page.keyboard.press('Enter');
            assert.equal(await page.locator(`#settingsPanel-${name}`).evaluate(el=>el.classList.contains('active')),true);
        }
        await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.id),'appSettingsClose');
        await page.keyboard.press('Shift+Tab');assert.equal(await page.evaluate(()=>document.activeElement.dataset.tab),'about');
        await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.id),'settingsMenuTrigger');
        assert.deepEqual(requests,[]);
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps([
        module, executable, html, '\n'.join(functions), width, height, long_labels, reduced_motion,
    ]), text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
