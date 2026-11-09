'use strict';
// Memory type survives manual edits; provenance describes the author, not authority.
const memoryEdits=new Map();
function scopeName(value){return labels[value]||value;}
function scopeValue(value){const text=value.trim();return Object.entries(labels).find(([key,label])=>key!=='auto'&&label===text)?.[0]||text;}
const permissionNames={allowed:'客户同意接收符合其需求的消息',no_marketing:'不接收营销消息',do_not_contact:'不再主动联系',unknown:'尚未确认'};
const servicePermissionNames={allowed:'客户同意接收已约定的订单或售后通知',denied:'不接收服务联系'};
function permissionOptions(m){return m.key==='contact_permission'?permissionNames:m.key==='service_permission'?servicePermissionNames:null;}
function memoryState(m){
 if(m.status==='disabled')return '已停用';
 if(m.expires_at&&new Date(m.expires_at)<=(m.scope==='customer'?new Date(asOf()):new Date()))return '已到期';
 return memoryAvailable(m)?'生效中':'尚未生效';
}
function memoryContent(m){
 if(permissionOptions(m))return m.source_quote&&!m.human_locked ? '客户说：'+m.source_quote : '当前设置：'+(permissionOptions(m)[m.value]||m.value);
 if(m.kind==='example'&&m.evidence?.matches_current)return m.evidence.selected_text||m.evidence.reason||'这次没有合适的表达。';
 const legacy='用户在此场景选择过的表达样本（不是永久风格规则）：';
 return m.value.startsWith(legacy)?m.value.slice(legacy.length):m.value;
}
function memoryCard(m){
 const kind=m.scope==='customer'?(m.kind==='rule'?'沟通要求':memoryNames[m.key]||'客户情况'):m.kind==='example'?'场景参考':'已确认偏好';
 const e=m.evidence;
 const badges=[...(m.scope==='customer'&&m.kind!=='rule'?[kind]:[]),...(m.applicability!=='所有情境'?[scopeName(m.applicability)]:[]),...(memoryState(m)!=='生效中'?[memoryState(m)]:[])];
 return `<article class="memory-row ${memoryAvailable(m)?'':'memory-muted'}" data-memory="${esc(m.id)}">
  <div class="memory-top"><div class="memory-labels">${badges.map(label=>`<span>${esc(label)}</span>`).join('')}</div><button class="quiet memory-edit-icon" data-memory-edit aria-label="编辑记忆" title="编辑"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m15 5 4 4M4 20l5-1L20 8a2.8 2.8 0 0 0-4-4L5 15Z"/></svg></button></div>
  <p class="memory-content">${esc(memoryContent(m))}</p>
  <details class="memory-evidence"><summary>来源${m.expires_at?' · 有效至 '+dateLabel(m.expires_at):''}</summary>
   ${e?`${e.customer_message?`<p><span>当时客户</span>${esc(e.customer_message)}</p>`:''}${e.reason?`<p><span>我的意见</span>${esc(e.reason)}</p>`:''}${!e.matches_current?'<p>记忆已手动调整，以下为原始选择。</p>':''}${e.selected_text?`<p><span>采用表达</span>${esc(e.selected_text)}</p>`:'<p>当时没有选用候选。</p>'}`:`<p>${esc(m.source)}</p>${m.source_quote?`<p>${esc(m.source_quote)}</p>`:''}`}
  </details>
  <form class="memory-edit hidden">
   <label>记住的内容${permissionOptions(m)?`<select data-memory-value>${Object.entries(permissionOptions(m)).map(([key,name])=>`<option value="${key}" ${key===m.value?'selected':''}>${name}</option>`).join('')}</select>`:`<textarea data-memory-value maxlength="4000" rows="3">${esc(m.value)}</textarea>`}</label>
   <div class="memory-fields"><label>适用情境<input data-applicability value="${esc(scopeName(m.applicability))}" list="memory-scenarios" required maxlength="200"></label><label>有效至<input type="datetime-local" data-expires value="${dateLocal(m.expires_at)}"></label></div>
   <div class="memory-actions"><button type="submit" class="primary">保存</button><button type="button" data-memory-cancel>取消</button><button type="button" data-memory-action="${m.status==='disabled'?'active':'disabled'}">${m.status==='disabled'?'启用':'停用'}</button><button type="button" data-memory-action="deleted" class="quiet">删除</button></div>
   <div class="memory-delete hidden"><span>删除后不再用于后续生成。</span><button type="button" data-memory-delete-confirm>确认删除</button><button type="button" data-memory-delete-cancel>保留</button></div>
  </form><p class="memory-status" role="status"></p>
 </article>`;
}
function memoryHTML(memories,empty='选中喜欢的表达后，会在这里积累。',options=true){
 const ordered=[...memories].sort((a,b)=>Number(memoryAvailable(b))-Number(memoryAvailable(a))||Number(a.kind==='example')-Number(b.kind==='example')||b.updated_at.localeCompare(a.updated_at));
 return `<div class="memory-list">${ordered.length?ordered.map(memoryCard).join(''):`<p class="empty">${esc(empty)}</p>`}</div>${options?`<datalist id="memory-scenarios"><option value="所有情境">${Object.entries(labels).filter(([k])=>k!=='auto').map(([k,v])=>`<option value="${esc(v)}"></option>`).join('')}</datalist>`:''}`;
}
function keepMemoryEdits(root){
 root.querySelectorAll('[data-memory]').forEach(row=>{
  if(!row.querySelector('.memory-edit').classList.contains('hidden')){
   memoryEdits.set(row.dataset.memory,{value:row.querySelector('[data-memory-value]').value,scope:row.querySelector('[data-applicability]').value,expires:row.querySelector('[data-expires]').value});
  }
 });
}
function bindMemories(memories,root){
 root.querySelectorAll('[data-memory]').forEach(row=>{
  const m=memories.find(item=>item.id===row.dataset.memory),form=row.querySelector('.memory-edit'),status=row.querySelector('.memory-status');
  const edit=()=>{form.classList.remove('hidden');row.querySelector('[data-memory-edit]').classList.add('hidden');};
  const draft=memoryEdits.get(m.id);
  if(draft){edit();row.querySelector('[data-memory-value]').value=draft.value;row.querySelector('[data-applicability]').value=draft.scope;row.querySelector('[data-expires]').value=draft.expires;}
  row.querySelector('[data-memory-edit]').onclick=()=>{edit();row.querySelector('[data-memory-value]').focus();};
  form.oninput=()=>keepMemoryEdits(root);
  row.querySelector('[data-memory-cancel]').onclick=()=>{memoryEdits.delete(m.id);row.outerHTML=memoryCard(m);bindMemories(memories,root);};
  const save=async(action)=>{
   keepMemoryEdits(root);status.textContent='正在保存…';status.classList.remove('failed');
   const data={expected_revision:m.revision};
   if(action==='save'){data.value=row.querySelector('[data-memory-value]').value;data.applicability=scopeValue(row.querySelector('[data-applicability]').value);const expires=row.querySelector('[data-expires]').value;data.expires_at=expires?expires+':00+08:00':null;}else data.status=action;
   form.querySelectorAll('button').forEach(b=>b.disabled=true);
   try{
    const updated=await api('/api/memories/'+m.id,{method:'PATCH',body:JSON.stringify(data)});
    memoryEdits.delete(m.id);const index=memories.findIndex(item=>item.id===m.id);
    // Updating a single row preserves other editors, expanded evidence and scroll.
    if(updated.status==='deleted'){memories.splice(index,1);row.remove();}
    else{updated.evidence=m.evidence?{...m.evidence,matches_current:false}:null;memories[index]=updated;row.outerHTML=memoryCard(updated);}
    bindMemories(memories,root);state.draft=null;if($('#result'))renderResult();toast(action==='deleted'?'已删除。':'已保存。');
    if(m.scope==='advisor'&&m.kind==='rule'&&(root.id==='preferences-content'||root.id==='drawer-preferences'))await renderPreferences(root);
   }catch(e){
    status.textContent=e.message;status.classList.add('failed');form.querySelectorAll('button').forEach(b=>b.disabled=false);
    if(e.status===409){
     const reload=document.createElement('button');reload.type='button';reload.textContent='载入最新，放弃本次修改';status.append(reload);
     reload.onclick=async()=>{
      try{const data=await api(m.scope==='advisor'?'/api/preferences':'/api/customers/'+m.owner_id+'?as_of='+encodeURIComponent(asOf()));
       const latest=data.memories.find(item=>item.id===m.id);memoryEdits.delete(m.id);
       if(latest){memories[memories.findIndex(item=>item.id===m.id)]=latest;row.outerHTML=memoryCard(latest);}else row.remove();bindMemories(memories,root);
      }catch(loadError){status.firstChild.textContent=loadError.message;}
     };
    }
   }
  };
  form.onsubmit=e=>{e.preventDefault();save('save');};
  row.querySelectorAll('[data-memory-action]').forEach(b=>b.onclick=()=>{
   if(b.dataset.memoryAction==='deleted'){row.querySelector('.memory-delete').classList.remove('hidden');return;}
   save(b.dataset.memoryAction);
  });
  row.querySelector('[data-memory-delete-confirm]').onclick=()=>save('deleted');
  row.querySelector('[data-memory-delete-cancel]').onclick=()=>row.querySelector('.memory-delete').classList.add('hidden');
 });
}
async function showMemories(scope){
 if(scope==='advisor'){openDrawer('<div id="drawer-preferences"><div class="page-heading"><h1>表达要求</h1></div></div>');return renderPreferences($('#drawer-preferences'));}
 const customerId=state.customer;
 try{
  const data=await api('/api/customers/'+customerId+'?as_of='+encodeURIComponent(asOf()));
  for(const m of data.memories){if(permissionOptions(m)&&!m.human_locked&&!m.source_quote){const source=(data.timeline||[]).find(e=>e.id===m.source_event_id);if(source)m.source_quote=source.text;}}
  const rules=data.memories.filter(m=>m.kind==='rule'),facts=data.memories.filter(m=>m.kind!=='rule'),history=data.reply_history||[];
  const supported=state.bootstrap.capabilities?.customer_preferences;
  const types={...(supported?{fact:'客户资料',rule:'沟通要求'}:{}),purchase:'订单',service:'售后',appointment:'预约'};
  const records=(data.timeline||[]).filter(e=>['purchase','service','appointment'].includes(e.kind)).slice().reverse();
  if(state.customer!==customerId)return;
  openDrawer(`<div class="customer-info-head"><h1>${esc(data.customer.name)} · 客户资料</h1><button id="add-customer-info">＋ 补充</button></div>
   <form id="customer-info-form" class="customer-memory-form" hidden>
    <label>类型<select id="customer-info-kind">${Object.entries(types).map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select></label>
    <label><span id="customer-info-label">内容</span><textarea id="customer-info-text" maxlength="4000" required></textarea></label>
    <details id="customer-info-condition-field"><summary>适用条件</summary><input id="customer-info-condition" aria-label="适用条件" maxlength="200" value="所有情境" required></details>
    <label id="customer-info-product-field" hidden>相关商品<select id="customer-info-product"><option value="">不指定商品</option>${state.bootstrap.products.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select></label>
    <div class="editor-actions"><button type="submit" class="primary">保存</button><button type="button" id="cancel-customer-info">取消</button></div><p id="customer-info-status" role="status"></p>
   </form><div class="customer-memory-sections">
   ${rules.length?`<section><h2>沟通要求</h2>${memoryHTML(rules,'')}</section>`:''}
   ${facts.length?`<section><h2>偏好与资料</h2>${memoryHTML(facts,'',false)}</section>`:''}
   <details class="customer-records"><summary>订单与服务 ${records.length}</summary>${records.map(e=>`<article><div class="subtle">${types[e.kind]} · ${dateLabel(e.occurred_at)}</div><p>${esc(e.text)}</p></article>`).join('')||'<p class="empty">暂无记录</p>'}</details>
   <details class="customer-adoptions"><summary>过往采用 ${history.length}</summary>${history.length?history.map(r=>`<article><div class="subtle">${dateLabel(r.as_of)} · 采用草稿</div><details><summary>当时客户</summary><p>${esc(r.customer_message)}</p></details><p>${esc(r.text)}</p>${r.reason?`<p class="subtle">${esc(r.reason)}</p>`:''}</article>`).join(''):'<p class="empty">暂无采用记录。</p>'}</details>
  </div>`);
  bindMemories(data.memories,$('#drawer-content'));
  bindCustomerInfo(customerId,types);
 }catch(e){error(e);}
}
function bindCustomerInfo(customerId,types){
 const form=$('#customer-info-form'),kindInput=$('#customer-info-kind'),text=$('#customer-info-text'),condition=$('#customer-info-condition'),product=$('#customer-info-product'),status=$('#customer-info-status');
 const conditionField=$('#customer-info-condition-field'),productField=$('#customer-info-product-field'),label=$('#customer-info-label'),addButton=$('#add-customer-info');
 const key=kind=>'virel-customer-'+kind+'-'+customerId;
 const selectedKey='virel-customer-info-'+customerId;
 let kind=readLocal(selectedKey)?.kind;
 if(!types[kind])kind=Object.keys(types).find(k=>readLocal(key(k))?.text)||Object.keys(types)[0];
 let draft;
 function loadDraft(){
  draft=readLocal(key(kind))||{id:crypto.randomUUID(),text:'',applicability:'所有情境',product_id:''};
  kindInput.value=kind;text.value=draft.text||'';condition.value=draft.applicability||'所有情境';product.value=draft.product_id||'';
  const business=['purchase','service','appointment'].includes(kind);
  conditionField.hidden=business;conditionField.open=condition.value!=='所有情境';condition.disabled=business;productField.hidden=!business;product.disabled=!business;
  label.textContent=business?'已确认的进展':'内容';
  text.placeholder=({fact:'例如：偏好绿色，平时带手机和折叠伞',rule:'写下与这位客户沟通时要注意的事',purchase:'例如：深杉绿 Pont 26 已交付',service:'例如：护理已完成，等客户确认取回时间',appointment:'例如：已确认周六下午三点到店试背'})[kind];
  status.textContent='';
 }
 function persist(){draft={...draft,text:text.value,applicability:condition.value,product_id:product.value};localStorage.setItem(key(kind),JSON.stringify(draft));localStorage.setItem(selectedKey,JSON.stringify({kind}));}
 loadDraft();form.hidden=!draft.text;
 addButton.onclick=()=>{form.hidden=false;text.focus();};
 kindInput.onchange=()=>{persist();kind=kindInput.value;loadDraft();localStorage.setItem(selectedKey,JSON.stringify({kind}));text.focus();};
 for(const input of [text,condition,product])input.oninput=persist;
 $('#cancel-customer-info').onclick=()=>{localStorage.removeItem(key(kind));loadDraft();form.hidden=true;};
 form.onsubmit=async event=>{
  event.preventDefault();if(!text.value.trim()){text.focus();return;}
  persist();draft.as_of=draft.as_of||asOf();localStorage.setItem(key(kind),JSON.stringify(draft));
  const controls=[...form.querySelectorAll('input,select,textarea,button')],disabled=controls.map(el=>el.disabled);
  controls.forEach(el=>el.disabled=true);addButton.disabled=true;status.textContent='正在保存…';
  const business=['purchase','service','appointment'].includes(kind);
  try{
   await post('/api/customers/'+customerId+(business?'/events':kind==='rule'?'/preferences':'/facts'),business?
    {id:draft.id,occurred_at:draft.as_of,kind,actor:'system',text:draft.text,data:{product_id:draft.product_id||null}}:
    {id:draft.id,text:draft.text,applicability:draft.applicability,as_of:draft.as_of});
  }catch(e){status.textContent=e.message;controls.forEach((el,i)=>el.disabled=disabled[i]);addButton.disabled=false;return;}
  localStorage.removeItem(key(kind));status.textContent='已保存';toast('已保存');
  if(form.isConnected)keepMemoryEdits($('#drawer-content'));
  try{await refreshCustomers();if(state.customer===customerId){await selectCustomer(customerId);if(form.isConnected&&$('#drawer').open)await showMemories('customer');}}
  catch(e){error(Error('已保存，页面刷新失败：'+e.message));}
  finally{if(form.isConnected){controls.forEach((el,i)=>el.disabled=disabled[i]);loadDraft();form.hidden=true;addButton.disabled=false;}}
 };
}
let preferenceDraft=null, preferenceExpanded=false, preferencePage=0, conditionalExpanded=false;
const pendingPreferenceDrafts=new Map();
function fitExpression(text){text.style.height='auto';text.style.height=Math.min(360,Math.max(76,text.scrollHeight))+'px';}
window.addEventListener('resize',()=>document.querySelectorAll('#expression-text').forEach(fitExpression));
async function renderPreferences(target){
 const root=target||$('#preferences-content');
 keepMemoryEdits(root);
 try{
  const p=await api('/api/preferences'),q=p.contextual_question;
  if(!p.document)throw Error('偏好编辑需要新版服务，请打开8791端口的工作台。');
  const globalDoc=p.global_document||p.document;
  const draft=preferenceDraft,doc=draft||globalDoc;
  const conditional=p.global_document?p.memories.filter(m=>m.kind==='rule'&&(m.applicability!=='所有情境'||m.expires_at||new Date(m.effective_at)>new Date())):[];
  const visibleConditions=conditionalExpanded?conditional:conditional.slice(0,3);
  const pending=q?(pendingPreferenceDrafts.get(q.id)||{text:'',scope:scopeName(q.scenario)}):null;
  const examples=p.memories.filter(m=>m.kind==='example').sort((a,b)=>Number(memoryAvailable(b))-Number(memoryAvailable(a))||b.updated_at.localeCompare(a.updated_at));
  const size=preferenceExpanded?6:3,maxPage=Math.max(0,Math.ceil(examples.length/size)-1);
  preferencePage=Math.min(preferencePage,maxPage);
  const shown=examples.slice(preferencePage*size,preferencePage*size+size);
  root.innerHTML=`<div class="preferences-stack compact-preferences">
   ${q?`<article class="pending-preference"><span class="pending-label">要记住这条要求吗？</span><p>“${esc(q.quote)}”</p><textarea id="context-rule" maxlength="500" aria-label="待确认偏好" placeholder="写成以后可沿用的完整要求">${esc(pending.text)}</textarea><label class="condition-field">适用条件<input id="context-scope" list="preference-conditions" maxlength="200" value="${esc(pending.scope)}"></label><div class="editor-actions"><button data-context-answer="remember">记住</button><button data-context-answer="keep_example" class="quiet">只保留例子</button><button data-context-answer="skip" class="quiet">忽略</button></div><p id="context-status" role="status"></p></article>`:''}
   <section class="preference-document"><div class="preference-section-title"><h2>表达要求</h2>${p.global_document?'<button id="add-condition" class="quiet">新增</button>':''}</div>
    <textarea id="expression-text" aria-label="表达要求" maxlength="20000" ${draft?'':'readonly'} placeholder="例如：接着客户的话说；先回答问题，不必每次追问。">${esc(doc.text)}</textarea>

    <div id="expression-actions" class="editor-actions" ${draft?'':'hidden'}><button id="save-expression" class="primary">保存</button><button id="cancel-expression">取消</button></div><p id="expression-status" role="status"></p>
   ${conditional.length?`<section class="conditional-preferences"><div class="conditional-tools">${conditional.length>3?`<button id="expand-conditions" class="quiet">${conditionalExpanded?'收起':'查看全部'}</button>`:''}</div>${memoryHTML(visibleConditions)}</section>`:''}
   </section>
   <datalist id="preference-conditions"><option value="所有情境"></option>${Object.entries(labels).filter(([key])=>key!=='auto').map(([,name])=>`<option value="${esc(name)}"></option>`).join('')}</datalist>
   <section class="preference-examples"><div class="preference-section-title"><h2>表达例子 <small>${examples.length}</small></h2><div class="example-tools">${state.bootstrap.capabilities?.manual_examples?'<button id="add-example" class="quiet">新增</button>':''}${examples.length>3?`<button id="expand-examples" class="quiet">${preferenceExpanded?'收起':'查看全部'}</button>`:''}</div></div>${shown.length?memoryHTML(shown):'<p class="empty">还没有保存的例子</p>'}
   ${preferenceExpanded&&examples.length>size?`<div class="example-pagination"><button id="example-prev" ${preferencePage===0?'disabled':''}>上一页</button><span>${preferencePage+1} / ${maxPage+1}</span><button id="example-next" ${preferencePage>=maxPage?'disabled':''}>下一页</button></div>`:''}</section></div>`;
  const find=s=>root.querySelector(s),text=find('#expression-text');
  fitExpression(text);
  let savingExpression=false;
  const beginEditing=()=>{if(savingExpression)return;if(!preferenceDraft)preferenceDraft={...globalDoc,originalText:globalDoc.text};text.readOnly=false;find('#expression-actions').hidden=false;};
  text.onfocus=beginEditing;text.onclick=beginEditing;
  text.oninput=()=>{if(preferenceDraft)preferenceDraft.text=text.value;find('#expression-status').textContent='尚未保存';fitExpression(text);};
  find('#cancel-expression').onclick=()=>{preferenceDraft=null;renderPreferences(root);};
  find('#save-expression').onclick=async()=>{
   if(!preferenceDraft||savingExpression)return;savingExpression=true;const buttons=[...root.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);text.readOnly=true;
   find('#expression-status').textContent='正在保存…';
   try{await api(p.global_document?'/api/preferences/global':'/api/preferences/document',{method:'PUT',body:JSON.stringify({text:text.value,revision:preferenceDraft.revision})});preferenceDraft=null;await renderPreferences(root);root.querySelector('#expression-status').textContent='已保存';state.draft=null;if($('#result'))renderResult();}
   catch(e){savingExpression=false;text.readOnly=false;find('#expression-status').textContent=e.message;buttons.forEach(b=>b.disabled=false);
    if(e.status===409){const reload=document.createElement('button');reload.textContent='读取最新内容';reload.className='quiet';reload.onclick=()=>{if(confirm('放弃本次编辑并读取最新内容？')){preferenceDraft=null;renderPreferences(root);}};find('#expression-status').append(reload);}}
  };
  root.querySelectorAll('[data-context-answer]').forEach(b=>b.onclick=async()=>{
   if(preferenceDraft){find('#context-status').textContent='请先保存或取消下方编辑。';return;}
   const buttons=[...root.querySelectorAll('[data-context-answer]')];buttons.forEach(el=>el.disabled=true);
   try{await post('/api/preferences/context-answer',{question_id:q.id,expected_revision:q.revision,action:b.dataset.contextAnswer,rule_text:find('#context-rule').value,applicability:scopeValue(find('#context-scope').value)});await renderPreferences(root);}
   catch(e){find('#context-status').textContent=e.message;buttons.forEach(el=>el.disabled=false);}
  });
  if(q){const keep=()=>pendingPreferenceDrafts.set(q.id,{text:find('#context-rule').value,scope:find('#context-scope').value});find('#context-rule').oninput=keep;find('#context-scope').oninput=keep;}
  if(find('#add-condition'))find('#add-condition').onclick=()=>openConditionalRule();
  if(find('#expand-conditions'))find('#expand-conditions').onclick=()=>{conditionalExpanded=!conditionalExpanded;renderPreferences(root);};
  if(find('#add-example'))find('#add-example').onclick=()=>openExampleCreator();
  const paging=async fn=>{keepMemoryEdits(root);fn();await renderPreferences(root);};
  if(find('#expand-examples'))find('#expand-examples').onclick=()=>paging(()=>{preferenceExpanded=!preferenceExpanded;preferencePage=0;});
  if(find('#example-prev'))find('#example-prev').onclick=()=>paging(()=>preferencePage--);
  if(find('#example-next'))find('#example-next').onclick=()=>paging(()=>preferencePage++);
  bindMemories(p.memories,root);
 }catch(e){
  let notice=root.querySelector('.preference-load-error');if(!notice){notice=document.createElement('p');notice.className='preference-load-error';notice.setAttribute('role','alert');root.prepend(notice);}
  notice.textContent=e.message;const retry=document.createElement('button');retry.textContent='重新读取';retry.onclick=()=>renderPreferences(root);notice.append(' ',retry);
 }
}
window.addEventListener('beforeunload',e=>{if(preferenceDraft&&preferenceDraft.text!==preferenceDraft.originalText){e.preventDefault();e.returnValue='';}});

