"""A wall-clock limit for HTTP providers that can otherwise keep sockets alive forever.

The worker uses the existing credential loader and sanitized provider errors. Input
travels over stdin, not argv; credentials and provider stderr never enter artifacts.
"""
import json
import subprocess
import sys
import time
from pathlib import Path
from lanxu.provider import ModelReply, ProviderError


def run_bounded(command, payload, timeout, cwd=None):
    try:
        result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=cwd, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the worker; no abandoned HTTP threads remain.
        raise ProviderError('total_timeout', '模型服务超过总等待时限，本次调用已结束。', True) from None
    except OSError:
        raise ProviderError('provider_worker_launch', '无法启动模型请求进程。') from None
    if result.returncode:
        raise ProviderError('provider_worker_failed', '模型请求进程未正常完成。', True)
    try:
        body = json.loads(result.stdout)
        if body.get('error'):
            error = body['error']
            raise ProviderError(error['code'], error['message'], error.get('retryable', False))
        if not isinstance(body['text'], str) or not isinstance(body['metadata'], dict):
            raise ValueError('invalid worker envelope')
        return ModelReply(body['text'], body['metadata'])
    except (ValueError, KeyError, TypeError):
        raise ProviderError('provider_worker_response', '模型请求进程返回无效结果。', True) from None


class DeadlineDeepSeek:
    provider = 'deepseek'

    def __init__(self, primary, deadline=35):
        self.model = primary.model
        self.deadline = deadline

    def complete(self, system, user):
        started = time.monotonic()
        reply = run_bounded([sys.executable, '-m', 'atelier.deadline_provider'],
            {'model':self.model, 'system':system, 'user':user}, self.deadline,
            cwd=Path(__file__).resolve().parents[1])
        reply.metadata |= {'total_deadline_seconds':self.deadline,
                           'wall_latency_ms':round((time.monotonic()-started)*1000)}
        return reply


def worker():
    from lanxu.provider import DeepSeek
    try:
        request = json.loads(sys.stdin.read())
        reply = DeepSeek(request['model']).complete(request['system'],request['user'])
        response = {'text':reply.text, 'metadata':reply.metadata}
    except ProviderError as exc:
        response = {'error':{'code':exc.code, 'message':str(exc), 'retryable':exc.retryable}}
    except Exception:
        response = {'error':{'code':'provider_worker_failed', 'message':'模型请求进程异常。', 'retryable':True}}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))


if __name__ == '__main__':
    worker()
