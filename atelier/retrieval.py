"""Retrieve natural-language notes and examples across intents.

The primary store is SQLite. Vectors are an expendable, content-keyed in-process
cache, so editing, disabling or deleting a memory takes effect on the next call.
No external vector service and no implicit model download during a request.
"""
from collections import Counter, OrderedDict
import math
import os
from pathlib import Path
import re
import threading

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'BAAI/bge-small-zh-v1.5'
SCENES = {'greeting':'生日 节庆 祝福', 'recommendation':'商品推荐 选购 用途',
          'care':'购后 养护 售后', 'reactivation':'重新联系',
          'invitation':'活动 邀请 预约', 'price':'价格 还价 价值', 'conversation':'日常对话'}


def terms(text):
    """Chinese bigrams plus Latin words; no scenario classifier or exclusivity."""
    result=[]
    for word in re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+',text.lower()):
        result.extend([word] if len(word)<2 or word.isascii() else [word[i:i+2] for i in range(len(word)-1)])
    return result


def bm25(queries, documents):
    counts=[Counter(terms(d)) for d in documents]
    avg=sum(sum(c.values()) for c in counts)/max(1,len(counts)) or 1
    df=Counter(t for c in counts for t in c)
    result=[]
    for query in queries:
        row=[]
        for c in counts:
            score=0.
            for t in set(terms(query)):
                if t in c:
                    idf=math.log(1+(len(counts)-df[t]+.5)/(df[t]+.5))
                    score+=idf*c[t]*2.2/(c[t]+1.2*(.25+.75*sum(c.values())/avg))
            row.append(score)
        result.append(row)
    return result


class LocalEncoder:
    def __init__(self, path=None):
        path=Path(path or ROOT/'.runtime/bge-small-zh-v1.5')
        if not (path/'model_optimized.onnx').is_file():
            raise ValueError('语义检索模型尚未安装，请运行 scripts/setup_memory_retrieval.py。')
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise ValueError('语义检索需要 requirements-memory.txt 中的依赖。') from None
        self.model=TextEmbedding(MODEL,specific_model_path=str(path),local_files_only=True,
                                 threads=2,providers=['CPUExecutionProvider'])
        self.model_id=MODEL
        self.cache=OrderedDict()
        self.lock=threading.Lock()

    def encode(self, texts):
        # Cache by exact content: no stale vectors after a user correction.
        with self.lock:
            missing=list(dict.fromkeys(t for t in texts if t not in self.cache))
            if missing:
                for text,vector in zip(missing,self.model.embed(missing,batch_size=16)):
                    self.cache[text]=vector.tolist()
            result=[self.cache[t] for t in texts]
            for text in texts:self.cache.move_to_end(text)
            while len(self.cache)>2048:self.cache.popitem(last=False)
            return result


