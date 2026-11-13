"""Optional paid arbitration. No input actions, retries, plaintext keys or implicit mode changes."""
import hashlib
import json
import re
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from device_identity import WindowsProtector, atomic_write, canonical


class ArbitrationError(Exception):
    pass


class PersonalKeyStore:
    def __init__(self, root, protector=None):
        self.root = Path(root) / 'arbitration-credentials'
        self.protector = protector or WindowsProtector()

    def _path(self, owner):
        if not isinstance(owner, str) or not owner:
            raise ArbitrationError('需要登录账号')
        return self.root / (hashlib.sha256(owner.encode()).hexdigest() + '.dpapi')

    def set(self, owner, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 512 or any(c.isspace() for c in key):
            raise ArbitrationError('密钥格式无效')
        atomic_write(self._path(owner), self.protector.protect(canonical({'owner': owner, 'key': key})))

    def get(self, owner):
        path = self._path(owner)
        if not path.exists():
            return ''
        try:
            if path.stat().st_size > 16384:
                raise ValueError()
            data = json.loads(self.protector.unprotect(path.read_bytes()))
            if data.get('owner') != owner:
                raise ValueError()
            return data['key']
        except Exception:
            raise ArbitrationError('个人密钥无法解密，请重新保存') from None

    def clear(self, owner):
        self._path(owner).unlink(missing_ok=True)

    def adopt_legacy(self, owner):
        key = self.get('legacy-pending')
        if not key:
            raise ArbitrationError('没有待迁移的旧密钥')
        self.set(owner, key)
        self.clear('legacy-pending')


def owner_of(account):
    binding = account.binding() if account else None
    if binding is None or binding.account == 'unbound' or binding.origin == 'local':
        raise ArbitrationError('请先登录网站账号')
    return binding.origin + '/' + binding.account


def sanitize_config(cfg):
    result = dict(cfg)
    result.update(deepseek_api_key='', ai_enabled=False, arbitration_mode='off')
    return result


def migrate_config(path, store):
    """Encrypt first, then replace active config. Old user backup archives are not touched."""
    path = Path(path)
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding='utf-8'))
    key = str(data.get('deepseek_api_key') or '').strip()
    if key:
        store.set('legacy-pending', key)
    if key or data.get('ai_enabled') or data.get('arbitration_mode', 'off') != 'off':
        atomic_write(path, canonical(sanitize_config(data)))


class PlatformProvider:
    def __init__(self, account, client=None):
        from account_client import AccountClient
        self.account = account
        self.client = client or AccountClient(account.session.origin, timeout=2.5)
        self.policy = None
        self.summary = None

    def _call(self,*args,**kwargs):
        identity=self.account.upload_identity()
        if identity is None or identity.access_token!=kwargs.get('token'):
            raise ArbitrationError('账号已切换或登录已失效')
        try:return self.client.call(*args,**kwargs)
        except Exception as error:
            from account_security import notify_account
            message=notify_account(self.account,error,identity)
            if message:raise ArbitrationError(message) from None
            raise

    def _identity(self):
        identity = self.account.upload_identity()
        if not self.account.allowed() or identity is None:
            raise ArbitrationError('账号、设备或签名模型不可用')
        return identity

    def prepare(self):
        identity = self._identity()
        policy = self._call('arbitration/policy', token=identity.access_token)
        budget = self._call('arbitration/device-budget', token=identity.access_token)
        if self._identity().binding != identity.binding:
            raise ArbitrationError('账号已切换')
        if not policy.get('configured') or not policy.get('enabled'):
            raise ArbitrationError('平台仲裁尚未配置或已暂停')
        if budget.get('paused'):
            raise ArbitrationError('账号平台仲裁已暂停')
        from decimal import Decimal
        if not budget.get('unlimited') and Decimal(budget.get('available') or '0') <= 0:
            raise ArbitrationError('平台额度已用尽')
        self.policy, self.summary = policy, budget

    def read_budget(self):
        """Reading allowance does not enable a provider or require its API key."""
        identity = self._identity()
        budget = self._call('arbitration/device-budget',token=identity.access_token)
        if self._identity().binding != identity.binding:
            raise ArbitrationError('账号已切换')
        self.summary = budget
        return budget

    def ask(self, request):
        identity = self._identity()
        if self.policy is None:
            self.prepare()
        body = dict(request, policyVersion=self.policy['version'])
        result = self._call('arbitration/requests', body, token=identity.access_token)
        if self._identity().binding != identity.binding:
            raise ArbitrationError('账号已切换')
        for key in ('requestId', 'sessionId', 'generation', 'roundKey', 'policyVersion'):
            if result.get(key) != body[key]:
                raise ArbitrationError('平台回执上下文不一致')
        if result.get('state') != 'settled' or result.get('code') != 'ok':
            raise ArbitrationError('平台仲裁已停用，请查看网站费用记录并主动重新启用')
        return result.get('slot')


    def observe(self, request_id, state, elapsed_ms):
        identity = self._identity()
        self._call('arbitration/requests/'+str(uuid.UUID(request_id))+'/observation',
                         {'state':state,'elapsedMs':min(120000,max(0,int(elapsed_ms)))},token=identity.access_token)


