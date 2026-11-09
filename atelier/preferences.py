"""Lightweight explicit questions + scoped examples learned from real selections."""
import uuid
from collections import defaultdict
from .models import MemoryInput, PreferenceAnswer
from .store import now, encode, Conflict

QUESTIONS = [
 {'id':'length','key':'voice.length','title':'一般情况下，您希望回复多长？','options':['一两句，把重点说清楚','可以多一点，但每句都有用','看场景，不设固定长度']},
 {'id':'greeting','key':'voice.greeting','title':'熟悉的客户，开头怎么称呼更像您？','options':['自然带上客户偏好的称呼','接着聊，不必每次称呼','不确定，先看具体对话']},
 {'id':'invitation','key':'voice.invitation','title':'没有必须追问的事时，您习惯怎样收尾？','options':['说完就好，不必每次提问','给一个轻松的可选下一步','由当时的沟通目的决定']},
]


def question_rule(question, answer):
    """Keep the original question's subject and conditions in a standalone rule."""
    rules={
        'length':{
            '一两句，把重点说清楚':'回复长度：通常用一两句，把重点说清楚。',
            '可以多一点，但每句都有用':'回复长度：可以适当展开，但每句话都应有实际内容。',
            '看场景，不设固定长度':'回复长度：根据具体场景决定长短，不设固定字数或句数。'},
        'greeting':{
            '自然带上客户偏好的称呼':'称呼方式：与熟悉的客户交流时，自然使用客户偏好的称呼。',
            '接着聊，不必每次称呼':'称呼方式：与熟悉的客户继续聊天时，自然承接上文，不必每条消息都重复称呼。',
            '不确定，先看具体对话':'称呼方式：没有固定偏好，先结合具体对话判断如何称呼。'},
        'invitation':{
            '说完就好，不必每次提问':'收尾方式：没有必须追问的事项时，说清楚后自然结束，不必每次提问。',
            '给一个轻松的可选下一步':'收尾方式：没有必须追问的事项时，可以给客户一个轻松、可选择的下一步。',
            '由当时的沟通目的决定':'收尾方式：没有必须追问的事项时，根据本次沟通目的选择结束方式，不固定要求提问或给出下一步建议。'}
    }
    return rules[question['id']].get(answer,'关于“'+question['title']+'”，偏好是：'+answer)


def answer_question(store, advisor_id, answer: PreferenceAnswer):
    question = next(q for q in QUESTIONS if q['id'] == answer.question_id)
    old = next((m for m in store.memories('advisor',advisor_id,True) if m['key']==question['key']),None)
    memory = store.put_memory(MemoryInput(scope='advisor',owner_id=advisor_id,key=question['key'],
        value=question_rule(question,answer.answer),provenance='manual',source='偏好问答：'+question['title'],effective_at=now(),
        applicability=answer.applicability),expected_revision=old['revision'] if old else None)
    with store.connect() as db:
        db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?) ON CONFLICT(advisor_id,question_id) DO UPDATE SET state=excluded.state, answer=excluded.answer, answered_at=excluded.answered_at',
            (advisor_id,answer.question_id,'answered',answer.answer,now(),now()))
    return memory


def question_states(store, advisor_id):
    with store.connect() as db:
        states={r['question_id']:dict(r) for r in db.execute('SELECT * FROM questions WHERE advisor_id=?',(advisor_id,))}
    return [q | {'state':states.get(q['id'],{}).get('state','unanswered'),
                 'answer':states.get(q['id'],{}).get('answer')} for q in QUESTIONS]


def skip_question(store, advisor_id, question_id):
    if question_id not in [q['id'] for q in QUESTIONS]:
        raise ValueError('未知问题')
    with store.connect() as db:
        db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?) ON CONFLICT(advisor_id,question_id) DO UPDATE SET state=excluded.state',
                   (advisor_id,question_id,'skipped',None,now(),None))


def add_example(store, data, advisor_id='default'):
    """A manual example is a reference, never an inferred permanent style rule."""
    from .models import MemoryInput
    if not data.text.strip() or not data.applicability.strip():raise ValueError('请填写参考表达和适用条件。')
    value=('使用情境：'+data.context+'\n参考表达：'+data.text) if data.context else data.text
    row=MemoryInput(scope='advisor',owner_id=advisor_id,key='manual_example.'+data.id,
        value=value,provenance='manual',source='手动添加表达例子',effective_at=now(),
        applicability=data.applicability).model_dump()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM memories WHERE scope=? AND owner_id=? AND key=?',
                       ('advisor',advisor_id,row['key'])).fetchone()
        if old:
            if old['kind']!='example' or old['value']!=value or old['applicability']!=data.applicability:
                raise Conflict('这个例子已保存过其他内容，请重新打开后编辑。')
            return dict(old)
        row.update(id=uuid.uuid4().hex,kind='example',status='active',revision=1,human_locked=1,
                   created_at=now(),updated_at=now())
        keys=list(row)
        db.execute('INSERT INTO memories ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',
                   [row[k] for k in keys])
        store._audit(db,row,None,'create_example')
    return row


