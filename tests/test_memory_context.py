import tempfile
import unittest
from pathlib import Path

from atelier.engine import Engine
from atelier.models import Customer, DraftRequest, Event, MemoryInput, MemoryChange, ExampleInput, RuleInput
from atelier.preferences import add_example, add_rule
from atelier.preference_document import read_document, save_document
from atelier.retrieval import ExampleRetriever
from atelier.store import RelationshipStore, Conflict
from atelier.app import create_app
from test_atelier_engine import Fake, decision

AT='2026-11-07T10:00:00+08:00'


class MemoryContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=RelationshipStore(Path(self.tmp.name)/'app.sqlite3')
        self.store.add_customer(Customer(id='one',name='阿澄'))
        self.store.add_event(Event(id='inbound',customer_id='one',occurred_at=AT,kind='message',actor='client',
            text='今天生日，晚上跟朋友吃饭。顺便问问Pont 26能装电脑吗？'))
        self.engine=Engine(self.store,Fake([decision()]),Fake([{'verdict':'pass','issues':[]}]))

    def tearDown(self):self.tmp.cleanup()

    def request(self,**kw):
        return DraftRequest(**({'customer_id':'one','as_of':AT,'scenario':'greeting'}|kw))

    def example(self,id,text,scope='所有情境',owner='default'):
        return add_example(self.store,ExampleInput(id=id,text=text,applicability=scope),owner)

    def test_mixed_topics_recall_across_labels_and_include_product_facts(self):
        birthday=self.example('birthday','客户：今天生日，晚上跟朋友吃饭。参考表达：生日快乐！今晚吃得开心。','greeting')
        product=self.example('product','客户：Pont 26能装电脑吗？参考表达：装不了电脑，需要带电脑可以看更大的款式。','recommendation')
        context=self.engine.context(self.request())
        self.assertEqual({m['id'] for m in context['advisor_preferences']},{birthday['id'],product['id']})
        self.assertIn('PONT26',{p['id'] for p in context['products']})
        self.assertEqual(context['context_selection']['examples']['eligible_count'],2)

    def test_other_advisor_expired_disabled_deleted_never_reenter(self):
        first=self.example('one','生日快乐。','greeting')
        other=self.example('other','生日问候只写秘密表达。','greeting','someone_else')
        context=self.engine.context(self.request())
        self.assertNotIn(other['id'],{m['id'] for m in context['advisor_preferences']})
        self.store.change_memory(first['id'],MemoryChange(expected_revision=1,status='disabled'))
        self.assertEqual(self.engine.context(self.request())['advisor_preferences'],[])
        self.store.change_memory(first['id'],MemoryChange(expected_revision=2,status='active',value='客户问电脑，参考表达：这个包放不下电脑。'))
        updated=self.engine.context(self.request())['advisor_preferences'][0]
        self.assertNotIn('生日快乐',updated['value']);self.assertEqual(updated['revision'],3)
        self.store.change_memory(first['id'],MemoryChange(expected_revision=3,expires_at='2099-01-01T00:00:00+08:00'))
        from unittest.mock import patch
        with patch('atelier.store.now',return_value='2100-01-01T00:00:00+00:00'):
            self.assertEqual(self.engine.context(self.request())['advisor_preferences'],[])
        self.store.change_memory(first['id'],MemoryChange(expected_revision=4,status='deleted'))
        self.assertEqual(self.engine.context(self.request())['advisor_preferences'],[])

    def test_global_edit_does_not_consume_conditions_examples_or_expiry(self):
        personal=add_rule(self.store,RuleInput(id='global',text='自然称呼，不重复问候。',applicability='所有情境'))
        conditional=add_rule(self.store,RuleInput(id='mixed',text='祝福简短，商品问题仍需答清楚。',applicability='生日当天同时咨询商品时'))
        limited=self.store.put_memory(MemoryInput(scope='advisor',owner_id='default',key='temporary',value='活动期间不用昵称。',
            provenance='manual',source='测试：有效期规则',effective_at='2026-01-01T00:00:00+08:00',expires_at='2099-01-01T00:00:00+08:00'))
        sample=self.example('sample','生日快乐，今晚吃得尽兴。')
        before=[self.store.memory(x['id']) for x in (conditional,limited,sample)]
        doc=read_document(self.store,global_only=True)
        self.assertEqual(doc['text'],personal['value']);self.assertNotIn('【',doc['text'])
        updated=save_document(self.store,'不必每次称呼。',doc['revision'],global_only=True)
        self.assertEqual(updated['text'],'不必每次称呼。')
        self.assertEqual(before,[self.store.memory(x['id']) for x in (conditional,limited,sample)])
        with self.assertRaises(Conflict):save_document(self.store,'旧窗口覆盖',doc['revision'],global_only=True)
        self.assertEqual(read_document(self.store,global_only=True)['text'],'不必每次称呼。')
        context=self.engine.context(self.request())
        rule=next(m for m in context['advisor_preferences'] if m['id']==conditional['id'])
        self.assertEqual(rule['applicability'],'生日当天同时咨询商品时')

    def test_cold_start_uses_brand_without_inventing_personal_memory(self):
        context=self.engine.context(self.request())
        self.assertEqual(context['advisor_preferences'],[])
        self.assertEqual(context['context_selection']['personalization'],'brand_default')
        self.assertTrue(context['brand']['policies']);self.assertTrue(context['products'])
        self.assertIsNone(self.engine.gate(context))  # Reply to first inbound is allowed.
        active=self.engine.context(self.request(trigger='timer'))
        self.assertEqual(self.engine.gate(active).action,'needs_info')
        self.assertEqual(self.store.memories('advisor','default'),[])

    def test_long_history_pins_current_facts_and_recent_messages(self):
        for i in range(50):
            self.store.add_event(Event(id=f'old-{i:02}',customer_id='one',occurred_at=f'2026-10-{1+i//2:02}T{9+i%2:02}:00:00+08:00',
                kind='message',actor='client',text='最近比较忙。' if i else '我只用绿色的包。'))
        memory=self.store.put_memory(MemoryInput(scope='customer',owner_id='one',key='color',value='只用绿色的包',
            provenance='explicit',source='客户原话',source_event_id='old-00',source_quote='我只用绿色的包。',effective_at=AT))
        context=self.engine.context(self.request());self.engine.gate(context)
        compact=self.engine.compact_history(context)
        ids={e['id'] for e in compact['events']}
        self.assertIn('inbound',ids);self.assertIn('old-00',ids)
        self.assertLess(len(ids),32)
        self.assertIn(memory['id'],{m['id'] for m in compact['memories']})
        self.assertEqual({e[6:] for e in compact['allowed_evidence'] if e.startswith('event:')},ids)

    def test_api_rule_creation_and_global_editor_have_separate_scope(self):
        app=create_app(self.engine,seed=False,automation=False)
        try:
            client=app.test_client()
            payload={'id':'test-scope','text':'先祝福，再回答商品问题。','applicability':'生日并询问商品时'}
            response=client.post('/api/preferences/rules',json=payload)
            self.assertEqual(response.status_code,201)
            self.assertEqual(client.post('/api/preferences/rules',json=payload).json['id'],response.json['id'])
            prefs=client.get('/api/preferences').json
            self.assertEqual(prefs['global_document']['text'],'')
            doc=prefs['global_document']
            self.assertEqual(client.put('/api/preferences/global',json={'text':'语气自然。','revision':doc['revision']}).status_code,200)
            current=self.store.memory(response.json['id'])
            self.assertEqual(current['value'],payload['text']);self.assertEqual(current['applicability'],payload['applicability'])
        finally:app.extensions['job_queue'].pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
