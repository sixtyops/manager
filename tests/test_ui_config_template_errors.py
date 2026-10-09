"""Run shipped template callers with synthetic list and save responses."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / 'updater/templates/monitor.html'


@pytest.mark.parametrize('handler', ['loadConfigData', 'previewMergeForDevice',
                                     'saveConfigSection', 'saveAllConfigSections'])
@pytest.mark.parametrize('mode', ['valid', 'empty', 'http-error', 'json-error',
                                  'invalid-success', 'fetch-error'])
def test_template_caller_error_contract(handler, mode):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed to execute the shipped template callers')
    source = TEMPLATE.read_text()
    names = [handler, 'extractDetail']
    functions = []
    for name in names:
        match = re.search(r'        (?:async )?function ' + name + r'\([^\n]*\) \{\n.*?\n        \}', source, re.S)
        assert match, name
        functions.append(match.group())
    script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const [source,handler,mode]=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const calls=[],messages=[],alerts=[],renders=[],savedClasses=new Set();
const badge={textContent:'1 enforced'};
const status={textContent:'',classList:{add:x=>savedClasses.add(x),remove:x=>savedClasses.delete(x)}};
const original=[{id:9,category:'snmp',enabled:true}];
const fresh=[{id:10,category:'snmp',enabled:true}];
const context={console:{error(){}},configLoaded:true,configLoadFailed:false,
    configTemplates:original,configSuggestedCategories:new Set(['snmp']),activeColumnMode:'config',
    document:{getElementById:id=>id==='configTemplateBadge'?badge:id==='cfgScopeSelect'?{value:'global'}:id==='cfgStatus-snmp'?status:null},
    updateConfigStats(){},loadConfigTemplateUI(){renders.push('templates')},renderConfigPushRolloutUI(){},
    loadEnforceStatus(){},loadEnforceLog(){},populateScopeSelector(){},loadRecycleBin(){},renderTable(){},
    isSectionPopulated:cat=>cat==='snmp',collectFormData:()=>({enabled:true}),formDataToFragment:()=>({services:{snmp:{enabled:true}}}),
    showToast:m=>messages.push(m),alert:m=>alerts.push(m),showDiffModal(){renders.push('preview')},
    fetch:async(url,options)=>{
        calls.push({url,method:options?.method||'GET'});
        if(options?.method==='PUT'||options?.method==='POST') {
            if(url==='/api/config-push/preview')return {ok:true,json:async()=>({changed:true})};
            return {ok:true,status:200,json:async()=>({success:true})};
        }
        if(url!=='/api/config-templates')return {ok:true,status:200,json:async()=>({})};
        if(mode==='fetch-error')throw new Error('Synthetic fetch failure');
        return {ok:mode!=='http-error',status:mode==='http-error'?409:200,json:async()=>{
            if(mode==='json-error')throw new Error('Synthetic JSON failure');
            if(mode==='http-error')return {detail:{code:'invalid_template_data',message:'Stored template data is unreadable. No templates were returned.'}};
            if(mode==='invalid-success')return {};
            return {templates:mode==='empty'?[]:fresh};
        }};
    }};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
    await context[handler](handler==='previewMergeForDevice'?'192.0.2.1':'snmp');
    const failed=!['valid','empty'].includes(mode);
    const saves=calls.filter(c=>['POST','PUT'].includes(c.method)&&c.url!=='/api/config-push/preview');
    assert.equal(saves.length,handler.startsWith('save')?1:0);
    if(failed){
        assert.equal(context.configTemplates,original);
        assert.ok(!renders.includes('templates'));
        assert.ok(!calls.some(c=>c.url==='/api/config-push/preview'));
        assert.ok(!messages.includes('No config policies enabled.'));
        if(handler==='loadConfigData'){
            assert.equal(context.configLoaded,false);assert.equal(context.configLoadFailed,true);
            assert.equal(badge.textContent,'Unavailable');
        }else{
            assert.ok(messages.length>0);
            if(handler==='saveConfigSection'){
                assert.ok(messages[0].startsWith('Policy saved. Template list unavailable: '));
                assert.ok(savedClasses.has('saved'));
            }
            if(handler.startsWith('save')){
                assert.equal(context.configLoadFailed,true);assert.equal(badge.textContent,'Unavailable');
                assert.equal(alerts.length,0);
            }
        }
    }else if(handler==='previewMergeForDevice'){
        assert.equal(calls.some(c=>c.url==='/api/config-push/preview'),mode==='valid');
        assert.equal(messages.includes('No config policies enabled.'),mode==='empty');
    }else{
        assert.equal(JSON.stringify(context.configTemplates),JSON.stringify(mode==='empty'?[]:fresh));
        assert.ok(renders.includes('templates'));
        if(handler==='loadConfigData'){assert.equal(context.configLoaded,true);assert.equal(context.configLoadFailed,false);}
    }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps([
        '\n'.join(functions), handler, mode,
    ]), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