def add_rule(store, data, advisor_id='default'):
    value=data.text.strip();scope=data.applicability.strip()
    if not value or not scope:raise ValueError('请填写表达要求和适用条件。')
    key='manual_rule.'+data.id
    old=next((m for m in store.memories('advisor',advisor_id,True) if m['key']==key),None)
    if old:
        if old['value']==value and old['applicability']==scope:return old
        raise Conflict('本次新增已保存，请在已有要求中修改。')
    return store.put_memory(MemoryInput(scope='advisor',owner_id=advisor_id,key=key,
        value=value,applicability=scope,source='手动添加表达要求',provenance='manual',effective_at=now()))


def feedback(store, advisor_id, draft_id, mode, outcome, candidate_id, display_order, reason='', feedback_id=None,
             *, selected_text=None, learning_scope='example', rule_text='', applicability='', expected_revision=None):
    """Save a revisable decision and its scoped memory in ONE transaction.

    Explicit memory edits win over feedback refreshes. A retry is idempotent even
    if the original response was lost. Anonymous votes never create memories.
    """
    import json
    from .models import FeedbackInput
    data=FeedbackInput(draft_id=draft_id,mode=mode,outcome=outcome,candidate_id=candidate_id,
        display_order=display_order,reason=reason,feedback_id=feedback_id,selected_text=selected_text,
        learning_scope=learning_scope,rule_text=rule_text,applicability=applicability,expected_revision=expected_revision)
    draft=store.draft(data.draft_id)
    candidates=(draft.get('result') or {}).get('candidates',[])
    valid=[c['id'] for c in candidates]
    if len(valid)!=3 or sorted(data.display_order)!=sorted(valid):
        raise ValueError('展示顺序与候选不一致。')
    if data.outcome=='choice' and data.candidate_id not in valid:
        raise ValueError('未选择有效候选。')
    if data.outcome!='choice' and (data.candidate_id is not None or data.selected_text is not None):
        raise ValueError('未选择时不能附带候选或编辑稿。')
    if data.outcome=='no_contact' and data.learning_scope!='once':
        raise ValueError('暂不联系只记录本次判断；长期规则请在表达要求中单独确认。')
    is_rule=data.learning_scope in ('rule','customer_rule')
    if is_rule and (not data.rule_text.strip() or data.mode=='blind'):
        raise ValueError('请填写需要记住的明确偏好；匿名比较不学习偏好。')
    if not is_rule and data.rule_text:
        raise ValueError('只有确认长期偏好时才能提交偏好内容。')
    selected=next((c['text'] for c in candidates if c['id']==data.candidate_id),None)
    selected=data.selected_text if data.selected_text is not None else selected
    scenario=draft['result']['scenario']
    scope=data.applicability or scenario
    fid=data.feedback_id or uuid.uuid5(uuid.NAMESPACE_URL,advisor_id+':'+mode+':'+draft_id).hex
    fields={'id':fid,'advisor_id':advisor_id,'draft_id':draft_id,'mode':data.mode,'outcome':data.outcome,
        'candidate_id':data.candidate_id,'display_order':encode(data.display_order),'reason':data.reason,
        'selected_text':selected,'learning_scope':data.learning_scope,'rule_text':data.rule_text,'applicability':scope}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT * FROM feedback WHERE id=?',(fid,)).fetchone()
        old=dict(prior) if prior else None
        if old:
            if any(old[k]!=fields[k] for k in ('advisor_id','draft_id','mode')):
                raise Conflict('反馈编号已用于其他记录。')
            if all(old[k]==v for k,v in fields.items()):
                return {'saved':True,'duplicate':True,'learned':False,'feedback':old,'note':'反馈已保存。'}
            if data.expected_revision!=old['revision']:
                raise Conflict('反馈已变化，请读取最新记录后修改。')
        elif data.expected_revision is not None:
            raise Conflict('原反馈不存在，请刷新。')
        current=fields | {'created_at':old['created_at'] if old else now(),'updated_at':now(),
            'revision':old['revision']+1 if old else 1,'memory_id':old['memory_id'] if old else None,
            'memory_revision':old['memory_revision'] if old else None}
        memory=None
        if current['memory_id']:
            row=db.execute('SELECT * FROM memories WHERE id=?',(current['memory_id'],)).fetchone()
            memory=dict(row) if row else None
        last_action=db.execute('SELECT action FROM memory_audit WHERE memory_id=? ORDER BY revision DESC LIMIT 1',(memory['id'],)).fetchone()[0] if memory else None
        mutable=not memory or (memory['revision']==current['memory_revision'] and (memory['status']=='active' or last_action=='feedback_withdraw'))
        learn=data.mode=='calibration' and data.learning_scope!='once' and (
            is_rule or data.outcome=='choice' or (data.outcome=='none' and bool(data.reason)))
        note='反馈已保存。'; learned=False
        if mutable and learn:
            messages=[e for e in draft['context'].get('events',[]) if e['kind']=='message' and not e.get('redacted_for_generation')]
            evidence={'场景':scenario,'当时客户消息':next((e['text'][:200] for e in reversed(messages) if e['actor']=='client'),''),
                '选择':data.outcome,'采用正文':selected,'说明':data.reason}
            if data.outcome=='none':evidence['未采用的正文']=[c['text'] for c in candidates]
            # Keep conversational purpose and nearby turns, not facts copied
            # into permanent advisor rules. Saved snapshots remain inspectable.
            nearby='\n'.join(('客户：' if e['actor']=='client' else '顾问：')+e['text'][:250] for e in messages[-3:])
            sample='当时目的：'+draft['result'].get('purpose','')+'\n'+nearby+'\n'+('采用表达：'+selected if selected else '此次未选用任何一句。')
            if data.reason:sample+='\n说明：'+data.reason
            if data.outcome=='none':sample+='\n未采用的表达：'+' / '.join(c['text'] for c in candidates)
            value=data.rule_text if is_rule else sample
            customer_rule=data.learning_scope=='customer_rule'
            item=MemoryInput(scope='customer' if customer_rule else 'advisor',owner_id=draft['customer_id'] if customer_rule else advisor_id,key='feedback.'+fid,value=value,
                provenance='manual' if is_rule else 'inferred',
                source='反馈 '+fid,effective_at=draft['request']['as_of'] if customer_rule else now(),applicability=scope).model_dump()
            item.update(id=memory['id'] if memory else uuid.uuid4().hex,status='active',
                kind='rule' if is_rule else 'example',
                revision=memory['revision']+1 if memory else 1,human_locked=int(is_rule),
                created_at=memory['created_at'] if memory else now(),updated_at=now())
            if memory:
                keys=[k for k in item if k!='id']
                db.execute('UPDATE memories SET '+','.join(k+'=?' for k in keys)+' WHERE id=?',[item[k] for k in keys]+[item['id']])
            else:
                keys=list(item)
                db.execute('INSERT INTO memories ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',[item[k] for k in keys])
            store._audit(db,item,memory,'feedback')
            current.update(memory_id=item['id'],memory_revision=item['revision'])
            from .retrieval import SCENES
            learned=True;note='已保存本客户的沟通要求。' if customer_rule else '已保存表达要求。' if is_rule else '已保存例子 · '+SCENES.get(scope,scope)
        elif mutable and memory and not learn:
            updated=memory | {'status':'disabled','revision':memory['revision']+1,'updated_at':now()}
            db.execute("UPDATE memories SET status='disabled',revision=?,updated_at=? WHERE id=?",(updated['revision'],updated['updated_at'],memory['id']))
            store._audit(db,updated,memory,'feedback_withdraw')
            current.update(memory_revision=updated['revision'])
            note='反馈已保存，原学习样本已停用。'
        elif not mutable:
            note='反馈已保存；保留您对记忆的手动修改或停用。'
        if data.mode=='blind':note='比较已记录，不改变表达偏好。'
        keys=list(current)
        if old:
            fields_to_update=[k for k in keys if k!='id']
            db.execute('UPDATE feedback SET '+','.join(k+'=?' for k in fields_to_update)+' WHERE id=?',
                [current[k] for k in fields_to_update]+[fid])
        else:
            db.execute('INSERT INTO feedback ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',[current[k] for k in keys])
        db.execute('INSERT INTO feedback_audit(feedback_id,revision,before_json,after_json,created_at) VALUES(?,?,?,?,?)',
            (fid,current['revision'],encode(old) if old else None,encode(current),now()))
    return {'saved':True,'duplicate':False,'learned':learned,'feedback':current,'note':note}


