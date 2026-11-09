"""SQLite relationship store. Tombstones and optimistic revisions prevent silent memory resurrection."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .models import Customer, Event, MemoryInput, MemoryChange, instant

ROOT = Path(__file__).resolve().parents[1]
CONTACT_KEYS = frozenset({'contact_permission','service_permission','contact_after','contact_reason'})


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Conflict(ValueError):
    pass


class Missing(KeyError):
    pass


class RelationshipStore:
    def __init__(self, path=None):
        self.path = Path(path or ROOT / 'artifacts/v4/atelier.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            # SQLite table rebuild (008) keeps IDs and child references intact.
            # Disable enforcement only inside this migration connection, then
            # validate the whole graph before committing the atomic migration.
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 9:
                raise RuntimeError('数据库版本比程序新，请使用对应版本程序。')
            for target in range(version+1, 10):
                sql = (Path(__file__).parent / f'migrations/{target:03d}.sql').read_text()
                statement = ''
                for char in sql:
                    statement += char
                    if char == ';' and sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ''
                if statement.strip():
                    raise RuntimeError('迁移SQL不完整')
                db.execute(f'PRAGMA user_version={target}')
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('迁移后关联检查失败；本次迁移已回滚。')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_customer(self, customer: Customer):
        data = customer.model_dump()
        with self.connect() as db:
            old = db.execute('SELECT * FROM customers WHERE id=?', (customer.id,)).fetchone()
            if old:
                if any(old[k] != v for k, v in data.items()):
                    raise Conflict('同一客户编号已有不同资料。')
                return dict(old)
            db.execute('INSERT INTO customers VALUES (:id,:name,:salutation,:summary,:color,:created_at)',
                       data | {'created_at': now()})
        return self.customer(customer.id)

    def customer(self, customer_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
        if not row:
            raise Missing(customer_id)
        return dict(row)

    def customers(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM customers ORDER BY id')]

    def add_event(self, event: Event):
        self.customer(event.customer_id)
        data = event.model_dump()
        data['data'] = encode(data['data'])
        with self.connect() as db:
            old = db.execute('SELECT * FROM events WHERE id=?', (event.id,)).fetchone()
            if old:
                if any(old[k] != v for k, v in data.items()):
                    raise Conflict('事件编号已存在且内容不同，不能覆盖历史。')
                return self._event(old)
            db.execute('INSERT INTO events VALUES (:id,:customer_id,:occurred_at,:kind,:actor,:text,:data,:recorded_at)',
                       data | {'recorded_at': now()})
        return event.model_dump()

    @staticmethod
    def _event(row):
        data = dict(row)
        data['data'] = json.loads(data['data'])
        return data

    def timeline(self, customer_id, as_of=None):
        self.customer(customer_id)
        as_of = instant(as_of or now())
        with self.connect() as db:
            return [self._event(row) for row in db.execute(
                'SELECT * FROM events WHERE customer_id=? AND occurred_at<=? ORDER BY occurred_at,recorded_at,id',
                (customer_id, as_of))]

    @staticmethod
    def _audit(db, current, previous, action):
        db.execute('INSERT INTO memory_audit(memory_id,revision,action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',
                   (current['id'], current['revision'], action, encode(previous) if previous else None, encode(current), now()))

    def put_memory(self, memory: MemoryInput, *, expected_revision=None, kind=None, memory_id=None):
        """Import/extraction cannot overwrite human decisions; manual overwrite needs revision."""
        if memory.scope == 'customer':
            self.customer(memory.owner_id)
        if kind is not None and kind not in ('fact','rule','example'):
            raise ValueError('记忆类型无效。')
        if memory.expires_at and memory.expires_at <= memory.effective_at:
            raise ValueError('失效时间必须晚于生效时间。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if memory.source_event_id:
                source = db.execute('SELECT customer_id,text FROM events WHERE id=?', (memory.source_event_id,)).fetchone()
                if not source or memory.scope != 'customer' or source['customer_id'] != memory.owner_id:
                    raise ValueError('记忆来源必须属于该客户。')
                if memory.source_quote and memory.source_quote not in source['text']:
                    raise ValueError('记忆引用必须是来源消息中的原文。')
                if not memory.source_quote and memory.value in source['text']:
                    memory=memory.model_copy(update={'source_quote':memory.value})
            slot = (memory.scope=='advisor' or kind not in (None,'fact')
                    or memory.key in CONTACT_KEYS or memory.key.startswith('customer_note.'))
            if memory_id:
                old=db.execute('SELECT * FROM memories WHERE id=?',(memory_id,)).fetchone()
                if not old:raise Missing(memory_id)
                if old['scope']!=memory.scope or old['owner_id']!=memory.owner_id:
                    raise ValueError('不能修改其他客户或顾问的记忆。')
            elif slot or expected_revision is not None:
                rows=db.execute('SELECT * FROM memories WHERE scope=? AND owner_id=? AND key=?',
                                (memory.scope,memory.owner_id,memory.key)).fetchall()
                if len(rows)>1:raise Conflict('同类记忆有多条，请按记录编号指定要修改的那条。')
                old=rows[0] if rows else None
                if expected_revision is not None and not old:raise Conflict('要修改的记忆不存在。')
            else:
                # Retried delivery of the same source is idempotent. A shared
                # topic alone never identifies an existing fact to overwrite.
                old=db.execute('''SELECT * FROM memories WHERE scope=? AND owner_id=? AND kind='fact'
                    AND source_event_id IS ? AND source_quote IS ? AND value=? AND applicability=?''',
                    (memory.scope,memory.owner_id,memory.source_event_id,memory.source_quote,
                     memory.value,memory.applicability)).fetchone()
                if old and old['status']=='active' and not old['human_locked']:
                    return dict(old)
            if memory.provenance!='manual' and memory.source_event_id and memory.source_quote:
                protected=db.execute('''SELECT * FROM memories WHERE scope=? AND owner_id=?
                    AND source_event_id=? AND (human_locked=1 OR status!='active')''',
                    (memory.scope,memory.owner_id,memory.source_event_id)).fetchall()
                if any(not r['source_quote'] or r['source_quote'] in memory.source_quote
                       or memory.source_quote in r['source_quote'] for r in protected):
                    raise Conflict('这段原话的记忆已经过人工处理，自动提取不能换编号重新写入。')
            if old:
                old = dict(old)
                if memory.provenance != 'manual' and (old['human_locked'] or old['status'] != 'active'):
                    raise Conflict('该记忆已由人工修改、停用或删除，自动提取不得覆盖。')
                if memory.provenance == 'inferred' and old['provenance'] in ('explicit', 'manual'):
                    raise Conflict('推断不得覆盖已明确确认的事实。')
                if expected_revision != old['revision']:
                    raise Conflict('记忆已存在或已更新，请读取最新版本后再修改。')
                if memory.effective_at < old['effective_at']:
                    raise Conflict('旧资料不能覆盖更新的记忆。')
            current = memory.model_dump() | {
                'kind': kind or (old['kind'] if old else ('fact' if memory.scope=='customer' else ('example' if memory.provenance=='inferred' else 'rule'))),
                'id': old['id'] if old else uuid.uuid4().hex,
                'status': 'active', 'revision': old['revision'] + 1 if old else 1,
                'human_locked': int(memory.provenance == 'manual'),
                'created_at': old['created_at'] if old else now(), 'updated_at': now(),
            }
            if old:
                columns = [k for k in current if k != 'id']
                db.execute('UPDATE memories SET ' + ','.join(k+'=?' for k in columns) + ' WHERE id=?',
                           [current[k] for k in columns] + [current['id']])
            else:
                keys = list(current)
                db.execute('INSERT INTO memories (' + ','.join(keys) + ') VALUES (' + ','.join('?' for _ in keys) + ')',
                           [current[k] for k in keys])
            self._audit(db, current, old, 'replace' if old else 'create')
        return current

    def memory(self, memory_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
        if not row:
            raise Missing(memory_id)
        return dict(row)

    def change_memory(self, memory_id, change: MemoryChange):
        updates = change.model_dump(exclude_none=True, exclude={'expected_revision'})
        if 'expires_at' in change.model_fields_set:
            updates['expires_at'] = change.expires_at
        if not updates:
            raise ValueError('请提供需要修改的内容。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
            if not row:
                raise Missing(memory_id)
            old = dict(row)
            if old['revision'] != change.expected_revision:
                raise Conflict('记忆已更新，请刷新后再修改，避免覆盖其他修改。')
            if old['status'] == 'deleted':
                raise Conflict('已删除的记忆不能直接恢复；请明确创建新的人工记忆。')
            if updates.get('expires_at') and updates['expires_at'] <= old['effective_at']:
                raise ValueError('失效时间必须晚于生效时间。')
            current = old | updates | {'revision': old['revision'] + 1, 'human_locked': 1,
                                      'provenance': 'manual', 'updated_at': now()}
            columns = [k for k in current if k != 'id']
            db.execute('UPDATE memories SET ' + ','.join(k+'=?' for k in columns) + ' WHERE id=?',
                       [current[k] for k in columns] + [memory_id])
            self._audit(db, current, old, 'manual_'+current['status'])
        return current

    def memories(self, scope, owner_id, include_inactive=False):
        with self.connect() as db:
            sql = 'SELECT * FROM memories WHERE scope=? AND owner_id=?'
            if not include_inactive:
                sql += " AND status='active'"
            return [dict(r) for r in db.execute(sql+' ORDER BY key,effective_at,id', (scope, owner_id))]

    def context(self, customer_id, as_of, advisor_id='default'):
        """Effective facts + event history. Redact old evidence after memory correction/removal.

        Audit history stays available to the operator, never silently re-enters generation.
        Quote-level redaction retains unrelated facts; legacy sources fail closed.
        """
        as_of = instant(as_of)
        customer = self.customer(customer_id)
        records = self.memories('customer', customer_id, include_inactive=True)
        preferences = self.memories('advisor', advisor_id, include_inactive=True)
        active = lambda r: r['status'] == 'active' and r['effective_at'] <= as_of and (not r['expires_at'] or r['expires_at'] > as_of)
        suppressed = []
        for r in records:
            if r['source_event_id'] and (not active(r) or r['human_locked']):
                suppressed.append(r)
        # Earlier sources must not revive a superseded fact.
        with self.connect() as db:
            history = db.execute('SELECT a.before_json FROM memory_audit a JOIN memories m ON m.id=a.memory_id WHERE m.scope=? AND m.owner_id=? AND a.before_json IS NOT NULL',
                                 ('customer', customer_id)).fetchall()
        for row in history:
            previous = json.loads(row[0])
            if previous.get('source_event_id'):
                suppressed.append(previous)
        events = self.timeline(customer_id, as_of)
        pending = []
        with self.connect() as db:
            for event in events:
                if event['data'].get('requires_memory_update'):
                    extraction = db.execute('SELECT status FROM memory_extractions WHERE event_id=?', (event['id'],)).fetchone()
                    if not extraction or extraction['status'] != 'complete':
                        pending.append({'event_id':event['id'], 'status':extraction['status'] if extraction else 'pending'})
        for event in events:
            omissions=[r for r in suppressed if r['source_event_id']==event['id']]
            if not omissions:continue
            original=event['text'];spans=[];unknown=False
            for r in omissions:
                quote=r.get('source_quote')
                if not quote:
                    # Compatible with pre-v5 explicit extraction and seed facts.
                    value=r.get('value','')
                    source=r.get('source','').removeprefix('从客户原话提取：')
                    quote=value if value and value in original else source if source and source in original else None
                if not quote or quote not in original:
                    unknown=True;break
                start=0
                while (index:=original.find(quote,start))!=-1:
                    spans.append((index,index+len(quote)));start=index+len(quote)
            if unknown:
                # Older imports without a recoverable quote cannot safely be
                # rewritten. Retain other still-active quote-backed facts only.
                retained=[r.get('source_quote') or r['value'] for r in records
                    if r['source_event_id']==event['id'] and active(r) and not r['human_locked']
                    and not any(x['id']==r['id'] for x in omissions)]
                event['text']='历史原文部分已撤回。仍有效的原话：'+'；'.join(q for q in retained if q in original)
                event['redacted_for_generation']=not bool(retained)
            else:
                merged=[]
                for start,end in sorted(spans):
                    if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
                    else:merged.append((start,end))
                text=original
                for start,end in reversed(merged):text=text[:start]+'［已撤回的事实］'+text[end:]
                event['text']=text
                event['partially_redacted']=True
            event['data']={}
        # Static overview prose is not factual memory and must not override corrections.
        return {'customer': {k: customer[k] for k in ('id','name','salutation')}, 'as_of': as_of,
                'events': events, 'memories': [self.generation_memory(r) for r in records if active(r) and r['kind']=='fact'], 'pending_memory_updates':pending,
                'customer_preferences': [self.generation_memory(r) for r in records if active(r) and r['kind']=='rule'],
                'advisor_preferences': [r for r in preferences if r['status'] == 'active'
                    and r['effective_at'] <= now() and (not r['expires_at'] or r['expires_at'] > now())]}

    @staticmethod
    def generation_memory(record):
        # Original quotes stay available in the audit UI, not in model context
        # after a human correction. Even provenance text can leak withdrawn facts.
        result=record | {'source_quote':None}
        if record['human_locked']:
            result['source']='顾问手动确认或修正'
        elif record['key'] in CONTACT_KEYS and record.get('source_quote'):
            # The enum describes the current contact state, not its duration.
            # Keep the client's actual scope beside it, including for older
            # records whose stored applicability was the generic default.
            result['applicability']=record['source_quote']
        return result

    def save_draft(self, record):
        with self.connect() as db:
            db.execute('INSERT INTO drafts VALUES(?,?,?,?,?)', (record['id'], record.get('customer_id'),
                record['status'], record['created_at'], encode(record)))

    def draft(self, draft_id):
        with self.connect() as db:
            row = db.execute('SELECT record FROM drafts WHERE id=?', (draft_id,)).fetchone()
        if not row:
            raise Missing(draft_id)
        return json.loads(row[0])

    def drafts(self, customer_id=None, limit=50):
        with self.connect() as db:
            if customer_id:
                rows = db.execute('SELECT record FROM drafts WHERE customer_id=? ORDER BY created_at DESC LIMIT ?', (customer_id, limit))
            else:
                rows = db.execute('SELECT record FROM drafts ORDER BY created_at DESC LIMIT ?', (limit,))
            return [json.loads(r[0]) for r in rows]

    def backup(self, destination):
        """Consistent SQLite snapshot, including any committed WAL contents."""
        target=sqlite3.connect(destination)
        try:
            with self.connect() as db:db.backup(target)
        finally:
            target.close()
