"""Lossless paste preview, explicit confirmation, atomic and idempotent imports.

Splitting is deterministic. Unknown speakers and missing timestamps are surfaced;
no model invents turns or rewrites quotations while moving source into the CRM.
"""
import json
import re
from datetime import datetime, timezone, timedelta
from pydantic import Field, field_validator
from typing import Literal
from .models import Strict, Event, instant
from .store import Conflict, encode, now


class ImportPreview(Strict):
    text: str=Field(min_length=1,max_length=262144)
    as_of: str
    format: Literal['text','chatlab']='text'

    @field_validator('as_of')
    @classmethod
    def valid_time(cls,value):return instant(value)


class ImportTurn(Strict):
    actor: Literal['client','advisor']
    text: str=Field(min_length=1,max_length=6000)
    occurred_at: str
    time_confirmed: bool=False

    @field_validator('occurred_at')
    @classmethod
    def valid_time(cls,value):return instant(value)


class ImportConfirm(ImportPreview):
    id: str=Field(pattern=r'^[a-zA-Z0-9_-]{1,50}$')
    turns: list[ImportTurn]=Field(min_length=1,max_length=60)


DATE=r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?'
DATE_LINE=re.compile(r'^\[?('+DATE+r')\]?$')
TIME_PREFIX=re.compile(r'^\[?('+DATE+r')\]?\s*(.*)$')
NAME_TIME=re.compile(r'^([^\n:：]{1,30})\s+('+DATE+r')$')


def parse_date(text):
    return datetime.fromisoformat(text.replace('/','-')).replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat()


def preview(customer, data: ImportPreview):
    if data.format=='chatlab':return preview_chatlab(data)
    aliases={'客户':'client','顾客':'client','顾问':'advisor','销售':'advisor','我':'advisor'}
    for name in (customer['name'],customer['salutation']):
        if name:aliases[name]='client'
    turns=[];current=None;pending_time=None;pending_source=[]
    def finish():
        nonlocal current
        if current and current['text'].strip():
            current['text']=current['text'].strip();turns.append(current)
        elif current:
            pending_source.extend(current['source_lines'])
        current=None
    for line in data.text.splitlines():
        stripped=line.strip()
        if not stripped:
            if current:current['text']+='\n';current['source_lines'].append(line)
            continue
        date_only=DATE_LINE.fullmatch(stripped)
        if date_only:
            finish();pending_time=parse_date(date_only[1]);pending_source.append(line);continue
        named_time=NAME_TIME.fullmatch(stripped)
        line_time=None;body=stripped;header=None
        if named_time:
            header=named_time[1].strip();line_time=parse_date(named_time[2]);body=''
        else:
            timed=TIME_PREFIX.fullmatch(stripped)
            if timed:line_time=parse_date(timed[1]);body=timed[2]
            label=re.match(r'^([^:：\n]{1,30})[：:]\s*(.*)$',body)
            if label and label[1].strip().lower() not in ('https','http'):header=label[1].strip();body=label[2]
            elif body in aliases:header=body;body=''
        if header is not None:
            finish();stamp=line_time or pending_time
            current={'actor':aliases.get(header,'unknown'),'speaker':header,'text':body,
                'occurred_at':stamp or data.as_of,'time_confirmed':bool(stamp),'source_lines':pending_source+[line]}
            pending_source=[]
        elif current is None or line_time:
            finish();stamp=line_time or pending_time
            current={'actor':'unknown','speaker':'未识别','text':body,'occurred_at':stamp or data.as_of,
                'time_confirmed':bool(stamp),'source_lines':pending_source+[line]};pending_source=[]
        else:
            current['text']+='\n'+line;current['source_lines'].append(line)
    finish()
    # A trailing header must remain visible rather than disappearing silently.
    if pending_source:turns.append({'actor':'unknown','speaker':'待核对','text':'\n'.join(pending_source),
        'occurred_at':pending_time or data.as_of,'time_confirmed':bool(pending_time),'source_lines':pending_source})
    if not turns:raise ValueError('没有可导入的消息。')
    if len(turns)>60:raise ValueError('一次最多导入60条消息，请分段粘贴。')
    if any(len(t['text'])>6000 for t in turns):raise ValueError('单条消息超过6000字，请分段或补上说话人标记。')
    return {'turns':turns,'missing_times':sum(not t['time_confirmed'] for t in turns),
        'unknown_speakers':sum(t['actor']=='unknown' for t in turns)}


