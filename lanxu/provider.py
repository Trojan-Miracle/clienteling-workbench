"""唯一网络出口；不将请求头、密钥或供应商原始错误写入运行记录。"""
import json
import os
import time
from dataclasses import dataclass
import httpx
from .config import load_env

class ProviderError(RuntimeError):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable

@dataclass
class ModelReply:
    text: str
    metadata: dict

class DeepSeek:
    provider = 'deepseek'

    def __init__(self, model=None):
        load_env()
        self.model = model or os.getenv('LANXU_MODEL', 'deepseek-flash')
        self.timeout = float(os.getenv('LANXU_TIMEOUT', '90'))

    def complete(self, system, user):
        key = os.getenv('DEEPSEEK_API_KEY', '')
        if not key:
            raise ProviderError('missing_key', '尚未配置 DEEPSEEK_API_KEY。请在本地 .env 配置后重启。')
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=15), follow_redirects=False) as client:
                response = client.post('https://api.deepseek.com/chat/completions',
                    headers={'Authorization': f'Bearer {key}'},
                    json={'model': self.model, 'messages': [
                        {'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                        'response_format': {'type': 'json_object'}, 'max_tokens': 3000,
                        'thinking': {'type': 'disabled'}, 'temperature': 0.65})
        except httpx.TimeoutException:
            raise ProviderError('timeout', '模型请求超时，请稍后重试。', True) from None
        except httpx.HTTPError:
            raise ProviderError('network', '无法连接模型服务，请检查网络后重试。', True) from None
        if response.status_code != 200:
            known = {401: '模型服务鉴权失败，请检查本地密钥。', 402: '模型账户余额不足。',
                     429: '模型服务限流，请稍后重试。'}
            raise ProviderError(f'http_{response.status_code}', known.get(response.status_code,
                f'模型服务返回 HTTP {response.status_code}。'), response.status_code in [429, 500, 502, 503])
        try:
            body = response.json()
            choice = body['choices'][0]
            content = choice['message']['content']
            metadata = {'provider': self.provider, 'requested_model': self.model,
                'model': body.get('model'), 'response_id': body.get('id'),
                'usage': body.get('usage', {}), 'finish_reason': choice.get('finish_reason'),
                'latency_ms': round((time.monotonic() - started)*1000),
                'system_fingerprint': body.get('system_fingerprint')}
            if not isinstance(content, str) or not content.strip():
                raise ProviderError('empty_output', '模型返回了空内容。', True)
            if choice.get('finish_reason') != 'stop':
                raise ProviderError('incomplete_output', '模型输出未完整结束，请重试。', True)
            return ModelReply(content, metadata)
        except (KeyError, IndexError, ValueError, TypeError):
            raise ProviderError('invalid_response', '模型响应结构异常。', True) from None

class CodexText:
    """将已登录的Codex CLI用作纯文本模型通道，不委派开发任务，不开放工具。"""
    provider = 'codex_cli'

    def __init__(self, model=None, schema=None):
        import shutil
        self.model = model or os.getenv('LANXU_CODEX_MODEL', 'gpt-6-astra')
        self.schema = schema
        self.executable = os.getenv('LANXU_CODEX_BIN') or shutil.which('codex')
        self.timeout = float(os.getenv('LANXU_CODEX_TIMEOUT', '240'))

    def complete(self, system, user):
        import subprocess
        import tempfile
        from pathlib import Path
        if not self.executable:
            raise ProviderError('missing_codex','未找到 Codex CLI，请安装并登录或选择DeepSeek通道。')
        started=time.monotonic()
        with tempfile.TemporaryDirectory(prefix='lanxu-text-') as folder:
            output=Path(folder)/'response.json'
            command=[self.executable,'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
                     '--sandbox','read-only','--model',self.model,
                     '-c','model_reasoning_effort="high"','-c','features.shell_tool=false',
                     '-c','features.multi_agent=false','-c','web_search="disabled"',
                     '--json','--output-last-message',str(output),'-']
            if self.schema is not None:
                schema_file=Path(folder)/'schema.json'
                schema_file.write_text(json.dumps(self.schema))
                command[-1:-1]=['--output-schema',str(schema_file)]
            env={k:v for k,v in os.environ.items() if k not in ['DEEPSEEK_API_KEY','ANTHROPIC_API_KEY']}
            prompt='这是应用的纯文本JSON推理请求。禁止读取文件、执行命令、联网、调用工具或创建其他代理。直接按下面规则分析给定资料并返回JSON。\n\n规则：\n'+system+'\n\n输入资料：\n'+user
            try:
                proc=subprocess.run(command,input=prompt,text=True,capture_output=True,cwd=folder,env=env,timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise ProviderError('codex_timeout','Codex模型请求超时。',True) from None
            except OSError:
                raise ProviderError('codex_launch','无法启动Codex CLI。') from None
            usage={};tool_activity=False
            for line in proc.stdout.splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if event.get('type')=='turn.completed':usage=event.get('usage',{})
                if event.get('type')=='item.completed' and event.get('item',{}).get('type') not in ['agent_message','reasoning']:
                    tool_activity=True
            if tool_activity:
                raise ProviderError('unexpected_tool','纯文本模型请求出现非预期工具活动，结果未采用。')
            if proc.returncode or not output.exists():
                raise ProviderError('codex_failed','Codex调用失败，请检查登录状态和模型访问权限。',True)
            content=output.read_text().strip()
            if not content:raise ProviderError('empty_output','Codex返回空内容。',True)
            return ModelReply(content,{'provider':self.provider,'requested_model':self.model,
                'model':self.model,'model_identity_source':'CLI requested model; no server model ID returned',
                'usage':usage,'latency_ms':round((time.monotonic()-started)*1000),'finish_reason':'completed',
                'reasoning_effort':'high'})


def make_provider(review=False):
    prefix='LANXU_REVIEW_' if review else 'LANXU_'
    provider=os.getenv(prefix+'PROVIDER',os.getenv('LANXU_PROVIDER','deepseek'))
    if provider=='codex':
        return CodexText(os.getenv(prefix+'MODEL',os.getenv('LANXU_CODEX_MODEL','gpt-6-astra')))
    if provider!='deepseek':raise ProviderError('unknown_provider','不支持的模型通道配置。')
    return DeepSeek(os.getenv(prefix+'MODEL',os.getenv('LANXU_MODEL','deepseek-flash')))
