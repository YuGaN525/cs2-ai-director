"""Background complete-batch uploader; credentials only arrive from a live identity adapter."""
from dataclasses import dataclass
import hashlib
import json
import random
import threading
import urllib.error
import urllib.request
import uuid
from upload_outbox import Binding
from upload_protocol import API_VERSION, ProtocolError, verify_receipt


class UploadFailure(Exception):
    def __init__(self,status,code):self.status,self.code=status,code


@dataclass(frozen=True)
class UploadIdentity:
    binding: Binding
    access_token: str


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class TrainingUploadClient:
    def __init__(self,timeout=10):
        self.timeout=timeout
        self.identity_provider=None
        self.opener=urllib.request.build_opener(NoRedirect())

    def _request(self,request):
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                raw=response.read(65537)
                if len(raw)>65536:raise ProtocolError('响应超限')
                value=json.loads(raw)
                if value.get('success') is not True:raise ProtocolError('服务器未确认成功')
                return value['data']
        except urllib.error.HTTPError as error:
            status=error.code
            try:code=json.loads(error.read(65536)).get('code') or 'http_'+str(status)
            except (ValueError,TypeError):code='http_'+str(status)
            finally:error.close()
            raise UploadFailure(status,code)
        except (urllib.error.URLError,TimeoutError,OSError):raise UploadFailure(0,'network_unavailable')
        except (ValueError,KeyError,TypeError):raise ProtocolError('服务器响应不符合冻结协议')

    def capabilities(self,binding):
        value=self._request(urllib.request.Request(binding.origin+'/api/director/capabilities'))
        if value.get('apiVersion')!=API_VERSION or value.get('receiptUploadHash')!='sha256-file-bytes' or 'decision-v1' not in value.get('schemas',[]):
            raise ProtocolError('服务器协议不兼容')
        if value.get('acceptingUploads') is not True:raise UploadFailure(503,'upload_disabled')
        return value

    def upload(self,identity,batch):
        self._check_identity(identity)
        if (batch['account'],batch['origin'],batch['device'])!=(identity.binding.account,identity.binding.origin,identity.binding.device):
            raise ProtocolError('账号/设备/目标站点不一致')
        if not identity.access_token:raise UploadFailure(401,'login_required')
        if hashlib.sha256(batch['payload']).hexdigest()!=batch['upload_hash']:raise ProtocolError('本地批次文件损坏')
        capabilities=self.capabilities(identity.binding)
        if capabilities.get('scopedUploads',{}).get('version')=='director-upload-v1' and batch.get('descriptor'):
            return self._scoped_upload(identity,batch)
        boundary='director_'+uuid.uuid4().hex
        prefix=(f'--{boundary}\r\nContent-Disposition: form-data; name="source"\r\n\r\n{batch["source"]}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="batch.csv.gz"\r\nContent-Type: application/gzip\r\n\r\n').encode()
        body=prefix+batch['payload']+f'\r\n--{boundary}--\r\n'.encode()
        request=urllib.request.Request(identity.binding.origin+'/api/director/data/batches',data=body,method='POST',
            headers={'Authorization':'Bearer '+identity.access_token,'Content-Type':'multipart/form-data; boundary='+boundary})
        return verify_receipt(self._request(request),batch)

    def _check_identity(self,identity):
        if self.identity_provider is not None:
            current=self.identity_provider()
            if current is None or current.binding!=identity.binding or current.access_token!=identity.access_token:raise UploadFailure(401,'identity_changed')

    def _scoped_upload(self,identity,batch):
        description=json.loads(batch['descriptor']);base=identity.binding.origin+'/api/director/data/uploads'
        headers={'Authorization':'Bearer '+identity.access_token,'Content-Type':'application/json'}
        def complete():
            self._check_identity(identity)
            return self._request(urllib.request.Request(base+'/'+batch['id']+'/complete',data=b'{}',method='POST',headers=headers))
        try:
            state=self._request(urllib.request.Request(base,data=json.dumps(description,sort_keys=True,separators=(',',':')).encode(),method='POST',headers=headers))
        except UploadFailure as error:
            if error.code=='DIRECTOR_OBJECT_UNAVAILABLE':return verify_receipt(complete(),batch)
            raise
        if state.get('status')=='confirmed':return verify_receipt(state['receipt'],batch)
        if state.get('partBytes')!=262144 or not isinstance(state.get('parts'),list):raise ProtocolError('续传协议无效')
        for index in range(description['partCount']):
            if index in state['parts']:continue
            self._check_identity(identity)
            data=batch['payload'][index*262144:(index+1)*262144]
            request=urllib.request.Request(base+'/'+batch['id']+'/parts/'+str(index),data=data,method='PUT',
                headers={'Authorization':'Bearer '+identity.access_token,'Content-Type':'application/octet-stream','X-Part-Sha256':hashlib.sha256(data).hexdigest()})
            self._request(request)
        return verify_receipt(complete(),batch)


class UploadWorker:
    def __init__(self,outbox,identity_provider,client=None):
        self.outbox,self.identity_provider=outbox,identity_provider
        self.client=client or TrainingUploadClient()
        if isinstance(self.client,TrainingUploadClient):self.client.identity_provider=identity_provider
        self.stop_event=threading.Event();self.wake_event=threading.Event()
        self.thread=None;self.status='未登录，数据保留在本地';self._identity_fingerprint=None

    def run_once(self):
        identity=self.identity_provider()
        if identity is None or identity.binding.origin=='local':
            self.status='未登录，数据保留在本地';return False
        fingerprint=hashlib.sha256((str(identity.binding)+identity.access_token).encode()).hexdigest()
        if fingerprint!=self._identity_fingerprint:
            self.outbox.resume_authenticated(identity.binding);self._identity_fingerprint=fingerprint
        self.outbox.prepare_batch(identity.binding)
        batch=self.outbox.claim(identity.binding)
        if batch is None:
            states=self.outbox.stats(identity.binding)['states']
            self.status=('登录、权限或回执问题待处理，记录保留' if states.get('blocked')
                         else '存在被拒绝的数据，记录保留' if states.get('rejected')
                         else '等待后台重试补传' if states.get('pending') or states.get('inflight')
                         else '当前账号待传数据已同步')
            return False
        try:
            # Re-read the current identity immediately before transmitting any bytes.
            current=self.identity_provider()
            if current is None or current.binding!=identity.binding or current.access_token!=identity.access_token:
                raise UploadFailure(401,'identity_changed')
            receipt=self.client.upload(current,batch)
            self.outbox.acknowledge(batch,receipt);self.status='上传已确认';return True
        except UploadFailure as error:
            rejected=error.status in (400,409,413,415,422) or 300<=error.status<400
            blocked=error.status in (401,403)
            delay=min(3600,5*2**min(batch['attempts'],9))+random.uniform(0,3)
            self.outbox.fail(batch,error.code,retry_after=delay,blocked=blocked,rejected=rejected)
            self.status=('需要恢复登录/权限' if blocked else '数据被拒绝，已保留' if rejected else '离线或服务暂不可用，等待补传')
        except ProtocolError:
            self.outbox.fail(batch,'protocol_or_receipt_mismatch',blocked=True);self.status='协议或回执不匹配，数据已保留'
        return False

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                progressed=self.run_once()
            except Exception:
                self.status='本地队列不可用，请检查磁盘；数据未标为上传成功';progressed=False
            if not progressed:self.wake_event.wait(2);self.wake_event.clear()

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self._loop,name='training-uploader',daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set();self.wake_event.set()
        if self.thread:self.thread.join(timeout=12)
