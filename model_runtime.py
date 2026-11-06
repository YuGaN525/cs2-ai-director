"""Signed model catalogue, resumable downloads and immutable activation generations."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.request
import urllib.error
import http.client
import uuid
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from device_identity import canonical, unb64
from director_core.model_format import DECISION_FEATURES,forest_probability as probability
from offline_runtime import CachedModel
from client_version import VERSION

PURPOSE=b'yuzi-director-model-manifest-v1\0'
FEATURE_HASH=hashlib.sha256(canonical({'feature_version':'pov-v1','features':DECISION_FEATURES})).hexdigest()


class ModelError(ValueError):pass


class ModelNetworkError(Exception):
    def __init__(self,status,code):self.status,self.code=status,code


class RuntimeLock:
    """Held by a running director; model activation never waits on this lock."""
    def __init__(self,path):self.path=Path(path);self.handle=None
    def acquire(self):
        if self.handle is not None:return True
        self.path.parent.mkdir(parents=True,exist_ok=True);h=self.path.open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                if h.seek(0,2)==0:h.write(b'0');h.flush()
                h.seek(0);msvcrt.locking(h.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:h.close();return False
        self.handle=h;return True
    def release(self):
        if self.handle is not None:self.handle.close();self.handle=None


class ModelManager:
    def __init__(self,session,cache_root,trusted_keys,clock=time.time,client_version=VERSION):
        self.session,self.clock,self.keys=session,clock,dict(trusted_keys)
        self.client_version=tuple(int(x) for x in client_version.split('.'))
        self.root=Path(cache_root)/'models'/hashlib.sha256(session.origin.encode()).hexdigest()
        self.root.mkdir(parents=True,exist_ok=True)
        self.base_state_path=session.path.with_suffix('.models');self.state_path=self.base_state_path;self.account_id=None
        self.lock_path=session.path.with_suffix('.activation.lock')
        self.execution_lock=RuntimeLock(self.lock_path)
        self.status='等待获取正式模型';self.active_model=None;self.active_metadata=None

    def select_account(self,account_id):
        uuid.UUID(account_id)
        if self.account_id!=account_id:
            self.account_id=account_id;self.state_path=self.base_state_path.with_name(self.base_state_path.name+'.'+account_id)
            self.active_model=None;self.active_metadata=None

    def _state(self):
        if not self.state_path.exists():return {'sequence':0,'revoked':[],'active':None,'pending':None}
        return self.session._read(self.state_path)

    def verify(self,envelope,cached=False):
        try:
            p=envelope['payload'];key=self.keys[p['key_id']]
            if self.account_id is not None and p.get('account_id')!=self.account_id:raise ModelError('model_account_mismatch')
            Ed25519PublicKey.from_public_bytes(key).verify(unb64(envelope['signature']),PURPOSE+canonical(p))
            if p['manifest_schema']!=1 or p['product']!='cs2-ai-director' or p['channel']!='stable' or p['server_origin']!=self.session.origin:raise ModelError('model_product_mismatch')
            if type(p['release_sequence']) is not int or not 0<=p['release_sequence']<=9223372036854775807:raise ModelError('model_sequence_invalid')
            if not cached and (not p['issued_at']<=self.clock()+120 or not self.clock()<p['metadata_expires_at']<=p['issued_at']+86400):raise ModelError('model_metadata_expired')
            if len(p['revoked_models'])>500:raise ModelError('model_revocations_invalid')
            for value in p['revoked_models']:uuid.UUID(value)
            m=p['current']
            if m is not None:
                uuid.UUID(m['model_id'])
                if (m['model_format']!='forest-json-v1' or m['feature_version']!='pov-v1'
                        or m['features']!=DECISION_FEATURES or m['feature_manifest_sha256']!=FEATURE_HASH
                        or m['core_major']!=0 or tuple(map(int,m['min_client_version'].split('.')))>self.client_version
                        or not re.fullmatch('[a-f0-9]{64}',m['artifact_sha256'])
                        or type(m['artifact_bytes']) is not int or not 18<=m['artifact_bytes']<=20*1024*1024
                        or m['max_unpacked_bytes']!=64*1024*1024 or not 8<=len(m['test_vectors'])<=32):raise ModelError('model_incompatible')
            return p
        except ModelError:raise
        except Exception as e:raise ModelError('model_signature_or_manifest_invalid') from e

    def _validated(self,meta,path):
        model=CachedModel(path)
        if model.verified_hash()!=meta['artifact_sha256'] or path.stat().st_size!=meta['artifact_bytes']:raise ModelError('model_artifact_invalid')
        for vector in meta['test_vectors']:
            x=list(map(float,vector['features']));expected=float(vector['probability'])
            if len(x)!=8 or any(not math.isfinite(v) for v in x) or not math.isfinite(expected) or abs(probability(model.data,x)-expected)>1e-8:raise ModelError('model_test_vector_mismatch')
        return model

    def cached_model(self):
        self.active_metadata=None
        try:
            with self.session._transaction():state=self._state()
            if not state.get('active'):return None
            p=self.verify(state['active'],cached=True);meta=p['current']
            if meta is None or meta['model_id'] in state['revoked']:return None
            path=self.root/(meta['artifact_sha256']+'.json.gz')
            if self.active_model is None or self.active_model.path!=path or not self.active_model.current_hash():self.active_model=self._validated(meta,path)
            self.active_metadata=meta;return self.active_model
        except Exception:return None

    def _download(self,client,token,meta):
        final=self.root/(meta['artifact_sha256']+'.json.gz')
        if final.exists():
            try:self._validated(meta,final);return
            except ModelError:pass
        lock=RuntimeLock(self.root/(meta['artifact_sha256']+'.download.lock'))
        if not lock.acquire():raise ModelError('model_download_in_progress')
        try:
            ticket=client.call('models/'+meta['model_id']+'/ticket',{},token)
            path='/api/director/models/'+meta['model_id']+'/file'
            if ticket['path']!=path:raise ModelError('model_download_origin_mismatch')
            temp=self.root/(meta['artifact_sha256']+'.part')
            offset=temp.stat().st_size if temp.exists() else 0
            if offset>=meta['artifact_bytes']:offset=0
            headers={'Authorization':'Bearer '+token,'X-Director-Download-Ticket':ticket['ticket']}
            if offset:headers.update({'Range':f'bytes={offset}-','If-Range':'"'+meta['artifact_sha256']+'"'})
            request=urllib.request.Request(client.origin+path,headers=headers)
            with client.opener.open(request,timeout=client.timeout) as response:
                if response.status==206:
                    value=response.headers.get('Content-Range','')
                    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',value)
                    if not match or tuple(map(int,match.groups()))!=(offset,meta['artifact_bytes']-1,meta['artifact_bytes']):raise ModelError('model_range_invalid')
                elif response.status==200:offset=0
                else:raise ModelError('model_download_status')
                with temp.open('ab' if offset else 'wb') as stream:
                    total=offset
                    while True:
                        chunk=response.read(65536)
                        if not chunk:break
                        total+=len(chunk)
                        if total>meta['artifact_bytes']:raise ModelError('model_download_too_large')
                        stream.write(chunk)
                    stream.flush();os.fsync(stream.fileno())
            if total!=meta['artifact_bytes']:raise ModelError('model_download_incomplete')
            try:self._validated(meta,temp)
            except ModelError:
                temp.unlink(missing_ok=True);raise
            os.replace(temp,final)
        finally:lock.release()

    def sync(self,client,token,on_catalogue=None):
        envelope=client.call('models/manifest',token=token);p=self.verify(envelope)
        with self.session._transaction():
            state=self._state()
            if p['release_sequence']<state['sequence']:raise ModelError('model_catalogue_rollback')
            old=state.get('latest')
            if p['release_sequence']==state['sequence'] and old and old['payload']['current']!=p['current']:raise ModelError('model_sequence_conflict')
            state['sequence']=p['release_sequence'];state['latest']=envelope
            state['revoked']=sorted(set(state['revoked'])|set(p['revoked_models']))
            self.session._write(self.state_path,state)
        if on_catalogue is not None:on_catalogue()
        meta=p['current']
        if meta is None:self.status='暂无正式模型';return
        if meta['model_id'] in state['revoked']:raise ModelError('model_revoked')
        try:self._download(client,token,meta)
        except urllib.error.HTTPError as error:
            try:code=json.loads(error.read(65536)).get('code') or 'model_download_http_error'
            except (ValueError,TypeError):code='model_download_http_error'
            finally:error.close()
            raise ModelNetworkError(error.code,code)
        except (OSError,urllib.error.URLError,http.client.IncompleteRead) as error:
            raise ModelNetworkError(0,'model_download_or_storage_unavailable') from error
        with self.session._transaction():
            state=self._state()
            if p['release_sequence']!=state['sequence'] or meta['model_id'] in state['revoked']:return
            if state.get('active') and state['active']['payload']['current']['model_id']==meta['model_id']:
                state['active']=envelope;state['pending']=None;self.session._write(self.state_path,state)
                self.status='正式模型已校验 · 发布序号 '+str(p['release_sequence']);return
            state['pending']=envelope;self.session._write(self.state_path,state)
        barrier=RuntimeLock(self.lock_path)
        if not barrier.acquire():self.status='新模型已验证，将在停止导播后启用';return
        try:
            with self.session._transaction():
                state=self._state();pending=state.get('pending')
                if pending and pending['payload']['current']['model_id'] not in state['revoked']:
                    state['active']=pending;state['pending']=None;self.session._write(self.state_path,state)
            self.status='正式模型已验证 · 发布序号 '+str(p['release_sequence'])
        finally:barrier.release()
