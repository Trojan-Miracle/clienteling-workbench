"""Input-quality checks only; these do not generate or score the formal outputs."""
from collections import Counter
from datetime import datetime,timedelta
from pathlib import Path
import json,tempfile,unittest
from atelier.store import ROOT,RelationshipStore
from atelier.models import Customer,Event,DraftRequest
from atelier.engine import Engine
from scripts.run_v4_longitudinal import put_facts


class DeclaredCasesTests(unittest.TestCase):
    def test_50_sequential_cases_cover_scenarios_without_future_context(self):
        declared=json.loads((ROOT/'data/v4_evaluation/longitudinal.json').read_text())
        counts=Counter();ids=set();proactive_lapsed=0
        with tempfile.TemporaryDirectory() as folder:
            store=RelationshipStore(Path(folder)/'input-check.sqlite3')
            # Engine.context and hard gate do not call either provider.
            engine=Engine(store,generator=object(),reviewer=object())
            for person in declared['customers']:
                cid=person['id'];store.add_customer(Customer(id=cid,name=person['name'],salutation=person['salutation']))
                intro=Event(id=cid+'-intro',customer_id=cid,occurred_at=person['initial_at'],kind='message',actor='client',text=person['introduction'])
                store.add_event(intro);put_facts(store,cid,intro,person['facts'])
                previous=None
                for item in person['episodes']:
                    self.assertNotIn(item['case_id'],ids);ids.add(item['case_id']);counts[item['scenario']]+=1
                    as_of=datetime.fromisoformat(item['as_of'])
                    if previous:self.assertGreater(as_of,previous)
                    previous=as_of
                    for index,before in enumerate(item['before']):
                        event_data={'occurred_at':(as_of-timedelta(minutes=10-index)).isoformat(),**before}
                        store.add_event(Event(id=item['case_id']+'-prior-'+str(index),customer_id=cid,**event_data))
                    message=Event(id=item['case_id']+'-client',customer_id=cid,occurred_at=item.get('message_at',item['as_of']),kind='message',actor='client',text=item['message'])
                    store.add_event(message);put_facts(store,cid,message,item['facts'])
                    request=DraftRequest(customer_id=cid,as_of=item['as_of'],scenario=item['scenario'],trigger=item['trigger'],product_ids=item['product_ids'],occasion=item['occasion'],event_id=item['event_id'])
                    context=engine.context(request)
                    self.assertIsNone(engine.gate(context),item['case_id'])
                    self.assertTrue(all(datetime.fromisoformat(e['occurred_at'])<=as_of for e in context['events']))
                    self.assertTrue(all(e['customer_id']==cid for e in context['events']))
                    self.assertTrue(all(datetime.fromisoformat(m['effective_at'])<=as_of for m in context['memories']))
                    if item['scenario']=='reactivation' and item['trigger']=='timer':
                        self.assertGreater((as_of-datetime.fromisoformat(message.occurred_at)).days,180)
                        proactive_lapsed+=1
            self.assertEqual(len(ids),50)
            self.assertEqual(counts,declared['scenario_counts'])
            self.assertEqual(proactive_lapsed,4)


if __name__=='__main__':unittest.main()