function openExampleCreator(){
 let data;try{data=JSON.parse(localStorage.getItem('virel-new-example'));}catch{}
 data=data||{id:crypto.randomUUID(),text:'',context:'',applicability:'所有情境'};
 openDrawer(`<h1>添加例子</h1><form id="example-form"><label>客户情境<textarea id="new-example-context" maxlength="1000" placeholder="可不填，例如：客户在询问能否降价。">${esc(data.context)}</textarea></label><label>参考表达<textarea id="new-example-text" maxlength="2500" required>${esc(data.text)}</textarea></label><details ${data.applicability!=='所有情境'?'open':''}><summary>适用条件</summary><input id="new-example-scope" aria-label="适用条件" list="preference-conditions" maxlength="200" value="${esc(scopeName(data.applicability))}"></details><p id="example-status" role="status"></p><div class="editor-actions"><button class="primary" type="submit">保存</button><button type="button" id="cancel-example">取消</button></div></form>`);

 const keep=()=>{data={...data,text:$('#new-example-text').value,context:$('#new-example-context').value,applicability:scopeValue($('#new-example-scope').value)||'所有情境'};localStorage.setItem('virel-new-example',JSON.stringify(data));};
 $('#example-form').oninput=keep;$('#cancel-example').onclick=()=>$('#drawer').close();
 $('#example-form').onsubmit=async event=>{
  event.preventDefault();keep();const button=event.submitter;button.disabled=true;
  try{await post('/api/preferences/examples',data);localStorage.removeItem('virel-new-example');$('#drawer').close();preferencePage=0;await renderPreferences();toast('例子已保存。');}
  catch(err){$('#example-status').textContent=err.message;button.disabled=false;}
 };
 $('#new-example-text').focus();
}

