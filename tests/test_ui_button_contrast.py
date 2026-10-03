"""Measure shipped primary-button text colors in offline cached Chromium.

Use SIXTYOPS_UI_PLAYWRIGHT_MODULE and SIXTYOPS_UI_CHROMIUM_EXECUTABLE to
select existing tools. This test never installs tools or submits forms.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('surface', ['monitor', 'login', 'setup'])
@pytest.mark.parametrize('width,height', [(390, 844), (1440, 900)])
def test_primary_button_computed_contrast(surface, width, height):
    node = shutil.which('node')
    executable = os.environ.get('SIXTYOPS_UI_CHROMIUM_EXECUTABLE')
    if not node or not executable:
        pytest.skip('Existing Node/Playwright and cached Chromium are required')
    module = os.environ.get('SIXTYOPS_UI_PLAYWRIGHT_MODULE', 'playwright')
    source = (ROOT / f'updater/templates/{surface}.html').read_text()
    if surface == 'monitor':
        css = source.split('<style>', 1)[1].split('</style>', 1)[0]
        button = re.search(r'<button[^>]*id="addDeviceBtn"[^>]*>.*?</button>', source, re.S)
    else:
        css = (ROOT / 'static/common.css').read_text()
        button = re.search(r'<button[^>]*class="btn-primary"[^>]*>.*?</button>', source, re.S)
    assert button
    html = '<style>' + css + '</style><input id="before">' + button.group() + '<input id="after">'
    script = r'''
const assert=require('node:assert/strict');
const [modulePath,executablePath,html,width,height,surface]=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
function rgb(value){return value.match(/[\d.]+/g).slice(0,3).map(Number);}
function luminance(value){return rgb(value).map(v=>{v/=255;return v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4;}).reduce((n,v,i)=>n+v*[0.2126,0.7152,0.0722][i],0);}
function ratio(a,b){const x=luminance(a),y=luminance(b);return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05);}
(async()=>{
 const browser=await require(modulePath).chromium.launch({executablePath});
 try{
 const page=await browser.newPage({viewport:{width,height}}),requests=[];
 await page.route('**/*',r=>{requests.push(r.request().url());return r.abort();});
 await page.setContent(html);const button=page.locator('button.btn-primary');
 async function measure(){await button.evaluate(async e=>{await Promise.all(e.getAnimations().map(a=>a.finished.catch(()=>{})));});return button.evaluate(e=>{const s=getComputedStyle(e);return {foreground:s.color,background:s.backgroundColor,size:s.fontSize,weight:s.fontWeight,outline:s.outlineStyle,outlineWidth:s.outlineWidth,focus:e.matches(':focus-visible'),opacity:s.opacity,cursor:s.cursor};});}
 const normal=await measure();await button.hover();const hover=await measure();
 await page.mouse.move(0,0);await page.locator('#before').focus();await page.keyboard.press('Tab');
 assert.ok(await button.evaluate(e=>e===document.activeElement));const focus=await measure();
 for(const state of [normal,hover,focus])assert.ok(ratio(state.foreground,state.background)>=4.5,JSON.stringify({surface,width,state,contrast:ratio(state.foreground,state.background)}));
 assert.ok(focus.focus&&focus.outline!=='none'&&parseFloat(focus.outlineWidth)>0);
 await button.evaluate(e=>e.disabled=true);assert.equal(await button.isDisabled(),true);
 await page.locator('#before').focus();await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.id),'after');
 const disabled=await measure();
 if(surface==='monitor'){assert.equal(disabled.cursor,'not-allowed');assert.notEqual(disabled.background,normal.background);}
 // Disabled controls are not judged against the enabled-text contrast threshold.
 assert.deepEqual(requests,[]);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps([
        module, executable, html, width, height, surface,
    ]), text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