def feedback_history(store, advisor_id='default'):
    with store.connect() as db:
        return [dict(r) | {'display_order':__import__('json').loads(r['display_order'])} for r in db.execute(
            'SELECT * FROM feedback WHERE advisor_id=? ORDER BY COALESCE(updated_at,created_at) DESC',(advisor_id,))]


def preference_memories(store, advisor_id='default'):
    """Presentation evidence is separate from the memory that actually guides generation."""
    history=feedback_history(store,advisor_id)
    by_memory={f['memory_id']:f for f in history if f['memory_id']}
    result=[]
    for memory in store.memories('advisor',advisor_id,True):
        if memory['status']=='deleted':continue
        row=by_memory.get(memory['id'])
        evidence=None
        if row:
            draft=store.draft(row['draft_id'])
            messages=[e for e in draft['context'].get('events',[]) if e['kind']=='message' and e['actor']=='client']
            evidence={'customer_message':messages[-1]['text'] if messages else '',
                'selected_text':row['selected_text'],'reason':row['reason'],'outcome':row['outcome'],
                'draft_id':row['draft_id'],'feedback_id':row['id'],
                'matches_current':row['memory_revision']==memory['revision']}
        result.append(memory | {'evidence':evidence})
    return result


def contextual_question(store, advisor_id='default'):
    """Ask only when a real, still-active feedback reason has unclear future scope.

    No personality inference, no model-generated trait and no question for a
    deliberately once-only choice. The operator edits the proposed rule first.
    """
    with store.connect() as db:
        answered={r[0] for r in db.execute('SELECT question_id FROM questions WHERE advisor_id=?',(advisor_id,))}
    history=feedback_history(store,advisor_id)
    resolved={(r['reason'].strip(),r['applicability']) for r in history if 'feedback:'+r['id'] in answered}
    # A single choice is evidence, not a lasting trait. Ask only after the same
    # explicit reason recurs in separate drafts; there is no forced questionnaire.
    usable={m['id']:m for m in store.memories('advisor',advisor_id,True)
        if m['kind']=='example' and m['status']=='active' and not m['human_locked']
        and m['effective_at']<=now() and (not m['expires_at'] or m['expires_at']>now())}
    occurrences=defaultdict(set)
    for r in history:
        if r['mode']=='calibration' and r['learning_scope']=='example' and r['outcome'] in ('choice','none') and r['memory_id'] in usable:
            occurrences[(r['reason'].strip(),r['applicability'])].add(r['draft_id'])
    repeat={key:len(ids) for key,ids in occurrences.items()}
    for row in history:
        if row['mode']!='calibration' or row['learning_scope']!='example' or not row['reason'].strip():continue
        if row['outcome'] not in ('choice','none'):continue
        if row['memory_id'] not in usable:continue
        if (row['reason'].strip(),row['applicability']) in resolved:continue
        if repeat[(row['reason'].strip(),row['applicability'])]<2:continue
        memory=usable[row['memory_id']]
        if memory['status']!='active' or memory['human_locked']:continue
        qid='feedback:'+row['id']
        if qid in answered:continue
        return {'id':qid,'feedback_id':row['id'],'revision':row['revision'],
                'title':'这条意见，以后也适用吗？','quote':row['reason'],
                'scenario':row['applicability'],'draft_id':row['draft_id'],
                'occurrences':repeat[(row['reason'].strip(),row['applicability'])]}
    return None


