"""Run declared cases sequentially per relationship; persist every attempt and snapshot.

Execute only after the implementation and data are ready for a fixed-version run.
No simulated sends, no later-turn context leakage, no selection of best reruns.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import csv
import hashlib
import json
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atelier.store import ROOT, RelationshipStore
from atelier.engine import Engine
from atelier.models import Customer, Event, MemoryInput, DraftRequest


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def files():
    paths = []
    for directory in ('atelier','prompts/v4','data/v4','data/v4_evaluation'):
        paths.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and p.suffix in ('.py','.json','.sql','.txt') and '__pycache__' not in p.parts)
    paths += [ROOT/'lanxu/provider.py',ROOT/'lanxu/config.py',Path(__file__)]
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}


def put_facts(store, customer_id, source, facts):
    for key, value in facts.items():
        old = next((m for m in store.memories('customer',customer_id,True) if m['key']==key),None)
        if old and old['source_event_id']==source.id and old['value']==value:
            continue
        store.put_memory(MemoryInput(scope='customer',owner_id=customer_id,key=key,value=value,
            provenance='explicit',source='声明测试输入中的客户明确表达',source_event_id=source.id,
            effective_at=source.occurred_at), expected_revision=old['revision'] if old else None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--split',choices=['held_out_initial_run','seen_regression'],default='seen_regression')
    parser.add_argument('--run-dir',help='Resume this existing directory without rerunning saved cases.')
    args = parser.parse_args()
    source = ROOT/'data/v4_evaluation/longitudinal.json'
    declared = json.loads(source.read_text())
    out = ROOT/args.run_dir if args.run_dir else ROOT/'artifacts/v4/formal'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out.mkdir(parents=True,exist_ok=True)
    hashes = files()
    version_file = out/'version.json'
    if version_file.exists():
        prior = json.loads(version_file.read_text())
        if prior['files'] != hashes:
            raise RuntimeError('运行后源代码或数据发生变化；不能将新版本混入此批结果。')
    else:
        dump(version_file, {'created_at':datetime.now(timezone.utc).isoformat(), 'files':hashes,
            'evaluation_split':args.split, 'protocol':'Predeclared 50 cases, sequential events, one generation request per case with engine bounded review. No best-of selection.'})
        shutil.copyfile(source,out/'inputs.json')
        for relative in hashes:
            destination=out/'snapshot'/relative
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/relative,destination)
    store = RelationshipStore(out/'relationships.sqlite3')

    def relationship(person):
        engine = Engine(store)
        cid = person['id']
        store.add_customer(Customer(id=cid,name=person['name'],salutation=person['salutation']))
        intro = Event(id=cid+'-intro',customer_id=cid,occurred_at=person['initial_at'],kind='message',actor='client',text=person['introduction'])
        store.add_event(intro)
        # On resume, later memory versions already exist. Do not overwrite them with the intro.
        if not any((out/(item['case_id']+'.json')).exists() for item in person['episodes']):
            put_facts(store,cid,intro,person['facts'])
        results=[]
        for item in person['episodes']:
            path=out/(item['case_id']+'.json')
            if path.exists():
                record=json.loads(path.read_text())
            else:
                if files()!=hashes:
                    raise RuntimeError('固定版本运行中发生代码或资料变化，停止余下调用以免混版。')
                for index,before in enumerate(item['before']):
                    before_at=(datetime.fromisoformat(item['as_of'])-timedelta(minutes=10-index)).isoformat()
                    event_data={'occurred_at':before_at,**before}
                    store.add_event(Event(id=item['case_id']+'-prior-'+str(index),customer_id=cid,**event_data))
                message = Event(id=item['case_id']+'-client',customer_id=cid,
                    occurred_at=item.get('message_at',item['as_of']),kind='message',actor='client',text=item['message'])
                store.add_event(message)
                put_facts(store,cid,message,item['facts'])
                request=DraftRequest(customer_id=cid,as_of=item['as_of'],trigger=item['trigger'],
                    scenario=item['scenario'],product_ids=item['product_ids'],intent=item['intent'],
                    occasion=item['occasion'],event_id=item['event_id'])
                record=engine.generate(request)
                record['test_case_id']=item['case_id']
                record['evaluation_split']=args.split
                dump(path,record)
            result={'case_id':item['case_id'],'customer_id':cid,'status':record['status'],
                    'run_id':record['id'],'file':path.name,'output_count':len((record.get('result') or {}).get('candidates',[]))}
            results.append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        return results

    with ThreadPoolExecutor(max_workers=2) as pool:
        groups=list(pool.map(relationship,declared['customers']))
    results=sorted([result for group in groups for result in group],key=lambda x:x['case_id'])
    manifest={'run':str(out.relative_to(ROOT)),'results':results,'evaluation_split':args.split,
        'all_150_outputs_present':len(results)==50 and all(r['output_count']==3 for r in results),
        'human_reviews':0, 'note':'完成人数与人类评分需另由真实反馈证据汇总；这里不把自动运行算成人工认可。'}
    dump(out/'manifest.json',manifest)
    columns=['Case ID','Scenario','Client information','Conversation history','Product information','Output 1','Output 2','Output 3']
    encode=lambda value:json.dumps(value,ensure_ascii=False)
    with (out/'50_cases.csv').open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=columns)
        writer.writeheader()
        for item in results:
            record=json.loads((out/item['file']).read_text());context=record['context']
            candidates=(record.get('result') or {}).get('candidates',[])
            row={'Case ID':item['case_id'],'Scenario':record['request']['scenario'],
                'Client information':encode({'customer':context['customer'],'as_of':context['as_of'],'memories':context['memories'],'request':record['request']}),
                'Conversation history':encode(context['events']),
                'Product information':encode({'products':context['products'],'brand':context['brand'],'event':context['boutique_event']})}
            row.update({f'Output {i+1}':candidates[i]['text'] if i<len(candidates) else '' for i in range(3)})
            writer.writerow(row)
    dump(ROOT/'artifacts/v4/latest-formal.json',{'path':str(out.relative_to(ROOT))})
    print('FINISHED',json.dumps(manifest,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
