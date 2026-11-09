"""Anonymous cross-version comparisons, deliberately separate from preference learning."""
import json
import random
import uuid
from .store import ROOT, Conflict, Missing, encode, now


def dataset(path=None):
    path = path or ROOT/'artifacts/v4/comparisons.json'
    if not path.exists():
        return {'source':None, 'cards':[]}
    return json.loads(path.read_text())


def cards(store, advisor_id='default', path=None):
    data = dataset(path)
    with store.connect() as db:
        reviewed = {r[0] for r in db.execute('SELECT card_id FROM comparison_votes WHERE advisor_id=?',(advisor_id,))}
    public = []
    for card in data['cards']:
        if card['id'] in reviewed:
            continue
        choices = [{'id':c['id'], 'text':c['text']} for c in card['choices']]
        random.SystemRandom().shuffle(choices)
        public.append({'id':card['id'], 'context':card['context'], 'choices':choices})
    return public


def vote(store, data, advisor_id='default', path=None):
    if not isinstance(data,dict):
        raise ValueError('比较反馈应为对象。')
    source = dataset(path)
    card = next((c for c in source['cards'] if c['id']==data.get('card_id')),None)
    if not card:
        raise Missing('comparison card')
    valid = [c['id'] for c in card['choices']]
    order = data.get('display_order')
    outcome = data.get('outcome')
    choice = data.get('choice_id')
    reason = data.get('reason','')
    if not isinstance(order,list) or any(not isinstance(x,str) for x in order) or sorted(order)!=sorted(valid):
        raise ValueError('展示顺序与这组三个候选不一致。')
    if outcome not in ('choice','tie','none','skip'):
        raise ValueError('请选择有效反馈。')
    if (outcome=='choice' and choice not in valid) or (outcome!='choice' and choice is not None):
        raise ValueError('选择与反馈类型不一致。')
    if not isinstance(reason,str) or len(reason)>500:
        raise ValueError('补充原因请控制在500字内。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT * FROM comparison_votes WHERE advisor_id=? AND card_id=?',(advisor_id,card['id'])).fetchone()
        if prior:
            if prior['outcome']==outcome and prior['choice_id']==choice and prior['reason']==reason:
                return {'saved':True,'duplicate':True,'learned':False,'note':'这组比较已记录。'}
            raise Conflict('这组比较已记录，请继续下一组。')
        db.execute('INSERT INTO comparison_votes VALUES(?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex,advisor_id,card['id'],source['source'],outcome,choice,encode(order),reason,now()))
    return {'saved':True,'duplicate':False,'learned':False,'note':'已记录匿名比较，不会改变您的表达偏好。'}
