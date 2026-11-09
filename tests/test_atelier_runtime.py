import json
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from atelier.models import Event, MemoryInput, MemoryChange, DraftRequest
from atelier.store import RelationshipStore, ROOT, now, encode
from atelier.seed import seed_demo
from atelier.engine import Engine
from atelier.extraction import extract_event
from atelier.runtime import JobQueue, Relations
from lanxu.provider import ModelReply, ProviderError

AS_OF='2026-11-07T10:00:00+08:00'
class Fake:
    provider='fixture';model='test-only'
    def __init__(self,*items):self.items=iter(items);self.calls=[]
    def complete(self,system,user):
        self.calls.append(json.loads(user));item=next(self.items)
        if isinstance(item,Exception):raise item
        return ModelReply(json.dumps(item,ensure_ascii=False),{'provider':'test-fixture'})

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite3'
        self.store=RelationshipStore(self.path);seed_demo(self.store)
    def tearDown(self):self.temp.cleanup()
    def incoming(self,text,event_id='new-event'):
        return self.store.add_event(Event(id=event_id,customer_id='zhou',occurred_at=AS_OF,kind='message',actor='client',text=text,data={'requires_memory_update':True}))
    def engine(self,generator=None):return Engine(self.store,generator or Fake(),Fake())
    def request(self,**kw):return DraftRequest(**({'customer_id':'zhou','as_of':AS_OF,'trigger':'timer','product_ids':['TRANSIT']}|kw))
    def test_clock_defaults_to_live_and_preserves_manual_override(self):
        relations=Relations(self.engine())
        first='2026-11-09T10:00:00+00:00'
        later='2026-11-09T10:01:00+00:00'
        with patch('atelier.runtime.now',return_value=first):
            self.assertTrue(relations.clock()['live'])
            self.assertEqual(relations.current_time(),first)
        with patch('atelier.runtime.now',return_value=later):
            self.assertEqual(relations.current_time(),later)
            relations.set_clock(first,live=False,recheck=False)
            restarted=Relations(self.engine())
            self.assertFalse(restarted.clock()['live'])
            self.assertEqual(restarted.current_time(),first)
            restarted.set_clock(first,live=True,recheck=False)
            self.assertEqual(restarted.current_time(),later)
    def test_pending_extraction_blocks_old_permission(self):
        self.incoming('以后别发新品了。')
        engine=self.engine();self.assertEqual(engine.gate(engine.context(self.request())).action,'needs_info')
        record=extract_event(self.store,Fake({'memories':[{'key':'contact_permission','value':'no_marketing','quote':'以后别发新品了。'}]}),'new-event')
        self.assertEqual(record['status'],'complete')
        self.assertEqual(engine.gate(engine.context(self.request())).action,'no_action')
    def test_fabricated_quote_saves_no_memory(self):
        self.incoming('谢谢。')
        bad={'memories':[{'key':'color','value':'喜欢红色','quote':'喜欢红色'}]}
        record=extract_event(self.store,Fake(bad,bad),'new-event')
        self.assertEqual(record['status'],'failed')
        self.assertNotIn('color',[m['key'] for m in self.store.memories('customer','zhou')])
    def test_multiple_proposals_validated_before_any_write(self):
        self.incoming('喜欢黑色，明天可以联系。')
        bad={'memories':[
            {'key':'color','value':'喜欢黑色','quote':'喜欢黑色'},
            {'key':'contact_after','value':'2026-11-01T10:00:00+08:00','quote':'明天可以联系'}]}
        record=extract_event(self.store,Fake(bad,bad),'new-event')
        self.assertEqual(record['status'],'failed');self.assertEqual(record['applied'],[])
    def test_price_only_quote_cannot_be_saved_as_customer_budget(self):
        self.incoming('不是预算不够，我只是问5200能不能优惠。')
        bad={'memories':[{'key':'budget','value':'5200','quote':'5200'}]}
        result=extract_event(self.store,Fake(bad,bad),'new-event')
        self.assertEqual(result['status'],'failed');self.assertEqual(result['applied'],[])
        self.assertNotIn('budget',[m['key'] for m in self.store.memories('customer','zhou')])
        self.incoming('这次预算最多5200，不能再加。','budget-event')
        result=extract_event(self.store,Fake({'memories':[{'key':'budget','value':'这次预算最多5200','quote':'这次预算最多5200'}]}),'budget-event')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(next(m for m in self.store.memories('customer','zhou') if m['key']=='budget')['value'],'这次预算最多5200')
    def test_manual_permission_conflict_stays_blocked_until_resolved(self):
        m=next(m for m in self.store.memories('customer','zhou') if m['key']=='contact_permission')
        self.store.change_memory(m['id'],MemoryChange(expected_revision=1,value='allowed'))
        self.incoming('新品不用再发了。')
        proposal={'memories':[{'key':'contact_permission','value':'no_marketing','quote':'新品不用再发了。'}]}
        result=extract_event(self.store,Fake(proposal),'new-event')
        self.assertEqual(result['status'],'needs_review')
        engine=self.engine();self.assertEqual(engine.gate(engine.context(self.request())).action,'needs_info')
        self.store.change_memory(m['id'],MemoryChange(expected_revision=2,value='no_marketing'))
        result=extract_event(self.store,Fake(proposal),'new-event')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(engine.gate(engine.context(self.request())).action,'no_action')
    def test_extraction_replay_does_not_repeat_model_call(self):
        self.incoming('谢谢。');provider=Fake({'memories':[]})
        extract_event(self.store,provider,'new-event');extract_event(self.store,provider,'new-event')
        self.assertEqual(len(provider.calls),1)

    def test_bad_permission_value_gets_one_traced_repair(self):
        self.incoming('别主动给我发新品，有需要我自己问。')
        bad={'memories':[{'key':'contact_permission','value':'别主动给我发新品','quote':'别主动给我发新品'}]}
        good={'memories':[{'key':'contact_permission','value':'no_marketing','quote':'别主动给我发新品'}]}
        provider=Fake(bad,good)
        result=extract_event(self.store,provider,'new-event')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(result['attempts']),2)
        self.assertIn('联系意愿值无效',result['attempts'][0]['issues'][0])
        self.assertEqual(provider.calls[1]['event']['text'],'别主动给我发新品，有需要我自己问。')
        self.assertEqual(json.loads(provider.calls[1]['repair']['previous']),bad)
        self.assertEqual(self.engine().gate(self.engine().context(self.request())).action,'no_action')

    def test_failed_repairs_block_and_manual_retry_keeps_prior_evidence(self):
        self.incoming('别主动给我发新品。')
        bad={'memories':[{'key':'contact_permission','value':'拒绝新品','quote':'别主动给我发新品'}]}
        first=extract_event(self.store,Fake(bad,bad),'new-event')
        self.assertEqual(first['status'],'failed')
        self.assertEqual(first['applied'],[])
        self.assertEqual(self.engine().gate(self.engine().context(self.request())).action,'needs_info')
        second=extract_event(self.store,Fake({'memories':[{'key':'contact_permission','value':'no_marketing','quote':'别主动给我发新品'}]}),'new-event')
        self.assertEqual(second['status'],'complete')
        self.assertEqual(second['previous_runs'][0],first)

    def test_extraction_outage_is_not_guessed_or_retried_as_format_repair(self):
        self.incoming('别主动给我发新品。')
        provider=Fake(ProviderError('network','测试网络错误'))
        result=extract_event(self.store,provider,'new-event')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(provider.calls),1)
        self.assertEqual(result['applied'],[])

    def test_birthday_gift_is_not_a_birthday_date(self):
        self.incoming('给姐姐选生日礼物，她喜欢白色。')
        recipient={'key':'recipient','value':'姐姐','quote':'给姐姐选生日礼物'}
        bad={'memories':[recipient,{'key':'birthday','value':'姐姐生日','quote':'给姐姐选生日礼物'}]}
        provider=Fake(bad,{'memories':[recipient]})
        result=extract_event(self.store,provider,'new-event')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(provider.calls),2)
        self.assertNotIn('birthday',[m['key'] for m in self.store.memories('customer','zhou')])
        self.assertIn('recipient',[m['key'] for m in self.store.memories('customer','zhou')])

    def test_extractor_receives_local_clock_and_accepts_actual_birthday(self):
        self.incoming('今天是我生日。')
        provider=Fake({'memories':[{'key':'birthday','value':'今天是我生日','quote':'今天是我生日'}]})
        result=extract_event(self.store,provider,'new-event')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(provider.calls[0]['business_timezone'],'Asia/Shanghai')
        self.assertEqual(provider.calls[0]['event_local_time'],'2026-11-07T10:00:00+08:00')
        self.assertEqual(provider.calls[0]['event']['occurred_at'],'2026-11-07T02:00:00+00:00')
    def test_no_action_heartbeat_does_not_send_or_generate(self):
        engine=self.engine();relations=Relations(engine)
        relations.schedule(self.request(customer_id='liang'),'测试复查')
        first=relations.tick(AS_OF);second=relations.tick(AS_OF)
        self.assertEqual(first['reviews'][0]['status'],'waiting_event');self.assertEqual(second['reviews'],[])
        self.assertEqual(engine.generator.calls,[]);self.assertEqual(self.store.drafts(),[])
    def test_repeat_context_review_reuses_result(self):
        provider=Fake({'action':'contact','scenario':'price','purpose':'回应还价','reason':'客户主动提问','product_ids':['TRANSIT']})
        engine=self.engine(provider);relations=Relations(engine);req=self.request(trigger='reply')
        first=relations.assess(req);second=relations.assess(req)
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['reused']);self.assertEqual(len(provider.calls),1)
    def test_due_time_only_rechecks_at_customer_requested_time(self):
        self.store.put_memory(MemoryInput(scope='customer',owner_id='zhou',key='contact_after',value='2026-11-08T10:00:00+08:00',provenance='explicit',source='测试夹具',effective_at=AS_OF))
        provider=Fake({'action':'no_action','scenario':'reactivation','purpose':'等待新理由','reason':'约定时间到但没有有用的新内容','revisit_when':'有客户相关的新内容后','product_ids':[]})
        engine=self.engine(provider);relations=Relations(engine);relations.schedule(self.request(),'约定联系')
        self.assertEqual(relations.tick(AS_OF)['reviews'][0]['status'],'scheduled')
        self.assertEqual(relations.tick('2026-11-08T09:59:00+08:00')['reviews'],[])
        self.assertEqual(relations.tick('2026-11-08T10:01:00+08:00')['reviews'][0]['status'],'waiting_event')
        self.assertEqual(len(provider.calls),1)
    def test_persistent_jobs_deduplicate_active_request(self):
        ready=threading.Event();release=threading.Event();calls=[]
        def handler(payload):calls.append(payload);ready.set();release.wait(3);return {'saved':True}
        queue=JobQueue(self.store,{'test':handler})
        one=queue.submit('test',{'x':1});ready.wait(2);two=queue.submit('test',{'x':1})
        self.assertEqual(one,two);release.set();queue.pool.shutdown(wait=True)
        self.assertEqual(queue.get(one)['status'],'finished');self.assertEqual(len(calls),1)
        reopened=JobQueue(RelationshipStore(self.path),{'test':handler})
        self.assertTrue(reopened.get(one)['saved']);reopened.pool.shutdown()
    def test_restart_marks_only_running_interrupted_not_success(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO jobs(id,kind,payload,status,created_at) VALUES(?,?,?,?,?)',('crash','test','{}','running',now()))
        queue=JobQueue(self.store,{'test':lambda p:None},recover=True)
        self.assertEqual(queue.get('crash')['status'],'interrupted');queue.pool.shutdown()
    def test_schema_one_migrates_without_losing_old_rows(self):
        path=Path(self.temp.name)/'old.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript((ROOT/'atelier/migrations/001.sql').read_text())
            db.execute('PRAGMA user_version=1')
            db.execute('INSERT INTO customers VALUES(?,?,?,?,?,?)',('old','旧客户','','','forest',now()))
        db.close()
        migrated=RelationshipStore(path)
        self.assertEqual(migrated.customer('old')['name'],'旧客户')
        with migrated.connect() as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],9)

if __name__=='__main__':unittest.main()
