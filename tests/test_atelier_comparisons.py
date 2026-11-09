import json
import tempfile
import unittest
from pathlib import Path
from atelier.store import RelationshipStore, Conflict
from atelier.comparisons import cards, vote


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.store=RelationshipStore(root/'test.sqlite3');self.path=root/'comparison.json'
        self.path.write_text(json.dumps({'source':'test-fixture-only','cards':[{'id':'card','context':{'messages':[{'text':'谢谢'}]},
            'choices':[{'id':str(i),'text':'测试候选'+str(i),'private_source':{'variant':'private-version-'+str(i)}} for i in range(3)]}]}))
    def tearDown(self):self.temp.cleanup()

    def test_public_cards_expose_no_source_or_recommendation(self):
        public=cards(self.store,path=self.path)
        self.assertNotIn('private',json.dumps(public))
        self.assertNotIn('source',json.dumps(public))
        self.assertEqual({c['id'] for c in public[0]['choices']},{'0','1','2'})

    def test_vote_is_durable_deduplicated_and_does_not_train_preferences(self):
        data={'card_id':'card','outcome':'choice','choice_id':'2','display_order':['2','0','1']}
        self.assertFalse(vote(self.store,data,path=self.path)['learned'])
        self.assertTrue(vote(self.store,data,path=self.path)['duplicate'])
        self.assertEqual(self.store.memories('advisor','default'),[])
        self.assertEqual(cards(RelationshipStore(self.store.path),path=self.path),[])
        with self.assertRaises(Conflict):vote(self.store,data|{'choice_id':'1'},path=self.path)

    def test_forged_order_or_choice_not_counted(self):
        data={'card_id':'card','outcome':'choice','choice_id':'2','display_order':['2','0','1']}
        for delta in ({'choice_id':'elsewhere'},{'display_order':None},{'display_order':['2','2','0']},{'outcome':'skip'}):
            with self.assertRaises(ValueError):vote(self.store,data|delta,path=self.path)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM comparison_votes').fetchone()[0],0)


if __name__=='__main__':unittest.main()
