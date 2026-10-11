// Live account and role acceptance, including a real ordinary-user UI GPU task.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawnSync}=require('child_process');
const fs=require('fs'),path=require('path'),crypto=require('crypto');
(async()=>{
 const credential=spawnSync('ssh',['-o','BatchMode=yes','local-dgx','python3 -c \'from pathlib import Path; print(next(line.split("=",1)[1] for line in (Path.home()/"work/image-gen-platform/.env").read_text().splitlines() if line.startswith("PLATFORM_ADMIN_TOKEN=")))\''],{encoding:'utf8'});
 if(credential.status!==0)throw new Error('Cannot obtain initial login credential');
 const base=process.env.PLATFORM_BASE_URL||'http://127.0.0.1:18080';
 const password=crypto.randomBytes(18).toString('base64url'),suffix=Date.now().toString(36);
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 const accounts=[],checks=[],errors=[];
 const adminContext=await browser.newContext();const superPage=await adminContext.newPage();
 const requireValue=(value,name)=>{if(!value)throw new Error(name);checks.push(name);};
 async function signIn(page,name,secret){page.on('pageerror',e=>errors.push(String(e)));await page.goto(base);await page.locator('#frame-username').fill(name);await page.locator('#frame-password').fill(secret);await page.locator('#frame-login-form button').click();await page.locator('#dgx-studio').waitFor({state:'visible'});}
 try{
  await signIn(superPage,'admin',credential.stdout.trim());
  await superPage.locator('[data-page="users"]').click();
  for(const role of ['user','admin']){
   await superPage.locator('#ig-user-name').fill('verify-'+role+'-'+suffix);await superPage.locator('#ig-user-password').fill(password);await superPage.locator('#ig-user-role').selectOption(role);
   const posted=superPage.waitForResponse(r=>r.url().endsWith('/api/admin/users')&&r.request().method()==='POST');await superPage.locator('#ig-user-form button').click();
   const response=await posted;requireValue(response.status()===201,'super admin creates '+role);accounts.push(await response.json());
  }
  const managerContext=await browser.newContext(),managerPage=await managerContext.newPage();await signIn(managerPage,accounts[1].username,password);
  requireValue(!await managerPage.locator('[data-page="users"]').isVisible(),'admin cannot manage users');
  await managerPage.locator('[data-page="keys"]').click();await managerPage.locator('#ig-key-name').fill('live-account-verification');
  const keyResponse=managerPage.waitForResponse(r=>r.url().endsWith('/api/admin/keys')&&r.request().method()==='POST');await managerPage.locator('#ig-key-form button').click();const key=await (await keyResponse).json();
  const keyHeaders={Authorization:'Bearer '+key.key};requireValue((await managerPage.request.get(base+'/v1/models',{headers:keyHeaders})).status()===200,'admin API Key works');
  const userContext=await browser.newContext(),userPage=await userContext.newPage();await signIn(userPage,accounts[0].username,password);
  for(const name of ['keys','users','backends','settings'])requireValue(!await userPage.locator('[data-page="'+name+'"]').isVisible(),'user hides '+name);
  requireValue((await userPage.request.get(base+'/v1/models')).status()===403,'user cannot call v1');
  requireValue((await userPage.request.post(base+'/api/admin/keys',{data:{name:'denied'}})).status()===403,'user cannot create API Key');
  await userPage.waitForFunction(()=>[...document.querySelector('#ig-model').options].some(o=>o.value.startsWith('noobai-XL-1.1|')));
  const model=await userPage.locator('#ig-model option').evaluateAll(opts=>opts.find(o=>o.value.startsWith('noobai-XL-1.1|')).value);await userPage.locator('#ig-model').selectOption(model);await userPage.locator('#ig-prompt').fill('a small cabin in a snowy forest, detailed illustration');
  const posted=userPage.waitForResponse(r=>r.url().endsWith('/api/tasks')&&r.request().method()==='POST');await userPage.locator('#ig-submit').click();const response=await posted;requireValue(response.status()===202,'user submits UI task');const task=await response.json();
  await userPage.waitForFunction(id=>document.querySelector('#ig-result').dataset.signature===id+'|succeeded',task.id,{timeout:1200000});await userPage.waitForFunction(()=>{const im=document.querySelector('#ig-result img');return im&&im.complete&&im.naturalWidth===1024;});
  const result=await (await userPage.request.get(base+'/api/tasks/'+task.id)).json();requireValue(result.status==='succeeded'&&result.node_id&&result.model_revision,'user GPU task persists');
  const output=await userPage.request.get(base+result.files[0].url);requireValue(output.status()===200,'user downloads own image');
  const other=await (await superPage.request.get(base+'/api/tasks')).json();const foreign=other.tasks.find(t=>t.id!==task.id);requireValue(foreign&&(await userPage.request.get(base+'/api/tasks/'+foreign.id)).status()===404,'user cannot see another owner task');
  const dir=path.resolve('verification-output/accounts-live');fs.mkdirSync(dir,{recursive:true});await userPage.screenshot({path:path.join(dir,'user-result.png'),fullPage:true});fs.writeFileSync(path.join(dir,'generated.png'),await output.body());
  requireValue((await superPage.request.patch(base+'/api/admin/users/'+accounts[1].id,{data:{role:'user'}})).status()===200,'super admin demotes admin');requireValue((await managerPage.request.get(base+'/v1/models',{headers:keyHeaders})).status()===401,'demotion revokes API Key');
  requireValue((await managerPage.request.get(base+'/v1/models')).status()===403,'demotion changes existing session');
  await userPage.locator('#frame-logout').click();await userPage.locator('#frame-login').waitFor({state:'visible'});requireValue(await userPage.locator('#frame-password').inputValue()==='','logout clears password');
  requireValue(errors.length===0,'no browser errors');fs.writeFileSync(path.join(dir,'report.json'),JSON.stringify({passed:true,checks,task:result,errors},null,2));console.log('PASS live accounts, roles and ordinary-user GPU UI');
 }finally{
  for(const account of accounts){const response=await superPage.request.patch(base+'/api/admin/users/'+account.id,{data:{enabled:false}});if(response.status()!==200)throw new Error('Cannot disable verification account');}
  await browser.close();
 }
})().catch(e=>{console.error(e.message);process.exit(1);});
