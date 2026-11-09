"""Local single-advisor workbench; no sending integration, secrets stay server-side."""
import json
import os
import uuid
from urllib.parse import urlsplit
from flask import Flask, jsonify, request, send_from_directory
from pydantic import ValidationError
from .engine import Engine
from .models import Customer, DraftRequest, Event, MemoryInput, MemoryChange, PreferenceAnswer, FeedbackInput, RewriteInput, ExampleInput, RuleInput
from .preferences import answer_question, question_states, skip_question, feedback, feedback_history, contextual_question, answer_contextual_question, preference_memories, add_example, add_rule
from .seed import seed_demo
from .extraction import extract_event
from .runtime import JobQueue, Relations
from .store import ROOT, Conflict, Missing, now, encode
from . import comparisons
from . import conversation_import
from .preference_document import read_document, save_document
from .customer_context import add_customer_rule, add_customer_memory, adopted_replies
from .models import CustomerRuleInput
from .automatic import AutomaticDrafts
from .advisor_notes import AdvisorNotes, NoteInput

AS_OF='2026-11-07T10:00:00+08:00'


def create_app(engine=None, seed=True, automation=False, recover=False, web_dir=None):
    app=Flask(__name__,static_folder=None)
    web_dir=web_dir or ROOT/'web/v6'
    app.config['MAX_CONTENT_LENGTH']=128*1024
    engine=engine or Engine()
    store=engine.store
    if seed:
        seed_demo(store,engine.data_dir)
    relations=Relations(engine)
    automatic=AutomaticDrafts(engine,relations)
    notes=AdvisorNotes(engine,automatic)

    @app.before_request
    def origin_check():
        if request.endpoint in ('preview_import','confirm_import'):
            # Raw exports and their editable preview coexist in confirmation.
            request.max_content_length=2*1024*1024
        if request.method in ('POST','PUT','PATCH','DELETE'):
            origin=request.headers.get('Origin')
            if origin and urlsplit(origin).netloc!=request.host:
                return jsonify(error='请从本应用页面操作。'),403
            if not request.is_json:
                return jsonify(error='请发送JSON数据。'),415

    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.errorhandler(ValidationError)
    def invalid(exc):
        return jsonify(error='请检查输入。',fields=[{'field':'.'.join(map(str,e['loc'])),'message':e['msg']} for e in exc.errors()]),422
    @app.errorhandler(Conflict)
    def conflict(exc):return jsonify(error=str(exc)),409
    @app.errorhandler(Missing)
    def missing(exc):return jsonify(error='记录不存在。'),404
    @app.errorhandler(ValueError)
    def value_error(exc):return jsonify(error=str(exc)),422
    @app.errorhandler(400)
    def bad_request(exc):return jsonify(error='JSON内容无法读取。'),400
    @app.errorhandler(413)
    def too_big(exc):return jsonify(error='导入内容超过2MB，请分段导入。' if request.endpoint in ('preview_import','confirm_import') else '输入超过128KB。'),413

    @app.get('/')
    def index():return send_from_directory(web_dir,'index.html')
    @app.get('/assets/<path:name>')
    def asset(name):return send_from_directory(web_dir,name)

    @app.get('/api/bootstrap')
    def bootstrap():
        return jsonify(brand=engine.brand,products=engine.products,as_of=relations.current_time(),clock=relations.clock(),
            events=json.loads((engine.data_dir/'events.json').read_text()),model=engine.generator.model,
            capabilities={'confirmed_facts':True,'manual_examples':True,'contact_feedback':True,'scoped_preferences':True,'customer_preferences':True,'automatic_drafts':automation,'advisor_notes':True,'rewrite_references':True})

    @app.get('/api/customers')
    def customers():
        as_of=request.args.get('as_of') or relations.current_time()
        result=[]
        followups={f['customer_id']:f for f in relations.list()}
        for customer in store.customers():
            context=store.context(customer['id'],as_of)
            gate=engine.gate(context | {'request':DraftRequest(customer_id=customer['id'],as_of=as_of,trigger='timer').model_dump()})
            messages=[e for e in store.timeline(customer['id'],as_of) if e['kind']=='message']
            auto=automatic.state(customer['id'])
            pending=bool(messages and messages[-1]['actor']=='client' and not auto.get('paused') and not (auto.get('assessment',{}).get('action') in ('no_action','defer') and auto.get('status') not in ('queued','reviewing')))
            result.append(customer | {'last_message':messages[-1] if messages else None,
                'pending_reply':pending,'outreach':auto.get('assessment') if auto.get('assessment',{}).get('action') in ('no_action','defer') and auto.get('status') not in ('queued','reviewing') else gate.model_dump() if gate else None,
                'memory_count':len(context['memories']), 'followup':{k:v for k,v in followups.get(customer['id'],{}).items() if k not in ('review_record','review','request')}})
        return jsonify(result)

    @app.get('/api/customers/<customer_id>')
    def customer(customer_id):
        as_of=request.args.get('as_of') or relations.current_time()
        drafts=[d | {'is_current':engine.is_current(d,as_of,allow_clock_progress=True), 'can_copy':engine.is_current(d,as_of,ignore_preferences=True,allow_clock_progress=True)} for d in store.drafts(customer_id,1 if request.args.get('view')=='workspace' else 10)]
        with store.connect() as db:
            updates=[{'event_id':r['id'],'status':r['status'] or 'pending',
                'issues':json.loads(r['record']).get('issues',[]) if r['record'] else []}
                for r in db.execute('''SELECT e.id,x.status,x.record FROM events e LEFT JOIN memory_extractions x ON x.event_id=e.id
                WHERE e.customer_id=? AND e.occurred_at<=? AND json_extract(e.data,'$.requires_memory_update')=1
                AND (x.status IS NULL OR x.status!='complete')''',(customer_id,DraftRequest(customer_id=customer_id,as_of=as_of).as_of))]
        return jsonify(customer=store.customer(customer_id),timeline=store.timeline(customer_id,as_of),
            memories=[m for m in store.memories('customer',customer_id,True) if m['status']!='deleted'],
            reply_history=adopted_replies(store,store.context(customer_id,as_of),'default')[:12],
            drafts=[present_draft({k:v for k,v in d.items() if k not in ('attempts','system','user','prompt')} if request.args.get('view')=='workspace' else d) for d in drafts],memory_updates=updates,
            automatic=automatic.state(customer_id),advisor_note=notes.latest(customer_id))

    @app.post('/api/customers/<customer_id>/notes')
    def advisor_note(customer_id):
        store.customer(customer_id)
        raw=request.get_json()
        if not isinstance(raw,dict):raise ValueError('请填写要补充的内容。')
        data=NoteInput.model_validate(raw | {'customer_id':customer_id})
        old=notes.read(data.id)
        if old and old['input']!=data.model_dump():raise Conflict('该补充编号已用于其他内容。')
        if old and old.get('status') in ('complete','clarify'):return jsonify(note=notes.public(old))
        return jsonify(job_id=queue.submit('advisor_note',data.model_dump())),202

    @app.post('/api/customers/<customer_id>/retry-advice')
    def retry_advice(customer_id):
        store.customer(customer_id)
        return jsonify(automatic.retry(customer_id)),202

    @app.post('/api/customers/<customer_id>/preferences')
    def customer_preference(customer_id):
        result=add_customer_rule(store,customer_id,CustomerRuleInput.model_validate(request.get_json()))
        if automation:automatic.schedule(customer_id)
        return jsonify(result),201

    @app.post('/api/customers/<customer_id>/facts')
    def customer_fact(customer_id):
        result=add_customer_memory(store,customer_id,CustomerRuleInput.model_validate(request.get_json()),kind='fact')
        if automation:automatic.schedule(customer_id)
        return jsonify(result),201

    def present_draft(record):
        """Latest saved wording is a view, never a mutation of original model output."""
        texts={c['id']:c['text'] for c in (record.get('result') or {}).get('candidates',[])}
        with store.connect() as db:
            edits=[dict(r) for r in db.execute('SELECT candidate_id,text,created_at AS at FROM draft_edits WHERE draft_id=?',(record['id'],))]
            selected=db.execute("SELECT candidate_id,selected_text,COALESCE(updated_at,created_at) AS at FROM feedback WHERE draft_id=? AND advisor_id='default' AND mode='calibration' AND outcome='choice' ORDER BY COALESCE(updated_at,created_at) DESC LIMIT 1",(record['id'],)).fetchone()
        if selected and selected['selected_text'] is not None:
            edits.append({'candidate_id':selected['candidate_id'],'text':selected['selected_text'],'at':selected['at']})
        for edit in sorted(edits,key=lambda row:row['at']):texts[edit['candidate_id']]=edit['text']
        return record | {'display_texts':texts,'adopted_candidate':selected['candidate_id'] if selected else None}

    @app.post('/api/customers')
    def add_customer():
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('客户资料应为对象。')
        data.setdefault('id',uuid.uuid4().hex)
        return jsonify(store.add_customer(Customer.model_validate(data))),201

    @app.post('/api/customers/<customer_id>/events')
    def add_event(customer_id):
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('消息内容应为对象。')
        data=data | {'customer_id':customer_id}
        data.setdefault('id',uuid.uuid4().hex)
        event=Event.model_validate(data)
        if event.kind=='message' and event.actor=='client':
            event=event.model_copy(update={'data':event.data | {'requires_memory_update':True}})
        saved=store.add_event(event)
        if event.kind=='message' and event.actor=='client':
            job_id=queue.submit('extract',{'event_id':event.id})
            return jsonify(event=saved,job_id=job_id),202
        if automation and event.kind in ('purchase','service','appointment'):
            automatic.schedule(customer_id,trigger='event',clear_pause=True)
        elif event.kind in ('purchase','service','appointment'):
            product_id=event.data.get('product_id')
            selected=[product_id] if product_id in {p['id'] for p in engine.products} else []
            relations.schedule(DraftRequest(customer_id=customer_id,as_of=event.occurred_at,trigger='event',
                scenario='care' if event.kind in ('purchase','service') else 'auto',product_ids=selected,
                intent='根据最新订单、服务或预约记录复查是否需要联系，优先处理尚未完成的服务，不假定客户同意营销。'),
                {'purchase':'新订单：购后服务复查','service':'服务进展：复查后续安排','appointment':'预约变化：复查联系时机'}[event.kind])
        if automation and event.kind=='message' and event.actor=='advisor':
            # A recorded outgoing message settles the existing reply task.
            automatic.pause(customer_id,'已记录回复，等待客户的新消息')
        return jsonify(event=saved),201

    @app.post('/api/customers/<customer_id>/events/<event_id>/retry-memory')
    def retry_memory(customer_id,event_id):
        with store.connect() as db:
            event=db.execute('SELECT customer_id FROM events WHERE id=?',(event_id,)).fetchone()
        if not event or event['customer_id']!=customer_id:
            raise Missing(event_id)
        return jsonify(job_id=queue.submit('extract',{'event_id':event_id})),202

    @app.post('/api/customers/<customer_id>/imports/preview')
    def preview_import(customer_id):
        data=conversation_import.ImportPreview.model_validate(request.get_json())
        return jsonify(conversation_import.preview(store.customer(customer_id),data))

    @app.post('/api/customers/<customer_id>/imports')
    def confirm_import(customer_id):
        data=conversation_import.ImportConfirm.model_validate(request.get_json())
        result=conversation_import.confirm(store,customer_id,data)
        # Events are already committed. A full queue must not hide that success.
        try:result['job_id']=queue.submit('import_extract',{'event_ids':result['event_ids']})
        except ValueError as exc:result.update(job_id=None,processing_error=str(exc))
        return jsonify(result),200 if result['reused'] else 201

    @app.post('/api/memories')
    def add_memory():return jsonify(store.put_memory(MemoryInput.model_validate(request.get_json()))),201

    @app.patch('/api/memories/<memory_id>')
    def change_memory(memory_id):
        result=store.change_memory(memory_id,MemoryChange.model_validate(request.get_json()))
        if automation:
            if result['scope']=='customer':automatic.schedule(result['owner_id'])
            else:automatic.refresh_active()
        return jsonify(result)

    @app.get('/api/preferences')
    def preferences():
        return jsonify(memories=preference_memories(store),
                       document=read_document(store), global_document=read_document(store,global_only=True),
                       questions=question_states(store,'default'),contextual_question=contextual_question(store))

    @app.put('/api/preferences/global')
    def edit_global_preferences():
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('请提交表达要求。')
        result=save_document(store,data.get('text'),data.get('revision'),global_only=True)
        if automation:automatic.refresh_active()
        return jsonify(result)

    @app.post('/api/preferences/rules')
    def create_preference_rule():
        result=add_rule(store,RuleInput.model_validate(request.get_json()))
        if automation:automatic.refresh_active()
        return jsonify(result),201

    @app.put('/api/preferences/document')
    def edit_preference_document():
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('请提交表达要求。')
        result=save_document(store,data.get('text'),data.get('revision'))
        if automation:automatic.refresh_active()
        return jsonify(result)

    @app.post('/api/preferences/examples')
    def create_example():
        result=add_example(store,ExampleInput.model_validate(request.get_json()))
        if automation:automatic.refresh_active()
        return jsonify(result),201

    @app.post('/api/preferences/context-answer')
    def context_answer():
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('请提交问题答案。')
        return jsonify(answer_contextual_question(store,'default',data.get('question_id'),data.get('action'),
            data.get('rule_text',''),data.get('applicability',''),data.get('expected_revision')))

    @app.post('/api/preferences/answer')
    def answer():return jsonify(answer_question(store,'default',PreferenceAnswer.model_validate(request.get_json())))
    @app.post('/api/preferences/skip')
    def skip():
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('请求应为对象。')
        skip_question(store,'default',data.get('question_id'))
        return jsonify(saved=True)

    @app.post('/api/drafts')
    def generate():
        payload=DraftRequest.model_validate(request.get_json())
        engine.context(payload)  # validate before queueing
        job_id=queue.submit('draft',payload.model_dump())
        return jsonify(job_id=job_id),202

    @app.post('/api/drafts/<draft_id>/rewrite')
    def rewrite(draft_id):
        data=RewriteInput.model_validate(request.get_json())
        parent=store.draft(draft_id)
        if data.confirmed_facts is None:
            data=data.model_copy(update={'confirmed_facts':parent['context'].get('revision_request',{}).get('confirmed_facts','')})
        if parent['status']!='contact':raise ValueError('请选择有正文的原稿。')
        if not engine.is_current(parent,data.as_of,ignore_preferences=True,allow_clock_progress=True):
            raise Conflict('客户资料或情境已变化，请先生成当前建议；修改意见仍可保留。')
        ids=[c['id'] for c in parent['result']['candidates']]
        if data.candidate_id and data.candidate_id not in ids:raise ValueError('原稿候选不存在。')
        if data.edited_text and not data.candidate_id:raise ValueError('编辑稿需要指定原候选。')
        if data.reference_candidates is not None:
            if any(r.id not in ids for r in data.reference_candidates):raise ValueError('参考回复不存在。')
            if data.candidate_id and data.candidate_id not in [r.id for r in data.reference_candidates]:
                raise ValueError('编辑稿必须包含在勾选的参考回复中。')
        payload=data.model_dump() | {'parent_draft_id':draft_id,'advisor_id':parent['request']['advisor_id']}
        payload['reference_candidates']=encode(payload['reference_candidates']) if data.reference_candidates is not None else None
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM rewrite_requests WHERE id=?',(data.id,)).fetchone()
            if old and any(old[k]!=v for k,v in payload.items()):raise Conflict('重写编号已用于其他意见。')
            if not old:
                keys=list(payload)+['created_at']
                db.execute('INSERT INTO rewrite_requests ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',list(payload.values())+[now()])
            # A lost HTTP response must not produce a second completed rewrite.
            done=db.execute("SELECT id FROM drafts WHERE json_extract(record,'$.request.rewrite_id')=? AND status!='failed' ORDER BY created_at DESC LIMIT 1",(data.id,)).fetchone()
        if done:return jsonify(draft_id=done['id'],reused=True)
        draft_request=DraftRequest.model_validate(parent['request']).model_copy(update={'rewrite_id':data.id,'as_of':data.as_of})
        return jsonify(job_id=queue.submit('draft',draft_request.model_dump())),202

    @app.get('/api/jobs/<job_id>')
    def job(job_id):
        return jsonify(queue.get(job_id))

    @app.get('/api/operations')
    def operations():return jsonify(clock=relations.clock(),followups=relations.list())

    @app.post('/api/operations/review')
    def review_operations():
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('复查请求应为对象。')
        as_of=DraftRequest(customer_id=data.get('customer_id') or 'unused',as_of=data.get('as_of') or relations.current_time()).as_of
        if data.get('customer_id'):store.customer(data['customer_id'])
        if type(data.get('force',False)) is not bool:raise ValueError('force必须为布尔值。')
        return jsonify(job_id=queue.submit('review',{'as_of':as_of,'customer_id':data.get('customer_id'),'force':data.get('force',False)})),202

    @app.post('/api/clock')
    def change_clock():
        data=request.get_json()
        if not isinstance(data,dict) or type(data.get('live',False)) is not bool:raise ValueError('时钟设置不正确。')
        return jsonify(relations.set_clock(data.get('as_of',''),data.get('live',False),recheck=not automation))

    @app.get('/api/drafts/<draft_id>')
    def draft(draft_id):
        record=store.draft(draft_id)
        as_of=request.args.get('as_of') or relations.current_time()
        return jsonify(present_draft(record | {'is_current':engine.is_current(record,as_of,allow_clock_progress=True), 'can_copy':engine.is_current(record,as_of,ignore_preferences=True,allow_clock_progress=True)}))

    @app.post('/api/drafts/<draft_id>/check-copy')
    def check_copy(draft_id):
        data=request.get_json()
        if not isinstance(data,dict):raise ValueError('请求应为对象。')
        record=store.draft(draft_id)
        if not engine.is_current(record,data.get('as_of') or relations.current_time(),ignore_preferences=True,allow_clock_progress=True):
            raise Conflict('客户资料、商品或情境时间已变化，请重新生成后再复制。')
        return jsonify(can_copy=True)

    @app.get('/api/calibration')
    def calibration():
        drafts=store.drafts(limit=80)
        # Real completed outputs only. A recording is not counted as human feedback.
        cards=[];seen=set()
        with store.connect() as db:
            reviewed={r[0] for r in db.execute("SELECT draft_id FROM feedback WHERE advisor_id='default' AND mode='calibration'")}
        for d in drafts:
            if d['status']!='contact':continue
            key=(d['customer_id'],d['result']['scenario'])
            if key in seen or not engine.is_current(d,ignore_preferences=True):continue
            seen.add(key)
            if d['id'] in reviewed:continue
            context=d['context'];result=d['result']
            messages=[e for e in context['events'] if e['kind']=='message']
            cards.append({'draft_id':d['id'],'customer':context['customer'],
                'scenario':result['scenario'],'last_message':messages[-1]['text'] if messages else '',
                'context':context,'candidates':result['candidates']})
        return jsonify(cards)

    @app.get('/api/comparisons')
    def comparison_cards():return jsonify(comparisons.cards(store))

    @app.post('/api/comparisons/vote')
    def comparison_vote():return jsonify(comparisons.vote(store,request.get_json()))

    @app.post('/api/feedback')
    def save_feedback():
        data=FeedbackInput.model_validate(request.get_json())
        result=feedback(store,'default',**data.model_dump())
        if automation and data.mode=='calibration' and data.outcome=='no_contact':
            draft=store.draft(data.draft_id);latest=store.drafts(draft['customer_id'],1)
            if latest and latest[0]['id']==draft['id'] and engine.is_current(draft,relations.current_time(),ignore_preferences=True,allow_clock_progress=True):
                automatic.pause(draft['customer_id'],data.reason or '顾问选择此时暂不联系')
        return jsonify(result)

    @app.get('/api/feedback')
    def list_feedback():return jsonify(feedback_history(store))

    @app.post('/api/drafts/<draft_id>/edits')
    def save_edit(draft_id):
        draft=store.draft(draft_id);data=request.get_json()
        if not isinstance(data,dict):raise ValueError('编辑稿应为对象。')
        candidate=data.get('candidate_id');text=data.get('text')
        if candidate not in [c['id'] for c in (draft.get('result') or {}).get('candidates',[])]:raise ValueError('候选不存在。')
        if not isinstance(text,str) or not 1<=len(text.strip())<=1000:raise ValueError('编辑稿正文应为1至1000字。')
        if not engine.is_current(draft,data.get('as_of') or relations.current_time(),ignore_preferences=True,allow_clock_progress=True):raise Conflict('客户资料或情境已变化，请重新生成后编辑。')
        with store.connect() as db:
            db.execute('INSERT INTO draft_edits(draft_id,candidate_id,text,created_at) VALUES(?,?,?,?)',(draft_id,candidate,text.strip(),now()))
        return jsonify(saved=True)

    @app.get('/api/drafts/<draft_id>/edits')
    def draft_edits(draft_id):
        store.draft(draft_id)
        with store.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM draft_edits WHERE draft_id=? ORDER BY id',(draft_id,))]
        return jsonify(rows)

    def run_draft(payload):
        return {'draft_id':engine.generate(DraftRequest.model_validate(payload))['id']}

    def run_extract(payload):
        result=extract_event(store,engine.reviewer,payload['event_id'],prompt_dir=engine.prompt_dir,retriever=engine.retriever)
        if result['status']=='complete':
            with store.connect() as db:
                event=dict(db.execute('SELECT * FROM events WHERE id=?',(payload['event_id'],)).fetchone())
            if event['actor']=='client' and not payload.get('batch'):
                if automation:automatic.schedule(event['customer_id'],trigger='reply',clear_pause=True)
                else:relations.schedule(DraftRequest(customer_id=event['customer_id'],as_of=event['occurred_at'],trigger='reply',product_ids=[p['id'] for p in engine.products]),'新消息：复查客户当前需求')
        return {'memory_update':result}

    def run_import_extract(payload):
        results=[]
        # One batch runs chronologically; concurrent per-message extraction could
        # otherwise apply an older preference after the client's correction.
        for event_id in payload['event_ids']:
            results.append(run_extract({'event_id':event_id,'batch':automation})['memory_update'])
        if automation and payload['event_ids'] and all(r['status']=='complete' for r in results):
            with store.connect() as db:
                cid=db.execute('SELECT customer_id FROM events WHERE id=?',(payload['event_ids'][-1],)).fetchone()['customer_id']
            messages=[e for e in store.timeline(cid,relations.current_time()) if e['kind']=='message']
            if messages and messages[-1]['actor']=='client':automatic.schedule(cid,trigger='reply',clear_pause=True)
            elif messages:automatic.pause(cid,'已记录回复，等待客户的新消息')
        return {'memory_updates':results}

    queue=JobQueue(store,{'draft':run_draft,'extract':run_extract,'import_extract':run_import_extract,
        'review':lambda p:automatic.tick(**p) if automation else relations.tick(**p),
        'prepare':automatic.prepare,'advisor_note':notes.run},recover=recover)
    automatic.queue=queue
    if seed:
        existing={f['customer_id'] for f in relations.list()}
        seed_data=json.loads((engine.data_dir/'relationships.json').read_text())
        workflows=seed_data.get('workflows',{})
        for customer in store.customers():
            if customer['id'] not in existing:
                data=workflows.get(customer['id'],{})
                relations.schedule(DraftRequest(customer_id=customer['id'],as_of=AS_OF,
                    product_ids=data.get('product_ids',[p['id'] for p in engine.products]),
                    scenario=data.get('scenario','auto'),trigger=data.get('trigger','timer'),
                    occasion=data.get('occasion',''),event_id=data.get('event_id')),'今日关系复查')
    if automation:app.extensions['heartbeat_stop']=automatic.start()
    app.extensions.update(job_queue=queue,relations=relations,automatic=automatic,advisor_notes=notes)

    return app

if __name__=='__main__':
    import fcntl
    lock_path=ROOT/'artifacts/v4/server.lock'
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('w') as server_lock:
        fcntl.flock(server_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from .quick_review import register_quick_review
        app=create_app(automation=True,recover=True)
        register_quick_review(app,ROOT)
        app.run(host='127.0.0.1',port=int(os.getenv('ATELIER_PORT','8766')),debug=False,threaded=True,load_dotenv=False)
