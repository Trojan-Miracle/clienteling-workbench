'use strict';
// Shared editor for the workbench, calibration and saved feedback.
// Unsaved input remains local across navigation/reload; submitted revisions are server-side.
function readLocal(key, fallback=null){try{return JSON.parse(localStorage.getItem(key))??fallback;}catch{return fallback;}}
function editorKey(id){return 'virel-editor-'+id;}
async function existingFeedback(id){return (await api('/api/feedback')).find(f=>f.draft_id===id&&f.mode==='calibration')||null;}
async function saveSelection(id,candidate,text,reason='',order=['a','b','c']){
 const previous=await existingFeedback(id);
 return post('/api/feedback',{draft_id:id,mode:'calibration',outcome:'choice',candidate_id:candidate,
  selected_text:text,reason,display_order:order,learning_scope:'example',
  ...(previous?{feedback_id:previous.id,expected_revision:previous.revision}:{})});
}

function renderCalibration(){
 const card=state.cards[state.cardIndex];
 const receipt=state.lastFeedback?`<div class="feedback-receipt">已保存 <button id="edit-last" class="quiet">补充 / 修改上次选择</button></div>`:'';
 if(!card){$('#calibration-content').innerHTML=receipt+'<div class="empty">没有待比较的回复<br><button id="calibration-start" class="quiet">去客户页起草</button></div>';bindReceipt();$('#calibration-start').onclick=()=>switchView('workspace').catch(error);return;}
 const candidates=card.comparison_id?shuffle(card.candidates):card.candidates;state.order=candidates.map(c=>c.id);
 const cached=readLocal('virel-calibration-'+(card.draft_id||card.comparison_id),{});
 $('#calibration-content').innerHTML=`${receipt}<div class="calibration-context"><p class="eyebrow">${state.cardIndex+1} / ${state.cards.length} · ${esc(labels[card.scenario])}</p><h2>${esc(card.customer.salutation||card.customer.name)}</h2><p>“${esc(card.last_message)}”</p><details><summary>对话背景</summary>${contextHTML(card.context)}</details></div>
 <div class="calibration-cards">${candidates.map((c,i)=>`<article class="choice-card"><span>0${i+1}</span><p>${esc(c.text)}</p><button class="primary" data-choice="${esc(c.id)}">这句更合适</button></article>`).join('')}</div>
 <div class="calibration-bottom"><button data-outcome="none">都不想发</button>${card.draft_id&&state.bootstrap.capabilities?.contact_feedback?'<button id="calibration-no-contact">此时不该联系</button>':''}<button data-outcome="tie" title="已比较，没有明显偏好">差不多</button><button data-outcome="skip">跳过</button></div>`;
 bindReceipt();
 const options={fromCalibration:true,...(cached.reason&&!readLocal(editorKey(card.draft_id),{}).dirty?{reason:cached.reason}:{})};
 if($('#calibration-no-contact'))$('#calibration-no-contact').onclick=()=>openContactFeedback(card,cached.reason||'');
 $$('[data-choice],[data-outcome]').forEach(b=>b.onclick=async()=>{
  if(b.dataset.outcome==='none'&&card.draft_id){await openDraftEditor(card.draft_id,null,{...options,outcome:'none',referenceMode:true}).catch(error);return;}
  const buttons=$$('[data-choice],[data-outcome]');buttons.forEach(el=>el.disabled=true);
  try{
   const shared={outcome:b.dataset.outcome||'choice',display_order:state.order,reason:''};
   let r;
   if(card.comparison_id)r=await post('/api/comparisons/vote',{...shared,card_id:card.comparison_id,choice_id:b.dataset.choice||null});
   else{
    const old=await existingFeedback(card.draft_id);
    r=await post('/api/feedback',{...shared,draft_id:card.draft_id,candidate_id:b.dataset.choice||null,
     ...(old?{feedback_id:old.id,expected_revision:old.revision}:{})});
    state.lastFeedback=card.draft_id;
   }
   localStorage.removeItem('virel-calibration-'+(card.draft_id||card.comparison_id));
   state.cardIndex++;renderCalibration();toast(r.note);
  }catch(e){error(e);buttons.forEach(el=>el.disabled=false);}
 });
}
function bindReceipt(){if($('#edit-last'))$('#edit-last').onclick=()=>openDraftEditor(state.lastFeedback).catch(error);}

