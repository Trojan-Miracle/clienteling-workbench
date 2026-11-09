"""Prepare advice on relationship changes or due dates, never on page reads."""
import fcntl
import json
import threading
from .models import DraftRequest, instant
from .store import now, encode


def setting(store,key,default=None):
    with store.connect() as db:
        row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return json.loads(row['value']) if row else default


def save_setting(store,key,value):
    with store.connect() as db:
        db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encode(value)))


class AutomaticDrafts:
    def __init__(self,engine,relations):
        self.engine=engine;self.store=engine.store;self.relations=relations;self.queue=None

    def state(self,customer_id):
        state=setting(self.store,'automatic:'+customer_id,{})
        with self.store.connect() as db:
            row=db.execute('SELECT status,due_at,last_error FROM followups WHERE customer_id=?',(customer_id,)).fetchone()
        return state | ({'status':row['status'],'due_at':row['due_at'],'error':row['last_error']} if row else {'status':'idle'})

    def schedule(self,customer_id,*,trigger=None,intent='',advisor_note_id=None,due_at=None,clear_pause=False):
        clock=self.relations.current_time();state=self.state(customer_id)
        if state.get('paused') and not clear_pause:return None
        if clear_pause:
            state['paused']=False;save_setting(self.store,'automatic:'+customer_id,state)
        messages=[e for e in self.store.timeline(customer_id,clock) if e['kind']=='message']
        trigger=trigger or ('reply' if messages and messages[-1]['actor']=='client' else 'manual')
        request=DraftRequest(customer_id=customer_id,as_of=clock,trigger=trigger,intent=intent,advisor_note_id=advisor_note_id)
        return self.relations.schedule(request,'准备沟通建议',due_at)

    def pause(self,customer_id,reason):
        self.schedule(customer_id,clear_pause=True)
        with self.store.connect() as db:
            db.execute("UPDATE followups SET status='waiting_event',updated_at=?,last_error=NULL WHERE customer_id=?",(now(),customer_id))
        save_setting(self.store,'automatic:'+customer_id,{'paused':True,'assessment':{'action':'no_action','purpose':'暂不联系','reason':reason},'draft_id':None})

    def bootstrap(self):
        """Existing ready-but-stale advice is prepared once; settled waits stay settled."""
        clock=self.relations.current_time()
        existing={f['customer_id']:f for f in self.relations.list()}
        for customer in self.store.customers():
            cid=customer['id'];f=existing.get(cid)
            if self.state(cid).get('paused'):continue
            if f and f['status']=='waiting_event' and not setting(self.store,'automatic:'+cid):
                review=f.get('review') or {}
                assessment=review.get('assessment')
                if assessment and assessment['action']!='contact':
                    save_setting(self.store,'automatic:'+cid,{'draft_id':None,'assessment':assessment,
                        'prepared_at':review.get('created_at'),'as_of':review.get('as_of'),'paused':False})
            if f and f['status'] not in ('ready',):continue
            drafts=self.store.drafts(cid,1)
            if drafts and self.engine.is_current(drafts[0],clock,ignore_preferences=True,allow_clock_progress=True):continue
            if self.store.timeline(cid,clock):self.schedule(cid,trigger=f['request']['trigger'] if f else None)

    def refresh_active(self):
        # Only existing contact advice is rechecked after global wording changes.
        # A settled no-contact decision does not become a new outbound task.
        for row in self.relations.list():
            if row['status']!='ready' or self.state(row['customer_id']).get('paused'):continue
            req=DraftRequest.model_validate(row['request'])
            self.schedule(row['customer_id'],trigger=req.trigger,intent=req.intent,advisor_note_id=req.advisor_note_id)

    def recover(self):
        # JobQueue recovery runs before the heartbeat. Surface orphaned automatic
        # tasks as failures; never spin forever or silently rerun uncertain calls.
        with self.store.connect() as db:
            rows=list(db.execute("SELECT * FROM followups WHERE status IN ('queued','reviewing')"))
            for row in rows:
                job=db.execute("SELECT status FROM jobs WHERE kind='prepare' AND json_extract(payload,'$.customer_id')=? AND json_extract(payload,'$.revision')=? ORDER BY created_at DESC LIMIT 1",(row['customer_id'],row['updated_at'])).fetchone()
                if not job or job['status'] not in ('queued','running'):
                    db.execute("UPDATE followups SET status='needs_attention',last_error='上次准备未完成，请重试。' WHERE id=?",(row['id'],))

    def retry(self,customer_id):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM followups WHERE customer_id=?',(customer_id,)).fetchone()
        if not row or not row['last_error']:raise ValueError('当前没有失败的起草任务；请补充希望怎样调整。')
        request=DraftRequest.model_validate(json.loads(row['request']))
        self.schedule(customer_id,trigger=request.trigger,intent=request.intent,advisor_note_id=request.advisor_note_id,clear_pause=True)
        return self.tick(customer_id=customer_id)

    def tick(self,as_of=None,customer_id=None,force=False):
        # Force is an explicit operator/developer action, never a heartbeat default.
        clock=instant(as_of or self.relations.current_time())
        if force and customer_id:self.schedule(customer_id,clear_pause=True)
        # An unsent reply using “today/tomorrow” needs a fresh judgment the next
        # day. Settled no-contact records are deliberately excluded.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day=lambda value:datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai')).date()
        for ready in self.relations.list():
            if ready['status']!='ready' or (customer_id and ready['customer_id']!=customer_id):continue
            prepared=self.state(ready['customer_id']).get('as_of') or ready['request']['as_of']
            if prepared and day(prepared)!=day(clock):
                req=DraftRequest.model_validate(ready['request'])
                self.schedule(ready['customer_id'],trigger=req.trigger,intent=req.intent,advisor_note_id=req.advisor_note_id)
        with self.store.connect() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM followups WHERE status='scheduled' AND due_at<=?",(clock,))]
        queued=[]
        for row in rows:
            if customer_id and row['customer_id']!=customer_id:continue
            if self.state(row['customer_id']).get('paused'):continue
            with self.store.connect() as db:
                claimed=db.execute("UPDATE followups SET status='queued' WHERE id=? AND status='scheduled' AND updated_at=?",(row['id'],row['updated_at'])).rowcount
            if not claimed:continue
            try:
                job_id=self.queue.submit('prepare',{'customer_id':row['customer_id'],'revision':row['updated_at'],'as_of':clock})
                queued.append({'customer_id':row['customer_id'],'job_id':job_id})
            except Exception:
                with self.store.connect() as db:
                    db.execute("UPDATE followups SET status='scheduled' WHERE id=? AND status='queued' AND updated_at=?",(row['id'],row['updated_at']))
                raise
        return {'queued':queued}

    def prepare(self,payload):
        cid=payload['customer_id'];revision=payload['revision'];clock=payload['as_of']
        with self.store.connect() as db:
            changed=db.execute("UPDATE followups SET status='reviewing' WHERE customer_id=? AND status='queued' AND updated_at=?",(cid,revision)).rowcount
            row=db.execute('SELECT * FROM followups WHERE customer_id=?',(cid,)).fetchone()
        if not changed:return {'superseded':True}
        try:
            request=DraftRequest.model_validate(json.loads(row['request']) | {'as_of':clock,'scenario':'auto','product_ids':[],'event_id':None})
            assessment=None;review_id=None;draft=None
            if request.trigger!='reply' and not self.engine.advisor_directed(self.engine.context(request)):
                review=self.relations.assess(request)
                if review['status']=='failed':raise ValueError(review['error'])
                assessment=review['assessment'];review_id=review['id']
            if assessment is None or assessment['action']=='contact':
                # Reuse a current result after an idempotent event retry, but an
                # explicit new note has a distinct request and must be handled.
                previous=self.store.drafts(cid,1)
                if previous and previous[0]['request']==request.model_dump() and previous[0]['status']!='failed' and self.engine.is_current(previous[0],clock,allow_clock_progress=True):draft=previous[0]
                else:draft=self.engine.generate(request)
                if draft['status']=='failed':raise ValueError('回复未能完成，请重试。')
                assessment=draft['result'] | {'revisit_at':(assessment or {}).get('revisit_at')}
            due_at=assessment.get('revisit_at')
            if not due_at and assessment['action']=='defer':
                try:due_at=instant(assessment.get('revisit_when',''))
                except (ValueError,TypeError):pass
            if due_at and instant(due_at)<=clock:due_at=None
            status='ready' if assessment['action']=='contact' else 'needs_attention' if assessment['action']=='needs_info' else 'scheduled' if due_at else 'waiting_event'
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                changed=db.execute('UPDATE followups SET status=?,due_at=?,last_review_id=?,last_error=NULL WHERE customer_id=? AND updated_at=?',
                    (status,due_at or row['due_at'],review_id,cid,revision)).rowcount
                if changed:
                    state={'draft_id':draft['id'] if draft else None,'assessment':assessment,'prepared_at':now(),'as_of':clock,'paused':False}
                    db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('automatic:'+cid,encode(state)))
            return {'draft_id':draft['id'] if draft else None,'superseded':not bool(changed)}
        except Exception as exc:
            with self.store.connect() as db:
                db.execute("UPDATE followups SET status='needs_attention',last_error=? WHERE customer_id=? AND updated_at=?",(str(exc)[:500] if isinstance(exc,ValueError) else '准备回复时出错，请重试。',cid,revision))
            raise

    def start(self,interval=3):
        stop=threading.Event()
        def loop():
            lock=self.store.path.with_suffix('.automation.lock').open('a')
            owned=False
            try:
                while not stop.is_set():
                    if not owned:
                        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);owned=True;self.recover();self.bootstrap()
                        except BlockingIOError:
                            stop.wait(interval);continue
                    try:self.tick()
                    except (ValueError,RuntimeError):pass
                    stop.wait(interval)
            finally:lock.close()
        thread=threading.Thread(target=loop,name='automatic-drafts',daemon=True);thread.start()
        return stop
