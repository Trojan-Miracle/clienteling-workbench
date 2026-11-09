import json
import tempfile
import unittest
from pathlib import Path
from atelier.app import create_app
from atelier.customer_context import add_customer_rule
from atelier.engine import Engine
from atelier.extraction import extract_event
from atelier.models import Customer, CustomerRuleInput, DraftRequest, Event, MemoryChange, MemoryInput
from atelier.preferences import feedback, add_rule
from atelier.models import RuleInput
from atelier.store import RelationshipStore
from test_atelier_engine import Fake, decision

AT='2026-11-01T10:00:00+08:00'

class CustomerContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=RelationshipStore(Path(self.tmp.name)/'app.sqlite3')
        for cid in ('one','two'):
            self.store.add_customer(Customer(id=cid,name=cid))
            self.store.add_event(Event(id=cid+'-intro',customer_id=cid,occurred_at=AT,kind='message',actor='client',text='我想了解肩带。'))

    def tearDown(self):self.tmp.cleanup()

    def engine(self):return Engine(self.store,Fake([decision()]*30),Fake([{'verdict':'pass','issues':[]}]*30))
    def request(self,**kw):return DraftRequest(**({'customer_id':'one','as_of':AT}|kw))
    def choose(self,engine=None,customer='one',text='肩带调节的操作请再说清楚一些。',**kw):
        engine=engine or self.engine();draft=engine.generate(self.request(customer_id=customer,**kw.pop('request',{})))
        result=feedback(self.store,kw.pop('advisor_id','default'),draft_id=draft['id'],mode=kw.pop('mode','calibration'),
            outcome='choice',candidate_id='a',display_order=['a','b','c'],selected_text=text,**kw)
        return draft,result

    def test_customer_rules_are_separate_from_facts_and_global_preferences(self):
        global_rule=add_rule(self.store,RuleInput(id='global',text='一般直接用名字称呼。',applicability='所有情境'))
        item=add_customer_rule(self.store,'one',CustomerRuleInput(id='local',text='这位客户称徐老师。',as_of=AT))
        self.assertEqual(item['kind'],'rule')
        ctx=self.engine().context(self.request())
        self.assertEqual([m['id'] for m in ctx['customer_preferences']],[item['id']])
        self.assertEqual(ctx['memories'],[])
        self.assertEqual([m['id'] for m in ctx['advisor_preferences']],[global_rule['id']])
        self.assertFalse(self.engine().context(self.request(customer_id='two'))['customer_preferences'])
        self.store.change_memory(item['id'],MemoryChange(expected_revision=1,status='disabled'))
        self.assertFalse(self.engine().context(self.request())['customer_preferences'])

    def test_customer_rule_api_and_revision_conflicts(self):
        app=create_app(self.engine(),seed=False);client=app.test_client()
        try:
            body={'id':'rule','text':'讨论商品时解释使用差别。','applicability':'讨论商品时','as_of':AT}
            result=client.post('/api/customers/one/preferences',json=body)
            self.assertEqual(result.status_code,201);rule=result.json
            self.assertEqual(client.post('/api/customers/one/preferences',json=body).json['id'],rule['id'])
            self.assertEqual(client.post('/api/customers/missing/preferences',json=body).status_code,404)
            self.assertEqual(client.post('/api/customers/one/preferences',json=body|{'text':'另一条'}).status_code,409)
            self.assertFalse(client.get('/api/preferences').json['memories'])
            fact=client.post('/api/customers/one/facts',json=body|{'id':'color','text':'偏好绿色。','applicability':'所有情境'})
            self.assertEqual(fact.status_code,201)
            self.assertEqual(fact.json['kind'],'fact')
            self.assertTrue(any(m['value']=='偏好绿色。' for m in self.engine().context(self.request())['memories']))
            self.assertFalse(self.engine().context(self.request(customer_id='two'))['memories'])
            result=client.patch('/api/memories/'+rule['id'],json={'expected_revision':1,'value':'讨论商品时用客户熟悉的例子。'})
            self.assertEqual(result.json['kind'],'rule')
            self.assertEqual(client.patch('/api/memories/'+rule['id'],json={'expected_revision':1,'status':'disabled'}).status_code,409)
        finally:app.extensions['job_queue'].pool.shutdown(wait=True)

    def test_adopted_text_is_pinned_to_same_customer_not_sent(self):
        draft,result=self.choose(text='采用的编辑稿。')
        self.choose(customer='two',text='另一位客户的草稿。')
        ctx=self.engine().context(self.request());history=ctx['customer_reply_history']
        self.assertEqual([r['text'] for r in history],['采用的编辑稿。'])
        self.assertEqual(history[0]['id'],result['feedback']['id'])
        self.assertEqual(history[0]['delivery_status'],'unverified')
        self.assertFalse(any(e['actor']=='advisor' for e in ctx['events']))
        self.assertNotIn(result['feedback']['memory_id'],[m['id'] for m in ctx['advisor_preferences']])
        self.assertTrue(self.engine().is_current(draft,ignore_preferences=True))

    def test_once_blind_other_advisor_and_future_are_not_history(self):
        self.choose(learning_scope='once')
        self.choose(mode='blind')
        self.choose(advisor_id='other',request={'advisor_id':'other'})
        self.choose(request={'as_of':'2026-11-02T10:00:00+08:00'})
        ctx=self.engine().context(self.request())
        self.assertFalse(ctx['customer_reply_history'])
        self.assertFalse(ctx['advisor_preferences'])

    def test_feedback_can_confirm_requirement_only_for_this_customer(self):
        _,result=self.choose(learning_scope='customer_rule',rule_text='解释护理时给完整步骤。',applicability='护理')
        item=self.store.memory(result['feedback']['memory_id'])
        self.assertEqual((item['scope'],item['owner_id'],item['kind']),('customer','one','rule'))
        self.assertEqual(self.store.memories('advisor','default'),[])
        self.assertEqual(self.engine().context(self.request())['customer_preferences'][0]['value'],'解释护理时给完整步骤。')
        self.assertFalse(self.engine().context(self.request(customer_id='two'))['customer_preferences'])

    def test_editing_adopted_feedback_uses_latest_text(self):
        draft,result=self.choose(text='旧采用稿。');f=result['feedback']
        feedback(self.store,'default',draft_id=draft['id'],mode='calibration',outcome='choice',candidate_id='a',
            display_order=['a','b','c'],selected_text='新采用稿。',feedback_id=f['id'],expected_revision=f['revision'])
        history=self.engine().context(self.request())['customer_reply_history']
        self.assertEqual([r['text'] for r in history],['新采用稿。'])

    def test_withdrawal_cannot_return_through_history_or_general_examples(self):
        self.store.add_event(Event(id='color',customer_id='one',occurred_at=AT,kind='message',actor='client',text='我喜欢深蓝色。'))
        fact=self.store.put_memory(MemoryInput(scope='customer',owner_id='one',key='color',value='我喜欢深蓝色。',
            provenance='explicit',source='客户原话',source_event_id='color',source_quote='我喜欢深蓝色。',effective_at=AT))
        _,choice=self.choose(text='记得你喜欢深蓝色。')
        self.store.change_memory(choice['feedback']['memory_id'],MemoryChange(expected_revision=1,value='先回应你喜欢深蓝色，再说明使用方式。'))
        self.store.change_memory(fact['id'],MemoryChange(expected_revision=1,status='deleted'))
        ctx=self.engine().context(self.request())
        self.assertFalse(ctx['customer_reply_history'])
        self.assertNotIn('深蓝',json.dumps(ctx,ensure_ascii=False))

    def test_disabled_example_no_longer_appears_as_adopted_history(self):
        _,result=self.choose()
        self.store.change_memory(result['feedback']['memory_id'],MemoryChange(expected_revision=1,status='disabled'))
        self.assertFalse(self.engine().context(self.request())['customer_reply_history'])

    def test_recent_and_relevant_older_choices_bounded(self):
        for i in range(9):
            at=f'2026-11-{i+1:02d}T10:00:00+08:00'
            text='肩带怎么调节？' if i==0 else f'今天第{i}次询问包装。'
            self.store.add_event(Event(id='round'+str(i),customer_id='one',occurred_at=at,kind='message',actor='client',text=text))
            self.choose(text='肩带先卸力再调节。' if i==0 else f'第{i}次采用包装的说明。',request={'as_of':at})
        self.store.add_event(Event(id='new-question',customer_id='one',occurred_at='2026-11-10T10:00:00+08:00',kind='message',actor='client',text='肩带怎么调节？'))
        ctx=self.engine().context(self.request(as_of='2026-11-10T10:00:00+08:00'))
        history=ctx['customer_reply_history'];self.assertLessEqual(len(history),6)
        self.assertEqual([r['as_of'][8:10] for r in history[:3]],['09','08','07'])
        self.assertTrue(any('肩带先卸力' in r['text'] for r in history))

    def test_separate_customer_preferences_accumulate_from_explicit_messages(self):
        for i,(key,text) in enumerate([('salutation','以后叫我阿澄。'),('communication_detail','以后说操作步骤时详细一些。')]):
            event=Event(id='preference'+str(i),customer_id='one',occurred_at=AT,kind='message',actor='client',text=text)
            self.store.add_event(event)
            result=extract_event(self.store,Fake([{'memories':[{'key':key,'value':text,'quote':text,'expires_at':None}],'note':''}]),event.id)
            self.assertEqual(result['status'],'complete')
        facts={m['key']:m['value'] for m in self.engine().context(self.request())['memories']}
        self.assertEqual(facts,{'salutation':'以后叫我阿澄。','communication_detail':'以后说操作步骤时详细一些。'})
        self.assertFalse(self.store.memories('advisor','default'))

    def test_ablation_removes_customer_learning(self):
        self.choose(learning_scope='customer_rule',rule_text='按使用情境说明。')
        for variant in ('basic','no_memory','no_preferences'):
            engine=self.engine();ctx=engine.ablate(engine.context(self.request()),variant)
            self.assertFalse(ctx['customer_preferences']);self.assertFalse(ctx['customer_reply_history'])
            self.assertFalse(any(x.startswith(('customer_preference:','adopted_reply:')) for x in ctx['allowed_evidence']))
