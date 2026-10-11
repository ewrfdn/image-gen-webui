const root = document.getElementById('dgx-studio');
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusName = {available:'可生成',need_load:'需要加载',busy:'等待节点',unavailable:'暂不可用'};
const taskName = {pending:'等待调度',processing:'准备生成',loading:'加载模型',submitted:'正在生成',running:'正在生成',persisting:'保存结果',succeeded:'已完成',failed:'失败',unknown:'结果未知'};
let token = '';
let superAdmin = false, users = [];
let admin = false, models = [], nodes = [], keys = [], files = [], page = 'generate', modelKey = '', latestTask = null;
let queueCount = 0, maxCount = 100, editNode = null, referenceUrls = [], resultUrls = [], refreshRunning = false;
let settings = {max_count:100,refresh_seconds:5,stale_seconds:15};
let saving = false;
let resultVersion = 0;

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {...options, headers});
  if (!response.ok) {
    let message = '请求失败：'+response.status;
    try { const body = await response.json(); message = body.error?.message || message; } catch (_) {}
    if (response.status === 401) logout();
    throw new Error(message);
  }
  return response;
}
async function json(path, options) { return (await api(path,options)).json(); }
function message(id,text) { $(id).textContent=text; $(id).hidden=!text; }
function selected() { return models.find(m => m.model_id+'|'+m.revision===modelKey); }

function logout() {
  token=''; admin=false; superAdmin=false; users=[]; models=[]; nodes=[]; keys=[]; latestTask=null; modelKey=''; resultVersion++;
  sessionStorage.removeItem('frame-token'); root.hidden=true; $('frame-login').hidden=false;
  $('ig-key-value').value=''; $('ig-created-key').hidden=true;
  $('ig-node-admin').value=''; $('ig-node-inference').value='';
  $('frame-password').value=''; $('ig-user-password').value=''; $('ig-users').innerHTML='';
  $('ig-password-form').reset(); $('frame-identity').textContent='';
  $('ig-task-list').innerHTML=''; $('ig-result').innerHTML=''; delete $('ig-result').dataset.signature;
  clearReferences(); renderKeys(); renderNodes(); navigate('generate');
  resultUrls.forEach(URL.revokeObjectURL); resultUrls=[];
}
async function login() {
  try {
    const identity=await json('/api/session'); token=identity.user_id+'|'+Math.random(); permissions(identity);
    $('frame-password').value=''; $('frame-login').hidden=true; root.hidden=false;
    await refresh();
  } catch (error) { message('frame-login-error',error.message); }
}
function permissions(identity){admin=identity.admin;superAdmin=identity.role==='super_admin';$('frame-identity').textContent=identity.username+' · '+({user:'用户',admin:'管理员',super_admin:'超级管理员'}[identity.role]);root.querySelectorAll('[data-page]').forEach(button=>button.hidden=button.dataset.page==='users'?!superAdmin:!['generate','account'].includes(button.dataset.page)&&!admin);if((page==='users'&&!superAdmin)||(!['generate','account'].includes(page)&&!admin))navigate('generate');}
$('frame-login-form').addEventListener('submit',async e=>{e.preventDefault();try{await json('/api/auth/login',{method:'POST',body:JSON.stringify({username:$('frame-username').value,password:$('frame-password').value})});await login();}catch(error){message('frame-login-error',error.message);}});
$('frame-logout').onclick=async()=>{const pending=json('/api/auth/logout',{method:'POST'});logout();const button=$('frame-login-form').querySelector('button');button.disabled=true;try{await pending;}catch(error){message('frame-login-error',error.message);}finally{button.disabled=false;}};