function openConditionalRule(){
 let draft=readLocal('virel-new-rule')||{id:crypto.randomUUID(),text:'',applicability:''};
 openDrawer(`<h1>添加表达要求</h1><form id="condition-form"><label>适用条件<input id="condition-scope" list="preference-conditions" maxlength="200" required placeholder="例如：客户要求简短祝福时" value="${esc(draft.applicability)}"></label><label>表达要求<textarea id="condition-text" maxlength="4000" required placeholder="写下希望怎样表达">${esc(draft.text)}</textarea></label><p id="condition-status" role="status"></p><div class="editor-actions"><button class="primary" type="submit">保存</button><button type="button" id="cancel-condition">取消</button></div></form>`);
 const keep=()=>{draft={...draft,text:$('#condition-text').value,applicability:$('#condition-scope').value};localStorage.setItem('virel-new-rule',JSON.stringify(draft));};
 $('#condition-form').oninput=keep;$('#cancel-condition').onclick=()=>$('#drawer').close();
 $('#condition-form').onsubmit=async event=>{event.preventDefault();keep();event.submitter.disabled=true;
  try{await post('/api/preferences/rules',{...draft,applicability:scopeValue(draft.applicability)});localStorage.removeItem('virel-new-rule');$('#drawer').close();await renderPreferences();toast('表达要求已保存。');}
  catch(e){$('#condition-status').textContent=e.message;event.submitter.disabled=false;}
 };
 $('#condition-scope').focus();
}
