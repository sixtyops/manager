"""Measure shipped website link colors with existing cached browser assets.

Configure SIXTYOPS_UI_PLAYWRIGHT_MODULE, SIXTYOPS_UI_CHROMIUM_EXECUTABLE and
SIXTYOPS_WEBSITE_ASSET_CACHE as in test_website_disclosures.py. No downloads.
Optional SIXTYOPS_UI_EVIDENCE_DIR stores measured colors and screenshots.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('surface,count,size,weight', [
    ('index', 3, 14, 500), ('privacy', 2, 16, 400),
    ('terms', 2, 16, 400), ('404', 1, 16, 600),
])
@pytest.mark.parametrize('width,height', [(390, 844), (1440, 900)])
def test_actual_website_control_contrast(surface, count, size, weight, width, height):
    node = shutil.which('node')
    executable = os.environ.get('SIXTYOPS_UI_CHROMIUM_EXECUTABLE')
    cache = os.environ.get('SIXTYOPS_WEBSITE_ASSET_CACHE')
    if not node or not executable or not cache:
        pytest.skip('Existing Node/Playwright, cached Chromium and website assets are required')
    script = r'''
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const [modulePath,executablePath,root,cache,surface,count,size,weight,width,height,evidence]=JSON.parse(fs.readFileSync(0,'utf8'));
const rgb=s=>s.match(/[\d.]+/g).map(Number);
function luminance(s){return rgb(s).slice(0,3).map(v=>{v/=255;return v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4;}).reduce((n,v,i)=>n+v*[0.2126,0.7152,0.0722][i],0);}
function ratio(a,b){const x=luminance(a),y=luminance(b);return(Math.max(x,y)+0.05)/(Math.min(x,y)+0.05);}
(async()=>{
 const assets=new Map();
 for(const a of JSON.parse(fs.readFileSync(path.join(cache,'entries.json')))){
  const body=fs.readFileSync(path.join(cache,a.file));assert.equal(crypto.createHash('sha256').update(body).digest('hex'),a.sha256);assets.set(a.url,{...a,body});
 }
 for(const url of ['https://cdn.tailwindcss.com','https://cdn.tailwindcss.com/'])assets.set(url,assets.get('https://cdn.tailwindcss.com/3.4.17'));
 const browser=await require(modulePath).chromium.launch({executablePath,args:['--disable-background-networking','--host-resolver-rules=MAP * ~NOTFOUND']});
 try{
 const page=await browser.newPage({viewport:{width,height},serviceWorkers:'block'}),origin='http://website.invalid',blocked=[],errors=[],used=new Set();
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',r=>{
  const q=r.request(),a=assets.get(q.url()),u=new URL(q.url());
  if(q.method()==='GET'&&a){used.add(q.url());return r.fulfill({contentType:a.content_type,body:a.body,headers:{'access-control-allow-origin':'*'}});}
  const local={'/index.html':['index.html','text/html'],'/privacy.html':['privacy.html','text/html'],'/terms.html':['terms.html','text/html'],'/404.html':['404.html','text/html'],'/assets/css/custom.css':['assets/css/custom.css','text/css'],'/assets/js/main.js':['assets/js/main.js','application/javascript']}[u.pathname];
  if(q.method()==='GET'&&u.origin===origin&&local)return r.fulfill({contentType:local[1],body:fs.readFileSync(path.join(root,'website',local[0]))});
  blocked.push(q.url());return r.abort();
 });
 await page.goto(origin+'/'+surface+'.html');
 await page.waitForFunction(()=>[...document.querySelectorAll('style')].some(s=>s.textContent.includes('tailwindcss')));
 await page.evaluate(()=>document.fonts.ready);
 assert(await page.evaluate(()=>[...document.fonts].some(f=>f.family==='Inter'&&f.status==='loaded')));
 const controls=page.locator('a[class*="text-brand-"],a[class*="bg-brand-"]');assert.equal(await controls.count(),count);
 const measured=[];
 for(let i=0;i<count;i++){
  const control=controls.nth(i);await control.scrollIntoViewIfNeeded();await page.mouse.move(0,0);
  await control.evaluate(e=>new Promise(resolve=>{const check=()=>{let p=e;while(p&&getComputedStyle(p).opacity==='1')p=p.parentElement;p?requestAnimationFrame(check):resolve();};check();}));
  for(const state of ['normal','hover','focus']){
   if(state==='hover')await control.hover();
   if(state==='focus'){await page.mouse.move(0,0);await control.focus();await page.keyboard.press('Shift+Tab');await page.keyboard.press('Tab');assert(await control.evaluate(e=>e===document.activeElement));}
   await control.evaluate(async e=>{await Promise.all(e.getAnimations().map(a=>a.finished.catch(()=>{})));});
   const m=await control.evaluate(e=>{
    const s=getComputedStyle(e);let p=e,bg;
    while(p){const value=getComputedStyle(p).backgroundColor;if(value!=='rgba(0, 0, 0, 0)'){bg=value;break;}p=p.parentElement;}
    return {text:e.textContent.trim(),href:e.getAttribute('href'),foreground:s.color,background:bg,size:parseFloat(s.fontSize),weight:Number(s.fontWeight),focus:e.matches(':focus-visible'),outline:s.outlineStyle,outlineWidth:parseFloat(s.outlineWidth)};
   });
   assert.equal(rgb(m.foreground).length,3);assert.equal(rgb(m.background).length,3);
   assert.equal(m.size,size);assert.equal(m.weight,weight);m.contrast=ratio(m.foreground,m.background);
   assert(m.contrast>=4.5,JSON.stringify({surface,width,state,...m}));
   if(state==='focus')assert(m.focus&&m.outline!=='none'&&m.outlineWidth>0);
   measured.push({state,...m});
   if(evidence&&i===0){fs.mkdirSync(evidence,{recursive:true});await page.screenshot({path:path.join(evidence,`contrast-${surface}-${width}-${state}.png`)});}
  }
 }
 if(surface==='index'){
  await page.evaluate(()=>{const scroll=window.scrollTo.bind(window);window.lastScroll=null;window.scrollTo=(options)=>{window.lastScroll=options;return scroll(options);};});
  const install=page.getByRole('link',{name:'Install Now',exact:false});await install.focus();
  const destination=await page.evaluate(()=>document.getElementById('download').getBoundingClientRect().top+scrollY-56);
  await page.keyboard.press('Enter');assert.equal(await page.evaluate(()=>window.lastScroll.top),destination);
  await page.waitForFunction(()=>{const b=document.getElementById('download').getBoundingClientRect();return b.top<innerHeight&&b.bottom>56;});
 }else if(surface==='404'){
  await page.getByRole('link',{name:'Back to Home',exact:true}).click();assert.equal(page.url(),origin+'/index.html');await page.waitForFunction(()=>window.Alpine);
  assert.equal(await page.getByRole('heading',{name:'Your Tachyon Network, On Autopilot',exact:true}).count(),1);
 }
 assert.deepEqual(blocked,[]);assert.deepEqual(errors,[]);
 const result={surface,width,height,measured,used:used.size,blocked,errors};
 if(evidence)fs.writeFileSync(path.join(evidence,`contrast-${surface}-${width}.json`),JSON.stringify(result,null,2));
 console.log(JSON.stringify(result));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps([
        os.environ.get('SIXTYOPS_UI_PLAYWRIGHT_MODULE', 'playwright'), executable,
        str(ROOT), cache, surface, count, size, weight, width, height,
        os.environ.get('SIXTYOPS_UI_EVIDENCE_DIR'),
    ]), text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr
    print(result.stdout)
