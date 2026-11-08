"""Website device login + protected refresh rotation. No account passwords in this process."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import uuid
import sys
import webbrowser

from device_identity import DeviceSession, WindowsProtector, IdentityError, server_origin, atomic_write, canonical, unb64
from offline_runtime import CachedModel
from model_runtime import ModelNetworkError
from upload_outbox import Binding
from upload_worker import UploadIdentity, NoRedirect


class AccountError(Exception):
    def __init__(self,code,status=0):self.code,self.status=code,status;super().__init__(code)


class AccountClient:
    def __init__(self,origin,timeout=10):
        self.origin=server_origin(origin);self.timeout=timeout
        self.opener=urllib.request.build_opener(NoRedirect())

    def call(self,path,data=None,token='',raw=False):
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        request=urllib.request.Request(self.origin+'/api/director/'+path,
            data=canonical(data) if data is not None else None,headers=headers,method='POST' if data is not None else 'GET')
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                body=response.read(131073)
                if len(body)>131072:raise AccountError('response_too_large')
                result=json.loads(body)
        except urllib.error.HTTPError as error:
            try:result=json.loads(error.read(65536));code=result.get('error') or result.get('code') or 'http_'+str(error.code)
            except Exception:code='http_'+str(error.code)
            finally:error.close()
            raise AccountError(code,error.code)
        except (OSError,urllib.error.URLError,TimeoutError):raise AccountError('network_unavailable')
        except (ValueError,KeyError):raise AccountError('invalid_response')
        if raw:return result
        if result.get('success') is not True:raise AccountError(result.get('code','invalid_response'))
        return result['data']


class DesktopAccount:
    def __init__(self,root,origin,trusted_keys,model_path,client=None,clock=time.time,protector=None,model_keys=None):
        self.root=Path(root);self.clock=clock
        self.session=DeviceSession(self.root/'credentials',origin,trusted_keys,clock=clock,protector=protector)
        self.client=client or AccountClient(origin)
        self.local_device=hashlib.sha256(unb64(self.session.device_public_key())).hexdigest()
        self.auth_path=self.root/'credentials'/(hashlib.sha256(self.session.origin.encode()).hexdigest()+'.tokens')
        self.model=CachedModel(model_path);self.model.verified_hash()
        self.model_manager=None
        if model_keys is not None:
            from model_runtime import ModelManager
            self.model_manager=ModelManager(self.session,Path(model_path).parent,model_keys,clock)
        self.lock=threading.RLock();self.tokens=None;self.status='未登录网站账号'
        self.stop_event=threading.Event();self.cancel_event=threading.Event();self.thread=None
        self.sync_requested=threading.Event()
        self.pending=None;self.last_renew=0
        self._login_running=False;self._flow_generation=None
        self._signed_out_requested=False
        self._local_ready=False;self._local_expiry=0;self._local_wall=clock();self._local_mono=time.monotonic()
        self._local_model_hash=''
        self._local_reason='not_checked'
        self._reload()
        self.check_local()

    def _reload(self):
        try:self.tokens=self.session._read(self.auth_path)
        except Exception:self.tokens=None

    @staticmethod
    def _expires(value):return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()

    def _save_tokens(self,value):
        value=dict(value)
        if value.get('server_origin')!=self.session.origin:raise AccountError('wrong_server')
        uuid.UUID(value['account_id']);uuid.UUID(value['device_id'])
        if not value['access_token'].startswith('d1_') or not value['refresh_token'].startswith('dr1_'):raise AccountError('invalid_response')
        value.setdefault('local_generation',(self.tokens or {}).get('local_generation') or str(uuid.uuid4()))
        self.session._write(self.auth_path,value);self.tokens=value

    def binding(self):
        tokens=self.tokens
        if tokens and tokens.get('account_id'):return Binding(tokens['account_id'],self.session.origin,tokens['device_id'])
        # Prepared once; callbacks on the recorder thread must never wait for a network lock.
        return Binding('unbound','local',self.local_device)

    def upload_identity(self):
        tokens=self.tokens
        if self._signed_out_requested or not tokens or not tokens.get('account_id') or self._expires(tokens['access_expires_at'])<=self.clock():return None
        binding=Binding(tokens['account_id'],self.session.origin,tokens['device_id'])
        return UploadIdentity(binding,tokens['access_token'])

    def begin_login(self,open_browser=True):
        self.cancel_event.clear()
        with self.session._transaction():
            self._reload();self._flow_generation=(self.tokens or {}).get('local_generation','')
        result=self.client.call('device-auth/start',{'publicKey':self.session.device_public_key(),'deviceName':'CS2 AI Director'},raw=True)
        if result.get('verification_uri')!=self.session.origin+'/tools/ai-director/activate':raise AccountError('wrong_verification_origin')
        with self.lock:self.pending=result;self.status='请在网站确认设备码：'+result['user_code']
        if open_browser:webbrowser.open(result['verification_uri'])
        return result

    def complete_login(self):
        pending=self.pending
        if pending is None:return False
        proof=self.session.sign_proof('yuzi-director-device-poll-v1',pending['device_code'])
        value=self.client.call('device-auth/token',{'deviceCode':pending['device_code'],'proof':proof},raw=True)
        # Logout old local account before installing a different identity; queues retain original owners.
        with self.lock,self.session._transaction():
            self._reload()
            if self.cancel_event.is_set():return False
            if (self.tokens or {}).get('local_generation','')!=self._flow_generation:
                raise AccountError('local_session_changed')
            if self.tokens and self.tokens.get('account_id') and self.tokens['account_id']!=value['account_id']:self._block_current('signed_out')
            value['local_generation']=str(uuid.uuid4())
            self._save_tokens(value);self.pending=None;self.last_renew=0
            self._signed_out_requested=False
        self.status='账号登录成功，正在检查资格/模型';return True

    def login_async(self):
        with self.lock:
            if self._login_running:return
            self._login_running=True
        def work():
            try:
                value=self.begin_login();deadline=self.clock()+value['expires_in'];interval=value['interval']
                while self.clock()<deadline and not self.cancel_event.wait(interval):
                    try:
                        if self.complete_login():self.sync_once();return
                    except AccountError as error:
                        if error.code=='authorization_pending':continue
                        if error.code=='slow_down':interval+=5;continue
                        if error.status==429:interval=min(60,interval+5);continue
                        if error.code=='network_unavailable':interval=min(60,interval*2);continue
                        raise
                self.status='设备登录已取消或过期'
            except Exception as error:self.status='登录未完成：'+(error.code if isinstance(error,AccountError) else 'local_error')
            finally:
                self.pending=None
                with self.lock:self._login_running=False
        threading.Thread(target=work,name='device-login',daemon=True).start()

    def _block_current(self,reason):
        # Caller owns session transaction. Persist tombstone without nesting the OS file lock.
        state=self.session._read(self.session.path) if self.session.path.exists() else {}
        state['blocked']=reason;self.session._write(self.session.path,state);self._local_ready=False;self._local_reason=reason

    def logout(self):
        self.cancel_event.set()
        self._signed_out_requested=True
        self._local_ready=False
        with self.session._transaction():
            self._reload();old=self.tokens
            self._block_current('signed_out')
            self.session._write(self.auth_path,{'local_generation':str(uuid.uuid4())});self.tokens=None;self.status='已退出账号，原队列保留'
        if old and old.get('access_token'):
            try:self.client.call('logout',{},old['access_token'])
            except AccountError:pass

    def sync_once(self):
        # A shared OS lock spans refresh exchange and replacement, preventing director/trainer
        # from refreshing the same token concurrently. Network work is only on this background path.
        with self.session._transaction():
            self._reload()
            if not self.tokens or not self.tokens.get('account_id'):self.tokens=None;return
            try:
                if self._expires(self.tokens['access_expires_at'])<=self.clock()+60:
                    refresh=self.tokens['refresh_token']
                    # Avoid nesting device file lock: the outer transaction already owns it.
                    proof=__import__('device_identity').b64(self.session._device().sign(('yuzi-director-refresh-v1\0'+refresh).encode()))
                    self._save_tokens(self.client.call('token/refresh',{'refreshToken':refresh,'proof':proof},raw=True))
                token=self.tokens['access_token']
                if self.model_manager is not None:self.model_manager.select_account(self.tokens['account_id'])
                entitlement=self.client.call('me/entitlement',token=token)
                if not entitlement['enabled']:
                    self._block_current('entitlement_unavailable');self.status='账号已登录，暂无有效导播资格';return
                digest=self.model.verified_hash()
                if not digest and self.model_manager is None:self.status='账号资格有效，等待正式模型';return
                # Release shared transaction before install_verified_lease, which owns its own lock.
            except AccountError as error:
                if error.status in (401,403):
                    self._block_current('reauth_required');self.session._write(self.auth_path,{'local_generation':str(uuid.uuid4())});self.tokens=None
                self.status='暂时离线，按本地凭据检查' if error.status==0 else '需要重新登录或恢复资格：'+error.code
                return
        try:
            if self.model_manager is not None:
                from model_runtime import ModelError
                try:self.model_manager.sync(self.client,token,on_catalogue=self.check_local)
                except ModelError as error:self.model_manager.status='模型更新未完成：'+str(error)
                selected=self.model_manager.cached_model()
                if selected is None:
                    self._local_ready=False;self.status=self.model_manager.status;return
                self.model=selected;digest=self.model.current_hash()
            proof=self.session.sign_proof('yuzi-director-lease-v1',token+':'+digest)
            request={'modelSha256':digest,'proof':proof}
            if self.model_manager is not None:request['modelId']=self.model_manager.active_metadata['model_id']
            envelope=self.client.call('me/lease',request,token)
            with self.session._transaction():
                # A concurrent logout/account switch cannot be undone by a late network response.
                self._reload()
                if not self.tokens or self.tokens.get('access_token')!=token:return
                self.session._install_verified_lease_locked(envelope,self.tokens['account_id'])
            self.status='登录与设备绑定有效，离线凭据已续签168小时'+('；'+self.model_manager.status if self.model_manager else '');self.last_renew=self.clock();self.check_local()
        except IdentityError:
            # A rejected signed response cannot replace the last valid lease or
            # crash the diagnostic/GUI entry. Existing offline rules still apply.
            self.check_local()
            self.status='离线许可续签未通过验证，请核对系统时间或重新登录；当前执行资格以本机有效凭据为准'
        except (AccountError,ModelNetworkError) as error:
            self.status='等待服务端批准正式模型' if error.code=='DIRECTOR_MODEL_REQUIRED' else '续签未完成：'+error.code
            if error.status in (401,403) or error.code=='DIRECTOR_MODEL_REQUIRED':
                with self.session._transaction():
                    self._reload()
                    if self.tokens and self.tokens.get('access_token')==token:
                        self._block_current('entitlement_or_device_revoked')

    def status_snapshot(self):
        allowed = self.allowed()
        now = max(self.clock(), self._local_wall + max(0, time.monotonic()-self._local_mono))
        local_reason = self._local_reason
        if self.pending is not None:
            reason = 'login_pending'
        elif allowed:
            reason = 'ready'
        elif local_reason in ('entitlement_unavailable', 'entitlement_or_device_revoked'):
            reason = 'revoked'
        elif local_reason == 'reauth_required':
            reason = 'reauth_required'
        elif not self.tokens:
            reason = 'login_required'
        elif self.clock() < self._local_wall-2 or local_reason == 'clock_rollback':
            reason = 'clock_rollback'
        elif local_reason == 'offline_expired' or (self._local_expiry and now >= self._local_expiry):
            reason = 'lease_expired'
        elif local_reason == 'model_missing_or_changed' or (self._local_model_hash and self.model.current_hash() != self._local_model_hash):
            reason = 'model_changed'
        elif not self._local_model_hash:
            reason = 'model_or_validation_required'
        else:
            reason = 'local_validation_required'
        manager = self.model_manager
        return {'logged_in': bool(self.tokens), 'login_pending': self.pending is not None,
                'allowed': allowed, 'reason_code': reason,
                'lease_expires_at': self._local_expiry if self._local_expiry else None,
                'remaining_seconds': max(0, int(self._local_expiry-now)) if allowed else 0,
                'last_verified_at': self.last_renew or None,
                'model_ready': bool(self._local_model_hash),
                'model_state': manager.status if manager else '未配置正式模型发行源'}

    def allowed(self):
        # No network, credential IO or waiting on network-held locks in the input loop.
        effective=max(self.clock(),self._local_wall+max(0,time.monotonic()-self._local_mono))
        return bool(self._local_ready and self.tokens and self._local_model_hash and self.model.current_hash()==self._local_model_hash
                    and effective<self._local_expiry and self.clock()>=self._local_wall-2)

    def check_local(self):
        try:
            with self.session._transaction():self._reload()
            if not self.tokens or not self.tokens.get('account_id'):
                self.tokens=None;self._local_ready=False;return
            if self.model_manager is not None:
                self.model_manager.select_account(self.tokens['account_id'])
                selected=self.model_manager.cached_model()
                if selected is None:self._local_ready=False;self._local_reason='model_missing_or_changed';return
                self.model=selected
            state=self.session.check(self.tokens['account_id'],self.model.current_hash())
            self._local_ready,self._local_expiry=state.allowed,state.expires_at
            self._local_reason=state.reason
            self._local_model_hash=self.model.current_hash() if state.allowed else ''
            self._local_wall,self._local_mono=self.clock(),time.monotonic()
        except Exception:self._local_ready=False;self._local_reason='local_validation_failed'

    @property
    def last_state(self):
        from device_identity import AccessState
        return AccessState(self.allowed(),'账号/模型/离线凭据不可用')

    def _loop(self):
        while not self.stop_event.is_set():
            try:self.sync_once()
            except Exception:self.status='账号后台校验失败，本地凭据状态保留'
            for _ in range(60):
                if self.stop_event.wait(1):break
                if self.sync_requested.is_set():self.sync_requested.clear();break
                self.check_local()

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear()
        self.thread=threading.Thread(target=self._loop,name='device-session',daemon=True);self.thread.start()

    def acquire_execution(self):
        if self.model_manager is not None and not self.model_manager.execution_lock.acquire():return False
        self.check_local()
        if self.allowed():return True
        self.release_execution();return False

    def release_execution(self):
        if self.model_manager is not None:self.model_manager.execution_lock.release()
        self.sync_requested.set()

    def close(self):self.stop_event.set();self.cancel_event.set();self.release_execution()


def default_account():
    root=Path(os.environ.get('APPDATA') or Path.home())/'CS2AIDirector'
    # Trust keys are shipped/provisioned by the application, never by the login response.
    from public_trust import runtime_trust_path
    path=runtime_trust_path()
    if not path.exists():
        return DesktopAccount(root/'identity','https://www.yuzibei.cn',{},root/'verified-model.json.gz',model_keys={})
    from public_trust import read_public_trust
    try:config=read_public_trust(path)
    except (ValueError,OSError):
        account=DesktopAccount(root/'identity','https://www.yuzibei.cn',{},root/'verified-model.json.gz',model_keys={})
        account.status='发行包信任配置无效，请重新获取正式程序';return account
    return DesktopAccount(root/'identity',config['server_origin'],{k:unb64(v) for k,v in config['trusted_public_keys'].items()},root/'verified-model.json.gz',model_keys={k:unb64(v) for k,v in config.get('trusted_model_keys',{}).items()})


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='网站设备账号状态诊断（不输出令牌）')
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args(argv)
    account=default_account()
    try:
        account.sync_once();account.check_local()
        result={'logged_in':bool(account.tokens),'execution_allowed':account.allowed(),'status':account.status,
                'binding':{'origin':account.binding().origin,'account_id':account.binding().account,'device_id':account.binding().device}}
        atomic_write(args.report,canonical(result));return 0
    finally:account.close()
