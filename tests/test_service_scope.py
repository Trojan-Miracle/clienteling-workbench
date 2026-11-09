import tempfile
import unittest
from pathlib import Path
from atelier.models import Customer,Event,MemoryInput,MemoryChange,DraftRequest
from atelier.store import ROOT,RelationshipStore
from atelier.engine import Engine
from atelier.extraction import extract_event
from atelier.runtime import Relations
from test_atelier_engine import Fake


AS_OF='2026-11-08T10:00:00+08:00'


class ServiceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=RelationshipStore(Path(self.temp.name)/'qa.sqlite3')
        self.store.add_customer(Customer(id='client',name='虚构客户'))
        self.event('permission','client','message','新品活动别发，已有订单的进展可以照常告诉我。','2026-11-07T10:00:00+08:00')
        self.memory('contact_permission','no_marketing')
        self.event('repair','system','service','Pont26肩带检测完成，报价200元；需客户确认后维修。','2026-11-08T09:00:00+08:00',{'product_id':'PONT26'})
        self.output={'action':'contact','scenario':'care','purpose':'通知售后报价','reason':'客户允许订单服务联系',
          'revisit_when':'','recommended_id':'a','candidates':[{'id':x,'label':'报价确认','text':'肩带检测完成，维修报价200元，需要您确认后再处理。',
          'reason':'只报告获准通知的服务进展','evidence':['event:repair']} for x in 'abc']}

    def tearDown(self):self.temp.cleanup()
    def event(self,id,actor,kind,text,at,data=None):
        return self.store.add_event(Event(id=id,customer_id='client',actor=actor,kind=kind,text=text,occurred_at=at,data=data or {}))
    def memory(self,key,value,**kw):
        return self.store.put_memory(MemoryInput(scope='customer',owner_id='client',key=key,value=value,
          provenance='explicit',source='QA声明许可',effective_at='2026-11-07T10:00:00+08:00',**kw))
    def allow_service(self,**kw):return self.memory('service_permission','allowed',applicability='已有订单的进展可以照常告诉我',**kw)
    def engine(self,generator=None):
        return Engine(self.store,generator or Fake([self.output]),Fake([{'verdict':'pass','issues':[]}]),data_dir=ROOT/'data/v6',prompt_dir=ROOT/'prompts/v6')
    def request(self,**kw):return DraftRequest(**({'customer_id':'client','as_of':AS_OF,'scenario':'care','trigger':'event'}|kw))
    def gate(self,**kw):
        e=self.engine();return e.gate(e.context(self.request(**kw)))

    def test_order_alone_does_not_grant_service_permission(self):
        e=self.engine();r=e.generate(self.request())
        self.assertEqual(r['status'],'no_action');self.assertEqual(e.generator.calls,[])

    def test_explicit_service_consent_limits_catalog_and_preserves_freshness(self):
        self.allow_service();e=self.engine();r=e.generate(self.request())
        self.assertEqual(r['status'],'contact')
        self.assertEqual([p['id'] for p in r['context']['products']],['PONT26'])
        self.assertEqual(r['context']['contact_scope']['event_ids'],['repair'])
        self.assertTrue(e.is_current(r))
        self.assertEqual(e.ablate(r['context'],'no_memory')['events'][-1]['id'],'repair')

    def test_global_refusal_and_unrelated_scenarios_remain_blocked(self):
        self.allow_service()
        for scene in ['greeting','price','recommendation','invitation','reactivation']:
            self.assertEqual(self.gate(scenario=scene).action,'no_action')
        self.assertEqual(self.gate(product_ids=['TRANSIT']).action,'no_action')
        permission=next(m for m in self.store.memories('customer','client') if m['key']=='contact_permission')
        self.store.change_memory(permission['id'],MemoryChange(expected_revision=1,value='do_not_contact'))
        self.assertEqual(self.gate().action,'no_action')

    def test_deleted_or_expired_consent_does_not_open_exception(self):
        m=self.allow_service(expires_at='2026-11-08T09:30:00+08:00')
        self.assertEqual(self.gate().action,'no_action')
        self.store.change_memory(m['id'],MemoryChange(expected_revision=1,expires_at=None))
        self.assertIsNone(self.gate())
        self.store.change_memory(m['id'],MemoryChange(expected_revision=2,status='deleted'))
        self.assertEqual(self.gate().action,'no_action')

    def test_prior_response_requires_contextual_judgement_not_blanket_suppression(self):
        self.allow_service();self.event('notified','advisor','message','肩带报价200元，请确认。','2026-11-08T09:30:00+08:00')
        self.assertIsNone(self.gate())
        assessment={'action':'no_action','scenario':'care','purpose':'等待确认','reason':'刚刚已经通知报价',
                    'revisit_when':'客户确认或有新进展后','product_ids':['PONT26']}
        self.assertEqual(Relations(self.engine(Fake([assessment]))).assess(self.request())['assessment']['action'],'no_action')
        self.event('progress','system','service','维修已完成，待客户确认取回方式。','2026-11-08T09:45:00+08:00',{'product_id':'PONT26'})
        self.assertIsNone(self.gate())

    def test_contact_after_still_delays_authorized_service(self):
        self.allow_service();self.memory('contact_after','2026-11-09T10:00:00+08:00')
        self.assertEqual(self.gate().action,'defer')

    def test_wrong_scenario_fails_even_when_model_labels_are_otherwise_valid(self):
        self.allow_service();bad=self.output|{'scenario':'recommendation'}
        result=self.engine(Fake([bad,bad])).generate(self.request())
        self.assertEqual(result['status'],'failed')
        assessment={'action':'contact','scenario':'invitation','purpose':'邀请','reason':'营销','product_ids':['PONT26']}
        self.assertEqual(Relations(self.engine(Fake([assessment]))).assess(self.request())['status'],'failed')

    def test_extraction_keeps_service_permission_separate_and_scoped(self):
        proposal={'memories':[{'key':'contact_permission','value':'no_marketing','quote':'新品活动别发'},
          {'key':'service_permission','value':'allowed','quote':'已有订单的进展可以照常告诉我'}]}
        result=extract_event(self.store,Fake([proposal]),'permission',prompt_dir=ROOT/'prompts/v6')
        self.assertEqual(result['status'],'complete')
        service=next(m for m in self.store.memories('customer','client') if m['key']=='service_permission')
        self.assertEqual(service['applicability'],'已有订单的进展可以照常告诉我')
        self.assertIsNone(self.gate())

    def test_same_permission_value_with_new_scope_updates_instead_of_being_skipped(self):
        self.allow_service()
        self.event('narrow','client','message','以后只告诉我Pont26维修的进展。','2026-11-08T09:15:00+08:00')
        proposal={'memories':[{'key':'service_permission','value':'allowed','quote':'以后只告诉我Pont26维修的进展'}]}
        result=extract_event(self.store,Fake([proposal]),'narrow',prompt_dir=ROOT/'prompts/v6')
        self.assertEqual(result['status'],'complete')
        service=next(m for m in self.store.memories('customer','client') if m['key']=='service_permission')
        self.assertEqual(service['applicability'],'以后只告诉我Pont26维修的进展')
        self.assertEqual(service['revision'],2)