async function showFeedbackHistory(){
 const rows=(await api('/api/feedback')).filter(r=>r.mode==='calibration');
 openDrawer(`<h1>我的选择</h1>${rows.length?rows.map(r=>`<article class="feedback-history-row"><small>${dateLabel(r.updated_at||r.created_at)} · ${esc({choice:'已选用',none:'都不想发',tie:'差不多',skip:'跳过',comment:'补充意见',no_contact:'此时不该联系'}[r.outcome])}</small><p>${esc(r.selected_text||r.reason||'未补充原因')}</p><button data-open-feedback="${esc(r.draft_id)}">查看 / 修改</button></article>`).join(''):'<p class="empty">还没有选择记录。</p>'}`);
 $$('[data-open-feedback]').forEach(b=>b.onclick=()=>openDraftEditor(b.dataset.openFeedback).catch(error));
}
$('#feedback-history').onclick=()=>showFeedbackHistory().catch(error);

let activeEditor=null,editorLoadVersion=0;
function editorVisible(e){return activeEditor===e&&$('#drawer').open&&e.root?.isConnected;}
async function openDraftEditor(id,candidateId=null,options={}){
 const version=++editorLoadVersion,drawer=drawerVersion,view=state.view,customer=state.customer;
 const [draft,saved,edits]=await Promise.all([api('/api/drafts/'+id+'?as_of='+encodeURIComponent(asOf())),existingFeedback(id),api('/api/drafts/'+id+'/edits')]);
 if(version!==editorLoadVersion||drawer!==drawerVersion||view!==state.view||customer!==state.customer)return;
 let local=readLocal(editorKey(id),{});
 const conflict=Boolean(saved&&local.dirty&&local.savedRevision!==undefined&&local.savedRevision!==saved.revision);
 if(saved&&!local.dirty&&local.savedRevision!==saved.revision)local={};
 const texts=Object.fromEntries(draft.result.candidates.map(c=>[c.id,c.text]));
 for(const e of edits)texts[e.candidate_id]=e.text;
 if(saved?.candidate_id&&saved.selected_text)texts[saved.candidate_id]=saved.selected_text;
 Object.assign(texts,local.texts||{});
 if(candidateId&&options.editedText!==undefined)texts[candidateId]=options.editedText;
 const referenceMode=options.referenceMode??(candidateId?false:local.referenceMode??false);
 const editor={draft,saved,texts,conflict,referenceMode,referenceIds:local.referenceIds??draft.result.candidates.map(c=>c.id),baseRevision:local.savedRevision,dirty:local.dirty||options.editedText!==undefined||options.reason!==undefined,candidate:referenceMode||options.outcome&&options.outcome!=='choice'?null:candidateId||local.candidate||saved?.candidate_id||null,
  reason:options.reason??local.reason??saved?.reason??'',confirmed:local.confirmed??draft.context.revision_request?.confirmed_facts??'',scope:local.scope||saved?.learning_scope||'example',
  rule:local.rule??saved?.rule_text??'',applicability:local.applicability||saved?.applicability||draft.result.scenario,
  outcome:options.outcome||local.outcome||saved?.outcome||'comment',fromCalibration:options.fromCalibration??local.fromCalibration??false,pending:local.pending||null,
  rewriteId:local.rewriteId||null,rewriteSignature:local.rewriteSignature||null,rewriteAsOf:local.rewriteAsOf||null};
 activeEditor=editor;localStorage.setItem('virel-open-editor',id);renderDraftEditor(editor);
 if(editor.pending)watchRewrite(editor);
 else if(options.rewrite)startRewrite(editor);
}
function persistEditor(e){localStorage.setItem(editorKey(e.draft.id),JSON.stringify({texts:e.texts,candidate:e.candidate,reason:e.reason,confirmed:e.confirmed,
 scope:e.scope,rule:e.rule,applicability:e.applicability,outcome:e.outcome,fromCalibration:e.fromCalibration,pending:e.pending,dirty:e.dirty,savedRevision:e.conflict?e.baseRevision:e.saved?.revision,rewriteId:e.rewriteId,rewriteSignature:e.rewriteSignature,rewriteAsOf:e.rewriteAsOf,referenceMode:e.referenceMode,referenceIds:e.referenceIds}));}
