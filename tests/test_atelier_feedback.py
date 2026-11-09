import json
import tempfile
import unittest
from pathlib import Path
from atelier.models import MemoryChange, MemoryInput, Event
from atelier.preferences import feedback,feedback_history
from atelier.store import RelationshipStore,Conflict
from atelier.seed import seed_demo
from atelier.engine import Engine
from atelier.app import create_app
import test_atelier_engine as fixtures
from test_atelier_engine import Fake,decision,AS_OF


class FeedbackFlowTests(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp
    tearDown=fixtures.EngineTests.tearDown
    engine=fixtures.EngineTests.engine
    request=fixtures.EngineTests.request
    def choose(self,draft,**changes):
        values=dict(draft_id=draft['id'],mode='calibration',outcome='choice',candidate_id='a',display_order=['a','b','c'])
        return feedback(self.store,'default',**(values|changes))

    def test_manual_example_is_scoped_reference_and_creation_is_idempotent(self):
        engine=self.engine();app=create_app(engine,seed=False);client=app.test_client()
        data={'id':'example-manual-test','text':'这款目前不能调整价格。','context':'客户要求降低售价。','applicability':'price'}
        try:
            response=client.post('/api/preferences/examples',json=data)
            self.assertEqual(response.status_code,201,response.json)
            example=response.json
            self.assertEqual(example['kind'],'example');self.assertEqual(example['provenance'],'manual')
            self.assertEqual(client.post('/api/preferences/examples',json=data).json['id'],example['id'])
            self.assertEqual(len(self.store.memories('advisor','default')),1)
            self.assertEqual(client.post('/api/preferences/examples',json=data|{'text':'另一段内容'}).status_code,409)
            context=engine.context(self.request())
            self.assertEqual(context['advisor_preferences'][0]['id'],example['id'])
            self.assertIn('客户要求降低售价',context['advisor_preferences'][0]['value'])
            self.assertEqual(engine.context(self.request(customer_id='lin',scenario='greeting'))['advisor_preferences'],[])
            self.assertEqual(client.get('/api/preferences').json['document']['text'],'')
        finally:app.extensions['job_queue'].pool.shutdown(wait=True)

    def test_legacy_answer_expansion_preserves_raw_answer_scope_and_manual_edits(self):
        from atelier.models import PreferenceAnswer
        from atelier.preferences import answer_question,QUESTIONS,question_rule
        from scripts.normalize_preference_answers import normalize
        q=next(q for q in QUESTIONS if q['id']=='invitation')
        answer='由当时的沟通目的决定'
        memory=answer_question(self.store,'default',PreferenceAnswer(question_id='invitation',answer=answer,applicability='price'))
        self.assertIn('收尾方式',memory['value']);self.assertIn('没有必须追问',memory['value'])
        # Recreate an unchanged old-format answer, then repair it through the
        # production maintenance path. Other manual edits must stay untouched.
        with self.store.connect() as db:db.execute('UPDATE memories SET value=? WHERE id=?',(answer,memory['id']))
        untouched=answer_question(self.store,'default',PreferenceAnswer(question_id='length',answer='看场景，不设固定长度'))
        untouched=self.store.change_memory(untouched['id'],MemoryChange(expected_revision=untouched['revision'],value='详细说明，不设篇幅限制。'))
        report=normalize(self.store,Path(self.tmp.name)/'normalization')
        self.assertEqual(len(report['changes']),1)
        self.assertEqual(self.store.memory(memory['id'])['applicability'],'price')
        self.assertEqual(self.store.memory(memory['id'])['value'],question_rule(q,answer))
        self.assertEqual(self.store.memory(untouched['id']),untouched)
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT answer FROM questions WHERE question_id='invitation'").fetchone()[0],answer)
        self.assertEqual(normalize(self.store,Path(self.tmp.name)/'normalization-again')['changes'],[])
        sent=self.engine().context(self.request())['advisor_preferences']
        self.assertIn('收尾方式',next(m['value'] for m in sent if m['id']==memory['id']))

    def test_tie_skip_and_no_contact_remain_distinct_without_learning(self):
        engine=self.engine([decision() for _ in range(3)],[{'verdict':'pass','issues':[]}]*3)
        for outcome in ('tie','skip','no_contact'):
            draft=engine.generate(self.request())
            result=self.choose(draft,outcome=outcome,candidate_id=None,reason='本次判断',learning_scope='once' if outcome=='no_contact' else 'example')
            self.assertEqual(result['feedback']['outcome'],outcome)
            self.assertFalse(result['learned'])
        self.assertEqual(self.store.memories('advisor','default'),[])
        self.assertEqual({row['outcome'] for row in feedback_history(self.store)},{'tie','skip','no_contact'})
        self.assertFalse(engine.gate(engine.context(self.request())))

    def test_multiple_samples_and_edited_text_are_retained(self):
        engine=self.engine([decision(),decision()],[{'verdict':'pass','issues':[]}]*2)
        one=engine.generate(self.request());two=engine.generate(self.request())
        self.choose(one,selected_text='这款没有折扣，价格是5200元。')
        self.choose(two,selected_text='不好意思，这款目前不能调整价格。')
        context=engine.context(self.request())
        memory=context['customer_reply_history']
        self.assertEqual(context['advisor_preferences'],[])  # Same-customer choices have a dedicated path.
        self.assertEqual(len(memory),2)
        text=json.dumps(memory,ensure_ascii=False)
        self.assertIn('这款没有折扣，价格是5200元。',text)
        self.assertIn('不好意思，这款目前不能调整价格。',text)
        self.assertNotIn('无法调整到5000元。',text)

    def test_once_does_not_learn_and_explicit_rule_does(self):
        draft=self.engine().generate(self.request())
        first=self.choose(draft,learning_scope='once',reason='这次简单点')
        self.assertFalse(first['learned']);self.assertEqual(self.store.memories('advisor','default'),[])
        second=self.choose(draft,learning_scope='rule',rule_text='价格回应先回答能否降价，不重复预算。',expected_revision=1)
        self.assertTrue(second['learned'])
        memory=self.store.memories('advisor','default')[0]
        self.assertEqual(memory['provenance'],'manual');self.assertEqual(memory['applicability'],'price')
        self.choose(draft,learning_scope='once',expected_revision=2)
        self.assertEqual(self.store.context('zhou',AS_OF)['advisor_preferences'],[])

    def test_explicit_price_rule_keeps_condition_in_mixed_topic_context(self):
        draft=self.engine().generate(self.request())
        self.choose(draft,learning_scope='rule',rule_text='价格只写一句，不称呼客户。',applicability='price')
        engine=self.engine()
        self.assertEqual(len(engine.context(self.request())['advisor_preferences']),1)
        mixed=engine.context(self.request(customer_id='lin',scenario='greeting'))['advisor_preferences']
        self.assertEqual(mixed[0]['applicability'],'price')
        self.assertEqual(mixed[0]['kind'],'rule')

    def test_update_is_idempotent_audited_and_rejects_stale_write(self):
        draft=self.engine().generate(self.request());first=self.choose(draft)
        second=self.choose(draft,selected_text='修改后的实际正文。',expected_revision=1)
        self.assertEqual(first['feedback']['id'],second['feedback']['id'])
        retry=self.choose(draft,selected_text='修改后的实际正文。',expected_revision=1)
        self.assertTrue(retry['duplicate'])
        with self.assertRaises(Conflict):self.choose(draft,reason='过时的另一处修改',expected_revision=1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM feedback').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM feedback_audit').fetchone()[0],2)
        self.assertEqual(feedback_history(self.store)[0]['selected_text'],'修改后的实际正文。')

    def test_negative_reason_saved_as_contextual_evidence_not_invented_trait(self):
        draft=self.engine().generate(self.request())
        self.choose(draft,outcome='none',candidate_id=None,reason='没有回答是否能降价')
        mem=self.store.memories('advisor','default')[0]
        self.assertIn('没有回答是否能降价',mem['value']);self.assertEqual(mem['provenance'],'inferred')
        self.choose(draft,outcome='none',candidate_id=None,reason='',expected_revision=1)
        self.assertEqual(self.store.context('zhou',AS_OF)['advisor_preferences'],[])

    def test_memory_manual_edit_survives_feedback_revision(self):
        draft=self.engine().generate(self.request());self.choose(draft)
        mem=self.store.memories('advisor','default')[0]
        self.store.change_memory(mem['id'],MemoryChange(expected_revision=1,value='用户明确改过的偏好。'))
        result=self.choose(draft,reason='新的样本反馈',expected_revision=1)
        self.assertFalse(result['learned'])
        self.assertEqual(self.store.memory(mem['id'])['value'],'用户明确改过的偏好。')

    def test_edit_and_disable_do_not_promote_sample_to_rule(self):
        draft=self.engine().generate(self.request());self.choose(draft)
        mem=self.store.memories('advisor','default')[0]
        self.assertEqual(mem['kind'],'example')
        revised=self.store.change_memory(mem['id'],MemoryChange(expected_revision=1,value='客户还价：5200能按5000卖吗？参考表达：不好意思，这款目前没有折扣。'))
        self.assertEqual(revised['kind'],'example')
        self.assertEqual(revised['provenance'],'manual')
        disabled=self.store.change_memory(mem['id'],MemoryChange(expected_revision=2,status='disabled'))
        self.assertEqual(disabled['kind'],'example')
        enabled=self.store.change_memory(mem['id'],MemoryChange(expected_revision=3,status='active'))
        context=self.engine().context(self.request())
        self.assertEqual(context['advisor_preferences'][0]['kind'],'example')
        self.assertEqual(enabled['kind'],'example')
        self.assertEqual(self.engine().context(self.request(customer_id='lin',scenario='greeting'))['advisor_preferences'],[])

    def test_preference_display_evidence_does_not_replace_manual_memory(self):
        from atelier.preferences import preference_memories
        draft=self.engine().generate(self.request());self.choose(draft,selected_text='原先选择的表达。',reason='先回答价格')
        before=preference_memories(self.store)[0]
        self.assertTrue(before['evidence']['matches_current'])
        self.store.change_memory(before['id'],MemoryChange(expected_revision=1,value='客户要求按5000购买，手动修正：这款目前不能调整价格。'))
        after=preference_memories(self.store)[0]
        self.assertEqual(after['value'],'客户要求按5000购买，手动修正：这款目前不能调整价格。')
        self.assertFalse(after['evidence']['matches_current'])
        self.assertEqual(after['evidence']['selected_text'],'原先选择的表达。')
        self.assertNotIn('evidence',self.engine().context(self.request())['advisor_preferences'][0])

    def test_saved_wording_is_visible_without_overwriting_model_evidence(self):
        engine=self.engine();draft=engine.generate(self.request());original=draft['result']['candidates'][0]['text']
        client=create_app(engine,seed=False).test_client()
        r=client.post('/api/drafts/'+draft['id']+'/edits',json={'candidate_id':'a','text':'先保存的编辑稿。','as_of':AS_OF})
        self.assertEqual(r.status_code,200,r.json)
        self.choose(draft,selected_text='后来采用的正文。',learning_scope='once')
        shown=client.get('/api/drafts/'+draft['id']).json
        self.assertEqual(shown['display_texts']['a'],'后来采用的正文。')
        self.assertEqual(shown['adopted_candidate'],'a')
        r=client.post('/api/drafts/'+draft['id']+'/edits',json={'candidate_id':'a','text':'采用后再次编辑的正文。','as_of':AS_OF})
        self.assertEqual(r.status_code,200,r.json)
        listed=client.get('/api/customers/zhou?as_of='+AS_OF.replace('+','%2B')).json['drafts'][0]
        self.assertEqual(listed['display_texts']['a'],'采用后再次编辑的正文。')
        self.assertEqual(self.store.draft(draft['id'])['result']['candidates'][0]['text'],original)

    def test_withdraw_one_fact_keeps_other_fact_in_same_message(self):
        self.store.add_event(Event(id='multi',customer_id='zhou',occurred_at=AS_OF,kind='message',actor='client',text='喜欢绿色，给姐姐买，周六试背。'))
        m=self.store.put_memory(MemoryInput(scope='customer',owner_id='zhou',key='own_color',value='喜欢绿色',source_quote='喜欢绿色',provenance='explicit',source='原话',source_event_id='multi',effective_at=AS_OF))
        self.store.change_memory(m['id'],MemoryChange(expected_revision=1,status='deleted'))
        context=self.store.context('zhou',AS_OF)
        text=next(e['text'] for e in context['events'] if e['id']=='multi')
        self.assertNotIn('绿色',text);self.assertIn('给姐姐买',text);self.assertIn('周六试背',text)
        self.assertIn('绿色',next(e['text'] for e in self.store.timeline('zhou',AS_OF) if e['id']=='multi'))

    def test_rewrite_uses_instruction_and_parent_but_never_learns_automatically(self):
        import time
        engine=self.engine([decision(),decision()],[{'verdict':'pass','issues':[]}]*2)
        draft=engine.generate(self.request());app=create_app(engine,seed=False);client=app.test_client()
        data={'id':'rewrite-test','instruction':'先直接回答不能降价，别重复预算。','as_of':AS_OF,'candidate_id':'b','edited_text':'这款不能降价。'}
        response=client.post('/api/drafts/'+draft['id']+'/rewrite',json=data)
        self.assertEqual(response.status_code,202,response.json)
        job_id=response.json['job_id']
        for _ in range(100):
            job=client.get('/api/jobs/'+job_id).json
            if job['status'] not in ('queued','running'):break
            time.sleep(.02)
        self.assertEqual(job['status'],'finished',job)
        revised=self.store.draft(job['draft_id'])
        self.assertEqual(revised['context']['revision_request']['instruction'],data['instruction'])
        self.assertEqual(revised['context']['revision_request']['edited_text'],data['edited_text'])
        self.assertEqual(self.store.memories('advisor','default'),[])
        retry=client.post('/api/drafts/'+draft['id']+'/rewrite',json=data)
        self.assertEqual(retry.json['draft_id'],revised['id'])
        self.assertTrue(engine.is_current(revised))
        app.extensions['job_queue'].pool.shutdown(wait=True)

    def test_confirmed_facts_reach_generator_reviewer_and_stay_scoped(self):
        import time
        engine=self.engine([decision() for _ in range(4)],[{'verdict':'pass','issues':[]}]*4)
        draft=engine.generate(self.request());app=create_app(engine,seed=False);client=app.test_client()
        fact='已向活动负责人确认，本客户这次三人可以同组看半小时；时段还没订。'
        def rewrite(parent,rid,**extra):
            body={'id':rid,'instruction':'据实回应客户','as_of':AS_OF,'candidate_id':'a'}|extra
            response=client.post('/api/drafts/'+parent['id']+'/rewrite',json=body)
            self.assertEqual(response.status_code,202,response.json)
            for _ in range(100):
                job=client.get('/api/jobs/'+response.json['job_id']).json
                if job['status'] not in ('queued','running'):break
                time.sleep(.02)
            self.assertEqual(job['status'],'finished',job)
            return self.store.draft(job['draft_id'])
        try:
            revised=rewrite(draft,'facts-1',confirmed_facts=fact)
            self.assertEqual(revised['context']['revision_request']['confirmed_facts'],fact)
            self.assertIn('revision:confirmed_facts',revised['context']['allowed_evidence'])
            self.assertEqual(engine.generator.calls[-1]['revision_request']['confirmed_facts'],fact)
            self.assertEqual(engine.reviewer.calls[-1]['context']['revision_request']['confirmed_facts'],fact)
            self.assertEqual(engine.context(self.request()).get('revision_request'),None)
            self.assertEqual(self.store.memories('advisor','default'),[])
            again=rewrite(revised,'facts-2')
            self.assertEqual(again['context']['revision_request']['confirmed_facts'],fact)
            removed=rewrite(again,'facts-3',confirmed_facts='')
            self.assertNotIn('confirmed_facts',removed['context']['revision_request'])
            self.assertNotIn('revision:confirmed_facts',removed['context']['allowed_evidence'])
        finally:app.extensions['job_queue'].pool.shutdown(wait=True)

    def test_rewrite_only_sends_checked_references_including_empty_selection(self):
        import time
        from atelier.automatic import save_setting
        original=decision()
        for c in original['candidates']:c['text']='这款目前不能调整价格。原稿标记'+c['id']
        note_id='reference-test-note'
        save_setting(self.store,'advisor-note:'+note_id,{'status':'complete','input':{'customer_id':'zhou','text':'回复价格问题'},
            'plan':{'action':'draft','instruction':'回复价格问题','confirmed_facts':''},
            'original_candidates':[{'id':'a','text':'上次补充中的旧稿标记'}]})
        engine=self.engine([original,decision(),decision(),decision()],[{'verdict':'pass','issues':[]}]*4)
        draft=engine.generate(self.request(advisor_note_id=note_id))
        self.choose(draft)
        app=create_app(engine,seed=False);client=app.test_client()
        try:
            for index,refs in enumerate(([{'id':c['id']} for c in original['candidates']],[{'id':'b','text':'按当前价格答复。已编辑标记'}],[])):
                body={'id':'reference-test-'+str(index),'instruction':'自然回应','as_of':AS_OF,'reference_candidates':refs}
                response=client.post('/api/drafts/'+draft['id']+'/rewrite',json=body)
                self.assertEqual(response.status_code,202,response.json)
                for _ in range(150):
                    job=client.get('/api/jobs/'+response.json['job_id']).json
                    if job['status'] not in ('queued','running'):break
                    time.sleep(.02)
                self.assertEqual(job['status'],'finished',job)
                wanted=[{'id':r['id'],'text':r.get('text') or next(c['text'] for c in original['candidates'] if c['id']==r['id'])} for r in refs]
                for context in (engine.generator.calls[-1],engine.reviewer.calls[-1]['context']):
                    self.assertEqual(context['revision_request']['previous_candidates'],wanted)
                    payload=json.dumps(context,ensure_ascii=False)
                    self.assertNotIn('上次补充中的旧稿标记',payload)
                    for c in original['candidates']:
                        if c['text'] not in [r['text'] for r in wanted]:self.assertNotIn(c['text'],payload)
                replay=client.post('/api/drafts/'+draft['id']+'/rewrite',json=body)
                self.assertEqual(replay.json['draft_id'],job['draft_id'])
                changed=body|{'reference_candidates':[{'id':'a'}] if not refs else []}
                self.assertEqual(client.post('/api/drafts/'+draft['id']+'/rewrite',json=changed).status_code,409)
            self.assertEqual(client.post('/api/drafts/'+draft['id']+'/rewrite',json=body|{'id':'bad-refs','reference_candidates':[{'id':'a'},{'id':'a'}]}).status_code,422)
            self.assertEqual(client.post('/api/drafts/'+draft['id']+'/rewrite',json=body|{'id':'bad-edit','candidate_id':'b','edited_text':'没勾选的内容'}).status_code,422)
        finally:app.extensions['job_queue'].pool.shutdown(wait=True)

    def test_contextual_question_uses_real_reason_and_respects_skip(self):
        from atelier.preferences import contextual_question,answer_contextual_question
        draft=self.engine().generate(self.request())
        self.assertIsNone(contextual_question(self.store))
        self.choose(draft,reason='先回答能否降价',learning_scope='once')
        self.assertIsNone(contextual_question(self.store))
        self.choose(draft,reason='先回答能否降价',learning_scope='example',expected_revision=1)
        self.assertIsNone(contextual_question(self.store))
        second=self.engine().generate(self.request())
        self.choose(second,reason='先回答能否降价',learning_scope='example')
        q=contextual_question(self.store);self.assertEqual(q['quote'],'先回答能否降价')
        with self.assertRaises(Conflict):answer_contextual_question(self.store,'default',q['id'],'skip',expected_revision=0)
        answer_contextual_question(self.store,'default',q['id'],'skip',expected_revision=q['revision'])
        self.assertIsNone(contextual_question(self.store))

    def test_contextual_question_confirmation_creates_explicit_scoped_rule(self):
        from atelier.preferences import contextual_question,answer_contextual_question
        draft=self.engine().generate(self.request());self.choose(draft,reason='别反复提预算')
        second=self.engine().generate(self.request());self.choose(second,reason='别反复提预算')
        q=contextual_question(self.store)
        answer_contextual_question(self.store,'default',q['id'],'remember','价格回复不重复预算上限。','price',q['revision'])
        memories=self.store.memories('advisor','default')
        self.assertEqual(len([m for m in memories if m['kind']=='example']),2)
        memory=next(m for m in memories if m['kind']=='rule')
        self.assertEqual(memory['value'],'价格回复不重复预算上限。')
        self.assertEqual(memory['provenance'],'manual')
        self.assertIsNone(contextual_question(self.store))

    def test_rewrite_rejects_changed_customer_and_retains_original(self):
        engine=self.engine();draft=engine.generate(self.request());app=create_app(engine,seed=False);client=app.test_client()
        self.store.add_event(Event(id='new-fact',customer_id='zhou',occurred_at=AS_OF,kind='message',actor='client',text='先不买了'))
        r=client.post('/api/drafts/'+draft['id']+'/rewrite',json={'id':'stale','instruction':'再短点','as_of':AS_OF})
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.store.draft(draft['id'])['result'],draft['result'])
        app.extensions['job_queue'].pool.shutdown(wait=True)

    def test_rewrite_allows_clock_progress_without_hiding_actual_context_changes(self):
        import time
        engine=self.engine([decision(),decision()],[{'verdict':'pass','issues':[]}]*2)
        draft=engine.generate(self.request());app=create_app(engine,seed=False);client=app.test_client()
        later='2026-11-07T10:00:30+08:00'
        body={'id':'clock-rewrite','instruction':'更自然一点','as_of':later,'reference_candidates':[]}
        try:
            response=client.post('/api/drafts/'+draft['id']+'/rewrite',json=body)
            self.assertEqual(response.status_code,202,response.json)
            for _ in range(150):
                job=client.get('/api/jobs/'+response.json['job_id']).json
                if job['status'] not in ('queued','running'):break
                time.sleep(.02)
            self.assertEqual(job['status'],'finished',job)
            self.assertEqual(engine.generator.calls[-1]['as_of'],'2026-11-07T02:00:30+00:00')
            self.store.add_event(Event(id='changed-after-draft',customer_id='zhou',occurred_at=later,kind='message',actor='client',text='改看别的商品了。'))
            self.assertEqual(client.post('/api/drafts/'+draft['id']+'/rewrite',json=body|{'id':'clock-changed'}).status_code,409)
        finally:app.extensions['job_queue'].pool.shutdown(wait=True)

if __name__=='__main__':unittest.main()
