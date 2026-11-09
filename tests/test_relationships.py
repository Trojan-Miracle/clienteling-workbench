import json
import tempfile
import unittest
from pathlib import Path
from atelier.models import Customer, Event, MemoryInput, MemoryChange
from atelier.store import RelationshipStore, Conflict

T0 = '2026-11-01T10:00:00+08:00'
T1 = '2026-11-07T10:00:00+08:00'


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'relationships.sqlite3'
        self.store = RelationshipStore(self.path)
        self.store.add_customer(Customer(id='c1', name='虚构客户林', salutation='林女士'))
        self.store.add_customer(Customer(id='c2', name='虚构客户周'))
        self.event = Event(id='e1', customer_id='c1', occurred_at=T0, kind='message',
                           actor='client', text='平时喜欢深蓝色。')
        self.store.add_event(self.event)

    def tearDown(self):
        self.tmp.cleanup()

    def memory(self, **changes):
        return MemoryInput(**(dict(scope='customer', owner_id='c1', key='color', value='喜欢深蓝色',
            provenance='explicit', source='客户原话', source_event_id='e1', effective_at=T0) | changes))

    def test_persist_across_restart_and_migration_idempotent(self):
        memory = self.store.put_memory(self.memory())
        reopened = RelationshipStore(self.path)
        self.assertEqual(reopened.memory(memory['id'])['value'], '喜欢深蓝色')
        self.assertEqual(len(reopened.timeline('c1', T1)), 1)
        with reopened.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 9)

    def test_expired_memory_can_be_extended_or_have_expiry_cleared(self):
        memory=self.store.put_memory(self.memory(expires_at='2026-11-03T10:00:00+08:00'))
        self.assertEqual(self.store.context('c1',T1)['memories'],[])
        changed=self.store.change_memory(memory['id'],MemoryChange(expected_revision=1,expires_at=None))
        self.assertIsNone(changed['expires_at'])
        self.assertEqual(self.store.context('c1',T1)['memories'][0]['value'],'喜欢深蓝色')
        with self.assertRaises(ValueError):
            self.store.change_memory(memory['id'],MemoryChange(expected_revision=2,expires_at='2026-10-01T10:00:00+08:00'))

    def test_delete_cannot_resurrect_from_extraction_or_raw_message(self):
        memory = self.store.put_memory(self.memory())
        self.store.change_memory(memory['id'], MemoryChange(expected_revision=1, status='deleted'))
        with self.assertRaises(Conflict):
            self.store.put_memory(self.memory(), expected_revision=2)
        context = self.store.context('c1', T1)
        self.assertEqual(context['memories'], [])
        self.assertNotIn('深蓝', json.dumps(context, ensure_ascii=False))
        self.assertIn('深蓝', self.store.timeline('c1', T1)[0]['text'])  # audit not rewritten

    def test_manual_correction_overrides_source_and_old_model_guess(self):
        memory = self.store.put_memory(self.memory())
        self.store.change_memory(memory['id'], MemoryChange(expected_revision=1, value='只看黑色'))
        with self.assertRaises(Conflict):
            self.store.put_memory(self.memory(provenance='inferred'), expected_revision=2)
        context = self.store.context('c1', T1)
        self.assertIn('只看黑色', json.dumps(context, ensure_ascii=False))
        self.assertNotIn('深蓝', json.dumps(context, ensure_ascii=False))

    def test_disable_and_reenable_are_explicit(self):
        memory = self.store.put_memory(self.memory())
        disabled = self.store.change_memory(memory['id'], MemoryChange(expected_revision=1, status='disabled'))
        self.assertEqual(self.store.context('c1', T1)['memories'], [])
        self.store.change_memory(memory['id'], MemoryChange(expected_revision=disabled['revision'], status='active'))
        self.assertEqual(len(self.store.context('c1', T1)['memories']), 1)

    def test_stale_edit_rejected_not_silently_overwritten(self):
        memory = self.store.put_memory(self.memory())
        self.store.change_memory(memory['id'], MemoryChange(expected_revision=1, value='黑色'))
        with self.assertRaises(Conflict):
            self.store.change_memory(memory['id'], MemoryChange(expected_revision=1, value='白色'))
        self.assertEqual(self.store.memory(memory['id'])['value'], '黑色')

    def test_expiry_and_future_events_do_not_leak_into_context(self):
        self.store.put_memory(self.memory(expires_at='2026-11-03T10:00:00+08:00'))
        self.store.add_event(Event(id='future', customer_id='c1', occurred_at='2026-12-01T10:00:00+08:00',
                                  kind='message', actor='client', text='未来才会购买'))
        context = self.store.context('c1', T1)
        self.assertEqual(context['memories'], [])
        self.assertNotIn('未来才会购买', json.dumps(context, ensure_ascii=False))
        self.assertNotIn('深蓝', json.dumps(context, ensure_ascii=False))

    def test_customer_memory_source_must_belong_to_customer(self):
        with self.assertRaises(ValueError):
            self.store.put_memory(self.memory(owner_id='c2'))

    def test_inference_cannot_override_explicit_fact(self):
        self.store.put_memory(self.memory())
        with self.assertRaises(Conflict):
            self.store.put_memory(self.memory(provenance='inferred', value='喜欢红色'), expected_revision=1)

    def test_customer_and_advisor_preferences_are_separate(self):
        self.store.put_memory(self.memory())
        self.store.put_memory(self.memory(scope='advisor', owner_id='default', key='length',
            value='价格回应简短', applicability='价格回应', source_event_id=None, provenance='manual'))
        context = self.store.context('c1', T1)
        self.assertEqual(len(context['memories']), 1)
        self.assertEqual(context['advisor_preferences'][0]['applicability'], '价格回应')
        self.assertEqual(self.store.context('c1', T1, advisor_id='another')['advisor_preferences'], [])

    def test_new_source_replaces_old_without_reviving_old_history(self):
        original = self.store.put_memory(self.memory())
        self.store.add_event(Event(id='e2', customer_id='c1', occurred_at=T1, kind='message',
                                  actor='client', text='现在只看黑色'))
        self.store.put_memory(self.memory(value='黑色', source_event_id='e2', effective_at=T1), expected_revision=1)
        context = self.store.context('c1', T1)
        self.assertEqual(context['memories'][0]['revision'], 2)
        self.assertNotIn('深蓝', json.dumps(context, ensure_ascii=False))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_audit WHERE memory_id=?', (original['id'],)).fetchone()[0], 2)

    def test_event_idempotency_and_conflict(self):
        self.store.add_event(self.event)
        self.assertEqual(len(self.store.timeline('c1', T1)), 1)
        with self.assertRaises(Conflict):
            self.store.add_event(self.event.model_copy(update={'text':'不同内容'}))

    def test_same_business_time_keeps_recording_order_not_random_id_order(self):
        for event_id,actor,text in [('z-first','client','先问绿色'),('y-second','advisor','先看绿色'),('a-last','client','改成黑色')]:
            self.store.add_event(Event(id=event_id,customer_id='c1',occurred_at=T1,kind='message',actor=actor,text=text))
        self.assertEqual([e['id'] for e in self.store.timeline('c1',T1)][-3:],['z-first','y-second','a-last'])

    def test_backup_contains_committed_memory(self):
        memory = self.store.put_memory(self.memory())
        backup = Path(self.tmp.name) / 'backup.sqlite3'
        self.store.backup(backup)
        self.assertEqual(RelationshipStore(backup).memory(memory['id'])['value'], '喜欢深蓝色')


if __name__ == '__main__':
    unittest.main()
