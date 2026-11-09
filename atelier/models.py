"""Runtime contracts: factual memory is separate from advisor preferences."""
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


def instant(value: str) -> str:
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('时间必须带时区，例如 2026-11-07T09:00:00+08:00')
    return dt.astimezone(timezone.utc).isoformat()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Customer(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    name: str = Field(min_length=1, max_length=60)
    salutation: str = Field(default='', max_length=60)
    summary: str = Field(default='', max_length=500)
    color: str = Field(default='forest', pattern=r'^(forest|sage|stone|slate|clay)$')


class Event(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    customer_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    occurred_at: str
    kind: Literal['message', 'purchase', 'service', 'appointment', 'contact', 'note']
    actor: Literal['client', 'advisor', 'system']
    text: str = Field(min_length=1, max_length=6000)
    data: dict = Field(default_factory=dict)

    @field_validator('occurred_at')
    @classmethod
    def valid_time(cls, value):
        return instant(value)


class MemoryInput(Strict):
    scope: Literal['customer', 'advisor']
    owner_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    key: str = Field(pattern=r'^[a-zA-Z0-9_.-]{1,80}$')
    value: str = Field(min_length=1, max_length=4000)
    provenance: Literal['explicit', 'inferred', 'manual']
    source: str = Field(min_length=1, max_length=200)
    source_event_id: str | None = None
    source_quote: str | None = Field(default=None, min_length=1, max_length=3000)
    effective_at: str
    expires_at: str | None = None
    applicability: str = Field(default='所有情境', min_length=1, max_length=200)

    @field_validator('effective_at', 'expires_at')
    @classmethod
    def valid_time(cls, value):
        return instant(value) if value else value


class MemoryChange(Strict):
    expected_revision: int = Field(ge=1)
    value: str | None = Field(default=None, min_length=1, max_length=4000)
    status: Literal['active', 'disabled', 'deleted'] | None = None
    applicability: str | None = Field(default=None, min_length=1, max_length=200)
    expires_at: str | None = None

    @field_validator('expires_at')
    @classmethod
    def valid_time(cls, value):
        return instant(value) if value else value


class RuleInput(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    text: str = Field(min_length=1,max_length=4000)
    applicability: str = Field(min_length=1,max_length=200)


class CustomerRuleInput(RuleInput):
    applicability: str = Field(default='所有情境',min_length=1,max_length=200)
    as_of: str
    expires_at: str | None = None

    @field_validator('as_of','expires_at')
    @classmethod
    def valid_time(cls,value):
        return instant(value) if value else value


class PreferenceAnswer(Strict):
    question_id: Literal['length', 'greeting', 'invitation']
    answer: str = Field(min_length=1, max_length=500)
    applicability: str = Field(default='所有情境', min_length=1, max_length=200)


class DraftRequest(Strict):
    customer_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    advisor_id: str = Field(default='default', pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    as_of: str
    trigger: Literal['reply', 'manual', 'timer', 'event'] = 'reply'
    scenario: Literal['auto', 'greeting', 'recommendation', 'care', 'reactivation', 'invitation', 'price', 'conversation'] = 'auto'
    product_ids: list[str] = Field(default_factory=list, max_length=6)
    intent: str = Field(default='', max_length=1200)
    occasion: str = Field(default='', max_length=500)
    event_id: str | None = None
    variant: Literal['full', 'basic', 'no_brand', 'no_memory', 'no_preferences'] = 'full'
    rewrite_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    advisor_note_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{1,60}$')

    @field_validator('as_of')
    @classmethod
    def valid_time(cls, value):
        return instant(value)


class Suggestion(Strict):
    id: Literal['a', 'b', 'c']
    label: str = Field(min_length=1, max_length=30)
    # Match the editor/feedback payload capacity; this is not a writing target.
    text: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=300)
    evidence: list[str] = Field(default_factory=list, max_length=10)


class Decision(Strict):
    action: Literal['contact', 'defer', 'no_action', 'needs_info']
    scenario: Literal['greeting', 'recommendation', 'care', 'reactivation', 'invitation', 'price', 'conversation']
    purpose: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    revisit_when: str = Field(default='', max_length=300)
    recommended_id: Literal['a', 'b', 'c'] | None = None
    candidates: list[Suggestion] = Field(default_factory=list, max_length=3)


class QualityReview(Strict):
    verdict: Literal['pass', 'revise']
    issues: list[str] = Field(default_factory=list, max_length=8)


class MemoryProposal(Strict):
    key: str = Field(default='note',pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    value: str = Field(min_length=1, max_length=1200)
    quote: str = Field(min_length=2, max_length=1200)
    replaces_id: str | None = Field(default=None,min_length=1,max_length=80)
    expires_at: str | None = None
    @field_validator('expires_at')
    @classmethod
    def valid_time(cls, value):
        return instant(value) if value else value


class Extraction(Strict):
    memories: list[MemoryProposal] = Field(default_factory=list, max_length=32)
    note: str = Field(default='', max_length=500)


class Assessment(Strict):
    action: Literal['contact','defer','no_action','needs_info']
    scenario: Literal['greeting','recommendation','care','reactivation','invitation','price','conversation']
    purpose: str = Field(min_length=1,max_length=200)
    reason: str = Field(min_length=1,max_length=500)
    revisit_when: str | None = Field(default=None,max_length=300)
    revisit_at: str | None = None
    product_ids: list[str] = Field(default_factory=list,max_length=6)
    @field_validator('revisit_at')
    @classmethod
    def valid_time(cls,value):
        return instant(value) if value else value


class FeedbackInput(Strict):
    draft_id: str = Field(min_length=1,max_length=80)
    mode: Literal['calibration','blind'] = 'calibration'
    outcome: Literal['choice','none','tie','skip','comment','no_contact']
    candidate_id: Literal['a','b','c'] | None = None
    display_order: list[str] = Field(min_length=3,max_length=3)
    reason: str = Field(default='',max_length=500)
    feedback_id: str | None = Field(default=None,pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    selected_text: str | None = Field(default=None,min_length=1,max_length=1000)
    learning_scope: Literal['once','example','rule','customer_rule'] = 'example'
    rule_text: str = Field(default='',max_length=500)
    applicability: str = Field(default='',max_length=200)
    expected_revision: int | None = Field(default=None,ge=1)


class ExampleInput(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    text: str = Field(min_length=1,max_length=2500)
    context: str = Field(default='',max_length=1000)
    applicability: str = Field(default='所有情境',min_length=1,max_length=200)


class RewriteReference(Strict):
    id: Literal['a','b','c']
    text: str | None = Field(default=None,min_length=1,max_length=1000)


class RewriteInput(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    instruction: str = Field(min_length=1,max_length=1200)
    candidate_id: Literal['a','b','c'] | None = None
    edited_text: str | None = Field(default=None,min_length=1,max_length=1000)
    confirmed_facts: str | None = Field(default=None,max_length=2000)
    reference_candidates: list[RewriteReference] | None = Field(default=None,max_length=3)
    as_of: str

    @field_validator('reference_candidates')
    @classmethod
    def unique_references(cls,value):
        if value is not None and len({r.id for r in value})!=len(value):
            raise ValueError('参考回复不能重复。')
        return value

    @field_validator('as_of')
    @classmethod
    def valid_time(cls,value):
        return instant(value)
