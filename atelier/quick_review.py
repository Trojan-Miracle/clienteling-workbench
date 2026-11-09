"""Developer review: frozen originals, explicit feedback, durable model revisions.

Nothing here writes advisor preferences or silently changes generation prompts.
The separate SQLite schema is additive and initialized idempotently.
"""
import difflib
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from flask import jsonify, request, send_from_directory
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from lanxu.provider import ProviderError
from .providers import generation_provider


class RevisionResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revised_text: str = Field(min_length=1, max_length=1600)
    changes: list[str] = Field(min_length=1, max_length=6)
    general_suggestion: str = Field(min_length=1, max_length=1200)
    limits: str = Field(min_length=1, max_length=800)


def review_provider():
    # Rewriting is generation. Use the same configuration as the workbench,
    # rather than silently choosing its separate review/judge model.
    return generation_provider()


def register_quick_review(app, root, db_path=None, provider=None):
    root = Path(root)
    dataset = json.loads((root/'data/review12.json').read_text())
    cases = {c['case_id']: c for c in dataset['cases']}
    db_path = Path(db_path or root/'artifacts/human-review12/reviews.sqlite3')
    db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect():
        db = sqlite3.connect(db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    with connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS responses(
            dataset_id TEXT NOT NULL, case_id TEXT NOT NULL, revision INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(dataset_id,case_id));
            CREATE TABLE IF NOT EXISTS review_revisions(
            id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, case_id TEXT NOT NULL,
            signature TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            payload TEXT NOT NULL);
        ''')

    def records():
        with connect() as db:
            # A crashed process must not leave a permanently running spinner.
            stale = db.execute("SELECT * FROM review_revisions WHERE dataset_id=? AND status='running'", (dataset['dataset_id'],)).fetchall()
            for row in stale:
                payload = json.loads(row['payload'])
                if time.time() <= payload.get('deadline_at', row['created_at']+120):
                    continue
                payload |= {'status':'failed','error':'这次改写已中断，原稿和意见仍保留，可重新改写。'}
                db.execute("UPDATE review_revisions SET status='failed',payload=? WHERE id=?", (json.dumps(payload,ensure_ascii=False),row['id']))
            responses = {r['case_id']:json.loads(r['payload']) for r in db.execute('SELECT * FROM responses WHERE dataset_id=?',(dataset['dataset_id'],))}
            revisions = [json.loads(r['payload']) for r in db.execute('SELECT * FROM review_revisions WHERE dataset_id=? ORDER BY created_at',(dataset['dataset_id'],))]
        return {'responses':responses, 'revisions':revisions}

    @app.get('/review12')
    def quick_review_page():
        return send_from_directory(root/'web/v6', 'review12.html')

    @app.get('/api/quick-review')
    def quick_review_data():
        changes = json.loads((root/'data/review_changes.json').read_text())
        return jsonify(dataset | records() | {'product_changes':changes})

    @app.get('/api/quick-review/export')
    def quick_review_export():
        result = jsonify(dataset_id=dataset['dataset_id'], selection=dataset['selection'], **records())
        result.headers['Content-Disposition'] = 'attachment; filename="virel-developer-review.json"'
        return result

    @app.post('/api/quick-review/<case_id>')
    def save_quick_review(case_id):
        if case_id not in cases: return jsonify(error='案例不存在。'),404
        data = request.get_json()
        if not isinstance(data,dict): return jsonify(error='请提交评价。'),422
        decision, candidate = data.get('decision'), data.get('candidate_id')
        note, expected = data.get('note',''), data.get('revision')
        if decision not in ('ready','edit','none','skip','comment'):
            return jsonify(error='请选择评价或填写意见。'),422
        ids = {c['id'] for c in cases[case_id]['candidates']}
        if (decision in ('ready','edit') and candidate not in ids) or (decision not in ('ready','edit') and candidate is not None):
            return jsonify(error='候选选择不正确。'),422
        if not isinstance(note,str) or len(note)>2000 or type(expected) is not int or expected<0 or (decision=='comment' and not note.strip()):
            return jsonify(error='请填写有效意见，最多2000字。'),422
        scope = data.get('scope','general')
        if scope not in ('general','case','taste'): return jsonify(error='意见范围不正确。'),422
        origin = data.get('origin','interface')
        if origin not in ('interface','conversation'): return jsonify(error='意见来源不正确。'),422
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM responses WHERE dataset_id=? AND case_id=?',(dataset['dataset_id'],case_id)).fetchone()
            revision = row['revision'] if row else 0
            if expected != revision:
                return jsonify(error='另一窗口已更新本组评价。请重新读取后合并；当前输入仍保留。'),409
            saved = {'case_id':case_id,'decision':decision,'candidate_id':candidate,'scope':scope,'note':note.strip(),
                     'revision':revision+1,'updated_at':time.time(),'source_run_id':cases[case_id]['source_run_id'],
                     'source_sha256':cases[case_id]['source_sha256'],'kind':'developer_review','origin':origin}
            db.execute('INSERT OR REPLACE INTO responses VALUES(?,?,?,?)',(dataset['dataset_id'],case_id,revision+1,json.dumps(saved,ensure_ascii=False)))
        return jsonify(saved)

    def run_revision(job, context, model):
        try:
            system = (root/'prompts/v6/review_feedback.txt').read_text()
            job['prompt_sha256'] = hashlib.sha256(system.encode()).hexdigest()
            reply = model.complete(system, json.dumps(context,ensure_ascii=False))
            result = RevisionResult.model_validate_json(reply.text).model_dump()
            before, after = job['before'], result['revised_text']
            diff = [{'op':tag, 'before':before[i:j], 'after':after[k:l]} for tag,i,j,k,l in difflib.SequenceMatcher(None,before,after,autojunk=False).get_opcodes()]
            done = job | {'status':'complete','result':result,'diff':diff,'metadata':reply.metadata,'finished_at':time.time()}
        except ProviderError as exc:
            done = job | {'status':'failed','error':str(exc),'finished_at':time.time()}
        except (ValidationError, ValueError):
            done = job | {'status':'failed','error':'模型未返回完整的修改说明，请重试；原稿和意见仍保留。','finished_at':time.time()}
        except Exception:
            done = job | {'status':'failed','error':'本次改写未完成，请重试；原稿和意见仍保留。','finished_at':time.time()}
        with connect() as db:
            db.execute("UPDATE review_revisions SET status=?,payload=? WHERE id=? AND status='running'",(done['status'],json.dumps(done,ensure_ascii=False),job['id']))

    @app.post('/api/quick-review/<case_id>/rewrite')
    def rewrite_quick_review(case_id):
        if case_id not in cases: return jsonify(error='案例不存在。'),404
        data = request.get_json()
        if not isinstance(data,dict): return jsonify(error='请提交修改意见。'),422
        note, scope, parent = data.get('note'), data.get('scope'), data.get('parent_id')
        candidate = next((c for c in cases[case_id]['candidates'] if c['id']==data.get('candidate_id')),None)
        if not isinstance(note,str) or not note.strip() or len(note)>2000 or scope not in ('general','case','taste') or not candidate:
            return jsonify(error='请填写修改意见并选择改写基础。'),422
        try: request_id = str(uuid.UUID(data.get('request_id','')))
        except (ValueError,TypeError,AttributeError): return jsonify(error='改写请求编号无效。'),422
        records()  # Expire interrupted requests before checking queue capacity.
        model = provider or review_provider()
        deadline = float(getattr(model,'deadline',getattr(model,'timeout',60))) + 30
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT payload FROM responses WHERE dataset_id=? AND case_id=?',(dataset['dataset_id'],case_id)).fetchone()
            if not saved or json.loads(saved['payload'])['note']!=note.strip() or json.loads(saved['payload']).get('scope','general')!=scope:
                return jsonify(error='请先保存这次意见，再进行改写。'),409
            signature = hashlib.sha256(json.dumps([case_id,note,scope,candidate['id'],parent],ensure_ascii=False).encode()).hexdigest()
            old = db.execute('SELECT * FROM review_revisions WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['signature'] != signature: return jsonify(error='请求编号已用于其他修改。'),409
                return jsonify(json.loads(old['payload']))
            if db.execute("SELECT count(*) FROM review_revisions WHERE status='running'").fetchone()[0]>=2:
                return jsonify(error='已有两次改写正在进行，请稍等。'),429
            before = candidate['text']
            if parent:
                row = db.execute('SELECT * FROM review_revisions WHERE id=? AND dataset_id=? AND case_id=? AND status=\'complete\'',(parent,dataset['dataset_id'],case_id)).fetchone()
                if not row: return jsonify(error='上一次改写不存在或尚未完成。'),422
                before = json.loads(row['payload'])['result']['revised_text']
            job = {'id':request_id,'case_id':case_id,'candidate_id':candidate['id'],'parent_id':parent,
                   'note':note.strip(),'scope':scope,'before':before,'status':'running','created_at':time.time(),
                   'source_sha256':cases[case_id]['source_sha256'],'verdict':None,
                   'deadline_at':time.time()+deadline,
                   'origin':json.loads(saved['payload']).get('origin','interface')}
            db.execute('INSERT INTO review_revisions VALUES(?,?,?,?,?,?,?)',(request_id,dataset['dataset_id'],case_id,signature,'running',job['created_at'],json.dumps(job,ensure_ascii=False)))
        context = {'case':cases[case_id], 'original':before,'feedback':note.strip(),'scope':scope}
        threading.Thread(target=run_revision,args=(job,context,model),daemon=True).start()
        return jsonify(job),202

    @app.post('/api/quick-review/revisions/<revision_id>/verdict')
    def revision_verdict(revision_id):
        data = request.get_json()
        verdict = data.get('verdict') if isinstance(data,dict) else None
        if verdict not in ('accepted','needs_work'): return jsonify(error='请选择是否改到位。'),422
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM review_revisions WHERE id=? AND dataset_id=?',(revision_id,dataset['dataset_id'])).fetchone()
            if not row or row['status']!='complete': return jsonify(error='没有可评价的改写。'),404
            saved = json.loads(row['payload']) | {'verdict':verdict,'verdict_at':time.time()}
            db.execute('UPDATE review_revisions SET payload=? WHERE id=?',(json.dumps(saved,ensure_ascii=False),revision_id))
        return jsonify(saved)
