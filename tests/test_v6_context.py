"""Version isolation and genuine ablations; no external calls."""
import json
import tempfile
import unittest
from pathlib import Path
from atelier.engine import Engine
from atelier.models import Customer, DraftRequest, Event
from atelier.store import ROOT, RelationshipStore
from lanxu.provider import ModelReply


class Provider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, json.loads(user)))
        return ModelReply(json.dumps(self.result, ensure_ascii=False), {'provider':'test-fixture'})


class V6ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RelationshipStore(Path(self.temp.name)/'probe.sqlite3')
        self.store.add_customer(Customer(id='one', name='虚构客户'))
        self.store.add_event(Event(id='first', customer_id='one', occurred_at='2026-11-13T09:00:00+08:00',
                                  kind='message', actor='client', text='看看四把钥匙用的小包。'))
        self.store.add_event(Event(id='second', customer_id='one', occurred_at='2026-11-13T09:01:00+08:00',
                                  kind='message', actor='client', text='有什么特别的？'))
        self.output = {'action':'contact','scenario':'price','purpose':'解释设计','reason':'客户询问',
            'revisit_when':'','recommended_id':'a','candidates':[
                {'id':cid,'label':'说明设计','text':'这款可装四把普通钥匙。','reason':'回应用途','evidence':['product:ETUI04']}
                for cid in 'abc']}

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return Engine(self.store, Provider(self.output), Provider({'verdict':'pass','issues':[]}),
                      data_dir=ROOT/'data/v6', prompt_dir=ROOT/'prompts/v6')

    def request(self, variant='full'):
        return DraftRequest(customer_id='one', as_of='2026-11-13T10:00:00+08:00',
                            scenario='price', product_ids=['ETUI04'], variant=variant)

    def test_new_brand_fields_cannot_leak_into_no_brand(self):
        context = self.engine().context(self.request())
        self.assertTrue(context['products'][0]['design_notes'])
        context['products'][0]['future_story_field'] = 'Another narrative channel'
        stripped = Engine.ablate(context, 'no_brand')
        for field in ['design_notes','value_story','construction','gallery','future_story_field']:
            self.assertNotIn(field, stripped['products'][0])
        self.assertEqual(stripped['products'][0]['price_cny'], context['products'][0]['price_cny'])
        self.assertEqual(stripped['brand']['policies'], context['brand']['policies'])
        self.assertEqual(stripped['events'], context['events'])
        self.assertFalse(any(e.startswith('brand:') for e in stripped['allowed_evidence']))

    def test_basic_really_uses_minimal_prompt_and_records_actual_source(self):
        engine = self.engine()
        result = engine.generate(self.request('basic'))
        self.assertEqual(result['status'], 'contact')
        system, context = engine.generator.calls[0]
        self.assertEqual(system, (ROOT/'prompts/v6/basic.txt').read_text())
        self.assertEqual([e['id'] for e in context['events']], ['second'])
        self.assertEqual(context['memories'], [])
        self.assertEqual(context['advisor_preferences'], [])
        self.assertIn('data/v6/catalog.json', result['versions']['files'])
        self.assertIn('prompts/v6/basic.txt', result['versions']['files'])
        self.assertNotIn('data/v4/catalog.json', result['versions']['files'])
        self.assertEqual(len(engine.reviewer.calls), 1)

    def test_current_defaults_and_explicit_legacy_data_remain_available(self):
        old = Engine(self.store, Provider(self.output), Provider({'verdict':'pass','issues':[]}), data_dir=ROOT/'data/v4', prompt_dir=ROOT/'prompts/v4')
        current = self.engine()
        self.assertNotIn('design_notes', old.products[0])
        self.assertIn('design_notes', current.products[0])
        self.assertEqual(old.prompt_dir, ROOT/'prompts/v4')
        self.assertEqual(current.prompt_dir, ROOT/'prompts/v6')
        self.assertEqual(Engine(self.store,Provider(self.output),Provider({})).data_dir,ROOT/'data/v6')
