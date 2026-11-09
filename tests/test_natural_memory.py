import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from atelier.engine import Engine
from atelier.extraction import extract_event
from atelier.models import Customer, Event, MemoryInput, MemoryChange, DraftRequest
from atelier.retrieval import ExampleRetriever
from atelier.store import RelationshipStore, Conflict, ROOT
from test_atelier_runtime import Fake

AT='2026-11-09T10:00:00+08:00'
LATER='2026-11-10T10:00:00+08:00'


class NaturalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=RelationshipStore(Path(self.tmp.name)/'app.sqlite3')
        self.store.add_customer(Customer(id='one',name='小林'))
        self.store.add_customer(Customer(id='two',name='另一位'))

    def tearDown(self):self.tmp.cleanup()

    def message(self,id,text,at=AT,customer='one'):
        self.store.add_event(Event(id=id,customer_id=customer,occurred_at=at,
            kind='message',actor='client',text=text,data={'requires_memory_update':True}))

    def extract(self,id,items):
        provider=Fake({'memories':items})
        result=extract_event(self.store,provider,id)
        self.assertEqual(result['status'],'complete',result['issues'])
        return result

    def pair(self):
        self.message('pair','我自己喜欢绿色。这次给姐姐挑礼物，她喜欢白色，出门只带手机和卡。')
        self.extract('pair',[
            {'key':'color','value':'客户本人喜欢绿色。','quote':'我自己喜欢绿色'},
            {'key':'color','value':'这次收礼的姐姐喜欢白色。','quote':'这次给姐姐挑礼物，她喜欢白色'}])
        return self.store.memories('customer','one')

    def test_actual_duplicate_color_failure_now_saves_both_once(self):
        event='我自己喜欢绿色。这次给姐姐挑礼物，她喜欢白色，出门只带手机和卡。'
        self.message('original',event)
        # The original failed response, not an idealized replacement.
        self.extract('original',[
            {'key':'color','value':'绿色','quote':'我自己喜欢绿色'},
            {'key':'gift_recipient','value':'姐姐','quote':'这次给姐姐挑礼物'},
            {'key':'color','value':'白色','quote':'她喜欢白色'},
            {'key':'carry','value':'只带手机和卡','quote':'出门只带手机和卡'}])
        facts=self.store.memories('customer','one')
        self.assertEqual(len(facts),4)
        self.assertEqual(len({m['id'] for m in facts if m['key']=='color'}),2)
        provider=Fake()
        extract_event(self.store,provider,'original')
        self.assertFalse(provider.calls)
        self.assertEqual(self.store.memories('customer','one'),facts)

    def test_text_is_self_contained_and_new_topics_need_no_schema_field(self):
        self.message('travel','我周末常去徒步；这次买给妈妈，她只背轻一点的包。')
        self.extract('travel',[
            {'key':'hiking','value':'客户周末常去徒步。','quote':'我周末常去徒步'},
            {'value':'这次的收礼人是妈妈，她只背轻一点的包。','quote':'这次买给妈妈，她只背轻一点的包'}])
        facts=self.store.memories('customer','one')
        self.assertEqual({m['key'] for m in facts},{'hiking','note'})
        self.assertTrue(any('收礼人是妈妈' in m['value'] for m in facts))

    def test_update_one_person_by_id_keeps_other_person_and_audit(self):
        facts=self.pair();sister=next(m for m in facts if '姐姐' in m['value'])
        own=next(m for m in facts if '本人' in m['value'])
        self.message('change','姐姐改主意了，这次想要黑色。',LATER)
        self.extract('change',[{'key':'color','value':'这次收礼的姐姐改为想要黑色。',
            'quote':'姐姐改主意了，这次想要黑色','replaces_id':sister['id']}])
        self.assertEqual(self.store.memory(own['id']),own)
        self.assertEqual(self.store.memory(sister['id'])['revision'],2)
        context=self.store.context('one',LATER)
        self.assertNotIn('白色',json.dumps(context,ensure_ascii=False))
        self.assertIn('绿色',json.dumps(context,ensure_ascii=False))
        self.assertEqual(len(context['memories']),2)

    def test_delete_one_note_preserves_same_topic_sibling_and_prevents_replay(self):
        facts=self.pair();sister=next(m for m in facts if '姐姐' in m['value'])
        self.store.change_memory(sister['id'],MemoryChange(expected_revision=1,status='deleted'))
        context=self.store.context('one',AT)
        self.assertNotIn('白色',json.dumps(context,ensure_ascii=False))
        self.assertIn('绿色',json.dumps(context,ensure_ascii=False))
        with self.assertRaises(Conflict):
            self.store.put_memory(MemoryInput(scope='customer',owner_id='one',key='new_tag',
                value='姐姐喜欢白色',provenance='explicit',source='replay',source_event_id='pair',
                source_quote=sister['source_quote'],effective_at=AT))

    def test_same_label_does_not_select_update_target(self):
        self.pair()
        with self.assertRaises(Conflict):
            self.store.put_memory(MemoryInput(scope='customer',owner_id='one',key='color',
                value='黑色',provenance='explicit',source='ambiguous',effective_at=LATER),expected_revision=1)

    def test_cross_customer_or_protected_target_not_overwritten(self):
        own=self.pair()[0]
        self.message('other','我喜欢黑色。',customer='two')
        proposal={'memories':[{'key':'color','value':'客户喜欢黑色','quote':'我喜欢黑色','replaces_id':own['id']}]}
        result=extract_event(self.store,Fake(proposal,proposal),'other')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.store.memory(own['id']),own)
        self.store.change_memory(own['id'],MemoryChange(expected_revision=1,value='人工确认的偏好'))
        self.message('conflict','我现在喜欢黑色。',LATER)
        proposal={'memories':[{'key':'color','value':'客户喜欢黑色','quote':'我现在喜欢黑色','replaces_id':own['id']}]}
        result=extract_event(self.store,Fake(proposal),'conflict')
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.store.memory(own['id'])['value'],'人工确认的偏好')

    def test_duplicate_contact_control_still_fails_before_writing(self):
        self.message('bad','新品别发了，只发订单进度。')
        proposal={'memories':[{'key':'contact_permission','value':v,'quote':'新品别发了'} for v in ['allowed','no_marketing']]}
        result=extract_event(self.store,Fake(proposal,proposal),'bad')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.store.memories('customer','one'),[])

    def test_large_collection_retrieves_related_old_text_and_pins_contact(self):
        notes=[]
        for i in range(60):
            value='姐姐喜欢白色，这次给姐姐买礼物。' if i==0 else f'客户第{i}次旅行去了不同城市。'
            notes.append(self.store.put_memory(MemoryInput(scope='customer',owner_id='one',key='note',
                value=value,provenance='explicit',source='test fixture',effective_at=AT)))
        permission=self.store.put_memory(MemoryInput(scope='customer',owner_id='one',key='contact_permission',
            value='no_marketing',provenance='explicit',source='test fixture',effective_at=AT))
        self.store.add_event(Event(id='query',customer_id='one',occurred_at=LATER,kind='message',actor='client',text='给姐姐的礼物，颜色选什么？'))
        engine=Engine(self.store,Fake(),Fake(),retriever=ExampleRetriever())
        ctx=engine.compact_history(engine.context(DraftRequest(customer_id='one',as_of=LATER)))
        self.assertLessEqual(len(ctx['memories']),31)
        self.assertIn(notes[0]['id'],{m['id'] for m in ctx['memories']})
        self.assertIn(permission['id'],{m['id'] for m in ctx['memories']})
        self.assertEqual(ctx['memory_selection']['eligible_count'],60)
        self.store.change_memory(notes[0]['id'],MemoryChange(expected_revision=1,status='disabled'))
        ctx=engine.compact_history(engine.context(DraftRequest(customer_id='one',as_of=LATER)))
        self.assertNotIn('白色',json.dumps(ctx,ensure_ascii=False))

    def test_schema_seven_migration_keeps_foreign_keys_and_all_old_values(self):
        path=Path(self.tmp.name)/'old.sqlite3'
        with sqlite3.connect(path) as db:
            for i in range(1,8):db.executescript((ROOT/f'atelier/migrations/{i:03d}.sql').read_text())
            db.execute('PRAGMA user_version=7')
            db.execute("INSERT INTO customers VALUES('old','旧客户','','','forest',?)",(AT,))
            db.execute("INSERT INTO memories(id,scope,owner_id,key,value,provenance,source,effective_at,applicability,created_at,updated_at) VALUES('legacy','customer','old','color','绿色','explicit','旧资料',?,'所有情境',?,?)",(AT,AT,AT))
            db.execute("INSERT INTO memory_audit(memory_id,revision,action,after_json,created_at) VALUES('legacy',1,'create','{}',?)",(AT,))
            before=db.execute('SELECT * FROM memories').fetchall()
        migrated=RelationshipStore(path)
        RelationshipStore(path)
        with migrated.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM memories')],before)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT memory_id FROM memory_audit').fetchone()[0],'legacy')
        migrated.put_memory(MemoryInput(scope='customer',owner_id='old',key='color',value='姐姐喜欢白色',
            provenance='explicit',source='新资料',effective_at=AT))
        self.assertEqual(len(migrated.memories('customer','old')),2)
