import json
import unittest
from atelier.providers import FallbackProvider
from lanxu.provider import ModelReply,ProviderError

class Channel:
    def __init__(self,name,reply):self.provider=name;self.model=name;self.reply=reply;self.calls=0
    def complete(self,system,user):
        self.calls+=1
        if isinstance(self.reply,Exception):raise self.reply
        return ModelReply(json.dumps(self.reply),{'provider':self.provider,'model':self.model})

class FallbackTests(unittest.TestCase):
    def test_temporary_failure_uses_one_fallback_and_preserves_provenance(self):
        primary=Channel('primary',ProviderError('http_500','模型服务返回HTTP500',True))
        backup=Channel('backup',{'ok':True})
        reply=FallbackProvider(primary,backup).complete('rules','fictional input')
        self.assertTrue(reply.metadata['fallback_used'])
        self.assertEqual(reply.metadata['provider'],'backup')
        self.assertEqual(reply.metadata['primary_failure']['code'],'http_500')
        self.assertEqual((primary.calls,backup.calls),(1,1))
    def test_configuration_error_is_not_silently_hidden(self):
        primary=Channel('primary',ProviderError('missing_key','缺少配置'))
        backup=Channel('backup',{'ok':True})
        with self.assertRaises(ProviderError):FallbackProvider(primary,backup).complete('rules','input')
        self.assertEqual(backup.calls,0)
    def test_both_failures_are_retained_no_unbounded_retry(self):
        primary=Channel('primary',ProviderError('timeout','超时',True))
        backup=Channel('backup',ProviderError('network','连接失败',True))
        with self.assertRaises(ProviderError) as e:FallbackProvider(primary,backup).complete('rules','input')
        self.assertEqual(e.exception.code,'both_providers_failed')
        self.assertEqual(len(e.exception.failures),2)
        self.assertEqual((primary.calls,backup.calls),(1,1))
