"""Extract explicit, quote-backed customer memory. Human edits remain authoritative."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import ValidationError
from lanxu.provider import ProviderError
from .models import Extraction, MemoryInput, instant
from .store import ROOT, CONTACT_KEYS, Conflict, Missing, now, encode
from .retrieval import ExampleRetriever, select_customer_facts


def validate_memories(extracted, event, event_id):
    validated=[]
    for proposal in extracted.memories:
        if proposal.quote not in event['text']:
            raise ValueError('记忆引用不是客户消息原文。')
        if proposal.key=='budget' and re.fullmatch(r'[\s\d.,，¥￥零一二三四五六七八九十百千万两人民币元块整]+',proposal.quote):
            raise ValueError('预算引用只有金额，不能区分标价、还价与预算；需保留明确约束原话。')
        if proposal.key=='birthday':
            dated=re.search(r'[0-9一二两三四五六七八九十]{1,3}月[0-9一二两三四五六七八九十]{1,3}[日号]|\d{1,2}[-/]\d{1,2}',proposal.quote)
            relative=re.search(r'(?:今天|明天|后天|昨天)(?:是|过)?(?:我(?:的)?|本人)?生日|(?:我|本人)(?:的)?生日(?:是|在)?(?:今天|明天|后天|昨天)',proposal.quote)
            if not dated and not relative:
                raise ValueError('生日引用没有明确日期；挑生日礼物是购物用途，不能当作生日日期。')
        if proposal.key=='service_permission':
            if proposal.value not in ('allowed','denied'):
                raise ValueError('服务联系许可值只能为allowed或denied，范围需保留在原话中。')
            value=proposal.value
        elif proposal.key=='contact_permission':
            if proposal.value not in ('allowed','no_marketing','do_not_contact'):
                raise ValueError('联系意愿值无效。')
            value=proposal.value
        elif proposal.key=='contact_after':
            value=instant(proposal.value)
            if value<=event['occurred_at']:raise ValueError('约定联系时间不在消息之后。')
        else:
            value=proposal.value
        memory=MemoryInput(scope='customer',owner_id=event['customer_id'],key=proposal.key,
            value=value,provenance='explicit',source='从客户原话提取：'+proposal.quote[:160],source_event_id=event_id,source_quote=proposal.quote,
            effective_at=event['occurred_at'],expires_at=proposal.expires_at,
            applicability=proposal.quote if proposal.key=='service_permission' else '所有情境')
        if memory.expires_at and memory.expires_at<=memory.effective_at:
            raise ValueError('失效时间早于事实生效时间。')
        validated.append(memory)
    controls=[m.key for m in validated if m.key in CONTACT_KEYS]
    if len(set(controls))!=len(controls):
        raise ValueError('同次提取存在重复的联系控制字段，请根据原话确认一个有效值。')
    targets=[p.replaces_id for p in extracted.memories if p.replaces_id]
    if len(set(targets))!=len(targets):
        raise ValueError('同次提取不能多次修改同一条记忆，请合成一个明确的更新。')
    return validated


def extract_event(store, provider, event_id, *, prompt_dir=None, retriever=None):
    with store.connect() as db:
        event=db.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
        old=db.execute('SELECT status,record FROM memory_extractions WHERE event_id=?',(event_id,)).fetchone()
    if not event:raise Missing(event_id)
    if old and old['status']=='complete':return json.loads(old['record'])
    event=store._event(event)
    record={'event_id':event_id,'created_at':now(),'status':'running','applied':[],'skipped':[],'issues':[]}
    if old:
        prior=json.loads(old['record'])
        record['previous_runs']=prior.pop('previous_runs',[])+[prior]
    if event['kind']!='message' or event['actor']!='client':
        record.update(status='complete',note='只有客户消息参与自动偏好提取。')
    else:
        memories=store.memories('customer',event['customer_id'],True)
        system=((prompt_dir or ROOT/'prompts/v6')/'extract.txt').read_text()
        # Never send deleted values back to the extraction model.
        current=store.context(event['customer_id'],event['occurred_at'])
        safe_event=next(e for e in current['events'] if e['id']==event_id)
        current['request']={'scenario':'conversation','intent':'','occasion':''}
        selected,selection=select_customer_facts(retriever or ExampleRetriever(),current)
        visible={m['id']:m for m in selected}
        context={'event':safe_event,'business_timezone':'Asia/Shanghai',
                 'event_local_time':datetime.fromisoformat(event['occurred_at']).astimezone(ZoneInfo('Asia/Shanghai')).isoformat(),
                 'memories':selected,'memory_selection':selection,
                 'protected_ids':[m['id'] for m in memories if m['human_locked'] or m['status']!='active'],
                 'protected_keys':[m['key'] for m in memories if m['key'] in CONTACT_KEYS
                                   and (m['human_locked'] or m['status']!='active')]}
        record.update(system=system,user=encode(context))
        try:
            validated=None
            record['attempts']=[]
            payload=context
            for attempt in (1,2):
                entry={'attempt':attempt,'system':system,'user':encode(payload)}
                record['attempts'].append(entry)
                try:
                    reply=provider.complete(system,entry['user'])
                    entry.update(raw=reply.text,metadata=reply.metadata)
                    record.update(raw=reply.text,metadata=reply.metadata)
                    extracted=Extraction.model_validate_json(reply.text)
                    validated=validate_memories(extracted,safe_event,event_id)
                    for p in extracted.memories:
                        if p.replaces_id:
                            prior=visible.get(p.replaces_id)
                            if not prior:raise ValueError('replaces_id必须是输入中本客户当前有效的记忆编号。')
                            if (p.key in CONTACT_KEYS or prior['key'] in CONTACT_KEYS) and p.key!=prior['key']:
                                raise ValueError('不能用普通偏好修改联系控制字段。')
                    entry['issues']=[]
                    break
                except (ValidationError,ValueError) as exc:
                    entry['issues']=[str(exc)[:500]]
                    if attempt==2:raise
                    payload=context | {'repair':{'previous':entry.get('raw'),'issues':entry['issues'],
                        'instruction':'只依据event原文修正结构或引用。contact_permission.value只能是allowed/no_marketing/do_not_contact；quote保留原话。不要猜补事实，不把旧输出当事实。返回完整JSON。'}}
                except ProviderError as exc:
                    entry['error']={'code':exc.code,'message':str(exc)}
                    raise
            conflicts=False
            for proposal,memory in zip(extracted.memories,validated):
                prior=next((m for m in memories if m['id']==proposal.replaces_id),None) if proposal.replaces_id else (
                    next((m for m in memories if m['key']==memory.key),None) if memory.key in CONTACT_KEYS else None)
                # Idempotence after a process interruption between saving a memory and the extraction receipt.
                if prior and prior['status']=='active' and all(prior[key]==getattr(memory,key) for key in ('value','applicability','expires_at')) and (
                    prior['human_locked'] or all(prior[key]==getattr(memory,key) for key in ('source_event_id','source_quote'))):
                    record['skipped'].append({'key':memory.key,'reason':'本条消息已记录。'})
                    continue
                try:
                    saved=store.put_memory(memory,expected_revision=prior['revision'] if prior else None,
                                           memory_id=prior['id'] if prior else None)
                    record['applied'].append({'id':saved['id'],'key':saved['key'],'revision':saved['revision']})
                except Conflict as e:
                    conflicts=True
                    record['skipped'].append({'key':memory.key,'reason':str(e)})
            record.update(status='needs_review' if conflicts else 'complete',note=Extraction.model_validate_json(record['raw']).note)
            if conflicts:record['issues'].append('新消息与人工修正或已删除记忆冲突；请核实后修改记忆，再重试本条更新。')
        except (ValidationError,ValueError) as e:
            record.update(status='failed',issues=['记忆提取未通过原文/结构检查：'+str(e)[:200]])
        except ProviderError as e:
            record.update(status='failed',issues=[str(e)],error_code=e.code,provider_failures=getattr(e,'failures',[]))
    with store.connect() as db:
        db.execute('INSERT INTO memory_extractions VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,record=excluded.record,created_at=excluded.created_at',
                   (event_id,record['status'],encode(record),record['created_at']))
    return record
