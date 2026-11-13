"""Explicit desktop model submission; immutable requests, device auth, no publishing."""
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from client_update_lock import FileLock
from device_identity import atomic_write, canonical
from director_core.model_format import validate_forest, forest_probability
from upload_worker import NoRedirect

MAX_MODEL = 20 * 1024 * 1024
MAX_JSON = 64 * 1024 * 1024
MAX_METADATA = 65536
ROUTE = '/api/director/training/submissions'


class SubmissionError(Exception):
    def __init__(self, code, status=0):
        self.code, self.status = code, status
        super().__init__(code)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def nonfinite(_):
        raise ValueError('non_finite_json')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


def read_bounded(path, limit):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit+1)
    if not raw or len(raw) > limit:
        raise ValueError('file_size_limit')
    return raw


def load_result(model_path):
    """Read a stable pair, validate locally and never upload pickle or raw Demo."""
    model_path = Path(model_path)
    if not model_path.name.endswith('.json.gz'):
        raise ValueError('select_json_gzip_model')
    artifact = read_bounded(model_path, MAX_MODEL)
    metadata_path = model_path.with_suffix('').with_suffix('.submission.json')
    metadata = strict_json(read_bounded(metadata_path, MAX_METADATA))
    with gzip.GzipFile(fileobj=io.BytesIO(artifact)) as stream:
        unpacked = stream.read(MAX_JSON+1)
    if len(unpacked) > MAX_JSON:
        raise ValueError('unpacked_model_too_large')
    forest = validate_forest(strict_json(unpacked))
    digest = hashlib.sha256(artifact).hexdigest()
    if not isinstance(metadata, dict) or set(metadata) != {'name','evaluation','vectors','artifactSha256'}:
        raise ValueError('invalid_metadata')
    if metadata['artifactSha256'] != digest:
        raise ValueError('model_metadata_mismatch')
    def string(value, minimum, maximum):
        return isinstance(value,str) and minimum <= len(value) <= maximum
    def number(value):
        return type(value) in (int,float) and math.isfinite(value)
    evaluation = metadata['evaluation']
    if not string(metadata['name'],1,80) or not metadata['name'].strip():
        raise ValueError('invalid_model_name')
    fields = {'suiteVersion','splitHash','testRows','positiveRows','balancedAccuracy','notes'}
    if not isinstance(evaluation,dict) or set(evaluation) != fields:
        raise ValueError('invalid_evaluation')
    if (not string(evaluation['suiteVersion'],1,64)
            or not isinstance(evaluation['splitHash'],str)
            or not re.fullmatch('[a-f0-9]{64}',evaluation['splitHash'])
            or type(evaluation['testRows']) is not int or not 1 <= evaluation['testRows'] <= 9007199254740991
            or type(evaluation['positiveRows']) is not int or not 0 <= evaluation['positiveRows'] <= evaluation['testRows']
            or not number(evaluation['balancedAccuracy']) or not 0 <= evaluation['balancedAccuracy'] <= 1
            or not string(evaluation['notes'],0,500)):
        raise ValueError('invalid_evaluation')
    vectors = metadata['vectors']
    if not isinstance(vectors,list) or not 8 <= len(vectors) <= 32:
        raise ValueError('invalid_vectors')
    for vector in vectors:
        if not isinstance(vector,dict) or set(vector) != {'features','probability'}:
            raise ValueError('invalid_vector')
        features, probability = vector['features'], vector['probability']
        if (not isinstance(features,list) or len(features)!=8 or not all(number(x) for x in features)
                or not number(probability) or not 0 <= probability <= 1
                or abs(forest_probability(forest,features)-probability)>1e-9):
            raise ValueError('model_vector_mismatch')
    encoded = canonical(metadata)
    if len(encoded)>MAX_METADATA:
        raise ValueError('metadata_size_limit')
    return artifact, metadata