class ExampleRetriever:
    def __init__(self, encoder=None, limit=6):
        self.encoder=encoder
        self.limit=limit

    @classmethod
    def configured(cls):
        mode=os.environ.get('LANXU_RETRIEVAL_MODE','lexical')
        if mode not in ('lexical','hybrid'):raise ValueError('LANXU_RETRIEVAL_MODE 只能是 lexical 或 hybrid。')
        return cls(LocalEncoder() if mode=='hybrid' else None)

    def select(self, context, memories, *, limit=None, scope_policy=None):
        limit=self.limit if limit is None else limit
        request=context['request']
        messages=[e for e in context['events'] if e['kind']=='message' and not e.get('redacted_for_generation')]
        latest=next((e['text'] for e in reversed(messages) if e['actor']=='client'),'')
        # Each clause can retrieve: a birthday clause cannot hide a product query.
        instruction=context.get('revision_request',{}).get('instruction','')
        main='\n'.join(x for x in (latest,request.get('intent',''),request.get('occasion',''),instruction) if x)
        queries=list(dict.fromkeys([main]+[s.strip() for s in re.split(r'[。！？!?；;\n]',main) if len(s.strip())>=4]))[:10]
        if not main.strip():queries=[SCENES.get(request.get('scenario'),'当前客户沟通')]
        documents=[SCENES.get(m['applicability'],m['applicability'])+'\n'+m['value'] for m in memories]
        trace={'version':1,'method':'hybrid' if self.encoder else 'lexical',
               'model':self.encoder.model_id if self.encoder else None,
               'query':main,'eligible_count':len(memories),'selected':[],
               'scope_policy':scope_policy or '仅当前顾问的有效例子；情境用于相关性，不作互斥分类'}
        if not memories:return [],trace
        lexical=bm25(queries,documents)
        semantic=[[0.]*len(documents) for _ in queries]
        if self.encoder:
            vectors=self.encoder.encode(queries+documents)
            for qi,q in enumerate(vectors[:len(queries)]):
                for di,d in enumerate(vectors[len(queries):]):
                    semantic[qi][di]=sum(a*b for a,b in zip(q,d))
        # Scores select examples, never decide facts, contact permission or rules.
        rows=[]
        for qi in range(len(queries)):
            top=max(lexical[qi]) or 1
            rows.append([(.65*max(0,(semantic[qi][i]-.45)/.55)+.35*lexical[qi][i]/top)
                         if self.encoder else lexical[qi][i]/top for i in range(len(documents))])
        # No relevance cutoff: false negatives matter more here than a few
        # additional examples. Ownership/time/status filtering happens upstream.
        eligible=[i for i in range(len(memories)) if max(row[i] for row in rows)>0]
        chosen=[];seen=set()
        def add(i):
            # Equivalent copies add no learning evidence to a single prompt.
            key=re.sub(r'\s+','',memories[i]['value'])
            if i not in chosen and key not in seen:
                chosen.append(i);seen.add(key)
        # Cover multiple current intents, then fill remaining space by relevance.
        for row in rows:
            ranked=sorted(eligible,key=lambda i:(row[i],memories[i]['id']),reverse=True)
            if ranked and row[ranked[0]]>0:add(ranked[0])
            if len(chosen)>=limit:break
        for i in sorted(eligible,key=lambda i:(max(r[i] for r in rows),memories[i]['id']),reverse=True):
            if len(chosen)>=limit:break
            add(i)
        for i in chosen:
            trace['selected'].append({'id':memories[i]['id'],'revision':memories[i]['revision'],
                'score':round(max(r[i] for r in rows),4),'semantic':round(max(r[i] for r in semantic),4),
                'lexical':round(max(r[i] for r in lexical),4),'applicability':memories[i]['applicability']})
        return [memories[i] for i in chosen],trace


def select_customer_facts(retriever, context):
    """Notes stay verbatim; contact controls never depend on vector recall."""
    from .store import CONTACT_KEYS
    facts=context['memories']
    controls=[m for m in facts if m['key'] in CONTACT_KEYS]
    notes=[m for m in facts if m['key'] not in CONTACT_KEYS]
    if len(notes)<=30:
        return facts,{'method':'all_current','eligible_count':len(notes),
                      'selected_ids':[m['id'] for m in notes],'control_ids':[m['id'] for m in controls]}
    recent=sorted(notes,key=lambda m:(m['effective_at'],m['updated_at'],m['id']),reverse=True)[:6]
    recent_ids={m['id'] for m in recent}
    chosen,trace=retriever.select(context,[m for m in notes if m['id'] not in recent_ids],limit=24,
        scope_policy='仅本客户当前有效的文字记忆；标签允许重复，不按标签排除')
    ids=recent_ids|{m['id'] for m in chosen}|{m['id'] for m in controls}
    return [m for m in facts if m['id'] in ids],trace|{
        'eligible_count':len(notes),'selected_ids':[m['id'] for m in recent+chosen],
        'recent_ids':[m['id'] for m in recent],'control_ids':[m['id'] for m in controls]}
