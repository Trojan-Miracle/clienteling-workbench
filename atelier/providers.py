"""Bounded provider fallback for transient review/extraction outages, with explicit provenance."""
from lanxu.provider import make_provider, ProviderError
from .deadline_provider import DeadlineDeepSeek


class FallbackProvider:
    def __init__(self, primary, fallback):
        self.primary=primary;self.fallback=fallback
        self.provider=primary.provider;self.model=primary.model

    def complete(self, system, user):
        try:
            return self.primary.complete(system,user)
        except ProviderError as first:
            if not first.retryable:
                raise
            failure={'provider':self.primary.provider,'requested_model':self.primary.model,
                     'code':first.code,'message':str(first)}
            try:
                reply=self.fallback.complete(system,user)
            except ProviderError as second:
                error=ProviderError('both_providers_failed','首选模型服务暂不可用，备用通道也未完成本次处理。',True)
                error.failures=[failure,{'provider':self.fallback.provider,'requested_model':self.fallback.model,'code':second.code,'message':str(second)}]
                raise error from None
            reply.metadata=reply.metadata | {'fallback_used':True,'primary_failure':failure}
            return reply


def review_provider():
    primary=bounded(make_provider(review=True))
    fallback=generation_provider()
    if (primary.provider,primary.model)==(fallback.provider,fallback.model):return primary
    return FallbackProvider(primary,fallback)


def bounded(provider):
    return DeadlineDeepSeek(provider) if provider.provider=='deepseek' else provider


def generation_provider():
    return bounded(make_provider(review=False))