class SubmissionClient:
    def __init__(self, account, root=None, timeout=30):
        self.account = account
        self.root = Path(root or account.root) / 'training-submissions'
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def _identity(self, expected):
        current = self.account.upload_identity()
        if current is None:
            raise SubmissionError('login_required',401)
        if expected is not None and current.binding != expected.binding:
            raise SubmissionError('identity_changed',401)
        tokens = getattr(self.account,'tokens',None)
        if (hasattr(self,'_generation') and isinstance(tokens,dict)
                and tokens.get('local_generation') != self._generation):
            raise SubmissionError('identity_changed',401)
        return current

    def submit(self, model_path):
        identity = self._identity(None)
        tokens = getattr(self.account,'tokens',None)
        if isinstance(tokens,dict):
            self._generation = tokens.get('local_generation')
        artifact, metadata = load_result(model_path)
        caps = self._request(identity,'/capabilities')
        if (caps.get('schema')!='director-training-submission-v1' or caps.get('enabled') is not True
                or caps.get('candidateOnly') is not True or caps.get('modelFormat')!='forest-json-v1'
                or caps.get('featureVersion')!='pov-v1' or caps.get('idempotencyScope')!='account-device-requestId'
                or type(caps.get('maxFileBytes')) is not int or len(artifact)>caps['maxFileBytes']
                or type(caps.get('maxMetadataBytes')) is not int or len(canonical(metadata))>caps['maxMetadataBytes']):
            raise SubmissionError('incompatible_capabilities')
        context = dict(account=identity.binding.account,device=identity.binding.device,origin=identity.binding.origin,
                       artifactSha256=metadata['artifactSha256'],artifactBytes=len(artifact),metadata=metadata)
        key = hashlib.sha256(canonical(context)).hexdigest()
        self.root.mkdir(parents=True,exist_ok=True)
        with FileLock(self.root/(key+'.lock'),timeout=0):
            path = self.root/(key+'.json')
            if path.exists():
                state = strict_json(read_bounded(path,MAX_METADATA*3))
                if state.get('context')!=context or state.get('schema')!=1:
                    raise ValueError('saved_submission_corrupt')
                request_id = state['requestId']
                if str(uuid.UUID(request_id))!=request_id:
                    raise ValueError('saved_request_id_invalid')
            else:
                request_id = str(uuid.uuid4())
                state = dict(schema=1,requestId=request_id,context=context)
                atomic_write(path,canonical(state))
            receipt = self._lookup(identity,request_id,context)
            if receipt is None:
                body,content_type = self._multipart(request_id,artifact,metadata)
                if type(caps.get('maxRequestBytes')) is not int or len(body)>caps['maxRequestBytes']:
                    raise SubmissionError('request_size_limit')
                try:
                    receipt = self._receipt(self._request(identity,body=body,content_type=content_type),request_id,context)
                except SubmissionError as error:
                    if error.status and error.status<500:
                        raise
                    # Never immediately repeat a POST with an unknown outcome.
                    receipt = self._lookup(identity,request_id,context)
                    if receipt is None:
                        raise error
            if state.get('receipt') is not None and state['receipt']!=receipt:
                raise SubmissionError('receipt_changed')
            state['receipt'] = receipt
            self._identity(identity)
            atomic_write(path,canonical(state))
            return receipt

    def _lookup(self,identity,request_id,context):
        try:
            return self._receipt(self._request(identity,'/'+request_id),request_id,context)
        except SubmissionError as error:
            if error.status==404 and error.code=='DIRECTOR_SUBMISSION_NOT_FOUND':
                return None
            raise

    @staticmethod
    def _receipt(value,request_id,context):
        try:
            expected = {'requestId','modelId','artifactSha256','artifactBytes','status','existingModel','submittedAt'}
            if set(value)!=expected or value['requestId']!=request_id:
                raise ValueError()
            if uuid.UUID(value['modelId']).int==0 or value['artifactSha256']!=context['artifactSha256']:
                raise ValueError()
            if type(value['artifactBytes']) is not int or value['artifactBytes']!=context['artifactBytes']:
                raise ValueError()
            if (type(value['existingModel']) is not bool or not isinstance(value['status'],str)
                    or not re.fullmatch('[a-z-]{1,32}',value['status'])
                    or (not value['existingModel'] and value['status']!='candidate')):
                raise ValueError()
            if datetime.fromisoformat(value['submittedAt'].replace('Z','+00:00')).tzinfo is None:
                raise ValueError()
            return value
        except (ValueError,TypeError,KeyError,AttributeError):
            raise SubmissionError('invalid_receipt') from None

    def _request(self, identity, suffix='', body=None, content_type='application/json'):
        current = self._identity(identity)
        request = urllib.request.Request(current.binding.origin+ROUTE+suffix, data=body,
            headers={'Authorization':'Bearer '+current.access_token,'Content-Type':content_type},
            method='POST' if body is not None else 'GET')
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                raw = response.read(MAX_METADATA+1)
                if len(raw)>MAX_METADATA:
                    raise SubmissionError('invalid_response')
                value = strict_json(raw)
                if not isinstance(value,dict) or value.get('success') is not True or not isinstance(value.get('data'),dict):
                    raise SubmissionError('invalid_response')
                self._identity(identity)
                return value['data']
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                value = strict_json(error.read(MAX_METADATA))
                code = value.get('code','http_'+str(status))
                if not isinstance(code,str) or not re.fullmatch('[A-Za-z0-9_]{1,80}',code):
                    code = 'http_'+str(status)
            except (ValueError,TypeError,AttributeError):
                code = 'http_'+str(status)
            finally:
                error.close()
            from account_security import notify_account
            notify_account(self.account,SubmissionError(code,status),current)
            raise SubmissionError(code,status) from None
        except (urllib.error.URLError,TimeoutError,OSError):
            raise SubmissionError('network_unavailable') from None
        except (ValueError,TypeError,KeyError):
            raise SubmissionError('invalid_response') from None

    @staticmethod
    def _multipart(request_id, artifact, metadata):
        boundary = 'yuzi_model_'+uuid.uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="requestId"\r\n\r\n{request_id}\r\n'
                  f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json\r\n\r\n').encode()
        middle = (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="decision_model.json.gz"\r\n'
                  'Content-Type: application/gzip\r\n\r\n').encode()
        return prefix+canonical(metadata)+middle+artifact+f'\r\n--{boundary}--\r\n'.encode(), 'multipart/form-data; boundary='+boundary


def explain_error(error):
    if isinstance(error,SubmissionError):
        from account_security import is_security_error,MESSAGES
        if is_security_error(error.code,error.status):return MESSAGES[error.code]
        if error.status == 401:
            return '登录已失效或账号已切换，请重新登录后重试。'
        if error.status == 403:
            return '当前账号没有训练提交权限，请由网站管理员授予训练权限并确认产品资格有效。'
        if error.status == 409:
            return '提交编号与服务端记录冲突，原记录已保留，请联系管理员核对。'
        if error.status == 429:
            return '提交频率达到限制，请稍后重试；原提交编号保持。'
        if error.status == 404:
            return '服务器尚未开放训练结果提交，请稍后重试。'
        return '尚未取得有效服务器回执。请稍后再次上传，程序会先核对原提交，避免重复登记。'
    if isinstance(error,FileNotFoundError):
        return '未找到模型或配套 .submission.json 评估文件，请选择当前训练器生成的结果。'
    if isinstance(error,TimeoutError):
        return '相同训练结果正在由另一个窗口提交，请稍后重试。'
    return '结果文件读取或校验失败，请重新训练，或确认模型与评估文件来自同一次训练。'
