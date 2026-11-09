"""Durable local jobs and relationship reviews. A review never sends a message."""
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pydantic import ValidationError
from lanxu.provider import ProviderError
from .models import DraftRequest, Assessment, instant
from .store import ROOT, now, encode, Missing


class JobQueue:
    def __init__(self, store, handlers, recover=False):
        self.store=store;self.handlers=handlers;self.pool=ThreadPoolExecutor(max_workers=2)
        if recover:
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='interrupted',error=?,finished_at=? WHERE status='running'",
                    ('服务已重启，无法确认中断时的模型请求结果；请查历史后决定是否重试。',now()))
                db.execute("UPDATE followups SET status='needs_attention',last_error=? WHERE status='reviewing'",
                    ('复查被服务重启中断，请手动重试。',))
                queued=[r['id'] for r in db.execute("SELECT id FROM jobs WHERE status='queued'")]
            for job_id in queued:self.pool.submit(self._work,job_id)

    def submit(self,kind,payload):
        if kind not in self.handlers:raise ValueError('未知任务类型')
        serialized=encode(payload)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute("SELECT id FROM jobs WHERE kind=? AND payload=? AND status IN ('queued','running')",(kind,serialized)).fetchone()
            if prior:return prior['id']
            pending=db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if pending>=12:raise ValueError('任务队列已满，请先等待当前任务完成。')
            job_id=uuid.uuid4().hex
            db.execute('INSERT INTO jobs(id,kind,payload,status,created_at) VALUES(?,?,?,?,?)',(job_id,kind,serialized,'queued',now()))
        self.pool.submit(self._work,job_id)
        return job_id

    def _work(self,job_id):
        with self.store.connect() as db:
            changed=db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=? AND status='queued'",(now(),job_id)).rowcount
            if not changed:return
            row=dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
        try:
            result=self.handlers[row['kind']](json.loads(row['payload']))
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET status='finished',result=?,finished_at=? WHERE id=?",(encode(result),now(),job_id))
        except Exception as exc:
            # Do not persist third-party traces, environment, or credentials.
            message=str(exc) if isinstance(exc,(ValueError,ProviderError,Missing)) else '任务内部错误，未将本次处理记为成功。'
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?",(message[:500],now(),job_id))

    def get(self,job_id):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:raise Missing(job_id)
        result={'id':row['id'],'kind':row['kind'],'status':row['status'],'error':row['error'],
                'created_at':row['created_at'],'finished_at':row['finished_at']}
        if row['result']:result.update(json.loads(row['result']))
        return result


