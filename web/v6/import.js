'use strict';
const importKey=id=>'virel-import-'+id;
function importState(id){try{return JSON.parse(localStorage.getItem(importKey(id)))||{text:''};}catch{return {text:''};}}
function saveImport(id,data){localStorage.setItem(importKey(id),JSON.stringify(data));}
const conversationDraftKey=id=>'virel-conversation-draft-'+id;
function conversationDraft(id){
 try{const saved=JSON.parse(localStorage.getItem(conversationDraftKey(id)));if(saved?.rows)return saved;}catch{}
 const old=messageDraft(id),rows=old.text?[{id:crypto.randomUUID(),actor:old.actor==='advisor'?'advisor':'client',text:old.text,occurred_at:asOf(),time_confirmed:false}]:[];
 const saved={id:crypto.randomUUID(),rows};
 if(rows.length){localStorage.setItem(conversationDraftKey(id),JSON.stringify(saved));localStorage.removeItem('virel-message-'+id);}
 return saved;
}
function saveConversationDraft(id,data){localStorage.setItem(conversationDraftKey(id),JSON.stringify(data));}
function conversationInputHTML(){
 const plus='<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M10 3v14M3 10h14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
 return `<div class="message-controls"><div class="message-add-row"><button type="button" data-add-message="client" aria-label="添加客户消息" title="添加客户消息">${plus}</button><button form="message-form" id="message-submit" type="submit" class="primary hidden">保存</button><button type="button" data-add-message="advisor" aria-label="添加我的消息" title="添加我的消息">${plus}</button></div><div class="message-save-row"><button type="button" id="legacy-import" class="quiet hidden"></button></div><p id="message-input-error" role="status"></p></div>`;
}
function bindConversationInput(customerId){
 const timeline=$('.timeline'),form=document.createElement('form');form.id='message-form';form.className='message-drafts';timeline.insertBefore(form,timeline.querySelector('.message-controls'));
 const submit=$('#message-submit'),status=$('#message-input-error'),addButtons=[...document.querySelectorAll('[data-add-message]')];let draft=conversationDraft(customerId),busy=false;
 const current=()=>form.isConnected&&state.customer===customerId;
 const persist=()=>saveConversationDraft(customerId,draft);
 function footer(){submit.classList.toggle('hidden',!draft.rows.length);submit.textContent=`保存 ${draft.rows.length} 条`;submit.disabled=busy;}
 function renderRows(focusId){
  form.innerHTML=draft.rows.map(r=>`<article class="message message-new ${esc(r.actor)}" data-new-message="${esc(r.id)}"><div class="new-message-meta"><select data-new-actor aria-label="说话人" required>${r.actor==='unknown'?'<option value="">选择身份</option>':''}<option value="client" ${r.actor==='client'?'selected':''}>客户</option><option value="advisor" ${r.actor==='advisor'?'selected':''}>我</option></select><time title="${esc(r.occurred_at)}">${dateLabel(r.occurred_at)}</time><button type="button" class="quiet" data-remove-message aria-label="移除这条未保存消息">×</button></div><textarea data-new-text required maxlength="6000" rows="2" aria-label="${r.actor==='advisor'?'我的消息':'客户消息'}" placeholder="${r.actor==='advisor'?'输入已发出的回复':'输入客户消息'}">${esc(r.text)}</textarea></article>`).join('');
  form.querySelectorAll('[data-new-message]').forEach(row=>{
   const record=draft.rows.find(r=>r.id===row.dataset.newMessage),input=row.querySelector('[data-new-text]');
   input.oninput=()=>{record.text=input.value;persist();status.textContent='';};
   input.onpaste=e=>{
    const text=e.clipboardData?.getData('text/plain')||'';
    const names=new Set(['客户','顾客','我','顾问','销售',state.detail.customer.name,state.detail.customer.salutation].filter(Boolean));
    const labelled=text.split('\n').filter(line=>{const match=line.trim().match(/^(?:\[?\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?\]?\s*)?([^:：\n]{1,30})[：:]/);return match&&names.has(match[1].trim());});
    if(labelled.length<2||input.selectionStart!==0||input.selectionEnd!==input.value.length)return;
    e.preventDefault();record.text=text;input.value=text;persist();splitPastedRecord(record,text).catch(err=>{if(current())status.textContent=err.message;});
   };
   row.querySelector('[data-new-actor]').onchange=e=>{record.actor=e.target.value||'unknown';persist();renderRows(record.id);};
   row.querySelector('[data-remove-message]').onclick=()=>{draft.rows=draft.rows.filter(r=>r.id!==record.id);persist();renderRows();};
  });
  footer();
  if(focusId){const row=[...form.querySelectorAll('[data-new-message]')].find(el=>el.dataset.newMessage===focusId);row?.querySelector('textarea').focus();row?.scrollIntoView({block:'nearest'});}
 }
 function setBusy(value){busy=value;form.querySelectorAll('input,textarea,select,button').forEach(el=>el.disabled=value);addButtons.forEach(el=>el.disabled=value);submit.disabled=value;form.setAttribute('aria-busy',String(value));}
 async function splitPastedRecord(record,text){
  setBusy(true);status.textContent='';const previous=localStorage.getItem(conversationDraftKey(customerId));
  try{
   const at=asOf(),preview=await post('/api/customers/'+customerId+'/imports/preview',{text,as_of:at});
   if(preview.turns.length<=1)return;
   if(draft.rows.length-1+preview.turns.length>60)throw Error('一次最多保存60条消息，请分段粘贴。');
   if(localStorage.getItem(conversationDraftKey(customerId))!==previous)throw Error('输入已改变，请重新粘贴。');
   const index=draft.rows.findIndex(r=>r.id===record.id);if(index<0)return;
   const rows=preview.turns.map(t=>({id:crypto.randomUUID(),actor:t.actor,text:t.text,occurred_at:t.occurred_at,time_confirmed:t.time_confirmed}));
   draft.rows.splice(index,1,...rows);persist();if(current())renderRows(rows[0].id);
  }finally{setBusy(false);}
 }
 addButtons.forEach(button=>button.onclick=()=>{
  if(draft.rows.length>=60){status.textContent='一次最多保存60条消息，请先保存。';return;}
  const row={id:crypto.randomUUID(),actor:button.dataset.addMessage,text:'',occurred_at:asOf(),time_confirmed:false};
  draft.rows.push(row);persist();status.textContent='';renderRows(row.id);
 });
 const legacy=importState(customerId),legacyButton=$('#legacy-import');
 if(legacy.text||legacy.job_id){legacyButton.classList.remove('hidden');legacyButton.textContent=legacy.job_id?'查看资料更新':'继续核对之前的记录';legacyButton.onclick=()=>openConversationImport(customerId).catch(error);}
 if(legacy.job_id)watchImport(customerId,legacy);
 form.onsubmit=async e=>{
  e.preventDefault();if(busy||!draft.rows.length)return;
  const turns=draft.rows.map(({actor,text,occurred_at,time_confirmed})=>({actor,text:text.trim(),occurred_at,time_confirmed}));
  if(turns.some(t=>!t.text||t.text.length>6000)){status.textContent='请填写每条消息，每条最多6000字';return;}
  const at=asOf(),text=turns.map(t=>`[${t.occurred_at}] ${t.actor==='client'?'客户':'我'}：${t.text}`).join('\n');
  if(text.length>262144){status.textContent='文字过多，请分批保存。';return;}
  const payload={id:draft.id,text,as_of:at,turns},submittedRows=JSON.parse(JSON.stringify(draft.rows));setBusy(true);status.textContent='';
  try{
   const response=await post('/api/customers/'+customerId+'/imports',payload);
   const latest=conversationDraft(customerId);
   if(latest.id===draft.id){
    const remaining=latest.rows.filter(row=>!submittedRows.some(sent=>JSON.stringify(sent)===JSON.stringify(row)));
    if(remaining.length)saveConversationDraft(customerId,{id:crypto.randomUUID(),rows:remaining});
    else localStorage.removeItem(conversationDraftKey(customerId));
   }
   // These source messages are committed, independently of memory processing.
   if(response.job_id){
    const pending={...payload,job_id:response.job_id};
    // Keep any older unconfirmed preview accessible instead of replacing it.
    const old=importState(customerId);if(!old.text||old.job_id)saveImport(customerId,pending);
    watchImport(customerId,pending);
   }
   if(current())await selectCustomer(customerId);await refreshCustomers();toast(response.processing_error?'消息已保存，客户资料待更新。':'消息已保存。');
  }catch(err){if(current())status.textContent=err.message;else error(err);}finally{setBusy(false);}
 };
 renderRows();
}
async function prepareConversationImport(customerId,source){
 const at=asOf(),format=source.format||'text',previous=localStorage.getItem(importKey(customerId));
 const preview=await post('/api/customers/'+customerId+'/imports/preview',{text:source.text,format,as_of:at});
 if(localStorage.getItem(importKey(customerId))!==previous)throw Error('输入已改变，请重新预览。');
 const data={id:crypto.randomUUID(),text:source.text,format,filename:source.filename||'',as_of:at,turns:preview.turns,skipped_messages:preview.skipped_messages||0};
 saveImport(customerId,data);return data;
}
async function openConversationImport(customerId){
 const saved=importState(customerId);
 if(saved.job_id){showImportProgress(customerId,saved);return;}
 if(saved.id&&saved.turns){renderImportPreview(customerId,saved);return;}
 if(saved.text){const data=await prepareConversationImport(customerId,saved);if(state.customer===customerId)renderImportPreview(customerId,data);}
}
function renderImportPreview(customerId,data){
 const customer=state.customers.find(c=>c.id===customerId);if(!customer)return;
 const speakers=[...new Map(data.turns.filter(t=>t.speaker_id).map(t=>[t.speaker_id,t.speaker])).entries()];
 const localTime=t=>new Date(new Date(t).getTime()+8*3600000).toISOString().slice(0,19);
 openDrawer(`<div class="import-head"><h1>核对对话</h1><span>${esc(customer.name)} · ${data.turns.length} 条</span></div>
 ${speakers.length?`<div class="import-speakers">${speakers.map(([id,name],i)=>`<label><span>${esc(name)}<small>${esc(id)}</small></span><select data-import-speaker="${i}" aria-label="${esc(name)}是谁"><option value="">选择身份</option><option value="client">${esc(customer.name)}（客户）</option><option value="advisor">我</option></select></label>`).join('')}</div>`:''}
 <details class="import-original"><summary>${esc(data.filename||'原文')}</summary><pre>${esc(data.text)}</pre></details>
 ${data.skipped_messages?`<p class="import-note">仅导入文字；${data.skipped_messages} 条图片、语音等消息未导入。</p>`:''}
 <form id="import-preview">${data.turns.some(t=>!t.time_confirmed)?'<p class="import-note">缺少时间的消息暂用当前情境时间，可逐条修改。</p>':''}
 <div class="import-turns">${data.turns.map((t,i)=>`<article class="import-turn" data-import-turn="${i}"><div class="import-turn-head"><span>${String(i+1).padStart(2,'0')}</span><label>${esc(t.speaker||'说话人')}<select data-import-actor required>${t.actor==='unknown'?'<option value="">请选择</option>':''}<option value="client" ${t.actor==='client'?'selected':''}>${esc(customer.name)}</option><option value="advisor" ${t.actor==='advisor'?'selected':''}>我</option></select></label><label>时间<input data-import-time type="datetime-local" step="1" required value="${localTime(t.occurred_at)}"></label></div><textarea aria-label="第${i+1}条消息" data-import-text required maxlength="6000" rows="2">${esc(t.text)}</textarea></article>`).join('')}</div>
 <div class="import-bottom"><p id="import-error" role="status"></p><button type="button" id="import-back">返回输入</button><button type="submit" class="primary">确认加入 ${data.turns.length} 条</button></div></form>`);
 $('#drawer').classList.add('import-dialog');
 const form=$('#import-preview'),back=$('#import-back'),status=$('#import-error');
 const collect=()=>({...data,turns:[...form.querySelectorAll('[data-import-turn]')].map((row,i)=>({...data.turns[i],actor:row.querySelector('[data-import-actor]').value||'unknown',text:row.querySelector('[data-import-text]').value,occurred_at:row.dataset.timeEdited==='true'?new Date(row.querySelector('[data-import-time]').value+'+08:00').toISOString():data.turns[i].occurred_at,time_confirmed:data.turns[i].time_confirmed||row.dataset.timeEdited==='true'}))});
 const remember=()=>{try{saveImport(customerId,collect());}catch{status.textContent='请补全消息时间。';}};
 $$('[data-import-time]').forEach(el=>el.oninput=()=>el.closest('[data-import-turn]').dataset.timeEdited='true');
 form.oninput=remember;
 function syncSpeakers(){
  $$('[data-import-speaker]').forEach(select=>{const id=speakers[Number(select.dataset.importSpeaker)][0],values=new Set(data.turns.flatMap((t,i)=>t.speaker_id===id?[form.querySelector(`[data-import-turn="${i}"] [data-import-actor]`).value]:[]));select.value=values.size===1?[...values][0]:'';});
 }
 $$('[data-import-speaker]').forEach(select=>select.onchange=()=>{const id=speakers[Number(select.dataset.importSpeaker)][0];data.turns.forEach((t,i)=>{if(t.speaker_id===id)form.querySelector(`[data-import-turn="${i}"] [data-import-actor]`).value=select.value;});remember();});
 $$('[data-import-actor]').forEach(select=>select.onchange=syncSpeakers);syncSpeakers();
 back.onclick=()=>{remember();$('#drawer').close();if(state.customer===customerId){renderConversation();}};
 form.onsubmit=async e=>{
  e.preventDefault();const confirmed=collect();saveImport(customerId,confirmed);
  const payload={id:confirmed.id,text:confirmed.text,format:confirmed.format||'text',as_of:confirmed.as_of,turns:confirmed.turns.map(({actor,text,occurred_at,time_confirmed})=>({actor,text,occurred_at,time_confirmed}))};
  e.submitter.disabled=true;back.disabled=true;status.textContent='';
  try{
   const result=await post('/api/customers/'+customerId+'/imports',payload);
   if(result.job_id){const pending={...confirmed,job_id:result.job_id};saveImport(customerId,pending);if(form.isConnected)showImportProgress(customerId,pending);else watchImport(customerId,pending);}
   else{localStorage.removeItem(importKey(customerId));if(form.isConnected)$('#drawer').close();toast('对话已加入；'+result.processing_error);}
   await refreshCustomers();if(state.customer===customerId)await selectCustomer(customerId);
  }catch(err){if(form.isConnected)status.textContent=err.message;else error(err);e.submitter.disabled=false;back.disabled=false;}
 };
}
const importWatchers=new Set();
function showImportProgress(customerId,data){
 openDrawer(`<h1>对话已加入</h1><div class="import-progress" data-import-owner="${esc(customerId)}" data-import-job="${esc(data.job_id)}"><p class="progress">正在更新客户记忆…</p><p class="subtle">可以关闭窗口，继续其他工作。</p></div>`);$('#drawer').classList.add('import-dialog');watchImport(customerId,data);
}
async function watchImport(customerId,data){
 if(importWatchers.has(data.job_id))return;importWatchers.add(data.job_id);
 const currentPane=()=>document.querySelector('.import-progress[data-import-owner="'+customerId+'"][data-import-job="'+data.job_id+'"]');
 try{
  while(true){
   const job=await api('/api/jobs/'+data.job_id);
   if(job.status==='finished'){
    const issues=(job.memory_updates||[]).filter(m=>m.status!=='complete');if(importState(customerId).job_id===data.job_id)localStorage.removeItem(importKey(customerId));
    if(currentPane())currentPane().innerHTML=`<p>${issues.length?'对话已保存，'+issues.length+'条记忆需要核实。':'客户记忆已更新。'}</p><button id="import-done" class="primary">回到对话</button>`;
    if($('#import-done'))$('#import-done').onclick=()=>$('#drawer').close();
    await refreshCustomers();if(state.customer===customerId)await selectCustomer(customerId);break;
   }
   if(['failed','interrupted'].includes(job.status))throw Error(job.error||'记忆更新中断，对话已保留。');
   await new Promise(resolve=>setTimeout(resolve,1500));
  }
 }catch(err){
  if(currentPane()){
   currentPane().innerHTML=`<p class="import-failure">${esc(err.message)}</p><button id="import-resume">继续查看</button><button id="import-to-conversation">回到对话核实</button>`;
   $('#import-resume').onclick=()=>watchImport(customerId,data);$('#import-to-conversation').onclick=()=>{if(importState(customerId).job_id===data.job_id)localStorage.removeItem(importKey(customerId));$('#drawer').close();if(state.customer===customerId)selectCustomer(customerId).catch(error);};
  }
 }finally{importWatchers.delete(data.job_id);}
}
