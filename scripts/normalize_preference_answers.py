"""Expand unchanged legacy questionnaire answers, retaining raw answers and audit."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from atelier.preferences import QUESTIONS,question_rule
from atelier.store import RelationshipStore,now


def normalize(store, output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    store.backup(output/'before.sqlite3')
    changes=[]
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        assert not db.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchone(),'Wait for active jobs'
        before={row['id']:dict(row) for row in db.execute('SELECT * FROM memories')}
        questions_before=[tuple(row) for row in db.execute('SELECT * FROM questions ORDER BY advisor_id,question_id')]
        for q in QUESTIONS:
            rows=db.execute("SELECT m.* FROM memories m JOIN questions q ON q.advisor_id=m.owner_id AND q.question_id=? WHERE m.scope='advisor' AND m.kind='rule' AND m.status='active' AND m.key=? AND m.source=? AND q.answer=m.value",
                            (q['id'],q['key'],'偏好问答：'+q['title'])).fetchall()
            for row in rows:
                old=dict(row)
                if old['value'] not in q['options']:continue
                value=question_rule(q,old['value'])
                current=old|{'value':value,'revision':old['revision']+1,'updated_at':now()}
                db.execute('UPDATE memories SET value=?,revision=?,updated_at=? WHERE id=?',
                           (value,current['revision'],current['updated_at'],old['id']))
                store._audit(db,current,old,'normalize_question_answer')
                changes.append({'id':old['id'],'before':old['value'],'after':value})
        after={row['id']:dict(row) for row in db.execute('SELECT * FROM memories')}
        changed={row['id'] for row in changes}
        assert set(before)==set(after)
        for mid,old in before.items():
            if mid not in changed:assert old==after[mid]
            else:assert {k:v for k,v in old.items() if k not in ('value','revision','updated_at')}=={k:v for k,v in after[mid].items() if k not in ('value','revision','updated_at')}
        assert questions_before==[tuple(row) for row in db.execute('SELECT * FROM questions ORDER BY advisor_id,question_id')]
    report={'changes':changes,'raw_answers_preserved':True,'other_memories_unchanged':True,'backup':str(output/'before.sqlite3')}
    (output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('database');parser.add_argument('--output',required=True);args=parser.parse_args()
    print(json.dumps(normalize(RelationshipStore(args.database),args.output),ensure_ascii=False))