function renderModels() {
  const previousLora=$('ig-lora').value;
  const old=modelKey;
  if (!models.some(m=>m.model_id+'|'+m.revision===modelKey)) {
    modelKey=(models.find(m=>m.status!=='unavailable')||models[0]) ? (()=>{const m=models.find(m=>m.status!=='unavailable')||models[0];return m.model_id+'|'+m.revision;})() : '';
  }
  $('ig-model').innerHTML=models.length?models.map(m=>`<option value="${escape(m.model_id+'|'+m.revision)}" ${m.status==='unavailable'?'disabled':''}>${escape(m.configuration.display_name)} · ${m.configuration.type==='video'?'视频':'图片'} · ${statusName[m.status]}${models.filter(x=>x.model_id===m.model_id).length>1?' · '+escape(m.revision.slice(0,8)):''}</option>`).join(''):'<option value="">暂无可用模型，请接入后端</option>';
  $('ig-model').value=modelKey;
  const model=selected();
  $('ig-lora').innerHTML='<option value="">不使用 LoRA</option>'+(model?.loras||[]).map(l=>`<option value="${escape(l.lora_id)}">${escape(l.display_name||l.lora_id)}</option>`).join('');
  if(old===modelKey&&model?.loras.some(l=>l.lora_id===previousLora)) $('ig-lora').value=previousLora;
  const ref=model?.configuration.reference;
  $('ig-reference-field').hidden=!ref?.supported&&files.length===0;
  $('ig-reference').disabled=!ref?.supported;
  $('ig-reference').accept=ref?.mime_types.join(',')||'';
  $('ig-ref-limit').textContent=ref?.supported?`${ref.mime_types.map(t=>t.split('/')[1].toUpperCase()).join(' / ')} · 最多 ${ref.max_count} 个 · 单个 ${Math.round(ref.max_file_bytes/1024/1024)} MB`:'此模型不支持参考文件，请移除已有文件。';
  $('ig-model-note').textContent=model?statusName[model.status]+' · '+(ref.supported?'支持参考文件':'文生图'):'模型目录会自动刷新';
  $('ig-output-type').textContent=model?.configuration.type==='video'?'视频':'图片';
  admission();
}
function admission() {
  const model=selected(); const ref=model?.configuration.reference;
  let error='';
  if(!model||model.status==='unavailable')error='暂无可用模型。';
  else if(queueCount>=maxCount)error='等待队列已满，请稍后再试。';
  else if(files.length>(ref?.max_count||0)||files.length<(ref?.min_count||0))error='参考文件数量不符合此模型要求。';
  else if(files.some(f=>!ref.mime_types.includes(f.type)||f.size>ref.max_file_bytes))error='参考文件类型或大小不符合此模型要求。';
  message('ig-submit-error',error);
  $('ig-submit').disabled=!!error||saving;
  $('ig-submit').textContent=saving?'正在提交…':queueCount>=maxCount?'队列已满':model?.configuration.type==='video'?'生成视频':'生成图片';
  $('ig-queue').textContent=`等待 ${queueCount} / ${maxCount}`;
}
$('ig-model').onchange=()=>{modelKey=$('ig-model').value;$('ig-lora').value='';renderModels();};
function clearReferences(){files=[];$('ig-reference').value='';referenceUrls.forEach(URL.revokeObjectURL);referenceUrls=[];$('ig-reference-list').innerHTML='';renderModels();}
$('ig-reference').onchange=()=>{
  referenceUrls.forEach(URL.revokeObjectURL);referenceUrls=[];files=Array.from($('ig-reference').files);
  $('ig-reference-list').innerHTML='';
  for(const file of files){const item=document.createElement('span');item.textContent=file.name;$('ig-reference-list').append(item);if(file.type.startsWith('image/')){const img=document.createElement('img');const url=URL.createObjectURL(file);referenceUrls.push(url);img.src=url;img.alt=file.name;$('ig-reference-list').append(img);}}
  if(files.length){const button=document.createElement('button');button.className='ig-button';button.type='button';button.textContent='移除参考文件';button.onclick=clearReferences;$('ig-reference-list').append(button);}
  admission();
};
$('ig-generate-form').addEventListener('submit',async e=>{
  e.preventDefault();admission();if($('ig-submit').disabled)return;
  const model=selected(); const prompt=$('ig-prompt').value.trim();if(!prompt)return message('ig-submit-error','请输入提示词。');
  saving=true;admission();let submissionError='';
  try{
    const references=[];
    for(const file of files){const form=new FormData();form.append('file',file);references.push((await json('/api/uploads',{method:'POST',body:form})).id);}
    const lora=$('ig-lora').value;
    const task=await json('/api/tasks',{method:'POST',body:JSON.stringify({model:model.model_id,revision:model.revision,prompt,references,loras:lora?[{lora_id:lora,scale:1}]:[]})});
    latestTask=task.id;clearReferences();await refresh();
  }catch(error){submissionError=error.message;}finally{saving=false;admission();if(submissionError)message('ig-submit-error',submissionError);}
});
async function showResult(task){
  if($('ig-result').dataset.signature===task.id+'|'+task.status)return;
  const version=++resultVersion;
  $('ig-result').dataset.signature=task.id+'|'+task.status;
  resultUrls.forEach(URL.revokeObjectURL);resultUrls=[];
  $('ig-result').innerHTML=`<strong>${escape(taskName[task.status])}</strong><span class="ig-caption">${escape(task.id)}</span>`;
  if(task.wait_reason){const span=document.createElement('span');span.textContent=statusName[task.wait_reason]||task.wait_reason;$('ig-result').append(span);}
  if(task.error){const span=document.createElement('span');span.className='ig-error';span.textContent=task.error;$('ig-result').append(span);}
  for(const file of task.files){
    const blob=await (await api(file.url)).blob();if(version!==resultVersion)return;const url=URL.createObjectURL(blob);resultUrls.push(url);
    const media=document.createElement(file.mime.startsWith('video/')?'video':'img');media.src=url;media.style.maxWidth='100%';media.style.maxHeight='500px';if(media.tagName==='VIDEO')media.controls=true;else media.alt='生成结果';
    const link=document.createElement('a');link.className='ig-button';link.href=url;link.download=file.storage_key.split('/').pop();link.textContent='下载'+(file.mime.startsWith('video/')?'视频':'图片');
    $('ig-result').append(media,link);
  }
  if(task.status==='unknown'&&admin){const button=document.createElement('button');button.className='ig-button';button.textContent='确认重新生成';button.onclick=async()=>{if(!confirm('原执行可能已完成，重新生成可能产生重复结果。继续？'))return;try{await json('/api/admin/tasks/'+task.id+'/retry',{method:'POST'});await refresh();}catch(error){message('ig-submit-error',error.message);}};$('ig-result').append(button);}
}
function renderTasks(tasks){
  $('ig-task-list').innerHTML=tasks.slice(0,8).map(t=>`<button type="button" class="ig-task-row ig-history" data-task="${escape(t.id)}"><span>${escape(t.model_id)} · ${t.id.slice(0,8)}</span><span>${taskName[t.status]}</span></button>`).join('');
  if(!latestTask&&tasks.length)latestTask=tasks[0].id;
  const task=tasks.find(t=>t.id===latestTask);if(task)showResult(task).catch(error=>{delete $('ig-result').dataset.signature;message('ig-submit-error',error.message);});
}
$('ig-task-list').onclick=e=>{const button=e.target.closest('[data-task]');if(button){latestTask=button.dataset.task;refresh();}};