def preview_chatlab(data):
    """Read exported private-chat text. Sender identity is confirmed by the advisor.

    Native databases and arbitrary JSON are deliberately not guessed. The original
    export stays in raw_text; non-text omissions are counted in the preview.
    """
    try:raw=json.loads(data.text.lstrip('\ufeff'))
    except (ValueError, RecursionError):raise ValueError('JSON文件无法读取，请重新导出。') from None
    if not isinstance(raw,dict) or not isinstance(raw.get('chatlab'),dict):
        raise ValueError('请选择 ChatLab 格式的 JSON，或导出为 TXT。')
    if raw['chatlab'].get('version') not in ('0.0.1','0.0.2'):
        raise ValueError('暂不支持这个 ChatLab 版本，请导出为 TXT。')
    if not isinstance(raw.get('meta'),dict) or raw['meta'].get('type')!='private':
        raise ValueError('请选择与这位客户的单聊记录，暂不导入群聊。')
    messages=raw.get('messages');members=raw.get('members',[])
    if not isinstance(messages,list) or not isinstance(members,list):
        raise ValueError('文件缺少有效的消息或成员列表，请重新导出。')
    names={m['platformId']:m.get('accountName') or m['platformId'] for m in members
           if isinstance(m,dict) and isinstance(m.get('platformId'),str)
           and isinstance(m.get('accountName',''),str)}
    turns=[];skipped=0;senders=set()
    for i,message in enumerate(messages,1):
        if not isinstance(message,dict) or type(message.get('type')) is not int:
            raise ValueError(f'第{i}条消息格式不完整，请重新导出。')
        if message['type']!=0:skipped+=1;continue
        sender=message.get('sender');body=message.get('content');stamp=message.get('timestamp')
        if not isinstance(sender,str) or not sender or not isinstance(body,str) or not body.strip():
            raise ValueError(f'第{i}条文字消息缺少发送者或内容，请核对导出文件。')
        if type(stamp) not in (int,float) or not 0<=stamp<253402300800:
            raise ValueError(f'第{i}条消息时间无效，ChatLab 时间应为 Unix 秒数。')
        try:occurred_at=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        except (ValueError,OverflowError,OSError):raise ValueError(f'第{i}条消息时间无法读取。') from None
        if len(body)>6000:raise ValueError(f'第{i}条消息超过6000字，请分段导入。')
        senders.add(sender)
        name=message.get('accountName') or names.get(sender) or sender
        if not isinstance(name,str):raise ValueError(f'第{i}条消息的发送者名称无效。')
        turns.append({'actor':'unknown','speaker':name,'speaker_id':sender,'text':body,
                      'occurred_at':occurred_at,'time_confirmed':True})
    if len(senders)>2:raise ValueError('记录包含两人以上的文字消息，请选择与客户的单聊。')
    if not turns:raise ValueError('文件中没有可导入的文字消息。')
    if len(turns)>60:raise ValueError('一次最多导入60条文字消息，请在导出时缩小日期范围。')
    turns.sort(key=lambda t:t['occurred_at'])
    return {'turns':turns,'missing_times':0,'unknown_speakers':len(turns),'skipped_messages':skipped}


def confirm(store,customer_id,data: ImportConfirm):
    store.customer(customer_id)
    stamps=[t.occurred_at for t in data.turns]
    if stamps!=sorted(stamps):raise ValueError('消息需按时间先后排列，请核对时间。')
    if any(t>data.as_of for t in stamps):raise ValueError('消息时间晚于当前情境，请修改时间或情境日期。')
    payload=data.model_dump()
    if data.format=='text':payload.pop('format')  # Keep retries of pre-file imports compatible.
    content=encode(payload)
    events=[Event(id='import_'+data.id+'_'+str(i).zfill(3),customer_id=customer_id,kind='message',
        actor=t.actor,text=t.text,occurred_at=t.occurred_at,
        data={'import_id':data.id,'import_position':i,'time_confirmed':t.time_confirmed,
              'requires_memory_update':t.actor=='client'}) for i,t in enumerate(data.turns)]
    event_ids=[e.id for e in events]
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM conversation_imports WHERE id=?',(data.id,)).fetchone()
        if old:
            if old['customer_id']!=customer_id or old['confirmed_json']!=content:raise Conflict('导入编号已有不同内容，请重新预览。')
            return {'event_ids':json.loads(old['event_ids']),'reused':True}
        recorded=now()
        for e in events:
            values=e.model_dump() | {'data':encode(e.data),'recorded_at':recorded}
            db.execute('INSERT INTO events VALUES(:id,:customer_id,:occurred_at,:kind,:actor,:text,:data,:recorded_at)',values)
        db.execute('INSERT INTO conversation_imports VALUES(?,?,?,?,?,?)',
            (data.id,customer_id,data.text,content,encode(event_ids),recorded))
    return {'event_ids':event_ids,'reused':False}
