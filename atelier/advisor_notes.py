"""A free-text advisor entry, with bounded, validated local actions and receipts."""
import json
import threading
from collections import defaultdict
from typing import Literal
from pydantic import Field, ValidationError, field_validator
from lanxu.provider import ProviderError
from .models import Strict, DraftRequest, MemoryInput, Event, instant
from .store import Conflict, encode, now, CONTACT_KEYS
from .automatic import setting, save_setting


class NoteInput(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    customer_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    text: str = Field(min_length=1,max_length=4000)
    as_of: str
    @field_validator('as_of')
    @classmethod
    def time(cls,value):return instant(value)


class NoteMemory(Strict):
    scope: Literal['customer','advisor']='customer'
    kind: Literal['fact','rule']='fact'
    key: str = Field(default='note',pattern=r'^[a-zA-Z0-9_.-]{1,60}$')
    value: str = Field(min_length=1,max_length=2000)
    quote: str = Field(min_length=1,max_length=3000)
    replaces_id: str | None = None
    applicability: str = Field(default='所有情境',min_length=1,max_length=200)
    expires_at: str | None = None
    @field_validator('expires_at')
    @classmethod
    def time(cls,value):return instant(value) if value else None


class NoteEvent(Strict):
    kind: Literal['purchase','service','appointment']
    text: str = Field(min_length=1,max_length=2000)
    quote: str = Field(min_length=1,max_length=3000)
    product_id: str | None = None


class NotePlan(Strict):
    action: Literal['draft','rewrite','update','defer','pause','clarify']
    trigger: Literal['auto','reply','manual']='auto'
    instruction: str = Field(default='',max_length=1200)
    confirmed_facts: str = Field(default='',max_length=2000)
    memories: list[NoteMemory] = Field(default_factory=list,max_length=8)
    events: list[NoteEvent] = Field(default_factory=list,max_length=4)
    revisit_at: str | None = None
    clarification: str = Field(default='',max_length=400)
    @field_validator('revisit_at')
    @classmethod
    def time(cls,value):return instant(value) if value else None


class AdvisorNotes:
    def __init__(self,engine,automatic):
        self.engine=engine;self.store=engine.store;self.automatic=automatic
        self.locks=defaultdict(threading.Lock)

    def read(self,note_id):return setting(self.store,'advisor-note:'+note_id)

    def _save(self,record):
        save_setting(self.store,'advisor-note:'+record['input']['id'],record)
        save_setting(self.store,'advisor-last-note:'+record['input']['customer_id'],record['input']['id'])

    def latest(self,cid):
        note_id=setting(self.store,'advisor-last-note:'+cid)
        record=self.read(note_id) if note_id else None
        if not record:return None
        return self.public(record)

    @staticmethod
    def public(record):
        return {k:record.get(k) for k in ('status','summary','changes','question','error')} | {'id':record['input']['id'],'text':record['input']['text']}

    def run(self,payload):
        data=NoteInput.model_validate(payload)
        with self.locks[data.customer_id]:return self._run(data)

    def _run(self,data):
        self.store.customer(data.customer_id)
        record=self.read(data.id)
        if record and record['input']!=data.model_dump():raise Conflict('该补充编号已用于其他内容。')
        if record and record.get('status') in ('complete','clarify'):return {'note':self.public(record)}
        previous=self.latest(data.customer_id)
        record=record or {'input':data.model_dump(),'status':'planning','changes':[],'attempts':[],'applied':[],
            'clarification_context':previous if previous and previous['status']=='clarify' and previous['id']!=data.id else None}
        self._save(record)
        try:
            if not record.get('plan'):
                messages=[e for e in self.store.timeline(data.customer_id,data.as_of) if e['kind']=='message']
                request=DraftRequest(customer_id=data.customer_id,as_of=data.as_of,trigger='reply' if messages and messages[-1]['actor']=='client' else 'manual',intent=data.text[:1200])
                context=self.engine.context(request)
                current=self.store.drafts(data.customer_id,1)
                current=current[0] if current and self.engine.is_current(current[0],data.as_of,ignore_preferences=True,allow_clock_progress=True) else None
                record['original_candidates']=(current or {}).get('result',{}).get('candidates',[]) if (current or {}).get('result') else []
                record['base_memories']={m['id']:m for m in [*self.store.memories('customer',data.customer_id,True),*self.store.memories('advisor','default',True)]}
                system=(self.engine.prompt_dir/'advisor_note.txt').read_text()
                user={'advisor_input':data.text,'context':context,'current_reply':record['original_candidates'],
                    'previous_clarification':record.get('clarification_context')}
                record['planning_context']=user;record['planning_system']=system
                for attempt in range(2):
                    reply=self.engine.generator.complete(system,encode(user))
                    trace={'raw':reply.text,'metadata':reply.metadata};record['attempts'].append(trace)
                    try:
                        plan=NotePlan.model_validate_json(reply.text);self.validate(plan,data,record)
                        record['plan']=plan.model_dump();self._save(record);break
                    except (ValueError,ValidationError) as exc:
                        trace['error']=str(exc);self._save(record)
                        if attempt:raise ValueError('这条补充暂时无法处理，请换一种说法。') from None
                        user['repair']={'issues':str(exc),'previous':reply.text}
            plan=NotePlan.model_validate(record['plan'])
            if plan.action=='clarify':
                record.update(status='clarify',summary='',question=plan.clarification);self._save(record)
                return {'note':self.latest(data.customer_id)}
            record['status']='applying';self._save(record)
            # The raw advisor entry is an internal note, not a sent chat message.
            source_id='advisor-'+data.id
            if plan.memories or plan.events:
                self.store.add_event(Event(id=source_id,customer_id=data.customer_id,occurred_at=data.as_of,kind='note',actor='system',text=data.text,data={'advisor_note_id':data.id}))
            for i,m in enumerate(plan.memories):
                marker='memory-'+str(i)
                if marker in record['applied']:continue
                base=record['base_memories'].get(m.replaces_id)
                if not base and m.key in CONTACT_KEYS:
                    base=next((x for x in record['base_memories'].values() if x['scope']=='customer' and x['key']==m.key),None)
                key=base['key'] if base else m.key if m.key in CONTACT_KEYS else ('customer_note.' if m.scope=='customer' else 'advisor_note.')+data.id+'.'+str(i)
                candidates=self.store.memories(m.scope,data.customer_id if m.scope=='customer' else 'default',True)
                old=next((x for x in candidates if x['id']==base['id']),None) if base else next((x for x in candidates if x['key']==key),None)
                reused=old and old['value']==m.value and old['applicability']==m.applicability and old['source']=='顾问补充 '+data.id
                if not reused:
                    if base and (not old or old['revision']!=base['revision']):raise Conflict('这条资料刚被修改，请在客户资料中核对后重新补充。')
                    self.store.put_memory(MemoryInput(scope=m.scope,owner_id=data.customer_id if m.scope=='customer' else 'default',key=key,value=m.value,
                        provenance='manual',source='顾问补充 '+data.id,source_event_id=source_id if m.scope=='customer' else None,
                        source_quote=m.quote if m.scope=='customer' else None,effective_at=data.as_of if m.scope=='customer' else now(),
                        applicability=m.applicability,expires_at=m.expires_at),kind=m.kind,expected_revision=base['revision'] if base else None,memory_id=base['id'] if base else None)
                record['applied'].append(marker);record['changes'].append(m.quote if m.key in CONTACT_KEYS else m.value);self._save(record)
            for i,event in enumerate(plan.events):
                marker='event-'+str(i)
                if marker in record['applied']:continue
                self.store.add_event(Event(id=data.id+'-event-'+str(i),customer_id=data.customer_id,occurred_at=data.as_of,kind=event.kind,actor='system',text=event.text,data={'product_id':event.product_id,'advisor_note_id':data.id}))
                record['applied'].append(marker);record['changes'].append(event.text);self._save(record)
            if plan.action=='pause':
                self.automatic.pause(data.customer_id,plan.instruction or data.text);summary='已暂缓，等有新情况再判断'
            elif plan.action=='defer':
                self.automatic.schedule(data.customer_id,trigger='timer',intent=plan.instruction,advisor_note_id=data.id,due_at=plan.revisit_at,clear_pause=True)
                save_setting(self.store,'automatic:'+data.customer_id,{'paused':False,'draft_id':None,'assessment':{'action':'defer','purpose':'已调整跟进时间','reason':data.text,'revisit_when':plan.revisit_at}})
                summary='已安排到时再判断'
            elif plan.action=='rewrite' and not record['original_candidates']:
                summary='当前没有可修改的回复'
            else:
                self.automatic.schedule(data.customer_id,trigger=None if plan.trigger=='auto' else plan.trigger,intent=plan.instruction,advisor_note_id=data.id,clear_pause=True)
                self.automatic.tick();summary='已补充，正在更新回复' if record['changes'] else '正在更新回复'
            record.update(status='complete',summary=summary,error=None);self._save(record)
            return {'note':self.latest(data.customer_id)}
        except Exception as exc:
            record.update(status='failed',error=str(exc) if isinstance(exc,(ValueError,ProviderError)) else '处理未完成，请重试。')
            self._save(record);raise

    def validate(self,plan,data,record):
        if plan.confirmed_facts and plan.confirmed_facts not in data.text:raise ValueError('本次确认须逐字引用顾问补充，不得补造或改写。')
        if plan.action=='clarify':
            if not plan.clarification or plan.memories or plan.events or plan.confirmed_facts:raise ValueError('需要确认时不能同时修改资料。')
        if plan.action=='defer' and (not plan.revisit_at or plan.revisit_at<=data.as_of):raise ValueError('跟进时间必须明确且晚于当前时间。')
        products={p['id'] for p in self.engine.products}
        for m in plan.memories:
            base=record['base_memories'].get(m.replaces_id)
            key=base['key'] if base else m.key
            if m.quote not in data.text:raise ValueError('资料必须引用本次补充中的原话。')
            if m.scope=='advisor' and m.kind!='rule':raise ValueError('顾问偏好只能保存明确的表达要求。')
            if m.replaces_id:
                base=record['base_memories'].get(m.replaces_id)
                if not base or base['scope']!=m.scope or base['owner_id']!=(data.customer_id if m.scope=='customer' else 'default'):raise ValueError('不能修改其他客户或顾问的资料。')
                if base['kind']!=m.kind:raise ValueError('不能改变已有资料的类型。')
                if base['status']!='active':raise ValueError('不能恢复已停用或删除的资料。')
            if key in CONTACT_KEYS and (m.scope!='customer' or m.kind!='fact'):raise ValueError('联系边界属于当前客户。')
            if key=='contact_permission' and m.value not in ('allowed','no_marketing','do_not_contact','unknown'):raise ValueError('联系意愿值无效。')
            if key=='service_permission' and m.value not in ('allowed','denied'):raise ValueError('服务联系意愿值无效。')
            if key=='contact_after':instant(m.value)
            if m.expires_at and m.expires_at<=data.as_of:raise ValueError('失效时间不能早于本次补充。')
        for event in plan.events:
            if event.quote not in data.text:raise ValueError('业务进展必须引用本次补充的原话。')
            if event.product_id and event.product_id not in products:raise ValueError('业务记录引用了未知商品。')
