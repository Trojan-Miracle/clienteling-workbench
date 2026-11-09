import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from flask import Flask
from atelier.models import MemoryInput, MemoryChange
from atelier.store import RelationshipStore, Conflict
from atelier.preference_document import read_document, save_document
from atelier.quick_review import register_quick_review
from lanxu.provider import ModelReply, ProviderError

ROOT=Path(__file__).resolve().parents[1]

class Model:
    def __init__(self): self.calls=[]; self.fail=False
    def complete(self,system,user):
        self.calls.append(json.loads(user))
        if self.fail: raise ProviderError('test_failure','测试模型失败')
        return ModelReply(json.dumps({'revised_text':'生日快乐！今晚和朋友吃得开心。','changes':['删去重复称呼，保留聚餐祝福。'],
            'general_suggestion':'在相关时回应当下活动。','limits':'感谢不必扩写，不强制增加场景句。'},ensure_ascii=False),{'provider':'test_fixture'})

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'review.sqlite3';self.model=Model()
        self.app=Flask(__name__);register_quick_review(self.app,ROOT,self.path,self.model)
        self.client=self.app.test_client();self.case=self.client.get('/api/quick-review').json['cases'][0]
    def tearDown(self): self.tmp.cleanup()
    def save(self,**kw):
        return self.client.post('/api/quick-review/'+self.case['case_id'],json={'decision':'comment','candidate_id':None,'note':'自然回应当下的聚餐','scope':'general','revision':0}|kw)
    def rewrite(self,**kw):
        return self.client.post('/api/quick-review/'+self.case['case_id']+'/rewrite',json={'request_id':str(uuid.uuid4()),'candidate_id':self.case['candidates'][0]['id'],'note':'自然回应当下的聚餐','scope':'general'}|kw)
    def finished(self,jid):
        for _ in range(100):
            job=next(j for j in self.client.get('/api/quick-review').json['revisions'] if j['id']==jid)
            if job['status']!='running': return job
            time.sleep(.01)
        self.fail('worker did not finish')
    def test_rewriting_uses_generation_configuration(self):
        from unittest.mock import patch
        from atelier.quick_review import review_provider
        with patch('atelier.quick_review.generation_provider',return_value=self.model) as factory:
            self.assertIs(review_provider(),self.model)
            factory.assert_called_once_with()

    def test_originals_are_exact_and_balanced(self):
        cases=self.client.get('/api/quick-review').json['cases'];self.assertEqual(len(cases),12)
        from collections import Counter
        self.assertEqual(set(Counter(c['scenario'] for c in cases).values()),{2})
        for c in cases:
            raw=(ROOT/'tests/fixtures/review12'/f"{c['case_id']}.json").read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),c['source_sha256'])
            source=json.loads(raw)['result']['candidates']
            self.assertEqual({x['id']:x['text'] for x in source},{x['id']:x['text'] for x in c['candidates']})
    def test_feedback_durable_conflict_and_comment_only(self):
        self.assertEqual(self.save().status_code,200)
        self.assertEqual(self.save(note='不能覆盖').status_code,409)
        other=Flask('reload');register_quick_review(other,ROOT,self.path,self.model)
        r=other.test_client().get('/api/quick-review').json['responses'][self.case['case_id']]
        self.assertEqual(r['note'],'自然回应当下的聚餐');self.assertEqual(r['decision'],'comment')
        self.assertEqual(self.client.get('/api/quick-review/export').status_code,200)
    def test_real_workflow_dedup_parent_and_verdict(self):
        self.assertEqual(self.rewrite().status_code,409)
        self.save();rid=str(uuid.uuid4());r=self.rewrite(request_id=rid);self.assertEqual(r.status_code,202)
        done=self.finished(rid);self.assertEqual(done['status'],'complete');self.assertIn('diff',done)
        self.assertEqual(self.model.calls[0]['feedback'],'自然回应当下的聚餐')
        self.assertEqual(self.model.calls[0]['original'],self.case['candidates'][0]['text'])
        self.assertEqual(self.model.calls[0]['case']['events'],self.case['events'])
        self.assertEqual(done['result']['revised_text'],'生日快乐！今晚和朋友吃得开心。')
        self.assertIn('prompt_sha256',done)
        again=self.rewrite(request_id=rid);self.assertEqual(again.status_code,200);self.assertEqual(len(self.model.calls),1)
        verdict=self.client.post('/api/quick-review/revisions/'+rid+'/verdict',json={'verdict':'accepted'})
        self.assertEqual(verdict.json['verdict'],'accepted')
        second=self.rewrite(parent_id=rid);self.finished(second.json['id'])
        self.assertEqual(self.model.calls[-1]['original'],done['result']['revised_text'])
        self.assertEqual(self.rewrite(candidate_id='missing').status_code,422)
        self.assertEqual(self.rewrite(parent_id='missing').status_code,422)
    def test_generation_timeout_is_not_the_old_judge_timeout(self):
        from unittest.mock import patch
        import threading
        gate=threading.Event();started=threading.Event()
        original=self.model.complete
        def delayed(system,user):
            started.set();gate.wait(5);return original(system,user)
        self.model.complete=delayed;self.model.timeout=240
        self.save();r=self.rewrite();self.assertTrue(started.wait(1))
        try:
            clock=time.time()
            with patch('atelier.quick_review.time.time',return_value=clock+121):
                job=self.client.get('/api/quick-review').json['revisions'][0]
                self.assertEqual(job['status'],'running')
        finally:gate.set()
        self.assertEqual(self.finished(r.json['id'])['status'],'complete')

    def test_failure_preserves_feedback_and_old_revision(self):
        self.save();one=self.rewrite();self.finished(one.json['id']);self.model.fail=True
        two=self.rewrite();failed=self.finished(two.json['id']);self.assertEqual(failed['status'],'failed')
        records=self.client.get('/api/quick-review').json
        self.assertEqual(records['revisions'][0]['status'],'complete');self.assertIn(self.case['case_id'],records['responses'])