def answer_contextual_question(store, advisor_id, question_id, action, rule_text='', applicability='', expected_revision=None):
    question=contextual_question(store,advisor_id)
    if not question or question['id']!=question_id or question['revision']!=expected_revision:
        raise Conflict('这条问题已处理或来源已变化，请刷新偏好页。')
    if action not in ('remember','keep_example','skip'):raise ValueError('问题操作不正确。')
    if action=='remember':
        if not isinstance(rule_text,str) or not rule_text.strip():raise ValueError('请确认要记住的偏好内容。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM feedback WHERE id=? AND advisor_id=?',(question['feedback_id'],advisor_id)).fetchone()
        if not row or row['revision']!=question['revision']:raise Conflict('原选择已变化，请刷新。')
        if db.execute('SELECT 1 FROM questions WHERE advisor_id=? AND question_id=?',(advisor_id,question_id)).fetchone():
            raise Conflict('这条意见已处理，请刷新。')
        if action=='remember':
            # Add a confirmed rule without destroying the original example.
            item=MemoryInput(scope='advisor',owner_id=advisor_id,key='confirmed.'+row['id'],
                value=rule_text.strip(),provenance='manual',source='确认表达要求，来自反馈 '+row['id'],
                effective_at=now(),applicability=applicability or question['scenario']).model_dump()
            item.update(id=uuid.uuid4().hex,kind='rule',status='active',revision=1,human_locked=1,created_at=now(),updated_at=now())
            keys=list(item)
            db.execute('INSERT INTO memories ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',[item[k] for k in keys])
            store._audit(db,item,None,'confirm_preference')
        db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?) ON CONFLICT(advisor_id,question_id) DO NOTHING',
            (advisor_id,question_id,'skipped' if action=='skip' else 'answered',rule_text if action=='remember' else action,now(),now()))
    return {'saved':True,'note':'偏好已保存。' if action=='remember' else '已记录，不再重复询问。'}
