"""Contact state does not imply permanent intent or permission after expiry."""
import tempfile
import unittest
from pathlib import Path

from atelier.engine import Engine
from atelier.extraction import extract_event
from atelier.models import Customer, DraftRequest, Event, MemoryChange
from atelier.store import RelationshipStore
from test_atelier_runtime import Fake

AT='2026-11-09T10:00:00+08:00'


class ContactScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=RelationshipStore(Path(self.temp.name)/'db.sqlite3')
        self.store.add_customer(Customer(id='one',name='林先生'))
        self.engine=Engine(self.store,Fake(),Fake())

    def tearDown(self):
        self.temp.cleanup()

    def extract(self,text,value,*,event_id='pause',at=AT,expires=None):
        self.store.add_event(Event(id=event_id,customer_id='one',occurred_at=at,
            kind='message',actor='client',text=text,data={'requires_memory_update':True}))
        result=extract_event(self.store,Fake({'memories':[{'key':'contact_permission',
            'value':value,'quote':text,'expires_at':expires}]}),event_id)
        self.assertEqual(result['status'],'complete')
        return self.store.memories('customer','one')[0]

    def context(self,at=AT,trigger='timer'):
        return self.engine.context(DraftRequest(customer_id='one',as_of=at,trigger=trigger))

    def test_existing_generic_scope_is_presented_with_original_qualifier(self):
        # Existing records had this generic applicability too. Context adapts
        # them without rewriting user data or inventing a duration.
        original=self.extract('先别给我推荐东西。','no_marketing')
        self.assertEqual(original['applicability'],'所有情境')
        context=self.context()
        self.assertEqual(context['memories'][0]['applicability'],'先别给我推荐东西。')
        self.assertIsNone(context['memories'][0]['expires_at'])
        self.assertEqual(self.store.memory(original['id']),original)
        self.assertEqual(self.engine.gate(context).action,'no_action')
        self.assertIn('重新允许',self.engine.gate(context).revisit_when)
        self.assertIsNone(self.engine.gate(self.context(trigger='reply')))

    def test_no_end_time_never_guesses_an_automatic_restart(self):
        self.extract('这阵子先别主动联系，我有需要来找你。','do_not_contact')
        later=self.context('2026-12-09T10:00:00+08:00')
        self.assertEqual(self.engine.gate(later).action,'no_action')
        self.assertIn('这阵子',later['memories'][0]['applicability'])
        self.assertIsNone(later['memories'][0]['expires_at'])

    def test_expiry_is_not_new_marketing_consent(self):
        self.extract('今天不要给我发新品。','no_marketing',expires='2026-11-10T00:00:00+08:00')
        self.assertEqual(self.engine.gate(self.context()).action,'no_action')
        later=self.context('2026-11-10T00:00:00+08:00')
        self.assertFalse(later['memories'])
        self.assertEqual(self.engine.gate(later).action,'needs_info')

    def test_explicit_new_permission_replaces_pause_but_inquiry_does_not(self):
        original=self.extract('暂时别发新品。','no_marketing')
        self.store.add_event(Event(id='inquiry',customer_id='one',occurred_at='2026-11-10T10:00:00+08:00',
            kind='message',actor='client',text='这个包多大？'))
        self.assertIsNone(self.engine.gate(self.context('2026-11-10T10:00:00+08:00','reply')))
        self.assertEqual(self.engine.gate(self.context('2026-11-10T10:00:00+08:00')).action,'no_action')
        updated=self.extract('现在可以给我发合适的新品了。','allowed',event_id='resume',at='2026-11-11T10:00:00+08:00')
        self.assertEqual(updated['id'],original['id'])
        self.assertEqual(updated['revision'],2)
        context=self.context('2026-11-11T10:00:00+08:00')
        self.assertIsNone(self.engine.gate(context))
        self.assertEqual(context['memories'][0]['applicability'],'现在可以给我发合适的新品了。')

    def test_manual_scope_does_not_reintroduce_old_source(self):
        original=self.extract('先别给我推荐东西。','no_marketing')
        self.store.change_memory(original['id'],MemoryChange(expected_revision=1,
            value='allowed',applicability='只允许通知已确认到货的通勤包'))
        current=self.context()['memories'][0]
        self.assertEqual(current['applicability'],'只允许通知已确认到货的通勤包')
        self.assertNotIn('先别',current['source'])
        self.assertIsNone(current['source_quote'])


if __name__=='__main__':unittest.main()
