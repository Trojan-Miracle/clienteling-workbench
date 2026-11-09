"""One editable document over existing scoped rules; examples stay independent."""
import hashlib
import json
import uuid
from collections import defaultdict
from .models import MemoryInput
from .store import Conflict, now, encode

SCENES = {'greeting':'生日 / 节庆', 'recommendation':'商品推荐', 'care':'购后关怀',
          'reactivation':'重新联系', 'invitation':'活动邀请', 'price':'价格异议', 'conversation':'日常对话'}


def rules_in(db, owner):
    return [dict(r) for r in db.execute("SELECT * FROM memories WHERE scope='advisor' AND owner_id=? AND kind='rule' AND status!='deleted' ORDER BY key", (owner,))]


def document_from(rules):
    revision = hashlib.sha256(encode(rules).encode()).hexdigest()
    groups = defaultdict(list)
    clock = now()
    for r in rules:
        if r['status'] != 'active' or (r['expires_at'] and r['expires_at'] <= clock):
            continue
        # Future and expiring rules keep their original time boundaries.
        groups[(r['applicability'], r['effective_at'] if r['effective_at'] > clock else '', r['expires_at'] or '')].append(r)
    groups.setdefault(('所有情境', '', ''), [])
    sections = []
    for (scope, start, end), rows in sorted(groups.items(), key=lambda x: (x[0] != ('所有情境','',''), x[0])):
        title = SCENES.get(scope, scope)
        if start: title += ' · 从 ' + start
        if end: title += ' · 至 ' + end
        sections.append({'marker': '【' + title + '】', 'scope':scope, 'start':start, 'end':end,
                         'ids':[r['id'] for r in rows], 'text':'\n'.join(r['value'] for r in rows)})
    text = sections[0]['text'] if len(sections) == 1 else '\n\n'.join(s['marker']+'\n'+s['text'] for s in sections)
    return {'text':text, 'revision':revision, 'sections':sections}


def read_document(store, owner='default', *, global_only=False):
    with store.connect() as db:
        rules=rules_in(db,owner)
        if global_only:rules=[r for r in rules if r['applicability']=='所有情境' and not r['expires_at'] and r['effective_at']<=now()]
        return document_from(rules)


def save_document(store, text, revision, owner='default', *, global_only=False):
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError('表达要求最多20000字。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rules = rules_in(db, owner)
        if global_only:rules=[r for r in rules if r['applicability']=='所有情境' and not r['expires_at'] and r['effective_at']<=now()]
        doc = document_from(rules)
        if revision != doc['revision']:
            raise Conflict('表达要求已在别处更新。你的修改仍保留，请读取最新内容后合并。')
        if text == doc['text']:
            return doc
        sections = doc['sections']
        bodies = [text.strip()]
        if len(sections) > 1:
            # These visible headings protect scope/expiry from accidental broadening.
            lines = text.splitlines()
            positions = []
            for s in sections:
                matches = [i for i, line in enumerate(lines) if line == s['marker']]
                if len(matches) != 1:
                    raise ValueError('请保留每个【适用情境】标题，只修改标题下的内容；留空可移除该段要求。')
                positions.append(matches[0])
            if positions != sorted(positions) or any(line.strip() for line in lines[:positions[0]]):
                raise ValueError('请保留适用情境标题的顺序，内容写在对应标题下。')
            bodies = ['\n'.join(lines[p+1:(positions[i+1] if i+1<len(positions) else len(lines))]).strip() for i,p in enumerate(positions)]
        if any(len(body) > 4000 for body in bodies):
            raise ValueError('每个适用情境的表达要求最多4000字。')
        for section, body in zip(sections, bodies):
            if body == section['text']:
                continue
            stamp = now()
            # Retire the old rules with an audit, including former feedback rules.
            for old in rules:
                if old['id'] not in section['ids']:
                    continue
                retired = old | {'status':'disabled', 'revision':old['revision']+1, 'human_locked':1, 'updated_at':stamp}
                db.execute("UPDATE memories SET status='disabled',revision=?,human_locked=1,updated_at=? WHERE id=?", (retired['revision'],stamp,old['id']))
                store._audit(db, retired, old, 'document_replace')
            if body:
                key = 'document.' + uuid.uuid4().hex
                row = MemoryInput(scope='advisor', owner_id=owner, key=key, value=body, provenance='manual',
                    source='表达要求编辑', effective_at=section['start'] or stamp,
                    expires_at=section['end'] or None, applicability=section['scope']).model_dump()
                row.update(id=uuid.uuid4().hex, kind='rule', status='active', revision=1,
                           human_locked=1, created_at=stamp, updated_at=stamp)
                keys = list(row)
                db.execute('INSERT INTO memories ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')', [row[k] for k in keys])
                store._audit(db, row, None, 'document_save')
        updated=rules_in(db,owner)
        if global_only:updated=[r for r in updated if r['applicability']=='所有情境' and not r['expires_at'] and r['effective_at']<=now()]
        return document_from(updated)
