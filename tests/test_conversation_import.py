import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from atelier.conversation_import import preview,confirm,ImportPreview,ImportConfirm
from atelier.models import Customer,MemoryInput,MemoryChange
from atelier.store import RelationshipStore,Conflict,ROOT

AS_OF='2026-11-07T10:00:00+08:00'

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=RelationshipStore(Path(self.temp.name)/'test.sqlite3')
        self.customer=self.store.add_customer(Customer(id='new',name='沈南',salutation='南南'))
    def tearDown(self):self.temp.cleanup()
    def parse(self,text):return preview(self.customer,ImportPreview(text=text,as_of=AS_OF))
    def data(self,text,identity='batch'):
        turns=[{k:t[k] for k in ('actor','text','occurred_at','time_confirmed')} for t in self.parse(text)['turns']]
        return ImportConfirm(id=identity,text=text,as_of=AS_OF,turns=turns)
    def test_multiline_roles_and_unknown_are_preserved(self):
        data=self.parse('沈南：想给爸爸买一个钥匙包。\n他喜欢绿色。\n顾问：平常有几把钥匙？\n朋友：不知道。')
        self.assertEqual([t['actor'] for t in data['turns']],['client','advisor','unknown'])
        self.assertEqual(data['turns'][0]['text'],'想给爸爸买一个钥匙包。\n他喜欢绿色。')
        self.assertEqual(data['unknown_speakers'],1);self.assertEqual(data['missing_times'],3)
        self.assertEqual(self.parse('一段没有说话人标记的原话')['turns'][0]['actor'],'unknown')
    def test_timestamped_wechat_headers_and_url(self):
        data=self.parse('沈南 2026-11-06 09:00\n这个颜色可以\nhttps://example.com\n顾问 2026-11-06 09:05\n好的。')
        self.assertEqual(len(data['turns']),2)
        self.assertIn('https://example.com',data['turns'][0]['text'])
        self.assertEqual(data['turns'][1]['occurred_at'],'2026-11-06T01:05:00+00:00')
        self.assertEqual(data['missing_times'],0)
    def test_atomic_idempotent_order_and_different_payload_conflict(self):
        data=self.data('客户：先看绿色。\n顾问：好的。\n客户：还是给自己选白色吧。')
        result=confirm(self.store,'new',data)
        self.assertFalse(result['reused']);self.assertTrue(confirm(self.store,'new',data)['reused'])
        events=self.store.timeline('new',AS_OF)
        self.assertEqual(len(events),3);self.assertEqual(events[-1]['text'],'还是给自己选白色吧。')
        self.assertEqual([e['data']['import_position'] for e in events],[0,1,2])
        with self.assertRaises(Conflict):confirm(self.store,'new',self.data('客户：不同内容。'))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT raw_text FROM conversation_imports').fetchone()[0],data.text)
    def test_invalid_chronology_and_future_write_nothing(self):
        for text in ('2026-11-06 10:00 客户：第一条\n2026-11-06 09:00 顾问：第二条','2026-11-08 10:00 客户：未来消息'):
            with self.assertRaises(ValueError):confirm(self.store,'new',self.data(text))
        self.assertEqual(self.store.timeline('new',AS_OF),[])
    def test_sql_failure_rolls_back_entire_batch(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON events WHEN NEW.id='import_batch_001' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):confirm(self.store,'new',self.data('客户：一\n顾问：二'))
        self.assertEqual(self.store.timeline('new',AS_OF),[])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_imports').fetchone()[0],0)

    def export(self,messages=None,**changes):
        raw={'chatlab':{'version':'0.0.2'},'meta':{'type':'private'},
             'members':[{'platformId':'wx_customer','accountName':'南南'},{'platformId':'wx_me','accountName':'顾问微信名'}],
             'messages':messages if messages is not None else [
                 {'sender':'wx_me','type':0,'timestamp':1793926807,'content':'好的。'},
                 {'sender':'wx_customer','type':0,'timestamp':1793926801,'content':'我喜欢绿色。\n姐姐喜欢白色。'},
                 {'sender':'wx_customer','type':1,'timestamp':1793926804,'content':'[图片]'}]}
        raw.update(changes)
        return json.dumps(raw,ensure_ascii=False)

    def chatlab(self,text):return preview(self.customer,ImportPreview(text=text,format='chatlab',as_of=AS_OF))

    def test_chatlab_preserves_text_seconds_sender_and_counts_nontext(self):
        result=self.chatlab(self.export());turns=result['turns']
        self.assertEqual(len(turns),2);self.assertEqual(result['skipped_messages'],1)
        self.assertEqual(turns[0]['text'],'我喜欢绿色。\n姐姐喜欢白色。')
        self.assertEqual(turns[0]['speaker_id'],'wx_customer');self.assertEqual(turns[0]['speaker'],'南南')
        self.assertEqual(turns[0]['actor'],'unknown')  # matching nickname is not a verified identity
        self.assertEqual(turns[0]['occurred_at'],'2026-11-06T01:00:01+00:00')
        self.assertEqual(turns[1]['occurred_at'],'2026-11-06T01:00:07+00:00')

    def test_chatlab_rejects_invalid_files_without_partial_preview(self):
        valid=json.loads(self.export())['messages'][1]
        invalid=[self.export(meta={'type':'group'}),'not json','[]',self.export(chatlab={'version':'99'}),
                 self.export([valid,{'type':0}]),self.export([{**valid,'timestamp':1793926801000}]),
                 self.export([{**valid,'timestamp':True}]),self.export([{**valid,'content':None}]),
                 self.export([{**valid,'sender':str(i)} for i in range(3)]),
                 self.export([{**valid,'type':1}]),self.export([valid]*61)]
        for text in invalid:
            with self.subTest(text=text[:60]):
                with self.assertRaises(ValueError):self.chatlab(text)
        self.assertEqual(self.store.timeline('new',AS_OF),[])

    def test_chatlab_confirmation_persists_raw_and_confirmed_identity(self):
        text=self.export();turns=self.chatlab(text)['turns']
        turns=[{k:t[k] for k in ('text','occurred_at','time_confirmed')}|{'actor':'client' if t['speaker_id']=='wx_customer' else 'advisor'} for t in turns]
        data=ImportConfirm(id='file',text=text,format='chatlab',as_of=AS_OF,turns=turns)
        confirm(self.store,'new',data);self.assertTrue(confirm(self.store,'new',data)['reused'])
        events=self.store.timeline('new',AS_OF)
        self.assertEqual([e['actor'] for e in events],['client','advisor'])
        self.assertEqual([e['data']['requires_memory_update'] for e in events],[True,False])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT raw_text FROM conversation_imports').fetchone()[0],text)

    def test_text_import_retry_keeps_old_confirmation_shape(self):
        data=self.data('客户：绿色。');confirm(self.store,'new',data)
        with self.store.connect() as db:
            stored=json.loads(db.execute('SELECT confirmed_json FROM conversation_imports').fetchone()[0])
        self.assertNotIn('format',stored);self.assertTrue(confirm(self.store,'new',ImportConfirm(**stored))['reused'])

    def test_export_size_limit_is_separate_from_normal_requests(self):
        from atelier.app import create_app
        from atelier.engine import Engine
        from test_atelier_engine import Fake
        app=create_app(Engine(self.store,Fake([]),Fake([])),seed=False);client=app.test_client()
        try:
            raw=self.export([{'sender':'me','type':0,'timestamp':1793926801+i,'content':'绿'*3000} for i in range(15)])
            result=client.post('/api/customers/new/imports/preview',json={'text':raw,'format':'chatlab','as_of':AS_OF})
            self.assertEqual(result.status_code,200,result.json);self.assertEqual(len(result.json['turns']),15)
            self.assertEqual(client.post('/api/customers',json={'name':'a'*140000}).status_code,413)
            self.assertEqual(client.post('/api/customers/new/imports/preview',json={'text':'a'*2200000,'as_of':AS_OF}).status_code,413)
        finally:app.extensions['job_queue'].pool.shutdown(wait=True)
    def test_v4_migration_retains_rows_and_sample_type_after_manual_edit(self):
        path=Path(self.temp.name)/'v4.sqlite3'
        with sqlite3.connect(path) as db:
            for i in range(1,5):db.executescript((ROOT/f'atelier/migrations/{i:03d}.sql').read_text())
            db.execute('PRAGMA user_version=4')
            db.execute("INSERT INTO memories(id,scope,owner_id,key,value,provenance,source,effective_at,applicability,created_at,updated_at) VALUES('sample','advisor','default','example.price','已编辑的价格样本','manual','原反馈',?,'price',?,?)",(AS_OF,AS_OF,AS_OF))
        db.close()
        migrated=RelationshipStore(path)
        self.assertEqual(migrated.memory('sample')['kind'],'example')
        self.assertEqual(migrated.memory('sample')['value'],'已编辑的价格样本')
        RelationshipStore(path)
        with migrated.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memories').fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],9)

if __name__=='__main__':unittest.main()
