import json
import tempfile
import unittest
from pathlib import Path
from atelier.automatic import AutomaticDrafts, setting
from atelier.advisor_notes import AdvisorNotes, NoteInput
from atelier.engine import Engine
from atelier.runtime import Relations
from atelier.models import DraftRequest, Event, MemoryInput, MemoryChange
from atelier.store import RelationshipStore, Conflict
from atelier.seed import seed_demo
from atelier.app import create_app
from test_atelier_engine import Fake, decision, AS_OF

class CaptureQueue:
    def __init__(self):self.tasks=[]
    def submit(self,kind,payload):
        self.tasks.append((kind,payload));return str(len(self.tasks))

class AutomaticAdviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=RelationshipStore(Path(self.tmp.name)/'test.sqlite3');seed_demo(self.store)
        self.engine=Engine(self.store,Fake([decision()]*10),Fake([{'verdict':'pass','issues':[]}]*10))
        self.relations=Relations(self.engine);self.relations.set_clock(AS_OF,recheck=False)
        self.auto=AutomaticDrafts(self.engine,self.relations);self.queue=CaptureQueue();self.auto.queue=self.queue
        self.notes=AdvisorNotes(self.engine,self.auto)
    def tearDown(self):self.tmp.cleanup()
    def note(self,text,plan,identity='entry'):
        self.engine.generator=Fake([plan]);data={'id':identity,'customer_id':'zhou','text':text,'as_of':AS_OF}
        return self.notes.run(data),data
    def test_existing_settled_review_is_visible_without_new_model_call(self):
        self.relations.schedule(DraftRequest(customer_id='liang',as_of=AS_OF,trigger='timer'),'旧版复查')
        self.relations.tick(customer_id='liang')
        self.auto.bootstrap()
        self.assertEqual(self.auto.state('liang')['assessment']['action'],'no_action')
        self.assertEqual(self.auto.state('liang')['status'],'waiting_event')
        self.assertFalse(self.engine.generator.calls)

    def test_old_ready_advice_is_rechecked_once_when_day_changes(self):
        self.auto.schedule('zhou')
        with self.store.connect() as db:db.execute("UPDATE followups SET status='ready' WHERE customer_id='zhou'")
        self.relations.set_clock('2026-11-08T10:00:00+08:00',recheck=False)
        self.auto.tick();self.auto.tick()
        self.assertEqual(len(self.queue.tasks),1)
        self.assertEqual(self.queue.tasks[0][1]['customer_id'],'zhou')

    def test_explicit_advisor_draft_is_not_replaced_by_contact_pause(self):
        text='还是发一条消息'
        self.auto.pause('liang','旧的暂停判断')
        before=self.store.memories('customer','liang',True)
        self.engine.generator=Fake([{'action':'draft','trigger':'manual','instruction':text},decision()])
        self.notes.run({'id':'explicit-draft','customer_id':'liang','text':text,'as_of':AS_OF})
        self.auto.prepare(self.queue.tasks[-1][1])
        draft=self.store.drafts('liang',1)[0]
        self.assertEqual(draft['status'],'contact')
        self.assertEqual(draft['context']['advisor_note']['text'],text)
        self.assertEqual(draft['context']['contact_advice']['action'],'no_action')
        self.assertEqual(len(self.engine.generator.calls),2)  # classify then draft, no contact assessment
        self.assertNotIn('current_contact_decision',self.engine.generator.calls[0])
        self.assertFalse(self.auto.state('liang')['paused'])
        self.assertEqual(before,self.store.memories('customer','liang',True))
        self.assertTrue(self.engine.is_current(draft,AS_OF))
        # Its one-time instruction is never carried into a fresh timer request.
        timer=DraftRequest(customer_id='liang',as_of=AS_OF,trigger='timer')
        self.assertEqual(self.engine.gate(self.engine.context(timer)).action,'no_action')
        self.assertNotIn('advisor_note',self.engine.context(timer))

    def test_pause_receipt_uses_understood_reason_not_blindly_echoing_input(self):
        self.note('这次先不发，等客户问',{'action':'pause','instruction':'本次暂停，等客户主动咨询再处理'})
        self.assertEqual(self.auto.state('zhou')['assessment']['reason'],'本次暂停，等客户主动咨询再处理')
        self.assertFalse(self.queue.tasks)

    def test_due_once_and_settled_silence_never_regenerates(self):
        due='2026-11-08T10:00:00+08:00'
        self.auto.schedule('liang',trigger='timer',due_at=due)
        self.auto.tick();self.assertFalse(self.queue.tasks)
        self.relations.set_clock(due,recheck=False);self.auto.tick();self.auto.tick();self.assertEqual(len(self.queue.tasks),1)
        self.auto.prepare(self.queue.tasks[0][1]);self.assertEqual(self.auto.state('liang')['status'],'waiting_event')
        self.assertFalse(self.engine.generator.calls)
        self.auto.bootstrap();self.auto.tick();self.relations.set_clock('2026-11-09T10:00:00+08:00',recheck=False);self.auto.tick()
        self.assertEqual(sum(t[1]['customer_id']=='liang' for t in self.queue.tasks),1)
    def test_new_message_generates_and_reads_do_not_submit(self):
        self.auto.schedule('zhou',trigger='reply');self.auto.tick();self.auto.prepare(self.queue.tasks[0][1])
        self.assertEqual(self.auto.state('zhou')['status'],'ready');self.assertEqual(len(self.store.drafts('zhou')),1)
        app=create_app(self.engine,seed=False)
        try:
            c=app.test_client()
            for _ in range(3):self.assertEqual(c.get('/api/customers/zhou?view=workspace&as_of='+AS_OF.replace('+','%2B')).status_code,200)
            self.assertEqual(len(self.engine.generator.calls),1)
            for text in ('','   '):
                self.assertEqual(c.post('/api/customers/zhou/notes',json={'id':'blank','text':text,'as_of':AS_OF}).status_code,422)
            self.assertEqual(c.post('/api/customers/zhou/retry-advice',json={}).status_code,422)
        finally:app.extensions['job_queue'].pool.shutdown()
    def test_superseded_generation_does_not_publish_over_newer_input(self):
        self.auto.schedule('zhou');self.auto.tick();old=self.queue.tasks[0][1]
        self.auto.schedule('zhou',intent='新情况');self.assertTrue(self.auto.prepare(old)['superseded']);self.assertFalse(self.engine.generator.calls)
        self.auto.tick();self.auto.prepare(self.queue.tasks[-1][1]);self.assertEqual(self.auto.state('zhou')['status'],'ready')
    def test_failure_retry_preserves_intent_and_note(self):
        self.auto.schedule('zhou',intent='先回答能不能优惠');self.auto.tick()
        self.engine.generator=Fake([ValueError('测试失败')]*2)
        with self.assertRaises(ValueError):self.auto.prepare(self.queue.tasks[-1][1])
        self.auto.retry('zhou');self.engine.generator=Fake([decision()]);self.auto.prepare(self.queue.tasks[-1][1])
        self.assertEqual(self.store.drafts('zhou')[0]['request']['intent'],'先回答能不能优惠')
        self.assertFalse(self.auto.state('zhou')['error'])
    def test_purpose_is_valid_without_fabricated_order(self):
        result,data=self.note('购买后关怀',{'action':'draft','instruction':'购买后关怀','trigger':'manual'})
        self.assertEqual(result['note']['status'],'complete');self.assertEqual(len(self.queue.tasks),1)
        request=self.relations.list()[0]['request'];self.assertEqual(request['intent'],'购买后关怀')
        self.assertEqual(request['advisor_note_id'],'entry');self.assertFalse(result['note']['changes'])
        count=len(self.engine.generator.calls);self.notes.run(data);self.assertEqual(len(self.queue.tasks),1);self.assertEqual(len(self.engine.generator.calls),count)
    def test_pause_and_defer_are_real_states_without_drafting(self):
        self.note('先别联系',{'action':'pause'})
        self.auto.tick();self.assertFalse(self.queue.tasks);self.assertTrue(self.auto.state('zhou')['paused'])
        self.note('明天10点再看看',{'action':'defer','revisit_at':'2026-11-08T10:00:00+08:00'},'tomorrow')
        self.auto.tick();self.assertFalse(self.queue.tasks);self.assertEqual(self.auto.state('zhou')['status'],'scheduled')
        self.auto.schedule('zhou',trigger='reply',clear_pause=True);self.auto.tick();self.assertEqual(len(self.queue.tasks),1)
    def test_customer_fact_persists_once_and_does_not_leak(self):
        plan={'action':'update','memories':[{'key':'color','value':'客户本人喜欢绿色','quote':'他自己喜欢绿色'}]}
        result,data=self.note('他自己喜欢绿色',plan)
        self.notes.run(data)
        saved=[m for m in self.store.memories('customer','zhou') if m['source']=='顾问补充 entry']
        self.assertEqual(len(saved),1);self.assertEqual(saved[0]['source_quote'],'他自己喜欢绿色')
        self.assertFalse(self.store.memories('advisor','default'))
        other=self.engine.context(DraftRequest(customer_id='chen',as_of=AS_OF));self.assertNotIn('客户本人喜欢绿色',json.dumps(other,ensure_ascii=False))
    def test_rewrite_does_not_become_global_memory(self):
        self.engine.generate(DraftRequest(customer_id='zhou',as_of=AS_OF))
        self.note('说得自然一点',{'action':'rewrite','instruction':'说得自然一点'})
        self.assertFalse(self.store.memories('advisor','default'));self.assertTrue(self.notes.read('entry')['original_candidates'])
    def test_confirmed_exception_is_scoped_context(self):
        text='负责人已经确认这次三个人可以同组'
        self.note(text,{'action':'update','confirmed_facts':text,'instruction':'按新确认回复'})
        req=DraftRequest.model_validate(self.relations.list()[0]['request']);context=self.engine.context(req)
        self.assertEqual(context['advisor_note']['confirmed_facts'],text)
        self.assertIn('advisor_note:confirmed_facts',context['allowed_evidence'])
        self.assertNotIn('advisor_note',self.engine.context(DraftRequest(customer_id='chen',as_of=AS_OF)))
        self.assertIn('最多2人',context['boutique_events'][0]['access'])
    def test_invalid_source_or_other_customer_cannot_write(self):
        other=self.store.memories('customer','chen')[0]
        cases=[{'value':'喜欢红色','quote':'他喜欢红色'}, {'value':'绿色','quote':'绿色','replaces_id':other['id']}]
        for i,memory in enumerate(cases):
            before=len(self.store.memories('customer','zhou',True))
            plan={'action':'update','memories':[memory]};self.engine.generator=Fake([plan,plan])
            with self.assertRaises(ValueError):self.notes.run({'id':'bad'+str(i),'customer_id':'zhou','text':'绿色','as_of':AS_OF})
            self.assertEqual(len(self.store.memories('customer','zhou',True)),before)
    def test_saved_note_id_cannot_be_reassigned(self):
        result,data=self.note('购买后关怀',{'action':'draft'})
        with self.assertRaises(Conflict):self.notes.run(data|{'customer_id':'chen'})
    def test_clarification_is_available_to_next_input(self):
        self.note('帮他安排一下',{'action':'clarify','clarification':'是安排到店还是护理？'})
        self.assertFalse(self.queue.tasks)
        self.note('到店',{'action':'draft','instruction':'协助安排到店'},'answer')
        self.assertEqual(self.engine.generator.calls[0]['previous_clarification']['question'],'是安排到店还是护理？')
    def test_clock_progress_keeps_unchanged_draft_but_checks_new_facts_and_day(self):
        draft=self.engine.generate(DraftRequest(customer_id='zhou',as_of=AS_OF))
        later='2026-11-07T10:03:00+08:00'
        self.assertTrue(self.engine.is_current(draft,later,allow_clock_progress=True))
        self.assertFalse(self.engine.is_current(draft,later))
        self.assertFalse(self.engine.is_current(draft,'2026-11-08T10:03:00+08:00',allow_clock_progress=True))
        self.store.add_event(Event(id='later',customer_id='zhou',occurred_at=later,actor='client',kind='message',text='改问别的事情'))
        self.assertFalse(self.engine.is_current(draft,later,allow_clock_progress=True))

    def test_preference_changes_refresh_active_advice_but_not_silence(self):
        self.auto.schedule('zhou');self.auto.tick();self.auto.prepare(self.queue.tasks[-1][1])
        self.auto.pause('liang','暂不联系')
        self.auto.refresh_active()
        self.assertEqual(self.auto.state('zhou')['status'],'scheduled')
        self.assertEqual(self.auto.state('liang')['status'],'waiting_event')

    def test_calibration_no_contact_settles_current_task_without_changing_permission(self):
        import threading
        from unittest.mock import patch
        self.engine.generate(DraftRequest(customer_id='zhou',as_of=AS_OF))
        draft=self.store.drafts('zhou')[0]
        with patch.object(AutomaticDrafts,'start',return_value=threading.Event()):app=create_app(self.engine,seed=False,automation=True)
        try:
            c=app.test_client();before=self.store.memories('customer','zhou')
            r=c.post('/api/feedback',json={'draft_id':draft['id'],'mode':'calibration','outcome':'no_contact','reason':'先等客户决定','learning_scope':'once','display_order':['a','b','c']})
            self.assertEqual(r.status_code,200,r.json)
            self.assertTrue(app.extensions['automatic'].state('zhou')['paused'])
            self.assertEqual(before,self.store.memories('customer','zhou'))
            customer=next(x for x in c.get('/api/customers').json if x['id']=='zhou')
            self.assertFalse(customer['pending_reply']);self.assertEqual(customer['outreach']['action'],'no_action')
        finally:app.extensions['job_queue'].pool.shutdown()

    def test_import_finishes_all_extraction_before_scheduling_and_outgoing_settles(self):
        import threading,time
        from unittest.mock import patch
        from atelier.conversation_import import preview,ImportPreview
        self.engine.reviewer=Fake([{'memories':[]},{'memories':[]}])
        with patch.object(AutomaticDrafts,'start',return_value=threading.Event()):app=create_app(self.engine,seed=False,automation=True)
        try:
            client=app.test_client()
            for identity,text,expected in [('incoming','客户：谢谢。\n我：不客气。\n客户：能看下绿色吗？','scheduled'),('outgoing','我：稍后给你看。','waiting_event')]:
                turns=preview(self.store.customer('zhou'),ImportPreview(text=text,as_of=AS_OF))['turns']
                turns=[{k:t[k] for k in ('actor','text','occurred_at','time_confirmed')} for t in turns]
                result=client.post('/api/customers/zhou/imports',json={'id':identity,'text':text,'as_of':AS_OF,'turns':turns})
                self.assertIn(result.status_code,(200,201),result.json)
                for _ in range(100):
                    job=app.extensions['job_queue'].get(result.json['job_id'])
                    if job['status'] in ('finished','failed'):break
                    time.sleep(.01)
                self.assertEqual(job['status'],'finished',job)
                self.assertEqual(app.extensions['automatic'].state('zhou')['status'],expected)
            self.assertFalse(self.engine.generator.calls)
        finally:app.extensions['job_queue'].pool.shutdown()

if __name__=='__main__':unittest.main()
