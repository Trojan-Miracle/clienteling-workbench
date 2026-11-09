"""Customer-specific requirements and adopted drafts, kept apart from sent events."""
import json
from .models import MemoryInput, instant
from .store import Conflict, now


def add_customer_rule(store, customer_id, data):
    return add_customer_memory(store,customer_id,data,kind='rule')


def add_customer_memory(store, customer_id, data, *, kind):
    if kind not in ('rule','fact'):raise ValueError('客户记忆类型无效。')
    store.customer(customer_id)
    text=data.text.strip(); condition=data.applicability.strip()
    if not text or not condition:raise ValueError('请填写内容和适用条件。')
    key=('communication_rule.' if kind=='rule' else 'customer_note.')+data.id
    old=next((m for m in store.memories('customer',customer_id,True) if m['key']==key),None)
    if old:
        if (old['kind']==kind and old['status']=='active' and old['value']==text
            and old['applicability']==condition and old['expires_at']==data.expires_at):return old
        raise Conflict('这条内容已保存过，请在已有记录上修改。')
    return store.put_memory(MemoryInput(scope='customer',owner_id=customer_id,key=key,value=text,
        provenance='manual',source='顾问为本客户设置的沟通要求' if kind=='rule' else '顾问补充的本客户偏好与资料',effective_at=data.as_of,
        expires_at=data.expires_at,applicability=condition),kind=kind)


def source_is_current(original, context):
    events={e['id']:e for e in context['events']}
    facts={m['id']:m for m in context['memories']}
    if any(m['id'] not in facts or m['revision']!=facts[m['id']]['revision']
           for m in original.get('memories',[]) if m.get('kind','fact')=='fact'):return False
    return not any(e['id'] not in events or events[e['id']].get('redacted_for_generation')
        or events[e['id']].get('partially_redacted') or events[e['id']]['text']!=e['text']
        for e in original.get('events',[]))


def eligible_advisor_examples(store, context, advisor_id, examples):
    """Do not reintroduce rejected history through the general example pool."""
    with store.connect() as db:
        sources={r['memory_id']:dict(r) for r in db.execute('''
            SELECT f.memory_id,f.memory_revision,f.outcome,d.customer_id,d.record
            FROM feedback f JOIN drafts d ON d.id=f.draft_id WHERE f.advisor_id=?''',(advisor_id,))}
    contexts={context['customer']['id']:context}; result=[]
    for m in examples:
        source=sources.get(m['id'])
        if not source:
            result.append(m);continue
        cid=source['customer_id']
        if cid not in contexts:contexts[cid]=store.context(cid,context['as_of'],advisor_id)
        if not source_is_current(json.loads(source['record'])['context'],contexts[cid]):continue
        # A human-edited example is a current reference, not the adopted text.
        # Its original source still cannot revive withdrawn customer facts.
        edited=m['human_locked'] and m['revision']!=source['memory_revision']
        if cid==context['customer']['id'] and source['outcome']=='choice' and not edited:continue
        result.append(m)
    return result


def adopted_replies(store, context, advisor_id):
    """Only this advisor/customer's still-usable choices; never an outbound log.

    Preserve withdrawal semantics: an edited/disabled learning source, a revoked
    fact, or a redacted source event must not return via an old draft snapshot.
    """
    with store.connect() as db:
        rows=db.execute('''SELECT f.*,d.record FROM feedback f JOIN drafts d ON d.id=f.draft_id
            JOIN memories m ON m.id=f.memory_id
            WHERE d.customer_id=? AND f.advisor_id=? AND f.mode='calibration'
              AND f.outcome='choice' AND f.learning_scope!='once'
              AND m.status='active' AND m.revision=f.memory_revision
            ORDER BY COALESCE(f.updated_at,f.created_at) DESC,f.id DESC''',
            (context['customer']['id'],advisor_id)).fetchall()
    results=[]
    for raw in rows:
        row=dict(raw); draft=json.loads(row['record']); original=draft['context']
        # Scenario time controls future leakage, even in a historical demo.
        if instant(draft['request']['as_of'])>context['as_of']:continue
        memory=store.memory(row['memory_id'])
        clock=context['as_of'] if memory['scope']=='customer' else now()
        if memory['effective_at']>clock or (memory['expires_at'] and memory['expires_at']<=clock):continue
        if not source_is_current(original,context):continue
        prior_events=original.get('events',[])
        text=row['selected_text']
        if not text:
            text=next((c['text'] for c in draft['result']['candidates'] if c['id']==row['candidate_id']),None)
        if not text:continue
        question=next((e['text'] for e in reversed(prior_events) if e['kind']=='message' and e['actor']=='client'),'')
        results.append({'id':row['id'],'revision':row['revision'],'draft_id':row['draft_id'],
            'memory_id':row['memory_id'],'customer_id':context['customer']['id'],
            'as_of':draft['request']['as_of'],'adopted_at':row['updated_at'] or row['created_at'],
            'kind':'adopted_draft','delivery_status':'unverified','customer_message':question,
            'text':text,'reason':row['reason'],'purpose':draft['result'].get('purpose',''),
            'applicability':row['applicability'] or draft['result']['scenario'],
            'value':'当时客户：'+question+'\n采用草稿：'+text+('\n选择意见：'+row['reason'] if row['reason'] else '')})
    return sorted(results,key=lambda r:(r['as_of'],r['adopted_at'],r['id']),reverse=True)


def select_adopted_replies(retriever, context, candidates):
    recent=candidates[:3]
    older,trace=retriever.select(context,candidates[3:])
    selected=recent+older[:3]
    return selected,{'eligible_count':len(candidates),'selected_ids':[r['id'] for r in selected],
        'recent_ids':[r['id'] for r in recent],'retrieved_ids':[r['id'] for r in older[:3]],
        'method':trace['method'],'policy':'本客户与当前顾问，最近3条加最多3条相关旧采用稿'}