function navigate(name){page=name;root.querySelectorAll('[data-screen]').forEach(e=>e.hidden=e.dataset.screen!==name);root.querySelectorAll('[data-page]').forEach(e=>e.setAttribute('aria-pressed',String(e.dataset.page===name)));loadAdmin().catch(error=>message('ig-backend-message',error.message));}
root.querySelector('.ig-nav').onclick=e=>{const button=e.target.closest('[data-page]');if(button)navigate(button.dataset.page);};
async function refresh(){if(!token||refreshRunning)return;refreshRunning=true;const sessionToken=token;try{const [catalog,history,identity]=await Promise.all([json('/api/models'),json('/api/tasks'),json('/api/session')]);if(sessionToken!==token)return;permissions(identity);models=catalog.models;queueCount=catalog.queue_count;maxCount=catalog.max_count;renderModels();renderTasks(history.tasks);if(page==='backends')await loadAdmin();}catch(error){if(sessionToken===token)message('ig-submit-error',error.message);}finally{refreshRunning=false;}}
async function loadAdmin(){
  if(!admin)return;
  const requestedPage=page, sessionToken=token;
  if(page==='backends'){const result=await json('/api/admin/backends');if(token!==sessionToken||page!==requestedPage)return;nodes=result.nodes;renderNodes();}
  if(page==='keys'){const result=await json('/api/admin/keys');if(token!==sessionToken||page!==requestedPage)return;keys=result.keys;renderKeys();}
  if(page==='settings'){const result=await json('/api/admin/settings');if(token!==sessionToken||page!==requestedPage)return;settings=result;$('ig-max').value=settings.max_count;$('ig-refresh').value=settings.refresh_seconds;$('ig-stale').value=settings.stale_seconds;}
  if(page==='users'&&superAdmin){const result=await json('/api/admin/users');if(token!==sessionToken||page!==requestedPage)return;users=result.users;renderUsers();}
}
function renderNodes(){
  $('ig-nodes').innerHTML=nodes.map(n=>`<article class="ig-node"><div class="ig-node-head"><div><div class="ig-node-title"><strong>${escape(n.name)}</strong><span class="ig-pill">${!n.enabled?'已禁用':n.quarantined?'执行结果待确认':n.connected?'已连接':'连接失败'}</span></div><div class="ig-note">${escape(n.id)} · ${escape(n.base_url)}</div><div class="ig-note">${n.models.length} 个模型 / ${n.models.reduce((s,m)=>s+m.loras.length,0)} 个 LoRA${n.error?' · '+escape(n.error):''}</div></div><div class="ig-node-controls"><button class="ig-button" data-action="sync" data-node="${n.id}">立即同步</button><button class="ig-button" data-action="edit" data-node="${n.id}">编辑</button><button class="ig-button" data-action="toggle" data-node="${n.id}">${n.enabled?'禁用':'启用'}</button></div></div><details><summary>查看模型、LoRA 与配置 JSON</summary>${n.models.map(m=>`<h3>${escape(m.model_id)}</h3><p class="ig-note">LoRA：${escape(m.loras.map(l=>l.display_name||l.lora_id).join('、')||'无')}</p><pre><code>${escape(JSON.stringify(m.configuration,null,2))}</code></pre>`).join('')||'<p class="ig-note">暂无有效模型配置</p>'}</details></article>`).join('');
  $('ig-cluster-foot').textContent=nodes.filter(n=>n.connected).length+' 个在线节点 · 模型与 LoRA 自动同步';
}
function openNode(node){editNode=node?.id||null;$('ig-node-form').hidden=false;$('ig-node-editor-title').textContent=node?'编辑后端':'接入后端';$('ig-node-name').value=node?.name||'';$('ig-node-url').value=node?.base_url||'';$('ig-node-admin').value='';$('ig-node-inference').value='';}
$('ig-add-node').onclick=()=>openNode(null);$('ig-cancel-node').onclick=()=>{$('ig-node-form').hidden=true;};
$('ig-node-form').addEventListener('submit',async e=>{e.preventDefault();const node=nodes.find(n=>n.id===editNode);try{await json('/api/admin/backends'+(editNode?'/'+editNode:''),{method:editNode?'PUT':'POST',body:JSON.stringify({name:$('ig-node-name').value,base_url:$('ig-node-url').value,inference_token:$('ig-node-inference').value,admin_token:$('ig-node-admin').value,enabled:node?.enabled??true})});$('ig-node-form').hidden=true;$('ig-node-admin').value='';$('ig-node-inference').value='';message('ig-backend-message','配置已保存，已尝试同步后端。');await loadAdmin();}catch(error){message('ig-backend-message',error.message);}});
$('ig-nodes').onclick=async e=>{const button=e.target.closest('[data-action]');if(!button)return;const node=nodes.find(n=>n.id===button.dataset.node);if(button.dataset.action==='edit')return openNode(node);try{if(button.dataset.action==='sync')await json('/api/admin/backends/sync',{method:'POST'});else await json('/api/admin/backends/'+node.id,{method:'PUT',body:JSON.stringify({name:node.name,base_url:node.base_url,enabled:!node.enabled})});await loadAdmin();await refresh();}catch(error){message('ig-backend-message',error.message);}};
function renderKeys(){ $('ig-keys').innerHTML=keys.map(k=>`<tr><td>${escape(k.name)}</td><td>${escape(k.prefix)}…</td><td>${k.revoked?'已撤销':k.enabled?'启用':'停用'}</td><td>${k.last_used_at?new Date(k.last_used_at).toLocaleString():'尚未调用'}</td><td><div class="ig-actions"><button class="ig-button" data-key="${k.id}" data-action="toggle" ${k.revoked?'disabled':''}>${k.enabled?'停用':'启用'}</button><button class="ig-button" data-key="${k.id}" data-action="revoke" ${k.revoked?'disabled':''}>撤销</button></div></td></tr>`).join(''); }
$('ig-key-form').addEventListener('submit',async e=>{e.preventDefault();try{const result=await json('/api/admin/keys',{method:'POST',body:JSON.stringify({name:$('ig-key-name').value})});$('ig-key-value').value=result.key;$('ig-created-key').hidden=false;$('ig-key-name').value='';await loadAdmin();}catch(error){alert(error.message);}});
$('ig-dismiss-key').onclick=()=>{$('ig-created-key').hidden=true;$('ig-key-value').value='';};
$('ig-keys').onclick=async e=>{const button=e.target.closest('[data-key]');if(!button)return;const key=keys.find(k=>k.id===button.dataset.key);try{await json('/api/admin/keys/'+key.id,{method:'PATCH',body:JSON.stringify(button.dataset.action==='revoke'?{revoked:true}:{enabled:!key.enabled})});await loadAdmin();}catch(error){alert(error.message);}};
$('ig-settings-form').addEventListener('submit',async e=>{e.preventDefault();try{settings=await json('/api/admin/settings',{method:'PUT',body:JSON.stringify({max_count:Number($('ig-max').value),refresh_seconds:Number($('ig-refresh').value),stale_seconds:Number($('ig-stale').value)})});message('ig-settings-message','参数已保存。');await refresh();}catch(error){message('ig-settings-message',error.message);}});
function renderUsers(){ $('ig-users').innerHTML=users.map(u=>`<tr><td>${escape(u.username)}</td><td><select data-role="${u.id}">${['user','admin','super_admin'].map(r=>`<option value="${r}" ${r===u.role?'selected':''}>${{user:'用户',admin:'管理员',super_admin:'超级管理员'}[r]}</option>`).join('')}</select></td><td>${u.enabled?'启用':'禁用'}</td><td><button class="ig-button" data-user="${u.id}" data-action="toggle">${u.enabled?'禁用':'启用'}</button> <button class="ig-button" data-user="${u.id}" data-action="password">重置密码</button></td></tr>`).join(''); }
$('ig-user-form').onsubmit=async e=>{e.preventDefault();try{await json('/api/admin/users',{method:'POST',body:JSON.stringify({username:$('ig-user-name').value,password:$('ig-user-password').value,role:$('ig-user-role').value})});$('ig-user-password').value='';$('ig-user-form').reset();message('ig-user-message','账号已创建。');await loadAdmin();}catch(error){message('ig-user-message',error.message);}};
$('ig-users').onchange=async e=>{const id=e.target.dataset.role;if(!id)return;try{await json('/api/admin/users/'+id,{method:'PATCH',body:JSON.stringify({role:e.target.value})});await loadAdmin();await login();}catch(error){message('ig-user-message',error.message);await loadAdmin();}};
$('ig-users').onclick=async e=>{const b=e.target.closest('[data-user]');if(!b)return;const u=users.find(u=>u.id===b.dataset.user);let body={enabled:!u.enabled};if(b.dataset.action==='password'){const password=prompt('请输入新密码（至少12位）');if(password===null)return;body={password};}try{await json('/api/admin/users/'+u.id,{method:'PATCH',body:JSON.stringify(body)});await loadAdmin();}catch(error){message('ig-user-message',error.message);}};
$('ig-password-form').onsubmit=async e=>{e.preventDefault();try{await json('/api/auth/password',{method:'POST',body:JSON.stringify({current_password:$('ig-current-password').value,password:$('ig-new-password').value})});$('ig-password-form').reset();logout();message('frame-login-error','密码已更新，请重新登录。');}catch(error){message('ig-password-message',error.message);}};
sessionStorage.removeItem('frame-token');
login();
setInterval(refresh,5000);
