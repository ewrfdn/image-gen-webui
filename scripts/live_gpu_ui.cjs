// Real browser -> server -> GPU -> stored image acceptance. Only run during GPU window.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawnSync}=require('child_process');
const fs=require('fs');
const path=require('path');
(async()=>{
 const credential=spawnSync('ssh',['-o','BatchMode=yes','local-dgx','python3 -c \'from pathlib import Path; print(next(line.split("=",1)[1] for line in (Path.home()/"work/image-gen-platform/.env").read_text().splitlines() if line.startswith("PLATFORM_ADMIN_TOKEN=")))\''],{encoding:'utf8'});
 if(credential.status!==0)throw new Error('SSH credential read failed');
 const token=credential.stdout.trim();
 const base=process.env.PLATFORM_BASE_URL||'http://127.0.0.1:18080';
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1100,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base);
  await page.locator('#frame-username').fill('admin');await page.locator('#frame-password').fill(token);await page.locator('#frame-login-form button').click();
  await page.locator('#dgx-studio').waitFor({state:'visible'});
  await page.waitForFunction(()=>[...document.querySelector('#ig-model').options].some(o=>o.value.startsWith('noobai-XL-1.1|')));
  const model=await page.locator('#ig-model option').evaluateAll(opts=>opts.find(o=>o.value.startsWith('noobai-XL-1.1|')).value);
  await page.locator('#ig-model').selectOption(model);
  await page.locator('#ig-lora').selectOption('bocchi_illustrious_lora');
  await page.locator('#ig-prompt').fill('a small cabin in a snowy forest, detailed illustration');
  const submitted=page.waitForResponse(r=>r.url().endsWith('/api/tasks')&&r.request().method()==='POST');
  await page.locator('#ig-submit').click();
  const response=await submitted;if(response.status()!==202)throw new Error('UI task rejected');
  const task=await response.json();
  await page.waitForFunction(id=>document.querySelector('#ig-result').dataset.signature===id+'|succeeded',task.id,{timeout:1200000});
  await page.locator('#ig-result img').waitFor({state:'visible',timeout:1200000});
  await page.waitForFunction(()=>{const im=document.querySelector('#ig-result img');return im&&im.complete&&im.naturalWidth===1024;},{},{timeout:30000});
  const result=await page.request.get(base+'/api/tasks/'+task.id,{headers:{Authorization:'Bearer '+token}});
  const detail=await result.json();
  if(detail.status!=='succeeded'||!detail.node_id||!detail.model_revision||!detail.files[0].storage_key)throw new Error('GPU task persistence failed');
  const download=await page.request.get(base+detail.files[0].url,{headers:{Authorization:'Bearer '+token}});
  if(download.status()!==200)throw new Error('Image download failed');
  const dir=path.resolve('verification-output/gpu-ui');fs.mkdirSync(dir,{recursive:true});
  fs.writeFileSync(path.join(dir,'real-ui-result.png'),await download.body());
  await page.screenshot({path:path.join(dir,'result-page.png'),fullPage:true});
  await page.reload();await page.locator('#dgx-studio').waitFor({state:'visible'});
  await page.waitForFunction(id=>document.querySelector('#ig-task-list').textContent.includes(id.slice(0,8)),task.id);
  if(errors.length)throw new Error('Browser runtime errors');
  fs.writeFileSync(path.join(dir,'report.json'),JSON.stringify({passed:true,gpu_generation_submitted:true,task:detail,checks:['real UI model and LoRA selection','real GPU generation','1024px image rendered','authenticated persisted download','history after reload'],errors},null,2));
  const marker=spawnSync('ssh',['-o','BatchMode=yes','local-dgx','touch /home/sakana/work/image-gen-platform/verification-output/gpu-e2e/ui.done'],{encoding:'utf8'});
  if(marker.status!==0)throw new Error('Cannot acknowledge UI completion');
  console.log('PASS real GPU UI, task '+task.id);
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exit(1);});