def describe_budget(budget):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal, InvalidOperation
    def amount(key):
        try:
            value=Decimal(str(budget.get(key)))
            if not value.is_finite() or value<0:raise ValueError()
            return format(value,'.8f').rstrip('0').rstrip('.') or '0'
        except (InvalidOperation,ValueError,TypeError):
            return '未知'
    if budget.get('unlimited') is True:
        text='平台额度：无限'
    elif budget.get('dailyEnabled') is True:
        text=f"平台今日可用 {amount('available')} 元 / 每日 {amount('dailyLimit')} 元；今日已用 {amount('dailyConsumed')} 元"
        try:
            when=datetime.fromisoformat(budget['nextResetAt'].replace('Z','+00:00'))
            if when.tzinfo is None:raise ValueError()
            text+='；下次重算 '+when.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')+'（北京时间）'
        except (KeyError,ValueError,TypeError,AttributeError):
            pass
    else:
        text=f"平台可用额度 {amount('available')} 元"
    if budget.get('paused'):text+='；平台仲裁已暂停'
    if budget.get('eligible') is False:text+='；当前资格不可用'
    return text+'。仅查询，不发起付费调用；额度耗尽只停止平台仲裁。'


class PersonalProvider:
    def __init__(self, account, store, model, opener=None):
        from upload_worker import NoRedirect
        self.account, self.store, self.model = account, store, model
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def ask(self, request):
        if not self.account or not self.account.allowed():
            raise ArbitrationError('账号、设备或签名模型不可用')
        owner = owner_of(self.account)
        key = self.store.get(owner)
        if not key or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', self.model):
            raise ArbitrationError('请配置个人密钥和有效模型标识')
        # Only numeric features and map; no names, Steam IDs or arbitrary prompts.
        prompt = json.dumps({'map': request['map'], 'features': ['aim_self','aim_enemy','win','enemy_low_hp','self_low_hp','proximity','enemy_damaged','hp_advantage'], 'candidates': request['candidates']})
        body = {'model': self.model, 'stream': False, 'max_tokens': 128,
                'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'},
                'messages': [{'role':'system','content':'Return JSON only: {"slot": integer or null}. Select only a supplied candidate.'}, {'role':'user','content':prompt}]}
        http = urllib.request.Request('https://api.deepseek.com/chat/completions', canonical(body),
                                      {'Authorization': 'Bearer '+key, 'Content-Type':'application/json'}, method='POST')
        try:
            with self.opener.open(http, timeout=2.5) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError()
                envelope = json.loads(raw)
                if envelope['choices'][0].get('finish_reason') != 'stop':
                    raise ValueError()
                result = json.loads(envelope['choices'][0]['message']['content'])
                if not isinstance(result, dict) or set(result) != {'slot'}:
                    raise ValueError()
                slot = result['slot']
                if type(slot) is not int or slot not in {c['slot'] for c in request['candidates']}:
                    return None
        except Exception:
            # Provider exception bodies can contain credentials or private payloads.
            raise ArbitrationError('个人 API 请求失败；未自动重试，继续本地规则') from None
        if not self.account.allowed() or owner_of(self.account) != owner:
            return None
        return slot


class CloudSession:
    def __init__(self, account, store, mode, model, provider=None):
        if mode not in ('platform', 'personal'):
            raise ArbitrationError('仲裁未启用')
        self.account, self.mode = account, mode
        self.owner = owner_of(account)
        self.binding = account.binding()
        self.provider = provider or (PlatformProvider(account) if mode == 'platform' else PersonalProvider(account, store, model))
        self.session = str(uuid.uuid4())
        self.generation = 0
        self.context = None
        self.disabled = False
        self.message = ''
        self.lock = threading.Lock()

    def allowed(self):
        try:
            valid = (not self.disabled and self.account.allowed() and owner_of(self.account) == self.owner
                     and self.account.binding() == self.binding)
            if not valid:
                self.disabled = True
            return valid
        except Exception:
            self.disabled = True
            return False

    def invalidate(self):
        with self.lock:
            self.generation += 1

    def sync(self, context):
        with self.lock:
            if context != self.context:
                self.generation += 1
                self.context = context

    def request(self, snap, candidates):
        from director_core import _candidate_features
        if not self.allowed():
            raise ArbitrationError('仲裁账号资格已变化，请主动重新启用')
        map_name = str(snap.map_name or '')
        if not re.fullmatch(r'[a-zA-Z0-9_]{1,48}', map_name):
            raise ArbitrationError('地图标识无效')
        with self.lock:
            generation = self.generation
            round_key = hashlib.sha256(repr(self.context).encode()).hexdigest()
        return {'requestId':str(uuid.uuid4()), 'sessionId':self.session, 'generation':generation,
                'roundKey':round_key, 'map':map_name,
                'candidates':[{'slot':c.slot,'features':_candidate_features(c)} for c in candidates]}

    def ask(self, request):
        if not self.allowed() or request['generation'] != self.generation:
            return None
        try:
            result = self.provider.ask(request)
        except Exception as error:
            if self.mode == 'platform':
                self.disabled = True  # No automatic restart after top-up/recovery.
            self.message = str(error) if isinstance(error, ArbitrationError) else '平台请求失败，请核对网站状态后重新启用'
            raise ArbitrationError(self.message) from None
        if not self.allowed() or request['generation'] != self.generation:
            return None
        if type(result) is not int or result not in {c['slot'] for c in request['candidates']}:
            return None
        return result
