import json
import tempfile
import unittest
from pathlib import Path
from atelier.models import DraftRequest, MemoryInput, MemoryChange, PreferenceAnswer, Event
from atelier.store import RelationshipStore, Conflict
from atelier.seed import seed_demo
from atelier.engine import Engine
from atelier.preferences import answer_question, feedback
from atelier.app import create_app
from lanxu.provider import ModelReply, ProviderError

AS_OF='2026-11-07T10:00:00+08:00'


def decision():
    return {'action':'contact','scenario':'price','purpose':'回复还价','reason':'客户主动询问价格',
            'revisit_when':'','recommended_id':'a','candidates':[
                {'id':cid,'label':'直接回应','text':'这款目前没有折扣。'+suffix,'reason':'回应明确问题','evidence':['policy:pricing']}
                for cid,suffix in zip('abc',['','价格是5200元。','无法调整到5000元。'])]}


class Fake:
    provider='fixture';model='test-only'
    def __init__(self,items):self.items=iter(items);self.calls=[]
    def complete(self,system,user):
        self.calls.append(json.loads(user))
        value=next(self.items)
        if isinstance(value,Exception):raise value
        return ModelReply(json.dumps(value,ensure_ascii=False),{'provider':'test-fixture'})


class AvailabilityGuardTests(unittest.TestCase):
    def test_only_cited_advisor_confirmation_can_proceed_to_semantic_review(self):
        from atelier.models import Decision
        data=decision();data['candidates'][0]['text']='已经为你预留好了。'
        context={'allowed_evidence':['policy:pricing','revision:confirmed_facts'],
                 'revision_request':{'confirmed_facts':'负责人已确认这位客户的预约已留位。'}}
        self.assertTrue(Engine.checks(Decision.model_validate(data),context))
        data['candidates'][0]['evidence'].append('revision:confirmed_facts')
        self.assertFalse(Engine.checks(Decision.model_validate(data),context))
        context.pop('revision_request')
        self.assertTrue(Engine.checks(Decision.model_validate(data),context))

    def test_unsupported_stock_and_reservation_cannot_pass_as_catalog_facts(self):
        from atelier.models import Decision
        for text in ('墨黑有货。','库存还没确认，不过墨黑有货。','现货需要核实，不过墨黑有货。','已经确认有货。','已经为您预留好了。','这款库存充足。','我现在联系沙龙已经确认有货。','现在联系沙龙确认有货了。','现货还要核实，不过已经预留好了。','选定前再核对现货情况，已经确认有货了。'):
            with self.subTest(text=text):
                data=decision();data['candidates'][0]['text']=text
                self.assertTrue(Engine.checks(Decision.model_validate(data),{'allowed_evidence':['policy:pricing']}))

    def test_uncertainty_and_catalog_color_are_not_stock_promises(self):
        from atelier.models import Decision
        for text in ('目录有墨黑，库存还需确认。','深杉绿的现货还需要核实。','不能承诺有货，也未确认预留。','我帮您确认是否有货。','如果有货，我再联系您。','这款不是现货。','我现在联系沙龙确认可售现货。','先别按有货来安排行程。','选定前再核对手机带壳后的适配和现货情况就好。','现货情况需要另查。','现货还要核实。'):
            with self.subTest(text=text):
                data=decision();data['candidates'][0]['text']=text
                self.assertFalse(Engine.checks(Decision.model_validate(data),{'allowed_evidence':['policy:pricing']}))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.sqlite3'
        self.store=RelationshipStore(self.path);seed_demo(self.store)
    def tearDown(self):self.tmp.cleanup()
    def engine(self,outputs=None,reviews=None):
        return Engine(self.store,Fake(outputs or [decision()]),Fake(reviews or [{'verdict':'pass','issues':[]}]))
    def request(self,**kw):
        return DraftRequest(**({'customer_id':'zhou','as_of':AS_OF,'product_ids':['TRANSIT'],'scenario':'price'}|kw))

    def test_opt_out_blocks_timer_without_model_call(self):
        engine=self.engine();record=engine.generate(self.request(customer_id='liang',trigger='timer'))
        self.assertEqual(record['status'],'no_action');self.assertEqual(engine.generator.calls,[])
        self.assertEqual(self.store.draft(record['id'])['result']['candidates'],[])

    def test_new_customer_auto_uses_catalog_but_explicit_selection_stays_narrow(self):
        from atelier.models import Customer
        self.store.add_customer(Customer(id='fresh',name='测试新客户'))
        self.store.add_event(Event(id='fresh-question',customer_id='fresh',occurred_at=AS_OF,
            kind='message',actor='client',text='Transit和Pont 20都能装电脑吗？'))
        engine=self.engine()
        context=engine.context(self.request(customer_id='fresh',scenario='auto',product_ids=[]))
        self.assertEqual({p['id'] for p in context['products']},{p['id'] for p in engine.products})
        self.assertIn('product:TRANSIT',context['allowed_evidence'])
        self.assertIn('不能装电脑',next(p['capacity'] for p in context['products'] if p['id']=='TRANSIT'))
        narrow=engine.context(self.request(customer_id='fresh',product_ids=['TRANSIT']))
        self.assertEqual([p['id'] for p in narrow['products']],['TRANSIT'])
        greeting=engine.context(self.request(customer_id='fresh',scenario='greeting',product_ids=[]))
        self.assertEqual({p['id'] for p in greeting['products']},{p['id'] for p in engine.products})  # Greetings can also contain product questions.

    def test_new_inbound_service_question_not_blocked_by_marketing_opt_out(self):
        self.store.add_event(Event(id='service-inbound',customer_id='liang',occurred_at=AS_OF,kind='message',actor='client',text='请问肩带能更换吗？'))
        engine=self.engine();self.assertIsNone(engine.gate(engine.context(self.request(customer_id='liang'))))
        self.assertEqual(engine.gate(engine.context(self.request(customer_id='liang',trigger='timer'))).action,'no_action')

    def test_automatic_drafting_has_activity_facts_without_manual_event_selection(self):
        engine=self.engine()
        context=engine.context(self.request(scenario='auto',product_ids=[]))
        self.assertIsNone(context['boutique_event'])
        event=next(e for e in context['boutique_events'] if e['id']=='bridge-preview')
        self.assertIn('最多2人',event['access'])
        self.assertIn('boutique:bridge-preview',context['allowed_evidence'])
        selected=engine.context(self.request(event_id='bridge-preview'))
        self.assertEqual(selected['boutique_events'],[selected['boutique_event']])
        with self.assertRaisesRegex(ValueError,'活动资料不存在'):
            engine.context(self.request(event_id='missing'))

    def test_old_draft_compatibility_does_not_ignore_changes_to_available_event_facts(self):
        import copy
        engine=self.engine()
        current=engine.generate(self.request())
        legacy=copy.deepcopy(current)
        legacy['context'].pop('boutique_events')
        legacy['context']['allowed_evidence']=[e for e in legacy['context']['allowed_evidence'] if not e.startswith('boutique:')]
        self.assertTrue(engine.is_current(legacy))
        self.assertTrue(engine.is_current(current))
        current['context']['boutique_events'][0]['access']='已过时的活动人数规则'
        self.assertFalse(engine.is_current(current))

    def test_contact_after_changes_decision_with_time(self):
        self.store.put_memory(MemoryInput(scope='customer',owner_id='zhou',key='contact_after',value='2026-11-08T10:00:00+08:00',
            provenance='explicit',source='测试夹具：客户要求明天再联系',effective_at=AS_OF))
        engine=self.engine()
        before=engine.gate(engine.context(self.request(trigger='timer')))
        after=engine.gate(engine.context(self.request(trigger='timer',as_of='2026-11-08T11:00:00+08:00')))
        self.assertEqual(before.action,'defer');self.assertIsNone(after)

    def test_naive_contact_time_requires_clarification(self):
        self.store.put_memory(MemoryInput(scope='customer',owner_id='zhou',key='contact_after',value='2026-11-08T10:00:00',provenance='manual',source='测试',effective_at=AS_OF))
        engine=self.engine();self.assertEqual(engine.gate(engine.context(self.request(trigger='timer'))).action,'needs_info')

    def test_generation_reviewer_failure_not_success(self):
        engine=self.engine(reviews=[ProviderError('network','测试网络错误')])
        record=engine.generate(self.request());self.assertEqual(record['status'],'failed')
        self.assertIsNone(record['result']);self.assertEqual(len(record['attempts']),2)

    def test_bad_evidence_repaired_once_then_fail(self):
        bad=decision();bad['candidates'][0]['evidence']=['product:FAKE']
        engine=self.engine([bad,bad]);record=engine.generate(self.request())
        self.assertEqual(record['status'],'failed');self.assertEqual(len(engine.generator.calls),2)
        self.assertEqual(engine.reviewer.calls,[])

    def test_malformed_review_retries_same_candidates_once(self):
        invalid={'verdict':'pass','issues':['格式测试']*9}
        engine=self.engine(reviews=[invalid,{'verdict':'pass','issues':[]}])
        record=engine.generate(self.request())
        self.assertEqual(record['status'],'contact')
        self.assertEqual(len(engine.generator.calls),1)
        self.assertEqual(engine.reviewer.calls[0],engine.reviewer.calls[1])
        self.assertEqual(record['attempts'][-1]['stage'],'review_format_retry')
        self.assertEqual(json.loads(record['attempts'][1]['raw']),invalid)

    def test_two_malformed_reviews_never_release_candidates(self):
        invalid={'verdict':'revise','issues':[]}
        engine=self.engine(reviews=[invalid,invalid])
        record=engine.generate(self.request())
        self.assertEqual(record['status'],'failed');self.assertIsNone(record['result'])
        self.assertEqual(len(engine.reviewer.calls),2);self.assertEqual(len(engine.generator.calls),1)

    def test_short_thank_you_allowed(self):
        output=decision();output.update(scenario='conversation',purpose='回应感谢')
        for c in output['candidates']:c['text']='不客气';c['evidence']=[]
        engine=self.engine([output]);record=engine.generate(self.request(customer_id='wu'))
        self.assertEqual(record['status'],'contact')

    def test_detailed_reply_survives_generation_review_and_adoption(self):
        # A multi-part explanation must reach review and be saved verbatim,
        # instead of being rejected before review by the old 300-char cap.
        text=('这款目前没有折扣，价格是5200元，不能以5000元成交。'
              'Transit的主体是高密织物，搭配小牛皮包角和棉质内衬，约重350克；'
              '与Pont 26相比轻260克，但这是重量差，不是另一只包的重量。'
              '包身外尺寸为23×16×7厘米，可放手机、短钱包、证件与钥匙，不能放电脑或长伞。'
              '有可调织物肩带，可以肩背或斜挎；它没有Pont系列的横桥扣。'
              '主空间外另有内部证件袋，双向拉链方便从两侧取用，但证件袋没有RFID屏蔽功能。'
              '日常护理可以局部轻拭，不能机洗、浸泡或使用酒精，它也不是防水包。'
              '标价包含标准防尘袋与品牌纸盒，礼物丝带和空白贺卡不另收费；这些是标准包装，不抵扣货价。'
              '墨黑是现有配色，现货与预留需要核实，不能仅凭商品资料保证有货。')
        self.assertGreater(len(text),300)
        output=decision()
        for candidate in output['candidates']:candidate['text']=text
        engine=self.engine([output]);record=engine.generate(self.request())
        self.assertEqual(record['status'],'contact')
        self.assertEqual(len(engine.generator.calls),1)
        self.assertEqual(engine.reviewer.calls[0]['decision']['candidates'][0]['text'],text)
        self.assertEqual(self.store.draft(record['id'])['result']['candidates'][0]['text'],text)
        feedback(self.store,'default',draft_id=record['id'],mode='calibration',outcome='choice',candidate_id='a',
            display_order=['a','b','c'],selected_text=text,learning_scope='once')
        with self.store.connect() as db:
            saved=db.execute('SELECT selected_text FROM feedback WHERE draft_id=?',(record['id'],)).fetchone()[0]
        self.assertEqual(saved,text)

    def test_preference_question_is_used_even_in_historical_demo(self):
        memory=answer_question(self.store,'default',PreferenceAnswer(question_id='length',answer='一两句，把重点说清楚'))
        engine=self.engine();engine.generate(self.request())
        self.assertEqual(engine.generator.calls[0]['advisor_preferences'][0]['id'],memory['id'])

    def test_saved_candidate_becomes_stale_after_memory_change(self):
        engine=self.engine();draft=engine.generate(self.request())
        self.assertTrue(engine.is_current(draft))
        memory=self.store.memories('customer','zhou')[0]
        self.store.change_memory(memory['id'],MemoryChange(expected_revision=1,value='客户已纠正的偏好'))
        self.assertFalse(engine.is_current(draft))
        self.assertEqual(self.store.draft(draft['id'])['result'],draft['result'])

    def test_copy_keeps_selected_text_after_learning_but_blocks_changed_facts(self):
        engine=self.engine();draft=engine.generate(self.request())
        feedback(self.store,'default',draft['id'],'calibration','choice','a',['a','b','c'])
        # Adding this same draft to its customer's reference history does not invalidate it.
        self.assertTrue(engine.is_current(draft))
        self.assertEqual(engine.context(self.request())['customer_reply_history'][0]['draft_id'],draft['id'])
        self.assertTrue(engine.is_current(draft,ignore_preferences=True))
        app=create_app(engine,seed=False);client=app.test_client()
        path='/api/drafts/'+draft['id']+'/check-copy'
        self.assertEqual(client.post(path,json={'as_of':AS_OF}).status_code,200)
        self.store.add_event(Event(id='changed-fact',customer_id='zhou',occurred_at=AS_OF,kind='message',actor='client',text='我不买包了，刚刚问错了。'))
        self.assertEqual(client.post(path,json={'as_of':AS_OF}).status_code,409)

    def test_time_and_new_message_invalidate_old_candidate(self):
        engine=self.engine();draft=engine.generate(self.request())
        self.assertFalse(engine.is_current(draft,'2026-11-08T10:00:00+08:00'))
        self.store.add_event(Event(id='latest-question',customer_id='zhou',occurred_at=AS_OF,
            kind='message',actor='client',text='我现在只想问护理，不讨论价格了。'))
        self.assertFalse(engine.is_current(draft))

    def test_calibration_choice_learns_scoped_example_but_blind_does_not(self):
        engine=self.engine();draft=engine.generate(self.request())
        result=feedback(self.store,'default',draft['id'],'blind','choice','b',['c','a','b'])
        self.assertFalse(result['learned']);self.assertEqual(self.store.memories('advisor','default'),[])
        learned=feedback(self.store,'default',draft['id'],'calibration','choice','a',['c','a','b'],feedback_id='one')
        self.assertTrue(learned['learned'])
        example=self.store.memories('advisor','default')[0]
        self.assertEqual(example['applicability'],'price');self.assertEqual(example['provenance'],'inferred')
        self.assertIn(draft['result']['candidates'][0]['text'],example['value'])
        second=feedback(self.store,'default',draft['id'],'calibration','choice','a',['c','a','b'],feedback_id='one')
        self.assertTrue(second['duplicate'])

    def test_deleted_preference_not_restored_by_next_choice(self):
        draft=self.engine().generate(self.request())
        feedback(self.store,'default',draft['id'],'calibration','choice','a',['a','b','c'])
        memory=self.store.memories('advisor','default')[0]
        self.store.change_memory(memory['id'],MemoryChange(expected_revision=1,status='deleted'))
        result=feedback(self.store,'default',draft['id'],'calibration','choice','b',['a','b','c'],expected_revision=1)
        self.assertFalse(result['learned']);self.assertEqual(self.store.context('zhou',AS_OF)['advisor_preferences'],[])

    def test_seed_restart_does_not_restore_deleted_customer_memory(self):
        memory=self.store.memories('customer','zhou')[0]
        self.store.change_memory(memory['id'],MemoryChange(expected_revision=1,status='deleted'))
        reopened=RelationshipStore(self.path);self.assertEqual(seed_demo(reopened)['new_customers'],0)
        self.assertEqual(reopened.memory(memory['id'])['status'],'deleted')

    def test_api_memories_revision_and_cross_origin(self):
        app=create_app(self.engine(),seed=False);client=app.test_client()
        memory=self.store.memories('customer','zhou')[0]
        path='/api/memories/'+memory['id']
        self.assertEqual(client.patch(path,json={'expected_revision':1,'value':'改写'},headers={'Origin':'https://other.invalid'}).status_code,403)
        self.assertEqual(client.patch(path,json={'expected_revision':1,'value':'明确修改'}).status_code,200)
        self.assertEqual(client.patch(path,json={'expected_revision':1,'value':'旧版本覆盖'}).status_code,409)
        self.assertEqual(client.get('/api/customers').status_code,200)

    def test_purchase_event_persists_and_schedules_review_without_sending(self):
        engine=self.engine();app=create_app(engine,seed=False);client=app.test_client()
        response=client.post('/api/customers/zhou/events',json={'id':'purchase-check','occurred_at':AS_OF,
            'kind':'purchase','actor':'system','text':'虚构订单：Transit已交付。','data':{'product_id':'TRANSIT'}})
        self.assertEqual(response.status_code,201)
        context=engine.context(self.request())
        self.assertTrue(any(e['id']=='purchase-check' for e in context['events']))
        followup=app.extensions['relations'].list()[0]
        self.assertEqual(followup['request']['trigger'],'event')
        self.assertEqual(followup['request']['product_ids'],['TRANSIT'])
        self.assertEqual(engine.generator.calls,[])


if __name__=='__main__':unittest.main()
