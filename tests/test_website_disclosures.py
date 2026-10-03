"""Exercise shipped website disclosures with cached Alpine, CSS, and fonts.

Use existing SIXTYOPS_UI_PLAYWRIGHT_MODULE, SIXTYOPS_UI_CHROMIUM_EXECUTABLE,
and SIXTYOPS_WEBSITE_ASSET_CACHE (entries.json plus hash-verified blobs).
No assets are downloaded. Optional SIXTYOPS_UI_EVIDENCE_DIR saves screenshots.
"""

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Disclosures(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.buttons = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'button' and 'aria-controls' in attrs:
            self.buttons.append(attrs)


def test_website_disclosure_associations():
    parser = Disclosures()
    parser.feed((ROOT / 'website/index.html').read_text())
    assert len(parser.buttons) == 4
    assert len(parser.ids) == len(set(parser.ids))
    for button in parser.buttons:
        assert button['aria-controls'] in parser.ids
        assert button['aria-expanded'] == 'false'
        assert ':aria-expanded' in button


@pytest.mark.parametrize('width,height', [(390, 844), (1440, 900)])
@pytest.mark.parametrize('reduced', [False, True])
def test_actual_alpine_disclosures(width, height, reduced):
    node = shutil.which('node')
    executable = os.environ.get('SIXTYOPS_UI_CHROMIUM_EXECUTABLE')
    cache = os.environ.get('SIXTYOPS_WEBSITE_ASSET_CACHE')
    if not node or not executable or not cache:
        pytest.skip('Existing Node/Playwright, cached Chromium and website assets are required')
    script = r'''
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const [modulePath,executablePath,root,cache,width,height,reduced,evidence]=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{
 const assets=new Map();
 for(const a of JSON.parse(fs.readFileSync(path.join(cache,'entries.json')))){
  const body=fs.readFileSync(path.join(cache,a.file));
  assert.equal(crypto.createHash('sha256').update(body).digest('hex'),a.sha256);
  assets.set(a.url,{...a,body});
 }
 for(const url of ['https://cdn.tailwindcss.com','https://cdn.tailwindcss.com/'])assets.set(url,assets.get('https://cdn.tailwindcss.com/3.4.17'));
 const browser=await require(modulePath).chromium.launch({executablePath,args:['--disable-background-networking','--host-resolver-rules=MAP * ~NOTFOUND']});
 try{
 const page=await browser.newPage({viewport:{width,height},reducedMotion:reduced?'reduce':'no-preference',serviceWorkers:'block'});
 const blocked=[],errors=[],origin='http://website.invalid',used=new Set();
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',r=>{
  const q=r.request(),a=assets.get(q.url()),u=new URL(q.url());
  if(q.method()==='GET'&&a){used.add(q.url());return r.fulfill({contentType:a.content_type,body:a.body,headers:{'access-control-allow-origin':'*'}});}
  const local={'/index.html':['index.html','text/html'],'/assets/css/custom.css':['assets/css/custom.css','text/css'],'/assets/js/main.js':['assets/js/main.js','application/javascript']}[u.pathname];
  if(q.method()==='GET'&&u.origin===origin&&local)return r.fulfill({contentType:local[1],body:fs.readFileSync(path.join(root,'website',local[0]))});
  blocked.push(q.url());return r.abort();
 });
 await page.goto(origin+'/index.html');
 await page.waitForFunction(()=>window.Alpine&&[...document.querySelectorAll('style')].some(s=>s.textContent.includes('tailwindcss')));
 await page.evaluate(()=>document.fonts.ready);
 await page.evaluate(()=>{const scroll=window.scrollTo.bind(window);window.scrollCalls=[];window.scrollTo=(...args)=>{window.scrollCalls.push(args);return scroll(...args);};});
 async function state(button,open,visible=open){
  await page.waitForFunction(([id,value])=>document.querySelector(`[aria-controls="${id}"]`).getAttribute('aria-expanded')===String(value),[await button.getAttribute('aria-controls'),open]);
  const id=await button.getAttribute('aria-controls'),panel=page.locator('#'+id);
  assert.equal(await panel.count(),1);if(visible)await panel.waitFor({state:'visible'});else await panel.waitFor({state:'hidden'});return panel;
 }
 async function focus(button){await button.focus();await page.keyboard.press('Tab');await page.keyboard.press('Shift+Tab');assert.equal(await button.evaluate(e=>e===document.activeElement),true);
  assert.equal(await button.evaluate(e=>{const s=getComputedStyle(e);return e.matches(':focus-visible')&&s.outlineStyle!=='none'&&parseFloat(s.outlineWidth)>0}),true);}
 const menu=page.getByRole('button',{name:'Menu',exact:true,includeHidden:true}),menuPanel=await state(menu,false,false);
 if(width===390){
  await focus(menu);await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>!!document.activeElement.closest('#mobile-menu-panel')),false);
  await menu.focus();await page.keyboard.press('Enter');await state(menu,true);
  if(evidence){fs.mkdirSync(evidence,{recursive:true});await page.screenshot({path:path.join(evidence,`menu-${width}-${reduced}.png`)});}
  await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')),'#features');
  await page.keyboard.press('Escape');await state(menu,false);assert.equal(await menu.evaluate(e=>e===document.activeElement),true);
  await page.keyboard.press('Space');await state(menu,true);
  await page.keyboard.press('Tab');await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')),'#download');
  await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>!!document.activeElement.closest('#mobile-menu-panel')),false);
  await menu.focus();await page.keyboard.press('Space');await state(menu,false);
  for(const fragment of ['features','download']){
   await menu.focus();await page.keyboard.press('Enter');await state(menu,true);
   await menuPanel.locator(`a[href="#${fragment}"]`).focus();
   const destination=await page.evaluate(id=>document.getElementById(id).getBoundingClientRect().top+scrollY-56,fragment);
   await page.keyboard.press('Enter');await state(menu,false);
   assert.equal(await page.evaluate(()=>window.scrollCalls.at(-1)[0].top),destination);
   await page.waitForFunction(id=>{const r=document.getElementById(id).getBoundingClientRect();return r.top<innerHeight&&r.bottom>56;},fragment);
  }
 }else{
  assert.equal(await menu.isVisible(),false);
  const brand=page.locator('nav a').first();await brand.focus();await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')),'#features');
  await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')),'#download');
  await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')),'#download');
  for(const fragment of ['features','download']){
   const link=page.locator(`nav .md\\:flex a[href="#${fragment}"]`);await link.focus();
   const destination=await page.evaluate(id=>document.getElementById(id).getBoundingClientRect().top+scrollY-56,fragment);
   await page.keyboard.press('Enter');assert.equal(await page.evaluate(()=>window.scrollCalls.at(-1)[0].top),destination);
   await state(menu,false,false);
  }
 }
 const faqs=['Where does SixtyOps run?','What are "dangerous" features?','What does it cost?'];
 for(let i=0;i<faqs.length;i++){
  const button=page.getByRole('button',{name:faqs[i],exact:true}),panel=await state(button,false);
  await focus(button);await page.keyboard.press('Enter');await state(button,true);
  await page.keyboard.press('Space');await state(button,false);
  await page.keyboard.press('Space');await state(button,true);
  if(i<2){const next=page.getByRole('button',{name:faqs[i+1],exact:true});await page.keyboard.press('Tab');assert.equal(await next.evaluate(e=>e===document.activeElement),true);await page.keyboard.press('Enter');await state(button,false);await state(next,true);await page.keyboard.press('Space');await state(next,false);}
  else {assert((await panel.innerText()).includes('off by default'));await page.keyboard.press('Space');await state(button,false);}
 }
 // Closed panels cannot receive keyboard focus, even if a future panel adds a link.
 const panel=page.locator('#faq-location-panel');await panel.evaluate(e=>e.insertAdjacentHTML('beforeend','<a href="#features" id="synthetic-panel-link">Synthetic</a>'));
 const first=page.getByRole('button',{name:faqs[0],exact:true}),second=page.getByRole('button',{name:faqs[1],exact:true});
 await first.focus();await page.keyboard.press('Tab');assert.equal(await second.evaluate(e=>e===document.activeElement),true);
 await first.focus();await page.keyboard.press('Enter');await state(first,true);await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.id),'synthetic-panel-link');
 await first.focus();await page.keyboard.press('Space');await state(first,false);await page.keyboard.press('Tab');assert.equal(await second.evaluate(e=>e===document.activeElement),true);
 await page.locator('#synthetic-panel-link').evaluate(e=>e.remove());
 if(evidence){fs.mkdirSync(evidence,{recursive:true});await first.focus();await page.keyboard.press('Enter');await state(first,true);await page.screenshot({path:path.join(evidence,`disclosures-${width}-${reduced}.png`)});}
 assert.deepEqual(blocked,[]);assert.deepEqual(errors,[]);
 console.log(JSON.stringify({width,height,reduced,alpine:await page.evaluate(()=>Alpine.version),used:used.size,blocked,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps([
        os.environ.get('SIXTYOPS_UI_PLAYWRIGHT_MODULE', 'playwright'), executable,
        str(ROOT), cache, width, height, reduced, os.environ.get('SIXTYOPS_UI_EVIDENCE_DIR'),
    ]), text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr
    print(result.stdout)