function renderDraftEditor(e){
 const d=e.draft, revision=d.context.revision_request;
 openDrawer(`<div class="editor-head"><h1>${revision?'修改后的建议':'调整这组建议'}</h1><small>${esc(d.context.customer.salutation||d.context.customer.name)} · ${esc(labels[d.result.scenario])}</small></div>
 ${e.conflict?`<div class="editor-alert">已保存反馈有更新，本地编辑仍保留。<details><summary>查看已保存的内容</summary><p>${esc(e.saved.selected_text||e.saved.reason)}</p><p>${esc(e.saved.rule_text)}</p></details><button id="editor-resolve-local">采用当前编辑</button><button id="editor-resolve-server">读取已保存内容</button></div>`:''}
 ${d.can_copy===false?'<p class="editor-alert">客户资料或情境已变化。可保存反馈，请回工作台重新起草。</p>':''}
 ${revision?`<details class="original-draft"><summary>查看原稿与修改要求</summary><p>${esc(revision.instruction)}</p>${revision.previous_candidates.map(c=>`<p>${esc(c.text)}</p>`).join('')}</details>`:''}
 <div class="editor-options">${d.result.candidates.map((c,i)=>`<article class="editor-option"><label>${e.referenceMode?`<input type="checkbox" name="editor-reference" value="${c.id}" ${e.referenceIds.includes(c.id)?'checked':''}>参考 ${i+1}`:`<input type="radio" name="editor-candidate" value="${c.id}" ${e.candidate===c.id?'checked':''}>采用 ${i+1}${c.id===d.result.recommended_id?' · 推荐':''}`}</label><textarea data-editor-text="${c.id}" aria-label="修改候选${c.id}" maxlength="1000">${esc(e.texts[c.id])}</textarea><details class="editor-candidate-reason" ${e.texts[c.id]!==c.text?'hidden':''}><summary>推荐理由</summary><p>${esc(c.reason)}</p></details></article>`).join('')}</div>

 <label class="editor-field">怎么修改<textarea id="editor-reason" maxlength="500" placeholder="希望怎么改">${esc(e.reason)}</textarea></label>
 ${state.bootstrap.capabilities?.confirmed_facts?`<details class="confirmed-facts" ${e.confirmed?'open':''}><summary>补充已确认情况</summary><textarea id="editor-confirmed" maxlength="2000" aria-label="已确认情况" placeholder="例如：已向活动负责人确认，本次三位同行可安排同组。">${esc(e.confirmed)}</textarea><small>仅用于本次回复</small></details>`:''}
 <div class="editor-actions"><button id="editor-rewrite" class="primary" ${e.pending||d.can_copy===false?'disabled':''}>${e.pending?'正在重写…':'按意见重写'}</button></div>
 <details class="learning-settings" ${['rule','customer_rule'].includes(e.scope)?'open':''}><summary>保存选项</summary> <label class="editor-outcome">这组表达<select id="editor-outcome">${e.referenceMode?'':`<option value="choice" ${e.candidate?'selected':''}>采用所选的一句</option>`}${Object.entries({comment:'只补充意见',none:'都不想发',tie:'差不多',skip:'跳过',...(state.bootstrap.capabilities?.contact_feedback?{no_contact:'此时不该联系'}:{})}).map(([key,label])=>`<option value="${key}" ${!e.candidate&&e.outcome===key?'selected':''}>${label}</option>`).join('')}</select></label><label class="editor-field">保存为<select id="editor-scope"><option value="example" ${e.scope==='example'?'selected':''}>表达例子</option><option value="once" ${e.scope==='once'?'selected':''}>仅本次</option><option value="rule" ${e.scope==='rule'?'selected':''}>我的表达要求</option>${state.bootstrap.capabilities?.customer_preferences?`<option value="customer_rule" ${e.scope==='customer_rule'?'selected':''}>本客户的沟通要求</option>`:''}</select></label><div id="editor-rule-fields" ${['rule','customer_rule'].includes(e.scope)?'':'hidden'}><label class="editor-field">沟通要求<textarea id="editor-rule" maxlength="500" placeholder="例如：回应还价时先说能否降价，不反复提预算。">${esc(e.rule)}</textarea></label><label class="editor-field">适用于<input id="editor-applicability" maxlength="200" list="editor-conditions" value="${esc(scopeName(e.applicability))}" placeholder="例如：生日当天，同时询问商品"><datalist id="editor-conditions"><option value="所有情境"></option>${Object.entries(labels).filter(([key])=>key!=='auto').map(([,name])=>`<option value="${esc(name)}"></option>`).join('')}</datalist></label></div></details>
 <div id="editor-status" class="editor-status" role="status"></div><div class="editor-actions editor-bottom"><button id="editor-submit" class="primary" ${e.pending?'disabled':''}>${e.candidate?'采用这句':'保存意见'}</button>${e.referenceMode?'':`<button id="editor-save-text" ${e.pending?'disabled':''}>仅保存草稿</button><button id="editor-copy" ${d.can_copy===false?'disabled':''}>复制</button>`}<button id="editor-back" class="quiet">返回</button></div>`);
 $('#drawer').classList.add('editor-dialog');e.root=$('.editor-head');
 if(e.conflict){$('#editor-resolve-local').onclick=()=>{e.conflict=false;persistEditor(e);renderDraftEditor(e);};$('#editor-resolve-server').onclick=()=>{localStorage.removeItem(editorKey(d.id));openDraftEditor(d.id).catch(error);};}
 $$('[data-editor-text]').forEach(el=>{const size=()=>{el.style.height='auto';el.style.height=Math.max(72,el.scrollHeight)+'px';};size();el.oninput=()=>{size();e.texts[el.dataset.editorText]=el.value;el.closest('.editor-option').querySelector('.editor-candidate-reason').hidden=el.value!==e.draft.result.candidates.find(c=>c.id===el.dataset.editorText).text;e.dirty=true;persistEditor(e);};});
 $$('[name="editor-reference"]').forEach(el=>el.onchange=()=>{e.referenceIds=$$('[name="editor-reference"]:checked').map(input=>input.value);e.dirty=true;persistEditor(e);});
 $$('[name="editor-candidate"]').forEach(el=>el.onchange=()=>{e.candidate=el.value;e.outcome='choice';$('#editor-outcome').value='choice';$('#editor-submit').textContent='采用这句';e.dirty=true;persistEditor(e);});
 $('#editor-outcome').onchange=()=>{e.outcome=$('#editor-outcome').value;$('#editor-submit').textContent=e.outcome==='choice'?'采用这句':'保存意见';if(e.outcome!=='choice'){e.candidate=null;$$('[name="editor-candidate"]').forEach(el=>el.checked=false);}e.dirty=true;persistEditor(e);};
 if($('#editor-confirmed'))$('#editor-confirmed').oninput=()=>{e.confirmed=$('#editor-confirmed').value;e.dirty=true;persistEditor(e);};
 $('#editor-reason').oninput=()=>{e.reason=$('#editor-reason').value;e.dirty=true;persistEditor(e);};
 $('#editor-rule').oninput=()=>{e.rule=$('#editor-rule').value;e.dirty=true;persistEditor(e);};
 $('#editor-applicability').onchange=()=>{e.applicability=scopeValue($('#editor-applicability').value);e.dirty=true;persistEditor(e);};
 $('#editor-scope').onchange=()=>{e.scope=$('#editor-scope').value;e.dirty=true;$('#editor-rule-fields').hidden=!['rule','customer_rule'].includes(e.scope);persistEditor(e);};
 $('#editor-rewrite').onclick=()=>startRewrite(e);
 $('#editor-back').onclick=()=>{$('#drawer').close();localStorage.removeItem('virel-open-editor');};
 $('#editor-submit').onclick=()=>submitEditorFeedback(e);
 if($('#editor-save-text'))$('#editor-save-text').onclick=async()=>{
  if(!e.candidate){editorNotice(e,'先选择需要保存的一句。',true);return;}
  try{await post('/api/drafts/'+d.id+'/edits',{candidate_id:e.candidate,text:e.texts[e.candidate],as_of:asOf()});if(state.draft?.id===d.id){state.draft.display_texts={...(state.draft.display_texts||{}),[e.candidate]:e.texts[e.candidate]};renderResult();}editorNotice(e,'编辑稿已保存。');}catch(err){editorNotice(e,err.message,true);}
 };
 if($('#editor-copy'))$('#editor-copy').onclick=async()=>{
  if(!e.candidate){editorNotice(e,'先选择一句。',true);return;}
  try{await post('/api/drafts/'+d.id+'/check-copy',{as_of:asOf()});await navigator.clipboard.writeText(e.texts[e.candidate]);editorNotice(e,'已复制。');}catch(err){editorNotice(e,err.message||'请选中正文手动复制。',true);}
 };
 if(e.pending)$$('#drawer-content textarea,#drawer-content input,#drawer-content select').forEach(el=>el.disabled=true);
 persistEditor(e);
}
function editorNotice(e,text,failed=false){if(!editorVisible(e))return;$('#editor-status').textContent=text;$('#editor-status').classList.toggle('failed',failed);}
async function submitEditorFeedback(e){
 if(e.conflict){editorNotice(e,'请先核对已更新的反馈，再决定保留哪个版本。',true);return;}
 if(!e.candidate&&!e.reason.trim()&&!['none','tie','skip','no_contact'].includes(e.outcome)){editorNotice(e,'请选择一句，或写下意见。',true);return;}
 const button=$('#editor-submit');button.disabled=true;
 try{
  const r=await post('/api/feedback',{draft_id:e.draft.id,outcome:e.candidate?'choice':e.outcome==='choice'?'comment':e.outcome,
   candidate_id:e.candidate,selected_text:e.candidate?e.texts[e.candidate]:null,display_order:['a','b','c'],reason:e.reason,
   learning_scope:e.outcome==='no_contact'?'once':e.scope,rule_text:e.outcome!=='no_contact'&&['rule','customer_rule'].includes(e.scope)?e.rule:'',applicability:['rule','customer_rule'].includes(e.scope)?e.applicability:e.draft.result.scenario,
   ...(e.saved?{feedback_id:e.saved.id,expected_revision:e.saved.revision}:{})});
  e.saved=r.feedback;if(state.draft?.id===e.draft.id){state.draft.adopted_candidate=e.candidate;if(e.candidate)state.draft.display_texts={...(state.draft.display_texts||{}),[e.candidate]:e.texts[e.candidate]};renderResult();}e.dirty=false;persistEditor(e);editorNotice(e,r.note);button.textContent=e.candidate?'已采用':'已保存';
  if(e.fromCalibration&&state.cards[state.cardIndex]?.draft_id===e.draft.id){state.lastFeedback=e.draft.id;state.cardIndex++;if(editorVisible(e))$('#drawer').close();renderCalibration();toast(r.note);}
 }catch(err){editorNotice(e,err.message,true);}finally{button.disabled=false;}
}
async function startRewrite(e){
 if(e.pending)return;
 if(e.confirmed.trim()&&!state.bootstrap.capabilities?.confirmed_facts){editorNotice(e,'当前服务尚未支持补充事实，请使用更新后的工作台。',true);return;}
 if(!e.reason.trim()&&!e.confirmed.trim()){editorNotice(e,'先写下希望怎样修改。',true);$('#editor-reason')?.focus();return;}
 if(e.referenceMode&&!state.bootstrap.capabilities?.rewrite_references){editorNotice(e,'请刷新页面，使用已更新的重写功能。',true);return;}
 const references=e.referenceMode?e.draft.result.candidates.filter(c=>e.referenceIds.includes(c.id)).map(c=>({id:c.id,text:e.texts[c.id]})):null;
 const at=asOf(),signature=JSON.stringify([e.reason,e.confirmed,e.candidate,e.candidate?e.texts[e.candidate]:null,references,state.bootstrap.clock.live?dateLocal(at).slice(0,10):at]);
 if(signature!==e.rewriteSignature||!e.rewriteAsOf){e.rewriteId=crypto.randomUUID();e.rewriteSignature=signature;e.rewriteAsOf=at;}
 persistEditor(e);$('#editor-rewrite').disabled=true;
 try{
  const r=await post('/api/drafts/'+e.draft.id+'/rewrite',{id:e.rewriteId,instruction:e.reason.trim()||'根据补充的已确认情况更新回复。',...(state.bootstrap.capabilities?.confirmed_facts?{confirmed_facts:e.confirmed}:{}),
   candidate_id:e.referenceMode?null:e.candidate,edited_text:!e.referenceMode&&e.candidate?e.texts[e.candidate]:null,...(e.referenceMode?{reference_candidates:references}:{}),as_of:e.rewriteAsOf});
  if(r.draft_id){await acceptRewrite(e,r.draft_id);return;}
  e.pending=r.job_id;persistEditor(e);if(editorVisible(e))renderDraftEditor(e);await watchRewrite(e);
 }catch(err){editorNotice(e,err.message,true);if(editorVisible(e))$('#editor-rewrite').disabled=false;}
}
async function watchRewrite(e){
 if(e.watching)return;e.watching=true;
 try{
  while(e.pending){
   const job=await api('/api/jobs/'+e.pending);
   if(job.status==='finished'){
    const d=await api('/api/drafts/'+job.draft_id+'?as_of='+encodeURIComponent(asOf()));
    e.pending=null;persistEditor(e);
    if(d.status!=='contact')throw Error(d.issues?.join('；')||d.result?.reason||'本次未生成可用回复。');
    await acceptRewrite(e,d.id);break;
   }
   if(['failed','interrupted'].includes(job.status)){e.pending=null;persistEditor(e);throw Error(job.error||'重写中断，原稿和意见已保留。');}
   await new Promise(r=>setTimeout(r,1500));
  }
 }catch(err){
  if(editorVisible(e)){renderDraftEditor(e);editorNotice(e,err.message,true);
   if(e.pending){const b=$('#editor-rewrite');b.disabled=false;b.textContent='恢复查看重写结果';b.onclick=()=>watchRewrite(e);}}
 }finally{e.watching=false;}
}
async function acceptRewrite(e,id){
 e.pending=null;persistEditor(e);
 if(e.fromCalibration&&state.cards[state.cardIndex]?.draft_id===e.draft.id){const updated=await api('/api/drafts/'+id);if(state.cards[state.cardIndex]?.draft_id===e.draft.id){state.cards[state.cardIndex]={...state.cards[state.cardIndex],draft_id:id,candidates:updated.result.candidates,context:updated.context,scenario:updated.result.scenario};renderCalibration();}}
 if(editorVisible(e)){await openDraftEditor(id,null,{fromCalibration:e.fromCalibration});if(activeEditor?.draft.id===id)editorNotice(activeEditor,'已按意见重写。原稿可展开查看。');}
 else{toast('重写已完成，可在客户记录中查看。');}
 if(state.customer===e.draft.customer_id){await selectCustomer(state.customer);}
}
$('#drawer').addEventListener('close',()=>localStorage.removeItem('virel-open-editor'));
// Resume only an editor the user left open, after app bootstrap has settled.
(async()=>{const id=localStorage.getItem('virel-open-editor');if(!id)return;for(let i=0;i<100&&!state.ready;i++)await new Promise(r=>setTimeout(r,100));if(state.ready)try{await openDraftEditor(id);}catch(e){error(e);}})();

