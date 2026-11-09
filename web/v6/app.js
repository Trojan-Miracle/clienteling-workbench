'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const state={ready:false,bootstrap:null,customers:[],customer:null,detail:null,draft:null,cards:[],cardIndex:0,order:[],view:'workspace',pending:null};
const labels={greeting:'生日 / 节庆',recommendation:'商品推荐',care:'购后关怀',reactivation:'重新联系',invitation:'活动邀请',price:'价格回应',conversation:'日常对话',auto:'根据对话判断'};
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function api(path,options={}){const r=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});let data;try{data=await r.json();}catch{throw Error('服务器响应无法读取，请检查服务是否仍在运行。');}if(!r.ok){const failure=Error(data.error+(data.fields?' '+data.fields.map(f=>f.field+': '+f.message).join('；'):''));failure.status=r.status;throw failure;}return data;}
const post=(path,data)=>api(path,{method:'POST',body:JSON.stringify(data)});
function error(e){$('#error').textContent=e.message||String(e);$('#error').classList.remove('hidden');}
function clearError(){$('#error').classList.add('hidden');}
function toast(text){$('#toast').textContent=text;$('#toast').classList.remove('hidden');clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').classList.add('hidden'),4200);}
function asOf(){if(state.bootstrap?.clock?.live)return new Date().toISOString();const v=state.bootstrap?.clock?.as_of;if(!v)throw Error('请选择时间。');return v;}
function dateLocal(v){return v?new Date(new Date(v).getTime()+8*3600000).toISOString().slice(0,16):'';}
function dateLabel(v){return new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
let drawerVersion=0,customerLoadVersion=0;
function openDrawer(html){drawerVersion++;$('#drawer').classList.remove('editor-dialog','import-dialog','archive-dialog');$('#drawer-content').innerHTML=html;$('#drawer').showModal();}
$('#drawer').addEventListener('close',()=>{drawerVersion++;});
async function refreshCustomers(){state.customers=await api('/api/customers?as_of='+encodeURIComponent(asOf()));renderCustomers();}
function renderCustomers(){
 const q=$('#search').value.trim().toLowerCase();
 const filter=state.customerFilter||'all';
 const filters={all:c=>true,reply:c=>c.pending_reply,follow:c=>!c.pending_reply&&c.followup?.status==='ready'&&!c.outreach,held:c=>!c.pending_reply&&!!c.outreach};
 const filterLabels={all:'全部',reply:'待回复',follow:'可跟进',held:'暂缓'};
 $('#customer-count').textContent=state.customers.length+' 位';
 if($('#client-filters')){
  $('#client-filters').innerHTML=Object.entries(filterLabels).map(([key,label])=>`<button class="${key===filter?'selected':''}" data-client-filter="${key}">${label}<span>${state.customers.filter(filters[key]).length}</span></button>`).join('');
  $$('[data-client-filter]').forEach(b=>b.onclick=()=>{state.customerFilter=b.dataset.clientFilter;renderCustomers();});
 }
 const visible=state.customers.filter(c=>filters[filter](c)&&(c.name+c.summary).toLowerCase().includes(q));
 $('#clients').innerHTML=visible.map(c=>`<button class="customer-row ${c.id===state.customer?'selected':''}" data-customer="${esc(c.id)}"><span class="initial">${esc(c.name[0])}</span><span class="customer-main"><span class="customer-line"><strong>${esc(c.name)}</strong>${c.last_message?`<time>${dateLabel(c.last_message.occurred_at).split(' ')[0]}</time>`:''}</span><p>${esc(c.last_message?.text||c.summary||'还没有对话')}</p><span class="row-status">${esc(c.pending_reply?'待回复':['reviewing','queued'].includes(c.followup?.status)?'正在准备':c.followup?.status==='needs_attention'?'需要核实':c.outreach?.action==='no_action'?'暂不主动联系':c.outreach?'等待合适时机':c.followup?.status==='ready'?'可跟进':c.followup?.status==='waiting_event'?'等待关系变化':'关系跟进')}</span></span></button>`).join('')||'<p class="empty">没有匹配的客户。</p>';
 $$('#clients [data-customer]').forEach(b=>b.onclick=()=>selectCustomer(b.dataset.customer).catch(error));
}
async function selectCustomer(id){
 const version=++customerLoadVersion;
 clearError();state.customer=id;localStorage.setItem('virel-current-customer',id);
 for(const root of [$('#conversation'),$('#compose')]){root.inert=true;root.setAttribute('aria-busy','true');}
 renderCustomers();
 let detail;try{detail=await api('/api/customers/'+id+'?view=workspace&as_of='+encodeURIComponent(asOf()));}
 catch(e){
  if(state.customer!==id||version!==customerLoadVersion)return;
  state.detail=null;state.draft=null;
  $('#conversation').innerHTML=`<div class="load-error" role="alert"><h2>客户资料未能加载</h2><p>${esc(e.message)}</p><button id="retry-customer">重试</button></div>`;
  $('#compose').innerHTML='';delete $('#compose').dataset.customer;
  for(const root of [$('#conversation'),$('#compose')]){root.inert=false;root.removeAttribute('aria-busy');}
  $('#retry-customer').onclick=()=>selectCustomer(id);return;
 }
 if(state.customer!==id||version!==customerLoadVersion)return;
 state.detail=detail;state.draft=detail.drafts[0]||null;renderConversation();renderCompose();
 for(const root of [$('#conversation'),$('#compose')]){root.inert=false;root.removeAttribute('aria-busy');}
}
function messageDraft(id){try{return JSON.parse(localStorage.getItem('virel-message-'+id))||{text:'',actor:'client'};}catch{return {text:'',actor:'client'};}}
let timelineObserver=null;
function watchTimeline(list){
 timelineObserver?.disconnect();let pinned=true;
 list.scrollTop=list.scrollHeight;
 list.addEventListener('scroll',()=>{pinned=list.scrollHeight-list.clientHeight-list.scrollTop<25;},{passive:true});
 timelineObserver=new ResizeObserver(()=>{if(pinned)list.scrollTop=list.scrollHeight;});timelineObserver.observe(list);
}
function renderConversation(){const {customer,timeline}=state.detail;$('#conversation').innerHTML=`<div class="customer-head"><h2>${esc(customer.name)}</h2><button id="memory-button" class="quiet">客户资料 ↗</button></div><p class="subtle">${esc(customer.summary)}</p>${(state.detail.memory_updates||[]).map(u=>`<div class="memory-notice">${esc(u.status==='pending'?'正在整理客户资料…':u.status==='needs_review'?'客户资料有冲突，请核对。':'客户资料未更新，请重试。')}${u.status==='pending'?'':`<button class="quiet" data-retry-memory="${esc(u.event_id)}">重新处理</button>`}</div>`).join('')}<div class="timeline">${timeline.map(e=>`<article class="message ${esc(e.actor)}"><div class="meta">${e.kind==='note'?'补充记录':e.actor==='client'?esc(customer.salutation||customer.name):e.actor==='advisor'?'我':({purchase:'订单',service:'售后',appointment:'预约'}[e.kind]||'记录')} · ${dateLabel(e.occurred_at)}</div><div class="bubble">${esc(e.text)}</div></article>`).join('')}${conversationInputHTML(customer.id)}</div>`;bindConversationInput(customer.id);$('#memory-button').onclick=()=>showMemories('customer');$$('[data-retry-memory]').forEach(b=>b.onclick=async()=>{const cid=state.customer;b.disabled=true;try{const r=await post('/api/customers/'+cid+'/events/'+b.dataset.retryMemory+'/retry-memory',{});await watchMemory(r.job_id,cid);}catch(e){error(e);}finally{b.disabled=false;}});const list=$('.timeline');watchTimeline(list);$('#conversation').dataset.version=conversationVersion(state.detail);}
function noteDraft(id){return readLocal('virel-advisor-note-'+id)||{id:crypto.randomUUID(),text:''};}
function saveNoteDraft(id,note){localStorage.setItem('virel-advisor-note-'+id,JSON.stringify(note));}
let noteResizeObserver=null;
function sizeAdvisorNote(text){
 if(!text.clientWidth)return;
 const css=getComputedStyle(text),border=parseFloat(css.borderTopWidth)+parseFloat(css.borderBottomWidth);
 const limit=3*parseFloat(css.lineHeight)+parseFloat(css.paddingTop)+parseFloat(css.paddingBottom)+border;
 text.style.height='auto';text.style.height=Math.min(limit,Math.max(42,text.scrollHeight+border))+'px';
}
function renderCompose(){
 const owner=state.customer,note=noteDraft(owner);$('#compose').dataset.customer=owner;
 $('#compose').innerHTML=`<h2>回复建议</h2><div id="result"></div><form id="advisor-note-form" class="advisor-note-form"><div class="note-compose-row"><textarea id="advisor-note-text" rows="1" maxlength="4000" aria-label="补充内容" placeholder="补充要求、近况或跟进安排（选填）">${esc(note.text)}</textarea><button id="advisor-note-submit" type="submit" class="primary" ${!note.text.trim()||note.job_id?'disabled':''}>${note.job_id?'正在处理…':'提交'}</button></div><div id="advisor-note-status" role="status" aria-live="polite"></div></form>`;
 const text=$('#advisor-note-text'),button=$('#advisor-note-submit');text.disabled=!!note.job_id;
 noteResizeObserver?.disconnect();let width=0;
 noteResizeObserver=new ResizeObserver(()=>{if(text.clientWidth!==width){width=text.clientWidth;sizeAdvisorNote(text);}});
 noteResizeObserver.observe(text);sizeAdvisorNote(text);
 text.oninput=()=>{sizeAdvisorNote(text);$('#advisor-note-status').replaceChildren();let saved=noteDraft(owner);if(saved.text!==text.value&&!saved.job_id)saved={id:crypto.randomUUID(),text:text.value};saveNoteDraft(owner,saved);button.disabled=!text.value.trim();button.textContent='提交';};
 $('#advisor-note-form').onsubmit=async e=>{
  e.preventDefault();let saved=noteDraft(owner);if(!saved.text.trim())return;
  if(saved.job_id){watchAdvisorNote(owner);return;}
  $('#advisor-note-status').replaceChildren();saved.as_of=saved.as_of||asOf();saveNoteDraft(owner,saved);button.disabled=true;text.disabled=true;button.textContent='正在处理…';
  try{
   const response=await post('/api/customers/'+owner+'/notes',{id:saved.id,text:saved.text,as_of:saved.as_of});
   if(response.job_id){saved.job_id=response.job_id;saveNoteDraft(owner,saved);await watchAdvisorNote(owner);}
   else await finishAdvisorNote(owner,saved,response.note);
  }catch(err){if(state.customer===owner){$('#advisor-note-status').textContent=err.message;button.disabled=false;text.disabled=false;button.textContent='重试';}}
 };
 renderNoteReceipt();renderResult();if(note.job_id)watchAdvisorNote(owner);
 if(state.pending)resumeJob();
}
function renderNoteReceipt(){
 const el=$('#advisor-note-status');if(!el)return;
 if(noteDraft(state.customer).text.trim())return;
 el.replaceChildren();const note=state.detail.advisor_note;if(!note)return;
 el.innerHTML=note.status==='clarify'?`<p>${esc(note.question)}</p>`:note.status==='failed'?`<p>${esc(note.error||'处理未完成，请重试')}</p>${note.changes?.length?`<details><summary>已保存的内容</summary>${note.changes.map(c=>`<p>${esc(c)}</p>`).join('')}</details>`:''}`:note.status==='complete'&&note.changes?.length?`<details><summary>已更新资料</summary>${note.changes.map(c=>`<p>${esc(c)}</p>`).join('')}</details>`:'';
}
const noteWatchers=new Set();
async function finishAdvisorNote(owner,saved,note){
 const current=noteDraft(owner);if(current.id===saved.id)localStorage.removeItem('virel-advisor-note-'+owner);
 if(state.customer===owner){await selectCustomer(owner);if(note){state.detail.advisor_note=note;renderNoteReceipt();}}
}
async function watchAdvisorNote(owner){
 if(noteWatchers.has(owner))return;noteWatchers.add(owner);
 try{
  for(;;){const saved=noteDraft(owner);if(!saved.job_id)break;
   const job=await api('/api/jobs/'+saved.job_id);
   if(job.status==='finished'){await finishAdvisorNote(owner,saved,job.note);break;}
   if(['failed','interrupted'].includes(job.status)){delete saved.job_id;saveNoteDraft(owner,saved);throw Error(job.error||'补充未能处理，请重试。');}
   await new Promise(r=>setTimeout(r,1500));
  }
 }catch(err){
  if(state.customer===owner){$('#advisor-note-status').textContent=err.message;const saved=noteDraft(owner);$('#advisor-note-text').disabled=!!saved.job_id;const button=$('#advisor-note-submit');button.disabled=false;button.textContent=saved.job_id?'继续查看':'重试';}
 }finally{noteWatchers.delete(owner);}
}
function renderResult(){
 const el=$('#result');if(!el)return;
 const automatic=state.detail?.automatic||{},assessment=automatic.assessment;
 if(automatic.error){el.innerHTML=`<div class="decision"><strong>回复准备未完成</strong><p>${esc(automatic.error)}</p><button id="retry-advice">重试</button></div>`;$('#retry-advice').onclick=async()=>{const b=$('#retry-advice');b.disabled=true;try{await post('/api/customers/'+state.customer+'/retry-advice',{});await pollWorkspace();}catch(e){error(e);b.disabled=false;}};return;}
 if(['queued','reviewing'].includes(automatic.status)||(automatic.status==='scheduled'&&new Date(automatic.due_at)<=new Date(asOf()))){el.innerHTML='<div class="progress">正在准备回复…</div>';return;}
 if(automatic.paused||(assessment&&assessment.action!=='contact')){el.innerHTML=`<div class="decision"><strong>${esc(assessment?.purpose||'暂不联系')}</strong>${esc(assessment?.reason||'等待新的情况')}${automatic.status==='scheduled'?`<p>${dateLabel(automatic.due_at)} 再查看</p>`:''}</div>`;return;}
 if(!state.draft||state.draft.is_current===false&&!state.draft.can_copy){el.innerHTML='<div class="empty result-empty">等待新消息或跟进时间</div>';return;}
 const d=state.draft;
 if(d.status==='failed'){el.innerHTML=`<div class="decision"><strong>起草未完成</strong><details><summary>错误详情</summary>${esc(d.issues.join('；'))}</details></div>`;return;}
 const result=d.result;if(!result)return;
 const textOf=c=>d.display_texts?.[c.id]??c.text;
 el.innerHTML=`${result.action==='contact'?`<details class="decision decision-contact"><summary>${esc(result.purpose)}</summary><div>${esc(result.reason)}${d.context.contact_advice?`<p>${esc(d.context.contact_advice.reason)}</p>`:''}${result.revisit_when?`<p>下次：${esc(result.revisit_when)}</p>`:''}</div></details>`:`<div class="decision"><strong>${esc(result.purpose)}</strong>${esc(result.reason)}${result.revisit_when?`<p>下次：${esc(result.revisit_when)}</p>`:''}</div>`}
 ${result.candidates.map((c,i)=>`<article class="candidate ${c.id===result.recommended_id?'recommended':''}" data-candidate="${c.id}"><div class="candidate-top"><span class="candidate-number">0${i+1}</span><span>${esc(c.label)}</span>${d.adopted_candidate===c.id?'<span class="recommend">已采用</span>':c.id===result.recommended_id?'<span class="recommend">推荐</span>':''}</div><p class="candidate-text">${esc(textOf(c))}</p>${textOf(c)===c.text?`<details class="candidate-reason"><summary>适合之处</summary>${esc(c.reason)}</details>`:''}<div class="candidate-actions"><button data-rewrite="${c.id}" class="quiet">修改</button><button data-copy="${c.id}">复制</button><button data-use="${c.id}" class="${c.id===result.recommended_id?'primary':''}">${d.adopted_candidate===c.id?'已采用':'采用这句'}</button></div></article>`).join('')}
 <details class="generation-context"><summary>参考信息</summary>${contextHTML(d.context)}</details>`;
 $$('[data-copy]').forEach(button=>button.onclick=async()=>{
  button.disabled=true;
  try{await post('/api/drafts/'+d.id+'/check-copy',{as_of:asOf()});const candidate=result.candidates.find(c=>c.id===button.dataset.copy);
   try{await navigator.clipboard.writeText(textOf(candidate));toast('已复制。');}catch{toast('请选中正文手动复制。');}
  }catch(e){error(e);}finally{button.disabled=false;}
 });
 $$('[data-rewrite]').forEach(button=>button.onclick=()=>openDraftEditor(d.id,button.dataset.rewrite).catch(error));
 $$('[data-use]').forEach(button=>button.onclick=async()=>{
  button.disabled=true;try{const candidate=result.candidates.find(c=>c.id===button.dataset.use);const r=await saveSelection(d.id,candidate.id,textOf(candidate));d.adopted_candidate=candidate.id;if(state.draft?.id===d.id)renderResult();toast(r.note);}catch(e){error(e);button.disabled=false;}
 });
}
async function resumeJob(){
 if(!state.pending||resumeJob.running)return;resumeJob.running=true;
 try{while(state.pending){const task=state.pending,job=await api('/api/jobs/'+task.id);
  if(['finished','failed','interrupted'].includes(job.status)){state.pending=null;localStorage.removeItem('virel-pending');if(state.customer===task.customer)await selectCustomer(task.customer);break;}
  await new Promise(r=>setTimeout(r,2000));
 }}catch(e){error(e);}finally{resumeJob.running=false;}
}
let workspacePolling=false;
function conversationVersion(detail){return JSON.stringify([detail.customer,detail.timeline.map(e=>[e.id,e.text]),detail.memories.map(m=>[m.id,m.revision]),detail.memory_updates]);}
async function pollWorkspace(){
 if(workspacePolling||!state.ready)return;workspacePolling=true;
 const owner=state.customer,version=customerLoadVersion;
 try{
  await refreshCustomers();if(state.view!=='workspace'||!owner||state.customer!==owner||version!==customerLoadVersion||$('#conversation').inert)return;
  const detail=await api('/api/customers/'+owner+'?view=workspace&as_of='+encodeURIComponent(asOf()));if(state.customer!==owner||version!==customerLoadVersion||state.view!=='workspace')return;
  const oldResult=JSON.stringify([state.detail?.automatic,state.draft]),oldNote=JSON.stringify(state.detail?.advisor_note);
  state.detail=detail;state.draft=detail.drafts[0]||null;
  if($('#conversation').dataset.version!==conversationVersion(detail)&&!document.activeElement?.matches('[data-new-text],[data-new-actor]'))renderConversation();
  if(oldResult!==JSON.stringify([detail.automatic,state.draft]))renderResult();
  if((oldNote!==JSON.stringify(detail.advisor_note)||oldResult!==JSON.stringify([detail.automatic,state.draft]))&&!noteDraft(owner).job_id)renderNoteReceipt();
 }catch(e){if(state.customer===owner&&version===customerLoadVersion&&state.view==='workspace')error(Error('暂时无法更新页面：'+e.message));}finally{workspacePolling=false;}
}
const memoryNames={contact_permission:'联系意愿',service_permission:'服务联系',contact_after:'约定联系时间',contact_reason:'联系依据',birthday:'生日',color:'颜色偏好',own_color:'本人颜色偏好',daily_carry:'日常随身物',carry:'当前用途',interest:'关注的商品',recipient:'收礼人需求',gift_recipient:'礼物对象',availability:'时间安排',communication:'沟通安排',salutation:'称呼偏好',communication_tone:'沟通语气',communication_detail:'信息详略',communication_format:'呈现方式',budget:'预算',care:'护理与售后',visit_plan:'到店计划',event_interest:'活动兴趣','voice.length':'回复长度','voice.greeting':'称呼习惯','voice.invitation':'收尾习惯'};
function memoryAvailable(m){const clock=m.scope==='customer'?new Date(asOf()):new Date();return m.status==='active'&&new Date(m.effective_at)<=clock&&(!m.expires_at||new Date(m.expires_at)>clock);}
function shuffle(a){const result=[...a];for(let i=result.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[result[i],result[j]]=[result[j],result[i]];}return result;}
function usedPreferencesHTML(context){
 const memories=context.advisor_preferences||[],customerRules=context.customer_preferences||[],history=context.customer_reply_history||[],selection=context.context_selection;
 if(!selection&&!memories.length&&!customerRules.length&&!history.length)return '';
 const rules=memories.filter(m=>m.kind==='rule'),examples=memories.filter(m=>m.kind==='example');
 return `<div class="context-used"><p><strong>本次参考</strong></p>${!memories.length&&!customerRules.length&&!history.length?'<p>按品牌默认风格起草。</p>':''}${customerRules.map(m=>`<p><span class="subtle">本客户 · ${esc(scopeName(m.applicability))}</span><br>${esc(m.value)}</p>`).join('')}${rules.map(m=>`<p><span class="subtle">我的表达 · ${esc(scopeName(m.applicability))}</span><br>${esc(m.value)}</p>`).join('')}${history.length?`<details><summary>本客户过往采用 ${history.length}</summary>${history.map(r=>`<p><span class="subtle">${dateLabel(r.as_of)} · 采用草稿</span><br>${esc(r.text)}</p>`).join('')}</details>`:''}${examples.length?`<details><summary>其他表达例子 ${examples.length}</summary>${examples.map(m=>`<p><span class="subtle">${esc(scopeName(m.applicability))}</span><br>${esc(m.value)}</p>`).join('')}</details>`:''}</div>`;
}
function contextHTML(context){
 const messages=context.messages||context.events?.filter(e=>e.kind==='message'&&!e.redacted_for_generation)||[];
 const facts=context.facts||context.memories||[];
 const products=context.products||[];
 return `<div class="context-facts">${context.as_of?`<p class="subtle">情境：${dateLabel(context.as_of)}</p>`:''}${messages.slice(-4,-1).map(e=>`<p><strong>${e.actor==='client'?'客户':'顾问'}：</strong>${esc(e.text)}</p>`).join('')}${facts.filter(m=>m.key!=='contact_permission').slice(0,5).map(m=>`<p>${esc(memoryNames[m.key]||'已知情况')}：${esc(memoryContent(m))}</p>`).join('')}${products.map(p=>`<p>${esc(p.name)} · ¥${Number(p.price_cny).toLocaleString()}</p>`).join('')}${context.occasion?`<p>${esc(context.occasion)}</p>`:''}${usedPreferencesHTML(context)}</div>`;
}
async function loadCalibration(){
 state.cards=await api('/api/calibration');state.cardIndex=0;renderCalibration();
}
async function switchView(view){const menu=$('.workspace-tools');if(menu)menu.open=false;clearError();state.view=view;$$('.view').forEach(el=>el.classList.toggle('hidden',el.id!==view));$$('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));if(view==='preferences')await renderPreferences();if(view==='calibration')await loadCalibration();if(view==='workspace'&&state.customer)await selectCustomer(state.customer);}
$('#brand-home').onclick=()=>showBrand();
function syncClockControls(){
 $('#as-of').value=dateLocal(asOf());
}
async function changeClock(live,time){
 const field=$('#as-of');field.disabled=true;
 try{state.bootstrap.clock=await post('/api/clock',{as_of:time,live});syncClockControls();await refreshCustomers();if(state.customer)await selectCustomer(state.customer);}
 catch(e){syncClockControls();error(e);}
 finally{field.disabled=false;}
}
$$('[data-view]').forEach(b=>b.onclick=()=>switchView(b.dataset.view).catch(error));$('#search').oninput=renderCustomers;$('#close-drawer').onclick=()=>$('#drawer').close();$('#as-of').onchange=()=>changeClock(!$('#as-of').value,$('#as-of').value?$('#as-of').value+':00+08:00':new Date().toISOString());
(async()=>{try{state.bootstrap=await api('/api/bootstrap');syncClockControls();await refreshCustomers();const saved=localStorage.getItem('virel-pending');if(saved){try{state.pending=JSON.parse(saved);}catch{localStorage.removeItem('virel-pending');}}const validCustomer=id=>state.customers.some(c=>c.id===id)?id:null;let editorCustomer=null;const editorId=localStorage.getItem('virel-open-editor');if(editorId){try{editorCustomer=(await api('/api/drafts/'+encodeURIComponent(editorId))).customer_id;}catch{localStorage.removeItem('virel-open-editor');}}const initial=validCustomer(editorCustomer)||validCustomer(state.pending?.customer)||validCustomer(localStorage.getItem('virel-current-customer'))||validCustomer('zhou')||state.customers[0]?.id;if(initial)await selectCustomer(initial);else{$('#conversation').innerHTML='<div class="empty">添加客户后，开始记录对话。</div>';$('#compose').innerHTML='';}state.ready=true;if(new URLSearchParams(location.search).get('view')==='preferences')await switchView('preferences');}catch(e){error(e);$('#clients').innerHTML='<div class="empty">客户资料加载失败，请检查服务后刷新。</div>';}})();

function reviewHTML(){
 const f=state.customers.find(c=>c.id===state.customer)?.followup,a=f?.review?.assessment;
 if(!f)return '';
 return `<details class="review-card"><summary>关系跟进 · ${esc({scheduled:'已安排',reviewing:'正在复查',ready:'可联系',waiting_event:'等待变化',needs_attention:'待核实'}[f.status]||f.status)}</summary>${a?`<p>${esc(a.reason)}</p>${a.revisit_when?`<small>下次：${esc(a.revisit_when)}</small>`:''}`:'<p>待复查</p>'}${f.last_error?`<small>${esc(f.last_error)}</small>`:''}</details>`;
}
async function awaitTask(id){for(;;){const job=await api('/api/jobs/'+id);if(job.status==='finished')return job;if(['failed','interrupted'].includes(job.status))throw Error(job.error||'任务中断，请查已有记录。');await new Promise(r=>setTimeout(r,1500));}}
async function watchMemory(id,customerId){const job=await awaitTask(id);if(job.memory_update?.status==='complete')toast('客户资料已更新。');await refreshCustomers();if(state.customer===customerId)await selectCustomer(customerId);}
$('#review-all').onclick=async()=>{const button=$('#review-all');button.disabled=true;button.textContent='正在复查…';try{const r=await post('/api/operations/review',{as_of:asOf()});await awaitTask(r.job_id);await refreshCustomers();if(state.customer)await selectCustomer(state.customer);toast('已检查到期跟进');}catch(e){error(e);}finally{button.disabled=false;button.textContent='检查到期跟进';}};
setInterval(()=>{if(state.bootstrap&&!document.hidden){if(state.bootstrap.clock.live&&document.activeElement!==$('#as-of')&&!$('#as-of').disabled)$('#as-of').value=dateLocal(new Date());pollWorkspace();}},3000);

$('#add-customer').onclick=()=>{openDrawer('<h1>新建客户</h1><form id="new-customer-form"><label>姓名<input id="new-customer-name" required maxlength="60"></label><label>偏好称呼<input id="new-customer-salutation" maxlength="60" placeholder="可以留空"></label><button class="primary" type="submit">建立客户档案</button></form>');$('#new-customer-form').onsubmit=async e=>{e.preventDefault();try{const c=await post('/api/customers',{name:$('#new-customer-name').value,salutation:$('#new-customer-salutation').value});$('#drawer').close();await refreshCustomers();await selectCustomer(c.id);toast('档案已建立，可以录入第一条客户消息。');}catch(e){error(e);}};};
