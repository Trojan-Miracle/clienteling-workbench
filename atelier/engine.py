"""Context -> contact gate -> model decision/candidates -> bounded review, all persisted."""
from datetime import datetime, timedelta
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from pydantic import ValidationError
from lanxu.provider import make_provider, ProviderError
from .models import DraftRequest, Decision, QualityReview, instant
from .store import RelationshipStore, ROOT, now, encode
from .providers import review_provider, generation_provider
from .retrieval import ExampleRetriever, select_customer_facts
from .customer_context import adopted_replies, select_adopted_replies, eligible_advisor_examples


class Engine:
    def __init__(self, store=None, generator=None, reviewer=None, *, data_dir=None, prompt_dir=None, retriever=None):
        self.store = store or RelationshipStore()
        self.generator = generator or generation_provider()
        self.reviewer = reviewer or review_provider()
        self.data_dir = Path(data_dir or ROOT/'data/v6')
        self.prompt_dir = Path(prompt_dir or ROOT/'prompts/v6')
        self.brand = json.loads((self.data_dir/'brand.json').read_text())
        self.products = json.loads((self.data_dir/'catalog.json').read_text())
        self.retriever = retriever or ExampleRetriever.configured()

    def context(self, request: DraftRequest):
        context = self.store.context(request.customer_id, request.as_of, request.advisor_id)
        products = {p['id']: p for p in self.products}
        if any(p not in products for p in request.product_ids):
            raise ValueError('选择了不存在的商品。')
        # A new relationship has no seeded product selection. Automatic advising
        # must still have authoritative catalog facts instead of guessing them.
        product_ids=request.product_ids
        if not product_ids:
            product_ids=list(products)
        context.update(brand=self.brand, products=[products[p] for p in product_ids],
                       request=request.model_dump(mode='json'))
        if request.advisor_note_id:
            from .automatic import setting
            note=setting(self.store,'advisor-note:'+request.advisor_note_id)
            if not note or note['input']['customer_id']!=request.customer_id or note.get('status') not in ('applying','complete'):
                raise ValueError('本次补充尚未处理完成或不属于这位客户。')
            context['advisor_note']={'text':note['input']['text'],'action':note['plan']['action'],'instruction':note['plan']['instruction'],
                'confirmed_facts':note['plan']['confirmed_facts'],'previous_candidates':note.get('original_candidates',[]),
                'scope':'仅本次回复；已确认例外不改变品牌通用规则'}
        excluded_reference_memories=set()
        reference_parent_id=None
        if request.rewrite_id:
            with self.store.connect() as db:
                revision=db.execute('SELECT * FROM rewrite_requests WHERE id=?',(request.rewrite_id,)).fetchone()
            if not revision:raise ValueError('重写意见不存在。')
            parent=self.store.draft(revision['parent_draft_id'])
            if parent['customer_id']!=request.customer_id or revision['advisor_id']!=request.advisor_id:
                raise ValueError('重写意见不属于当前客户和顾问。')
            references=parent['result']['candidates']
            if revision['reference_candidates'] is not None:
                chosen=json.loads(revision['reference_candidates'])
                by_id={c['id']:c for c in references}
                references=[{'id':r['id'],'text':r['text'] if r.get('text') is not None else by_id[r['id']]['text']} for r in chosen]
                reference_parent_id=parent['id']
                # Original drafts must not return via the previous advisor note
                # or an example learned from this very draft.
                if 'advisor_note' in context:context['advisor_note'].pop('previous_candidates',None)
                with self.store.connect() as db:
                    excluded_reference_memories={r['memory_id'] for r in db.execute(
                        'SELECT memory_id FROM feedback WHERE draft_id=? AND memory_id IS NOT NULL',(parent['id'],))}
            context['revision_request']={'instruction':revision['instruction'],'candidate_id':revision['candidate_id'],
                'edited_text':revision['edited_text'],'previous_candidates':references,
                'parent_draft_id':parent['id'],'scope':'仅修改本次，尚未保存为长期偏好'}
            if revision['confirmed_facts']:
                context['revision_request'].update(confirmed_facts=revision['confirmed_facts'],
                    confirmation_scope={'customer_id':request.customer_id,'event_id':request.event_id,
                                        'as_of':request.as_of,'source':'顾问手动确认，仅用于本次回复'})
        # Personal requirements remain conditional instructions; retrieve only
        # examples. A primary scenario is never an exclusive topic classifier.
        rules=[m for m in context['advisor_preferences'] if m['kind']=='rule']
        history,history_trace=select_adopted_replies(self.retriever,context,
            [r for r in adopted_replies(self.store,context,request.advisor_id) if r['draft_id']!=reference_parent_id])
        context['customer_reply_history']=history
        used_memory_ids={r['memory_id'] for r in history}
        examples=eligible_advisor_examples(self.store,context,request.advisor_id,
            [m for m in context['advisor_preferences'] if m['kind']=='example' and m['id'] not in used_memory_ids|excluded_reference_memories])
        samples,trace=self.retriever.select(context,examples)
        context['advisor_preferences']=rules+samples
        context['context_selection']={'examples':trace,
            'customer_reply_history':history_trace,
            'rules':[{'id':m['id'],'applicability':m['applicability']} for m in rules],
            'priority':['verified_facts_and_contact_boundaries','brand_voice','customer_requirements','advisor_requirements','reference_examples'],
            'personalization':'personal' if rules or samples or context['customer_preferences'] or history else 'brand_default'}
        events_path = self.data_dir/'events.json'
        events = json.loads(events_path.read_text()) if events_path.exists() else []
        context['boutique_event'] = next((e for e in events if e['id'] == request.event_id), None)
        if request.event_id and not context['boutique_event']:
            raise ValueError('活动资料不存在。')
        # The advisor need not classify the conversation or manually attach an
        # event before asking a question about it. Dates and access rules remain
        # in the source records; availability is not a booking or contact consent.
        context['boutique_events'] = [context['boutique_event']] if request.event_id else events
        context['allowed_evidence'] = ['brand:history','brand:design','brand:voice'] + [
            'policy:'+k for k in self.brand['policies']] + ['product:'+p for p in product_ids] + [
            'event:'+e['id'] for e in context['events'] if not e.get('redacted_for_generation')] + [
            'memory:'+m['id'] for m in context['memories']] + [
            'preference:'+m['id'] for m in context['advisor_preferences']] + ['request:intent','request:occasion']
        context['allowed_evidence'] += ['customer_preference:'+m['id'] for m in context['customer_preferences']]
        context['allowed_evidence'] += ['adopted_reply:'+r['id'] for r in history]
        context['allowed_evidence'] += ['boutique:'+e['id'] for e in context['boutique_events']]
        if context.get('revision_request',{}).get('confirmed_facts'):
            context['allowed_evidence'].append('revision:confirmed_facts')
        if context.get('advisor_note',{}).get('confirmed_facts'):
            context['allowed_evidence'].append('advisor_note:confirmed_facts')
        return context

    @staticmethod
    def advisor_directed(context):
        return bool(context.get('revision_request') or context.get('advisor_note',{}).get('action') in ('draft','rewrite'))

    def gate(self, context):
        # Contact automation and a human request to write a draft are different
        # actions. Keep contact advice as context without reversing the request.
        if self.advisor_directed(context) and not context.get('pending_memory_updates'):
            advice=self._contact_gate(json.loads(encode(context)))
            if advice:context['contact_advice']=advice.model_dump()
            return None
        return self._contact_gate(context)

    def _contact_gate(self, context):
        """Only hard contact restrictions. Nuanced intent/purpose belongs to model decision."""
        request = context['request']
        memories = {m['key']: m['value'] for m in context['memories']}
        messages = [e for e in context['events'] if e['kind'] == 'message']
        inbound = bool(messages and messages[-1]['actor'] == 'client')
        scenario = request['scenario'] if request['scenario'] != 'auto' else 'conversation'
        def block(action, reason, revisit):
            return Decision(action=action, scenario=scenario, purpose='尊重客户当前联系边界',
                            reason=reason, revisit_when=revisit)
        if context.get('pending_memory_updates'):
            return block('needs_info', '新消息的关系记忆尚未处理完成，请查看更新状态或重试，避免沿用旧联系意愿。', '新消息提取完成后')
        if request['trigger'] == 'reply':
            if not inbound:
                return block('needs_info', '没有待回应的客户消息，请先录入客户消息或改为主动联系复查。', '收到新的客户消息后')
            # A fresh inbound can receive a scoped service reply, even after marketing opt-out.
            return None
        if memories.get('contact_permission') == 'do_not_contact':
            return block('no_action', '客户当前要求不主动联系。', '客户主动咨询或明确重新允许联系后')
        if memories.get('service_permission')=='denied' and request['scenario']=='care':
            return block('no_action','客户已明确不希望主动收到这类服务消息。','客户主动咨询或重新允许服务联系后')
        service=self.service_scope(context)
        if service:
            context['contact_scope']=service
            # Service consent never opens the whole catalog for an upsell.
            context['products']=[p for p in context.get('products',[]) if p['id'] in service['product_ids']]
            if 'allowed_evidence' in context:
                context['allowed_evidence']=[e for e in context['allowed_evidence'] if not e.startswith('product:') or e[8:] in service['product_ids']]
        if memories.get('contact_permission')=='no_marketing' and not service:
            return block('no_action','客户当前暂停或拒绝营销；没有可据此联系的已授权服务进展。','客户主动咨询、明确重新允许相关联系，或出现获准通知的订单/售后进展后')
        if memories.get('contact_permission') != 'allowed' and not service:
            return block('needs_info', '缺少主动联系的明确依据，请核实客户意愿。', '确认客户愿意接收这类联系后')
        if 'contact_after' in memories:
            try:
                if datetime.fromisoformat(request['as_of']) < datetime.fromisoformat(instant(memories['contact_after'])):
                    return block('defer', '尚未到客户约定的联系时间。', memories['contact_after'])
            except ValueError:
                return block('needs_info', '约定联系时间格式不明确，请核实。', '补全带时区的联系时间后')
        if service:
            return None  # New, explicitly authorized service progress is not a marketing follow-up.
        outreaches = [e for e in context['events'] if e['kind'] == 'message' and e['actor'] == 'advisor' and e['data'].get('outbound')]
        last_inbound = next((e for e in reversed(messages) if e['actor'] == 'client'), None)
        order=lambda e:(e['occurred_at'],e.get('recorded_at',''),e['id'])
        unanswered = [e for e in outreaches if not last_inbound or order(e)>order(last_inbound)]
        if len(unanswered) >= self.brand['contact_defaults']['unanswered_outreach_limit']:
            return block('no_action', '连续主动联系尚未得到回复，暂不追加消息。', '客户回应或出现客户明确要求通知的新事件后')
        if outreaches:
            eligible = datetime.fromisoformat(outreaches[-1]['occurred_at']) + timedelta(days=self.brand['contact_defaults']['outbound_cooldown_days'])
            if datetime.fromisoformat(request['as_of']) < eligible:
                return block('defer', '近期已主动联系，先给客户留出回应空间。', eligible.isoformat())
        return None

    @staticmethod
    def service_scope(context):
        request=context['request']
        if request['scenario'] not in ('care','auto'):
            return None
        permission=next((m for m in context['memories'] if m['key']=='service_permission' and m['value']=='allowed'),None)
        if not permission or permission['applicability']=='所有情境':
            return None
        order=lambda e:(e['occurred_at'],e.get('recorded_at',''),e['id'])
        services=[e for e in context['events'] if e['kind'] in ('purchase','service')
                  and not e.get('redacted_for_generation') and e.get('data',{}).get('product_id')]
        if request['product_ids']:
            services=[e for e in services if e['data']['product_id'] in request['product_ids']]
        if not services:
            return None
        # Latest recorded state per product; whether it was already communicated
        # is a contextual judgement, not inferred from any unrelated advisor reply.
        services=list({e['data']['product_id']:e for e in sorted(services,key=order)}.values())
        return {'mode':'service_only','permission_id':permission['id'],'applies_to':permission['applicability'],
                'event_ids':[e['id'] for e in services],'product_ids':sorted({e['data']['product_id'] for e in services})}

    @staticmethod
    def checks(decision, context):
        if decision.action != 'contact':
            if decision.candidates or decision.recommended_id is not None:
                return ['不联系时不得包含可发送候选。']
            if not decision.revisit_when:
                return ['暂不联系时需说明复查条件。']
            return []
        issues = []
        if context.get('contact_scope',{}).get('mode')=='service_only':
            if decision.scenario!='care':
                issues.append('仅获准联系当前订单/售后进展，不能变成其他场景或营销。')
            expected={'event:'+e for e in context['contact_scope']['event_ids']}
            if any(not (set(c.evidence)&expected) for c in decision.candidates):
                issues.append('每条服务候选应引用本次获准通知的订单或售后记录。')
        if [c.id for c in decision.candidates] != ['a','b','c']:
            issues.append('联系时必须恰好三个候选，id依次a/b/c。')
        if decision.recommended_id not in [c.id for c in decision.candidates]:
            issues.append('缺少有效的推荐项。')
        for candidate in decision.candidates:
            if not any('\u4e00' <= c <= '\u9fff' for c in candidate.text):
                issues.append(f'{candidate.id}没有中文正文。')
            if set(candidate.evidence) - set(context['allowed_evidence']):
                issues.append(f'{candidate.id}引用了不存在的证据编号。')
            # Without an advisor confirmation, catch unsupported promises locally.
            # A cited confirmation still goes through the semantic review below,
            # which checks that each claim is actually supported by that fact.
            confirmed = (bool(context.get('revision_request',{}).get('confirmed_facts')) and 'revision:confirmed_facts' in candidate.evidence) or (bool(context.get('advisor_note',{}).get('confirmed_facts')) and 'advisor_note:confirmed_facts' in candidate.evidence)
            for match in re.finditer(r'([^，。！？；\n]+)([，。！？；\n]|$)',candidate.text):
                clause,ending=match.groups()
                stock=re.search(r'现货|有货|有库存|库存充足',clause)
                uncertain=re.search(r'是否|有没有|能否|如果|假如|的话|不确定|不能|无法|不承诺|尚未|还未|未确认|没货|没有|无现货|无货|不是现货|待(确认|核实|核对)|(?:需|还要|仍要).{0,4}(确认|核实|核对|另查|查询|查证)|帮.{0,6}(确认|核实|询问)|再.{0,4}(确认|核实|核对|问)',clause)
                planned_check=re.search(r'(现在|先|稍后|马上|尽快)(联系|向|找).{0,8}(确认|核实|核对)',clause)
                if planned_check and not re.search(r'(已经|已确认|确认了|有货了|确认有货)',clause):
                    uncertain=True
                if re.search(r'(别|不要|不能)按.{0,4}(有货|现货).{0,4}(安排|计划|出发)',clause):
                    uncertain=True
                reserved=re.search(r'(已|已经).{0,8}(预留|留位|留好)|(预留|留位).{0,5}(好了|成功)',clause)
                if (stock or reserved) and not uncertain and ending not in ('？','?') and not confirmed:
                    issues.append(f'{candidate.id}出现未经记录支持的库存或预留肯定表述：“{clause}”。本应用没有权威库存或预留记录，只能说明目录配色或等待确认。')
                    break
        return issues

    @staticmethod
    def ablate(context, variant):
        context = json.loads(encode(context))
        if variant in ('basic', 'no_brand'):
            # Retain hard business policy; remove narrative identity only.
            context['brand'] = {'name':context['brand']['name'], 'policies':context['brand']['policies']}
            for p in context['products']:
                factual={'id','name','collection','price_cny','colors','material','dimensions_cm','weight_g',
                         'capacity','carry','care','limits','use_guide','fictional','catalog_version'}
                for key in list(p):
                    if key not in factual:p.pop(key)
        if variant in ('basic', 'no_memory'):
            context['memories'] = []
            context['customer_preferences'] = []
            context['customer_reply_history'] = []
            messages = [e for e in context['events'] if e['kind']=='message']
            keep={e['id'] for e in messages[-1:]} | set(context.get('contact_scope',{}).get('event_ids',[]))
            context['events'] = [e for e in context['events'] if e['id'] in keep]
            context.pop('history_selection',None)
            context.pop('memory_selection',None)
        if variant in ('basic','no_preferences'):
            context['advisor_preferences'] = []
            context['customer_preferences'] = []
            context['customer_reply_history'] = []
            context.pop('context_selection',None)
        present = {'event:'+e['id'] for e in context['events'] if not e.get('redacted_for_generation')}
        present |= {'memory:'+m['id'] for m in context['memories']}
        present |= {'preference:'+m['id'] for m in context['advisor_preferences']}
        present |= {'customer_preference:'+m['id'] for m in context.get('customer_preferences',[])}
        present |= {'adopted_reply:'+r['id'] for r in context.get('customer_reply_history',[])}
        context['allowed_evidence'] = [e for e in context['allowed_evidence'] if not e.startswith(('event:','memory:','preference:','customer_preference:','adopted_reply:')) or e in present]
        if variant in ('basic','no_brand'):
            context['allowed_evidence'] = [e for e in context['allowed_evidence'] if not e.startswith('brand:')]
        return context

    def compact_history(self, context):
        """Gate sees the full timeline; generation gets recent + relevant history.

        Operational controls and authorized service events remain pinned.
        Ordinary notes use recent + relevant retrieval when the collection grows.
        """
        context['memories'],context['memory_selection']=select_customer_facts(self.retriever,context)
        memory_ids={m['id'] for m in context['memories']}
        context['allowed_evidence']=[e for e in context['allowed_evidence']
            if not e.startswith('memory:') or e[7:] in memory_ids]
        events=context['events']
        if len(events)<=32:return context
        messages=[e for e in events if e['kind']=='message']
        keep={e['id'] for e in messages[-12:]}
        keep.update(e['id'] for e in events[-8:])
        keep.update(m['source_event_id'] for m in context['memories'] if m.get('source_event_id'))
        keep.update(context.get('contact_scope',{}).get('event_ids',[]))
        older=[{'id':e['id'],'revision':1,'applicability':'本客户历史',
                'value':e['text']} for e in events if e['id'] not in keep and not e.get('redacted_for_generation')]
        chosen,trace=self.retriever.select(context,older)
        keep.update(m['id'] for m in chosen)
        context['events']=[e for e in events if e['id'] in keep]
        context['allowed_evidence']=[e for e in context['allowed_evidence'] if not e.startswith('event:') or e[6:] in keep]
        context['history_selection']={'total':len(events),'included':len(context['events']),
                                      'retrieved_ids':[m['id'] for m in chosen],'method':trace['method']}
        return context

    def generate(self, request: DraftRequest):
        started = time.monotonic()
        if request.rewrite_id:
            with self.store.connect() as db:
                row=db.execute('SELECT parent_draft_id FROM rewrite_requests WHERE id=?',(request.rewrite_id,)).fetchone()
            if not row:raise ValueError('重写意见不存在。')
            if not self.is_current(self.store.draft(row[0]),request.as_of,ignore_preferences=True,allow_clock_progress=True):
                raise ValueError('排队期间客户资料已变化，请重新起草；原修改意见已保存。')
        context = self.context(request)
        basic_prompt=self.prompt_dir/'basic.txt'
        gen_prompt = (basic_prompt if request.variant=='basic' and basic_prompt.exists() else self.prompt_dir/'generate.txt').read_text()
        review_prompt = (self.prompt_dir/'review.txt').read_text()
        paths = list((ROOT/'atelier').glob('*.py')) + list((ROOT/'atelier/migrations').glob('*.sql')) + list(self.prompt_dir.glob('*.txt')) + list(self.data_dir.glob('*.json')) + [ROOT/'lanxu/provider.py', ROOT/'lanxu/config.py']
        hashes = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
        record = {'id':uuid.uuid4().hex, 'customer_id':request.customer_id, 'created_at':now(),
                  'request':request.model_dump(mode='json'), 'status':'running', 'attempts':[],
                  'versions':{'variant':request.variant, 'files':hashes}, 'context':context, 'result':None, 'issues':[]}
        decision = self.gate(context)
        if decision:
            record.update(status=decision.action, result=decision.model_dump(), decision_source='contact_rules')
            return self._finish(record, started)
        context = self.ablate(self.compact_history(context), request.variant)
        record['context'] = context
        correction = None
        for attempt in (1,2):
            payload = context if correction is None else context | {'repair':correction}
            generation_system=gen_prompt
            if correction is not None:
                generation_system+='\n根据原始context核对并修复repair.issues中的实际问题，输出完整JSON。repair.previous是待修稿，不是事实来源。\n'
            entry = {'attempt':attempt, 'stage':'generation', 'system':generation_system, 'user':encode(payload)}
            record['attempts'].append(entry)
            try:
                reply = self.generator.complete(generation_system, entry['user'])
                entry.update(raw=reply.text, metadata=reply.metadata)
                decision = Decision.model_validate_json(reply.text)
                issues = self.checks(decision, context)
            except (ValidationError, ValueError) as exc:
                issues = ['输出不符合JSON结构或业务约定。']
                decision = None
            except ProviderError as exc:
                entry['error'] = {'code':exc.code, 'message':str(exc)}
                record.update(status='failed', issues=[str(exc)])
                return self._finish(record, started)
            entry['checks'] = issues
            if issues:
                correction = {'issues':issues, 'previous':entry.get('raw')}
                continue
            # Retry a malformed review once against the SAME candidates, without
            # coercing an invalid verdict to pass or generating a different answer.
            for review_try in (1,2):
                system=review_prompt
                if review_try==2:
                    system+='\n上次审查输出格式无效。重新审查同一资料和候选，返回符合约定的JSON；issues最多8项，revise须列出实际问题，pass时为空数组。\n'
                review_entry = {'attempt':attempt, 'stage':'review' if review_try==1 else 'review_format_retry', 'system':system,
                                'user':encode({'context':context,'decision':decision.model_dump()})}
                record['attempts'].append(review_entry)
                try:
                    reply = self.reviewer.complete(system, review_entry['user'])
                    review_entry.update(raw=reply.text, metadata=reply.metadata)
                    review = QualityReview.model_validate_json(reply.text)
                    if review.verdict == 'revise' and not review.issues:
                        raise ValueError('审查未说明具体问题')
                    break
                except (ValidationError, ValueError):
                    review_entry['checks']=['审查JSON结构或结论约定无效。']
                    if review_try==1:continue
                    record.update(status='failed', issues=['审查结果两次结构无效，请重试；没有将未审查结果冒充成功。'])
                    return self._finish(record, started)
                except ProviderError as exc:
                    review_entry['error'] = {'code':exc.code, 'message':str(exc), 'failures':getattr(exc,'failures',[])}
                    record.update(status='failed', issues=['质量审查未完成：'+str(exc)])
                    return self._finish(record, started)
            if review.verdict == 'pass':
                record.update(status=decision.action, result=decision.model_dump(), decision_source='model_with_review')
                return self._finish(record, started)
            correction = {'issues':review.issues, 'previous':decision.model_dump()}
        record.update(status='failed', issues=['达到一次修复上限，需顾问检查。', *(correction or {}).get('issues',[])])
        return self._finish(record, started)

    def is_current(self, record, as_of=None, ignore_preferences=False, allow_clock_progress=False):
        """A persisted original remains inspectable but must not be reused after context changes."""
        request = DraftRequest.model_validate(record['request'])
        original_time=request.as_of
        if as_of and request.as_of != instant(as_of):
            from datetime import datetime
            from zoneinfo import ZoneInfo
            day=lambda value:datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai')).date()
            if not allow_clock_progress or instant(as_of)<original_time or day(original_time)!=day(instant(as_of)):
                return False
            request=request.model_copy(update={'as_of':instant(as_of)})
        context = self.context(request)
        gate=self.gate(context)  # Reconstruct any purpose-limited context exactly as at generation.
        if gate and (record.get('result') or {}).get('action')=='contact':return False
        if record.get('decision_source') != 'contact_rules':
            context = self.ablate(self.compact_history(context), request.variant)
        original = json.loads(encode(record['context']))
        original.setdefault('pending_memory_updates', [])
        original.setdefault('customer_preferences', [])
        original['request'].setdefault('rewrite_id',None)
        original['request'].setdefault('advisor_note_id',None)
        if allow_clock_progress:
            context['as_of']=original['as_of']
            context['request']['as_of']=original['request']['as_of']
        for name in ('memories','advisor_preferences'):
            for m in original[name]:m.setdefault('source_quote',None)
        if ignore_preferences:
            for value in (context, original):
                value['advisor_preferences'] = []
                value['customer_preferences'] = []
                value['allowed_evidence'] = [e for e in value['allowed_evidence'] if not e.startswith(('preference:','customer_preference:'))]
        # Diagnostics do not change the meaning of a draft. Extra available
        # products do not revoke unchanged facts in an older saved response.
        for value in (context,original):
            value.pop('context_selection',None)
            value.pop('history_selection',None)
            value.pop('memory_selection',None)
            # Adopting a draft must not make that draft stale by adding itself
            # to reference history. Current factual checks still apply.
            value.pop('customer_reply_history',None)
            value['allowed_evidence']=[e for e in value['allowed_evidence'] if not e.startswith('adopted_reply:')]
        if not request.product_ids:
            original_ids={p['id'] for p in original.get('products',[])}
            context['products']=[p for p in context['products'] if p['id'] in original_ids]
            context['allowed_evidence']=[e for e in context['allowed_evidence'] if not e.startswith('product:') or e[8:] in original_ids]
        # Additional reference events do not invalidate an unchanged old draft.
        # Changes to an event that was supplied still invalidate its facts.
        original.setdefault('boutique_events', [original['boutique_event']] if original.get('boutique_event') else [])
        event_ids={e['id'] for e in original['boutique_events']}
        context['boutique_events']=[e for e in context['boutique_events'] if e['id'] in event_ids]
        context['allowed_evidence']=[e for e in context['allowed_evidence'] if not e.startswith('boutique:') or e[9:] in event_ids]
        return encode(context) == encode(original)

    def _finish(self, record, started):
        record['latency_ms'] = round((time.monotonic()-started)*1000)
        record['finished_at'] = now()
        self.store.save_draft(record)
        return record
