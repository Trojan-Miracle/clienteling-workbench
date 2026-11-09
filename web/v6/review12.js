'use strict';
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const scenes={greeting:'生日 / 节庆',recommendation:'商品推荐',price:'价格异议',care:'购后关怀',reactivation:'重新联系',invitation:'活动邀请'};
let data=null,index=0,choice=null,dirty=false,busy=false,timer=null,baseId=null,parentId=null;
const error=e=>{$('#error').hidden=false;$('#error').textContent=e.message||String(e);};
async function api(url,options){const r=await fetch(url,options);let j;try{j=await r.json();}catch{throw Error('读取结果失败，请重试。');}if(!r.ok)throw Error(j.error||'操作未完成，请重试。');return j;}
const post=(url,body)=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const current=()=>data.cases[index];
const jobs=()=>data.revisions.filter(r=>r.case_id===current()?.case_id);
function time(s){return new Date(s).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});}
function facts(c){
 // Permission enums are not customer prose. Show explicit source quotes when available;
 // the complete conversation remains above, including later restrictions.
 const permission=m=>['contact_permission','service_permission'].includes(m.key);
 const facts=c.memories.filter(m=>!permission(m)||m.source_quote);
 return `<h3>关系与对话</h3>${c.events.map(e=>`<article><small>${esc(e.actor==='client'?'客户':e.actor==='advisor'?'顾问':'订单 / 服务')} · ${esc(time(e.occurred_at))}</small><p>${esc(e.text)}</p></article>`).join('')}<h3>已知信息</h3>${facts.map(m=>`<p>${esc(permission(m)?'客户说：'+m.source_quote:m.value)}</p>`).join('')}${c.products.length?'<h3>相关商品</h3>':''}${c.products.map(p=>`<article><strong>${esc(p.name)} · ¥${esc(p.price_cny)}</strong><p>${esc(p.material)}；${esc(p.capacity)}；${esc(p.carry)}</p><p>${esc(p.value_story)}</p><p>${esc(p.care)}</p></article>`).join('')}`;
}
function navigate(i){if(busy)return;if(dirty&&!confirm('本组有未保存的意见，离开会丢弃。'))return;clearTimeout(timer);index=i;dirty=false;render();window.scrollTo({top:0});}
function select(decision,id=null){choice={decision,candidate_id:id};if(id){baseId=id;parentId=null;$('#rewrite-base').value=id;}dirty=true;paint();$('#saved').textContent='尚未保存';}
function paint(){
 document.querySelectorAll('[data-decision]').forEach(b=>b.setAttribute('aria-pressed',String(choice?.decision===b.dataset.decision&&(choice?.candidate_id||null)===(b.dataset.candidate||null))));
 document.querySelectorAll('[data-card]').forEach(c=>c.classList.toggle('selected',c.dataset.card===choice?.candidate_id));
 if(!$('#save'))return;
 const note=$('#note').value.trim(),running=jobs().some(j=>j.status==='running');
 $('#save').disabled=busy||(!choice&&!note);$('#rewrite').disabled=busy||running||!note;
 $('#rewrite').textContent=running?'正在改写…':'按意见改写';
}
function render(){
 $('#error').hidden=true;$('#saved').textContent='';
 const saved=Object.values(data.responses).filter(r=>r.decision!=='comment').length;
 $('#progress').textContent=`已评价 ${saved} / ${data.cases.length}`;
 $('#steps').innerHTML=data.cases.map((c,i)=>`<button class="${data.responses[c.case_id]?'done':''}" ${i===index?'aria-current="step"':''} data-step="${i}" title="${esc(scenes[c.scenario])}">${i+1}</button>`).join('');
 document.querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>navigate(Number(b.dataset.step)));
 if(index>=data.cases.length){
  const remaining=data.cases.findIndex(c=>!data.responses[c.case_id]||data.responses[c.case_id].decision==='comment');
  $('#content').innerHTML=`<div class="finish"><h2>${remaining<0?'本轮评价已保存':'已到最后一组'}</h2><p>已评价 ${saved} 组，改写记录 ${data.revisions.filter(r=>r.status==='complete').length} 条。</p><div class="actions"><button id="again">${remaining<0?'回看第1组':'继续未评价的案例'}</button><a href="/api/quick-review/export">导出记录</a></div></div>`;
  $('#again').onclick=()=>navigate(remaining<0?0:remaining);return;
 }
 const c=current(),r=data.responses[c.case_id],last=[...c.events].reverse().find(e=>e.actor==='client'&&e.kind==='message');
 choice=r&&r.decision!=='comment'?{decision:r.decision,candidate_id:r.candidate_id}:null;
 baseId=r?.candidate_id||c.candidates[0].id;parentId=null;
 const prior=jobs().filter(j=>j.status==='complete').at(-1);if(prior)baseId=prior.candidate_id;
 $('#content').innerHTML=`<section class="context"><span class="label">${index+1} / 12 · ${esc(scenes[c.scenario])} · ${esc(c.case_id)} · ${esc(time(c.as_of))}</span><h2>${esc(c.customer.salutation||c.customer.name)}</h2><p class="message">${esc(last?.text||'请结合关系背景判断。')}</p><details><summary>对话背景与商品事实</summary><div class="background">${facts(c)}</div></details></section>
 <fieldset id="form"><div class="candidate-grid">${c.candidates.map((a,i)=>`<article class="candidate" data-card="${esc(a.id)}"><span class="number">0${i+1}</span><p>${esc(a.text)}</p><div class="actions"><button data-decision="ready" data-candidate="${esc(a.id)}">可直接用</button><button data-decision="edit" data-candidate="${esc(a.id)}">需要修改</button></div></article>`).join('')}</div>
 <div class="actions alternatives"><button data-decision="none">都不合适</button><button data-decision="skip">暂时跳过</button></div>
 <section class="feedback-box"><div class="feedback-top"><h2><label for="note">修改意见</label></h2><details class="feedback-options"><summary>选项</summary><label>意见范围<select id="scope" aria-label="意见范围"><option value="general">共性问题</option><option value="case">仅针对本例</option><option value="taste">个人审美</option></select></label></details></div>
 <textarea id="note" maxlength="2000" placeholder="想怎么改？">${esc(r?.note||'')}</textarea>
 <div class="feedback-actions"><label class="rewrite-base">修改<select id="rewrite-base">${c.candidates.map((a,i)=>`<option value="${esc(a.id)}">候选 0${i+1}</option>`).join('')}${prior?`<option value="revision:${esc(prior.id)}">上次改写</option>`:''}</select></label><div class="actions"><button id="save">保存意见</button><button id="rewrite" class="primary">按意见改写</button></div></div></section></fieldset>
 <section id="revision-result" aria-live="polite"></section><div class="bottom"><button id="previous" ${index===0?'disabled':''}>上一组</button><button id="next">${index===11?'结束本轮':'下一组'} →</button></div>`;
 $('#scope').value=r?.scope||'general';$('#rewrite-base').value=baseId;
 document.querySelectorAll('[data-decision]').forEach(b=>b.onclick=()=>select(b.dataset.decision,b.dataset.candidate||null));
 const changed=()=>{dirty=true;$('#saved').textContent='尚未保存';paint();};$('#note').oninput=changed;$('#scope').onchange=changed;
 $('#rewrite-base').onchange=()=>{const val=$('#rewrite-base').value;if(val.startsWith('revision:')){parentId=val.slice(9);baseId=jobs().find(j=>j.id===parentId).candidate_id;}else{baseId=val;parentId=null;}};
 $('#previous').onclick=()=>navigate(index-1);
 $('#next').onclick=async()=>{if(dirty){try{await saveFeedback();}catch(e){error(e);return;}}navigate(index+1);};
 $('#save').onclick=()=>saveFeedback().catch(error);$('#rewrite').onclick=rewrite;
 renderRevisions();paint();if(r)$('#saved').textContent='本组意见已保存';schedulePoll();
}
function revisionHTML(j,old=false){
 if(j.status==='running')return '<div class="revision-panel"><p role="status">正在修改…</p></div>';
 if(j.status==='failed')return `<div class="failure">${esc(j.error)}<p>点击“按意见改写”重试。</p></div>`;
 const r=j.result,unchanged=j.before===r.revised_text;
 return `<div class="${old?'history-item':'revision-panel'}"><div class="revision-heading"><h2>${old?'历史回复':unchanged?'保留原句':'修改后'}</h2>${j.verdict?`<span class="status-pill">${j.verdict==='accepted'?'已确认':'还需修改'}</span>`:''}</div>
 <p class="after-text">${esc(r.revised_text)}</p>
 ${r.changes[0]&&r.changes[0].length<=60?`<p class="change-brief">${esc(r.changes[0])}</p>`:''}
 ${old?'':`<div class="actions"><button data-verdict="accepted" data-id="${esc(j.id)}" aria-pressed="${j.verdict==='accepted'}">这样可以</button><button data-verdict="needs_work" data-id="${esc(j.id)}" aria-pressed="${j.verdict==='needs_work'}">还不对</button><button id="continue-rewrite" data-id="${esc(j.id)}">继续修改</button></div>`}
 <details class="revision-details"><summary>查看改动</summary><h3>修改前</h3><p class="before-text">${esc(j.before)}</p><ul>${r.changes.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>
 <div class="text-diff">${j.diff.map(d=>d.op==='equal'?esc(d.after):(d.before?'<del>'+esc(d.before)+'</del>':'')+(d.after?'<ins>'+esc(d.after)+'</ins>':'')).join('')}</div>
 <details class="suggestion"><summary>${j.scope==='general'?'产品改进建议（未应用）':j.scope==='taste'?'个人偏好建议（未应用）':'本例建议'}</summary><p>${esc(r.general_suggestion)}</p><p>${esc(r.limits)}</p></details>
 ${old?`<p class="revision-note">当时的意见：${esc(j.note)}</p>`:''}</details></div>`;
}
function renderRevisions(){
 if(!$('#revision-result'))return;const list=jobs(),latest=list.at(-1);
 $('#revision-result').innerHTML=latest?revisionHTML(latest)+(list.length>1?`<details><summary>此前 ${list.length-1} 次修改</summary>${list.slice(0,-1).reverse().map(j=>revisionHTML(j,true)).join('')}</details>`:''):'';
 document.querySelectorAll('[data-verdict]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const j=await post('/api/quick-review/revisions/'+b.dataset.id+'/verdict',{verdict:b.dataset.verdict});data.revisions[data.revisions.findIndex(x=>x.id===j.id)]=j;renderRevisions();}catch(e){error(e);b.disabled=false;}});
 const again=$('#continue-rewrite');if(again)again.onclick=()=>{const j=list.find(x=>x.id===again.dataset.id);parentId=j.id;baseId=j.candidate_id;const select=$('#rewrite-base'),value='revision:'+j.id;if(![...select.options].some(o=>o.value===value))select.add(new Option('上次改写',value));select.value=value;$('#note').focus();$('#note').scrollIntoView({block:'center',behavior:'smooth'});$('#saved').textContent='继续填写修改意见';};
}
async function saveFeedback(){
 if(busy)throw Error('正在保存，请稍等。');
 const c=current(),note=$('#note').value,scope=$('#scope').value,payload={...(choice||{decision:'comment',candidate_id:null}),note,scope,revision:data.responses[c.case_id]?.revision||0};
 if(!choice&&!note.trim())throw Error('请选择评价或填写意见。');
 busy=true;$('#form').disabled=true;$('#saved').textContent='正在保存…';$('#error').hidden=true;
 try{const saved=await post('/api/quick-review/'+c.case_id,payload);data.responses[c.case_id]=saved;dirty=false;$('#saved').textContent='意见已保存';$(`[data-step="${index}"]`).classList.add('done');$('#progress').textContent=`已评价 ${Object.values(data.responses).filter(r=>r.decision!=='comment').length} / 12`;return saved;}
 catch(e){$('#saved').textContent='未保存，输入仍保留';throw e;}
 finally{busy=false;$('#form').disabled=false;paint();}
}
async function rewrite(){
 if(busy||jobs().some(j=>j.status==='running'))return;
 try{
  await saveFeedback();
  const payload={note:$('#note').value.trim(),scope:$('#scope').value,candidate_id:baseId,parent_id:parentId};
  // Reuse the ID after a lost HTTP reply; a completed/failed request is a new attempt.
  const signature=JSON.stringify([current().case_id,payload]),key='virel-review-request';let pending;
  try{pending=JSON.parse(sessionStorage.getItem(key));}catch{}
  const requestId=pending?.signature===signature?pending.id:crypto.randomUUID();
  sessionStorage.setItem(key,JSON.stringify({signature,id:requestId}));
  busy=true;paint();$('#saved').textContent='正在提交改写…';
  const j=await post('/api/quick-review/'+current().case_id+'/rewrite',{...payload,request_id:requestId});
  if(!data.revisions.some(x=>x.id===j.id))data.revisions.push(j);
  sessionStorage.removeItem(key);renderRevisions();$('#saved').textContent='意见已保存，改写进行中';schedulePoll();
 }catch(e){error(e);}finally{busy=false;paint();}
}
function schedulePoll(){clearTimeout(timer);if(jobs().some(j=>j.status==='running'))timer=setTimeout(poll,1400);}
async function poll(){
 try{const latest=await api('/api/quick-review');data.revisions=latest.revisions;renderRevisions();paint();if(!dirty&&!jobs().some(j=>j.status==='running'))$('#saved').textContent=jobs().at(-1)?.status==='complete'?'改写已完成，结果已保存':'改写未完成，意见已保留';schedulePoll();}
 catch(e){error(Error('暂时无法读取改写进度。意见已保存，点击“重新读取”恢复。'));}
}
async function load(){
 if(busy)return;if(dirty&&!confirm('重新读取会放弃未保存的修改。'))return;
 clearTimeout(timer);$('#reload').disabled=true;$('#error').hidden=true;
 try{const fresh=await api('/api/quick-review');if(!data){const active=fresh.revisions.findLast(j=>j.status==='running');index=active?fresh.cases.findIndex(c=>c.case_id===active.case_id):0;}data=fresh;dirty=false;
  $('#product-changes').innerHTML=data.product_changes.map(c=>`<article><h3>${esc(c.title)}</h3><p>${esc(c.detail)}</p><small>${esc(c.status)}</small></article>`).join('');$('#change-count').textContent=`${data.product_changes.length} 项`;render();
 }catch(e){error(e);if(!data)$('#content').textContent='读取失败，请重试。';}finally{$('#reload').disabled=false;}
}
$('#reload').onclick=load;window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});load();
