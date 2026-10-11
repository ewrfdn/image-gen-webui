// Run against server/tests/serve_preview.py, never against production inference.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs');
const path=require('path');
const assert=(v,m)=>{if(!v)throw new Error(m);};
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 const page=await browser.newPage({viewport:{width:1100,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 await page.goto('http://127.0.0.1:18082');
 await page.locator('#frame-token').fill('ui-verification-token-0000000000');
 await page.locator('#frame-login-form button').click();
 await page.locator('#dgx-studio').waitFor({state:'visible'});
 await page.locator('[data-page="backends"]').click();
 if(!(await page.locator('.ig-node').count())) {
 await page.locator('#ig-add-node').click();
 await page.locator('#ig-node-name').fill('UI test backend');
 await page.locator('#ig-node-url').fill('http://fake');
 await page.locator('#ig-node-admin').fill('admin');
 await page.locator('#ig-node-inference').fill('infer');
 await page.locator('#ig-node-form button').first().click();
 }
 await page.getByText('已连接',{exact:true}).waitFor();
 await page.locator('[data-page="generate"]').click();
 await page.waitForFunction(()=>document.querySelector('#ig-model').options.length===2);
 await page.locator('#ig-model').selectOption('test-image|r1');
 await page.locator('#ig-prompt').fill('a cabin');
 await page.locator('#ig-lora').selectOption('style');
 const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNwSGgAAAIEASHL15SiAAAAAElFTkSuQmCC','base64');
 await page.locator('#ig-reference').setInputFiles({name:'ref.png',mimeType:'image/png',buffer:png});
 await page.locator('#ig-submit').click();
 await page.locator('#ig-result img').waitFor({timeout:20000});
 assert(await page.locator('#ig-result a').getAttribute('download')==='0.png','image download available');
 const directory=path.resolve('verification-output/ui');fs.mkdirSync(directory,{recursive:true});
 await page.screenshot({path:path.join(directory,'image-result.png'),fullPage:true});
 // The single-slot backend keeps image resident; video legitimately waits under the 1h release policy.
 await page.locator('#ig-model').selectOption('test-video|r1');
 assert(await page.locator('#ig-lora').inputValue()==='','model switch clears LoRA');
 await page.locator('#ig-prompt').fill('animate a cabin');await page.locator('#ig-submit').click();
 await page.waitForFunction(()=>document.querySelector('#ig-task-list').textContent.includes('test-video'));
 await page.locator('[data-page="keys"]').click();await page.locator('#ig-key-name').fill('UI verification');await page.locator('#ig-key-form button').click();
 await page.locator('#ig-created-key').waitFor({state:'visible'});assert((await page.locator('#ig-key-value').inputValue()).startsWith('sk-frame-'),'real API key created');
 await page.locator('#ig-dismiss-key').click();assert(await page.locator('#ig-key-value').inputValue()==='','one-time key cleared');
 await page.locator('[data-action="revoke"]').click();await page.getByText('已撤销',{exact:true}).waitFor();
 await page.locator('[data-page="settings"]').click();await page.locator('#ig-max').fill('1');await page.locator('#ig-settings-form button').click();await page.getByText('参数已保存。',{exact:true}).waitFor();
 await page.locator('[data-page="generate"]').click();await page.waitForFunction(()=>document.querySelector('#ig-submit').disabled);assert(await page.locator('#ig-submit').isDisabled(),'queue full blocks submit');
 for(const width of [1100,390,320]){
  await page.setViewportSize({width,height:1000});
  for(const name of ['generate','backends','keys','settings']){
   await page.locator('[data-page="'+name+'"]').click();
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'no outer overflow '+width+' '+name);
   if(width!==320)await page.screenshot({path:path.join(directory,name+'-'+width+'.png'),fullPage:true});
  }
 }
 await page.locator('[data-page="keys"]').click();await page.locator('#ig-key-name').fill('Non-admin session');await page.locator('#ig-key-form button').click();await page.locator('#ig-created-key').waitFor({state:'visible'});
 const clientKey=await page.locator('#ig-key-value').inputValue();
 await page.locator('#frame-logout').click();
 assert(await page.locator('#frame-token').inputValue()==='','logout clears entered access token');
 assert(await page.locator('#ig-key-value').inputValue()==='','logout clears one-time key');
 await page.locator('#frame-token').fill(clientKey);await page.locator('#frame-login-form button').click();await page.locator('#dgx-studio').waitFor({state:'visible'});
 assert(!await page.locator('[data-page="keys"]').isVisible(),'API key session has no admin navigation');
 assert(await page.locator('[data-screen="generate"]').isVisible(),'API key login resets to generate');
 assert(await page.locator('#ig-keys tr').count()===0,'previous admin key rows cleared');
 assert(errors.length===0,'runtime errors: '+errors.join(';'));
 fs.writeFileSync(path.join(directory,'report.json'),JSON.stringify({passed:true,checks:['live login','backend configuration','model and LoRA catalogue','reference upload and image task persistence','image preview/download','video queue','key create/revoke','queue limit','responsive layout','logout and API-key session isolation'],errors},null,2));
 await browser.close();console.log('PASS: live UI and real server with controlled backend');
})().catch(e=>{console.error(e);process.exit(1);});
