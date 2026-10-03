"""Run the shipped About handler with synthetic API responses and DOM state."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / 'updater/templates/monitor.html'
CURRENT = 'You are running the latest version on the selected release channel.'
UNKNOWN = 'Update status is not available right now.'
UNCHECKED = 'Updates have not been checked yet.'
FAILED = 'Last update check failed. The latest version is unknown.'
BASE = dict(current_version='1.4.1-dev5', update_available=False,
            last_check='2026-10-03T10:00:00', last_check_error='', available_version='', release_notes='')


@pytest.mark.parametrize('data,mode,text,label,notes', [
    ({**BASE, 'last_check': ''}, 'ok', UNCHECKED, 'v1.4.1-dev5', None),
    ({**BASE, 'last_check_error': 'Synthetic failure'}, 'ok', FAILED, 'v1.4.1-dev5', None),
    ({**BASE, 'last_check_error': 'Synthetic failure', 'update_available': True,
      'available_version': '1.4.1-dev6', 'release_notes': 'Stale notes'}, 'ok', FAILED, 'v1.4.1-dev5', None),
    (BASE, 'ok', CURRENT, 'v1.4.1-dev5', None),
    ({**BASE, 'update_available': True, 'available_version': '1.4.1-dev6',
      'release_notes': '**Synthetic notes**'}, 'ok', None, 'v1.4.1-dev6', '**Synthetic notes**'),
    ({**BASE, 'update_available': True, 'available_version': '1.4.1-dev6'}, 'ok', 'A newer version is available.', 'v1.4.1-dev6', None),
    ({**BASE, 'update_available': True, 'available_version': '1.4.1-dev6',
      'release_notes': {}}, 'ok', UNKNOWN, '', None),
    (None, 'ok', UNKNOWN, '', None),
    ({}, 'ok', UNKNOWN, '', None),
    ({**BASE, 'update_available': 'false'}, 'ok', UNKNOWN, '', None),
    ({**BASE, 'last_check': 'invalid'}, 'ok', UNKNOWN, '', None),
    ({**BASE, 'available_version': '1.4.1-dev6'}, 'ok', UNKNOWN, '', None),
    ({**BASE, 'available_version': None}, 'ok', UNKNOWN, '', None),
    ({**BASE, 'last_check_error': None}, 'ok', UNKNOWN, '', None),
    ({**BASE, 'current_version': {}}, 'ok', UNKNOWN, '', None),
    ({**BASE, 'update_available': True, 'available_version': ''}, 'ok', UNKNOWN, '', None),
    (BASE, 'http-error', UNKNOWN, '', None),
    (BASE, 'json-error', UNKNOWN, '', None),
    (BASE, 'fetch-error', UNKNOWN, '', None),
])
def test_about_update_status(data, mode, text, label, notes):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed to execute the shipped About handler')
    match = re.search(r'        async function loadAboutPanel\(\) \{\n.*?\n        \}', TEMPLATE.read_text(), re.S)
    assert match
    script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const [source,data,mode,text,label,notes]=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
function element() {return {value:'stale notes', get textContent(){return this.value;},
    set textContent(value){this.value=value;}, get innerHTML(){return this.value;}, set innerHTML(value){this.value=value;}};}
const ids=['aboutInstanceId','aboutVersionText','appVersionDisplay','aboutWhatsNewVersion','aboutWhatsNewBody'];
const elements=Object.fromEntries(ids.map(id=>[id,element()]));elements.appVersionDisplay.textContent='v1.4.1-dev5';
const calls=[],rendered=[];
const context={document:{getElementById:id=>elements[id]},AbortSignal,
    renderReleaseNotes(value){rendered.push(value);return '<strong>Synthetic notes</strong>';},
    fetch:async (url,options)=>{
        calls.push({url,options});
        if(url==='/api/license/instance-id')return {ok:true,json:async()=>({instance_id:'synthetic-instance'})};
        assert.equal(url,'/api/updates');assert.ok(options.signal instanceof AbortSignal);
        if(mode==='fetch-error')throw new Error('Synthetic transport failure');
        return {ok:mode!=='http-error',json:async()=>{if(mode==='json-error')throw new Error('Invalid JSON');return data;}};
    }};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
    await context.loadAboutPanel();
    assert.equal(elements.aboutInstanceId.textContent,'synthetic-instance');
    assert.equal(elements.aboutWhatsNewVersion.textContent,label);
    if(notes){assert.deepEqual(rendered,[notes]);assert.equal(elements.aboutWhatsNewBody.innerHTML,'<div class="release-notes-content"><strong>Synthetic notes</strong></div>');}
    else{assert.deepEqual(rendered,[]);assert.equal(elements.aboutWhatsNewBody.textContent,text);}
    assert.deepEqual(calls.map(c=>c.url),['/api/license/instance-id','/api/updates']);
    // A later refresh must replace prior notes or current-version claims.
    if(mode==='ok'&&data){context.fetch=async url=>url==='/api/updates'?{ok:true,json:async()=>({...data,last_check:'',last_check_error:'',update_available:false})}:{ok:false};
        if(typeof data.current_version==='string') {await context.loadAboutPanel();assert.notEqual(elements.aboutWhatsNewBody.textContent,'You are running the latest version on the selected release channel.');}}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps([
        match.group(), data, mode, text, label, notes,
    ]), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