class Relations:
    def __init__(self,engine):
        self.engine=engine;self.store=engine.store

    def clock(self):
        with self.store.connect() as db:
            row=db.execute("SELECT value FROM settings WHERE key='clock'").fetchone()
        return json.loads(row['value']) if row else {'as_of':now(),'live':True}

    def current_time(self):
        clock=self.clock()
        return now() if clock['live'] else instant(clock['as_of'])

    def set_clock(self,as_of,live=False,recheck=True):
        as_of=instant(as_of)
        with self.store.connect() as db:
            db.execute("INSERT INTO settings VALUES('clock',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(encode({'as_of':as_of,'live':live}),))
            if recheck:
                db.execute("UPDATE followups SET status='scheduled',updated_at=? WHERE status IN ('ready','waiting_event','needs_attention')",(now(),))
        return self.clock()

    def schedule(self,request,title,due_at=None):
        followup_id='customer-'+request.customer_id
        with self.store.connect() as db:
            db.execute('''INSERT INTO followups(id,customer_id,title,request,due_at,status,updated_at) VALUES(?,?,?,?,?,'scheduled',?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,request=excluded.request,due_at=excluded.due_at,
            status='scheduled',last_error=NULL,updated_at=excluded.updated_at''',
            (followup_id,request.customer_id,title,encode(request.model_dump()),instant(due_at or request.as_of),now()))
        return followup_id

    def list(self):
        with self.store.connect() as db:
            rows=db.execute('SELECT f.*,r.record AS review_record FROM followups f LEFT JOIN reviews r ON r.id=f.last_review_id ORDER BY f.due_at,f.id')
            return [dict(r) | {'request':json.loads(r['request']),'review':json.loads(r['review_record']) if r['review_record'] else None} for r in rows]

    def assess(self,request,force=False):
        context=self.engine.context(request)
        hard=self.engine.gate(context)
        system=(self.engine.prompt_dir/'assess.txt').read_text()
        # Stable within a day unless facts, active memory, policies or hard contact conditions change.
        fp_context=json.loads(encode(context))
        fp_context['as_of']=request.as_of[:10]
        fp_context['request']['as_of']=request.as_of[:10]
        fingerprint=hashlib.sha256(encode({'context':fp_context,'gate':hard.model_dump() if hard else None,'prompt':system}).encode()).hexdigest()
        if not force:
            with self.store.connect() as db:
                row=db.execute("SELECT record FROM reviews WHERE customer_id=? AND fingerprint=? AND decision!='failed' ORDER BY created_at DESC LIMIT 1",(request.customer_id,fingerprint)).fetchone()
            if row:return json.loads(row['record']) | {'reused':True}
        record={'id':uuid.uuid4().hex,'customer_id':request.customer_id,'as_of':request.as_of,'created_at':now(),
                'context':context,'fingerprint':fingerprint,'source':'contact_rules' if hard else 'model','status':'running'}
        if hard:
            revisit_at=None
            try:revisit_at=instant(hard.revisit_when)
            except ValueError:pass
            assessment=Assessment(action=hard.action,scenario=hard.scenario,purpose=hard.purpose,reason=hard.reason,
                revisit_when=hard.revisit_when,revisit_at=revisit_at,product_ids=request.product_ids)
            record.update(status='complete',assessment=assessment.model_dump())
        else:
            record.update(system=system,user=encode(context))
            try:
                reply=self.engine.generator.complete(system,record['user'])
                record.update(raw=reply.text,metadata=reply.metadata)
                assessment=Assessment.model_validate_json(reply.text)
                if context.get('contact_scope',{}).get('mode')=='service_only' and assessment.action=='contact' and assessment.scenario!='care':
                    raise ValueError('服务许可不能用于营销复查。')
                if set(assessment.product_ids)-{p['id'] for p in context['products']}:
                    raise ValueError('复查引用了上下文中不存在的商品。')
                if assessment.revisit_at and assessment.revisit_at<=request.as_of:
                    raise ValueError('复查时间没有晚于当前情境。')
                if assessment.action!='contact' and not assessment.revisit_when:
                    raise ValueError('暂不联系时缺少复查条件。')
                record.update(status='complete',assessment=assessment.model_dump())
            except (ValueError,ValidationError):
                record.update(status='failed',error='复查结果不符合约定，请顾问检查后重试。')
            except ProviderError as exc:
                record.update(status='failed',error=str(exc),error_code=exc.code)
        with self.store.connect() as db:
            db.execute('INSERT INTO reviews(id,customer_id,as_of,decision,record,created_at,fingerprint) VALUES(?,?,?,?,?,?,?)',
                (record['id'],request.customer_id,request.as_of,record.get('assessment',{}).get('action','failed'),encode(record),record['created_at'],fingerprint))
        return record

    def tick(self,as_of=None,force=False,customer_id=None):
        as_of=instant(as_of or self.current_time())
        with self.store.connect() as db:
            sql="SELECT * FROM followups WHERE due_at<=? AND status='scheduled'"
            values=[as_of]
            if customer_id:
                sql='SELECT * FROM followups WHERE customer_id=?';values=[customer_id]
            rows=[dict(r) for r in db.execute(sql,values)]
        results=[]
        for row in rows:
            with self.store.connect() as db:
                claimed=db.execute("UPDATE followups SET status='reviewing',updated_at=? WHERE id=? AND status!='reviewing'",(now(),row['id'])).rowcount
            if not claimed:continue
            try:
                payload=json.loads(row['request']) | {'as_of':as_of}
                record=self.assess(DraftRequest.model_validate(payload),force)
                if record['status']=='failed':
                    status='needs_attention';due_at=row['due_at'];last_error=record['error']
                else:
                    assessment=record['assessment'];last_error=None
                    status='ready' if assessment['action']=='contact' else 'needs_attention' if assessment['action']=='needs_info' else 'scheduled' if assessment['revisit_at'] else 'waiting_event'
                    due_at=assessment['revisit_at'] or row['due_at']
                with self.store.connect() as db:
                    db.execute('UPDATE followups SET status=?,due_at=?,last_review_id=?,last_fingerprint=?,last_error=?,updated_at=? WHERE id=?',
                        (status,due_at,record['id'],record['fingerprint'],last_error,now(),row['id']))
                results.append({'customer_id':row['customer_id'],'status':status,'review_id':record['id'],'reused':record.get('reused',False)})
            except Exception:
                with self.store.connect() as db:
                    db.execute("UPDATE followups SET status='needs_attention',last_error=?,updated_at=? WHERE id=?",('复查异常，请重试。',now(),row['id']))
                raise
        return {'as_of':as_of,'reviews':results}

    def start_heartbeat(self,queue,interval=30):
        stop=threading.Event()
        def loop():
            while not stop.wait(interval):
                with self.store.connect() as db:
                    due=db.execute("SELECT COUNT(*) FROM followups WHERE status='scheduled' AND due_at<=?",(self.current_time(),)).fetchone()[0]
                if due:
                    try:queue.submit('review',{'as_of':self.current_time()})
                    except ValueError:pass  # next tick tries queue capacity, no model call was made
        thread=threading.Thread(target=loop,name='relationship-heartbeat',daemon=True);thread.start()
        return stop