function openContactFeedback(card,reason=''){
 openDrawer(`<h1>这次先不联系</h1><form id="contact-feedback" class="contact-feedback"><textarea id="contact-feedback-reason" maxlength="500" aria-label="原因" placeholder="原因（可选）">${esc(reason)}</textarea><p id="contact-feedback-status" role="status"></p><div class="editor-actions"><button type="submit" class="primary">保存本次判断</button><button type="button" id="cancel-contact-feedback">取消</button></div></form>`);
 $('#cancel-contact-feedback').onclick=()=>$('#drawer').close();
 $('#contact-feedback-reason').oninput=()=>localStorage.setItem('virel-calibration-'+card.draft_id,JSON.stringify({reason:$('#contact-feedback-reason').value}));
 $('#contact-feedback').onsubmit=async event=>{
  event.preventDefault();event.submitter.disabled=true;
  try{const old=await existingFeedback(card.draft_id);const result=await post('/api/feedback',{draft_id:card.draft_id,outcome:'no_contact',display_order:state.order,reason:$('#contact-feedback-reason').value,learning_scope:'once',...(old?{feedback_id:old.id,expected_revision:old.revision}:{})});
   localStorage.removeItem('virel-calibration-'+card.draft_id);state.lastFeedback=card.draft_id;if(state.cards[state.cardIndex]?.draft_id===card.draft_id)state.cardIndex++;$('#drawer').close();renderCalibration();toast(result.note);
  }catch(err){$('#contact-feedback-status').textContent=err.message;event.submitter.disabled=false;}
 };
}