class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=RelationshipStore(Path(self.tmp.name)/'data.sqlite3')
    def tearDown(self): self.tmp.cleanup()
    def put(self,key,text,scope='所有情境',**kw):
        return self.store.put_memory(MemoryInput(scope='advisor',owner_id='default',key=key,value=text,provenance='manual',source='test',effective_at='2026-01-01T00:00:00+00:00',applicability=scope,**kw))
    def test_consolidation_audit_and_active_generation_rules(self):
        a=self.put('voice.one','少用感叹号');b=self.put('voice.two','先回答问题')
        example=self.store.put_memory(MemoryInput(scope='advisor',owner_id='default',key='example.one',value='不客气',provenance='inferred',source='test',effective_at='2026-01-01T00:00:00+00:00'))
        doc=read_document(self.store);updated=save_document(self.store,'先回答问题，语气自然。',doc['revision'])
        self.assertEqual(updated['text'],'先回答问题，语气自然。')
        active=self.store.memories('advisor','default')
        self.assertEqual(len([m for m in active if m['kind']=='rule']),1)
        self.assertEqual(self.store.memory(example['id']),example)
        self.assertEqual(self.store.memory(a['id'])['status'],'disabled')
        with self.store.connect() as db:self.assertEqual(db.execute("select count(*) from memory_audit where action='document_replace'").fetchone()[0],2)
        with self.assertRaises(Conflict):save_document(self.store,'覆盖',doc['revision'])
        cleared=save_document(self.store,'',updated['revision']);self.assertEqual(cleared['text'],'')
        self.assertEqual(len([m for m in self.store.memories('advisor','default') if m['kind']=='rule']),0)
    def test_scopes_expiry_and_invalid_headings(self):
        a=self.put('voice.general','自然');b=self.put('voice.birthday','简单祝福','greeting',expires_at='2099-01-01T00:00:00+00:00')
        expired=self.put('voice.old','不能复活','price',expires_at='2026-02-01T00:00:00+00:00')
        doc=read_document(self.store);self.assertNotIn('不能复活',doc['text'])
        with self.assertRaises(ValueError):save_document(self.store,'全局套用',doc['revision'])
        self.assertEqual(self.store.memory(b['id']),b)
        save_document(self.store,doc['text'].replace('简单祝福','简短且回应当下'),doc['revision'])
        active=[m for m in self.store.memories('advisor','default') if m['value']=='简短且回应当下'][0]
        self.assertEqual(active['applicability'],'greeting');self.assertEqual(active['expires_at'],b['expires_at'])
        self.assertEqual(self.store.memory(expired['id']),expired)

if __name__=='__main__':unittest.main()
