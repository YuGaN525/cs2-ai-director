"""Identity-bound, resumable dataset HTTP client; success receipt and governance are separate."""
from datetime import datetime
from email.utils import parsedate_to_datetime
import time
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

import dataset_protocol as p
from training_submission import strict_json
from upload_worker import NoRedirect

ROUTE='/api/director/training/datasets'


class DatasetFailure(Exception):
    def __init__(self,code,status=0,retry_after=0):
        self.code,self.status,self.retry_after=code,status,retry_after;super().__init__(code)


def retry_seconds(value):
    try:
        seconds=int(value) if str(value).isdigit() else parsedate_to_datetime(value).timestamp()-time.time()
        return max(0,min(86400,seconds))
    except (TypeError,ValueError,OverflowError):return 0


def utc(value):
    p.require(type(value) is str and value.endswith('Z'),'RESPONSE_TIME')
    try:p.require(datetime.fromisoformat(value.replace('Z','+00:00')).tzinfo is not None,'RESPONSE_TIME')
    except ValueError as error:raise p.Invalid('RESPONSE_TIME') from error


def validate_receipt(value,data):
    p.fields(value,'schema clientDatasetId datasetId manifestSha256 requestHash receivedFiles receivedRows acceptedRows duplicateRows receivedAt initialGovernanceStatus')
    p.require(value['schema']==p.SCHEMA and value['clientDatasetId']==data['clientDatasetId'] and value['manifestSha256']==data['manifestSha256'] and value['requestHash']==p.digest(p.canonical(data)),'RECEIPT_MISMATCH')
    p.pattern(value['datasetId'],p.UUID);utc(value['receivedAt'])
    totals=data['manifest']['totals']
    p.integer(value['receivedFiles'],totals['files'],totals['files']);p.integer(value['receivedRows'],totals['rows'],totals['rows'])
    p.integer(value['acceptedRows'],0,totals['rows']);p.integer(value['duplicateRows'],0,totals['rows'])
    p.require(value['acceptedRows']+value['duplicateRows']==totals['rows'] and value['initialGovernanceStatus']=='pending-review','RECEIPT_MISMATCH')
    return value


def validate_state(value,data):
    p.fields(value,'schema clientDatasetId manifestSha256 requestHash transportStatus expiresAt receivedParts hasReceipt')
    p.require(value['schema']==p.SCHEMA and value['clientDatasetId']==data['clientDatasetId'] and value['manifestSha256']==data['manifestSha256'] and value['requestHash']==p.digest(p.canonical(data)),'STATE_MISMATCH')
    p.require(value['transportStatus'] in ('receiving','verifying','received','cancelled','expired','rejected'),'STATE_MISMATCH')
    p.require(type(value['hasReceipt']) is bool and value['hasReceipt']==(value['transportStatus']=='received'),'STATE_MISMATCH');utc(value['expiresAt'])
    files=data['manifest']['files'];seen=set();present={}
    p.require(type(value['receivedParts']) is list and len(value['receivedParts'])<=len(files),'PART_STATE')
    for item in value['receivedParts']:
        p.fields(item,'fileIndex parts');p.integer(item['fileIndex'],0,len(files)-1)
        index=item['fileIndex'];p.require(index not in seen,'PART_STATE');seen.add(index)
        p.require(type(item['parts']) is list and len(item['parts'])<=files[index]['partCount'],'PART_STATE')
        for part in item['parts']:p.integer(part,0,files[index]['partCount']-1)
        p.require(item['parts']==sorted(set(item['parts'])),'PART_STATE');present[index]=set(item['parts'])
    return present


def validate_governance(value,data,receipt):
    p.fields(value,'schema clientDatasetId datasetId revision governanceStatus sources')
    p.require(value['schema']==p.SCHEMA and value['clientDatasetId']==data['clientDatasetId'] and value['datasetId']==receipt['datasetId'],'GOVERNANCE_MISMATCH')
    p.integer(value['revision'],1)
    p.require(value['governanceStatus'] in ('pending-review','partially-included','included','excluded','blocked'),'GOVERNANCE_MISMATCH')
    expected={s['sourceId'] for s in data['manifest']['sources']};seen=set()
    p.require(type(value['sources']) is list and len(value['sources'])==len(expected),'GOVERNANCE_MISMATCH')
    for s in value['sources']:
        p.fields(s,'sourceId status blockers');p.require(s['sourceId'] in expected and s['sourceId'] not in seen,'GOVERNANCE_MISMATCH');seen.add(s['sourceId'])
        p.require(s['status'] in ('pending-review','included','excluded','blocked'),'GOVERNANCE_MISMATCH')
        p.require(type(s['blockers']) is list and len(s['blockers'])<=6 and all(type(b) is str for b in s['blockers']) and len(s['blockers'])==len(set(s['blockers'])),'GOVERNANCE_MISMATCH')
        p.require(all(b in ('SOURCE_UNVERIFIED','SOURCE_MISSING','READER_UNAVAILABLE','OBJECT_UNAVAILABLE','QUALITY_REJECTED','LEGACY_PROVENANCE_INCOMPLETE') for b in s['blockers']),'GOVERNANCE_MISMATCH')
    return value


class DatasetClient:
    def __init__(self,identity_provider,timeout=25,opener=None):
        self.identity_provider=identity_provider;self.timeout=timeout
        self.opener=opener or urllib.request.build_opener(NoRedirect())

    def check(self,expected,continuing=lambda:True):
        if not continuing():raise DatasetFailure('UPLOAD_PAUSED')
        current=self.identity_provider()
        if current is None or current.binding!=expected.binding or current.access_token!=expected.access_token:
            raise DatasetFailure('IDENTITY_CHANGED',401)
        return current

    def call(self,identity,suffix='',method='GET',body=None,headers=None,continuing=lambda:True):
        current=self.check(identity,continuing)
        request=urllib.request.Request(current.binding.origin+ROUTE+suffix,data=body,method=method,headers={'Authorization':'Bearer '+current.access_token,'Content-Type':'application/json',**(headers or {})})
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                raw=response.read(2*1024*1024+1)
                p.require(len(raw)<=2*1024*1024,'RESPONSE_LIMIT');value=strict_json(raw);status=response.status
            self.check(identity,continuing)
            p.require(type(value) is dict and value.get('success') is True and type(value.get('data')) is dict,'RESPONSE')
            return status,value['data']
        except urllib.error.HTTPError as error:
            status=error.code
            delay=retry_seconds(error.headers.get('Retry-After')) if error.headers else 0
            try:
                value=strict_json(error.read(65536));code=value.get('code','HTTP_'+str(status))
                if not isinstance(code,str) or not re.fullmatch('[A-Za-z0-9_]{1,80}',code):code='HTTP_'+str(status)
            except Exception:code='HTTP_'+str(status)
            finally:error.close()
            raise DatasetFailure(code,status,delay) from None
        except (urllib.error.URLError,TimeoutError,OSError):raise DatasetFailure('NETWORK_UNAVAILABLE') from None
        except (TypeError,KeyError,ValueError) as error:raise DatasetFailure('PROTOCOL_'+type(error).__name__) from None

    def receipt(self,identity,data,continuing=lambda:True):
        try:_,value=self.call(identity,'/'+data['clientDatasetId']+'/receipt',continuing=continuing)
        except DatasetFailure as error:
            if error.status==404 and error.code=='DIRECTOR_DATASET_RECEIPT_NOT_FOUND':return None
            # A not-yet-initialized ID may use the generic scoped not-found code.
            if error.status==404 and error.code=='DIRECTOR_DATASET_NOT_FOUND':return None
            raise
        return validate_receipt(value,data)

    def capabilities(self,identity,data,continuing=lambda:True):
        _,value=self.call(identity,'/capabilities',continuing=continuing)
        p.fields(value,'schema enabled rowSchemas featureVersion canonicalization authentication initialGovernanceStatus limits')
        p.require(type(value['enabled']) is bool,'CAPABILITIES')
        if not value['enabled']:raise DatasetFailure('DIRECTOR_DATASET_DISABLED',503)
        p.require(value['schema']==p.SCHEMA and value['featureVersion']=='pov-v1' and value['canonicalization']=='ascii-integer-json-v1' and value['authentication']=='device-entitlement-train' and value['initialGovernanceStatus']=='pending-review','CAPABILITIES')
        p.require(type(value['rowSchemas']) is list and all(type(s) is str for s in value['rowSchemas']) and len(value['rowSchemas'])==len(set(value['rowSchemas'])) and all(s['rowSchema'] in value['rowSchemas'] for s in data['manifest']['sources']),'CAPABILITIES')
        limits=value['limits'];p.fields(limits,' '.join(p.LIMITS))
        for key,hard in p.LIMITS.items():p.integer(limits[key],1,hard)
        p.require(limits['partBytes']==p.PART_BYTES,'CAPABILITIES')
        m=data['manifest'];t=m['totals']
        for total,limit in [('sources','maxSources'),('files','maxFiles'),('rows','maxRows'),('compressedBytes','maxCompressedBytes'),('expandedBytes','maxExpandedBytes')]:
            if t[total]>limits[limit]:raise DatasetFailure('DIRECTOR_DATASET_LIMIT',413)
        if len(p.canonical(m))>limits['maxManifestBytes'] or len(p.canonical(data))>limits['maxInitializeBytes']:raise DatasetFailure('DIRECTOR_DATASET_LIMIT',413)
        for f in m['files']:
            for field,limit in [('bytes','maxFileBytes'),('expandedBytes','maxExpandedFileBytes'),('rows','maxFileRows')]:
                if f[field]>limits[limit]:raise DatasetFailure('DIRECTOR_DATASET_LIMIT',413)
        return value

    def upload(self,identity,data,folder,continuing=lambda:True,progress=lambda done,total:None):
        receipt=self.receipt(identity,data,continuing)
        if receipt is not None:return receipt
        self.capabilities(identity,data,continuing)
        base='/'+data['clientDatasetId']
        try:_,state=self.call(identity,base,continuing=continuing)
        except DatasetFailure as error:
            if error.status!=404 or error.code!='DIRECTOR_DATASET_NOT_FOUND':raise
            _,state=self.call(identity,method='POST',body=p.canonical(data),continuing=continuing)
        present=validate_state(state,data)
        if state['transportStatus']=='received':return self.receipt(identity,data,continuing)
        if state['transportStatus'] in ('expired','cancelled','rejected'):raise DatasetFailure('DIRECTOR_DATASET_'+state['transportStatus'].upper(),410 if state['transportStatus']=='expired' else 409)
        if state['transportStatus']=='verifying':return None
        total=data['manifest']['totals']['compressedBytes'];done=0
        for f in data['manifest']['files']:
            raw=p.read_bounded(p.file_path(folder,f['index']),p.LIMITS['maxFileBytes'])
            p.require(len(raw)==f['bytes'] and p.digest(raw)==f['sha256'],'FILE_CHANGED')
            for index in range(f['partCount']):
                chunk=raw[index*p.PART_BYTES:(index+1)*p.PART_BYTES]
                if index not in present.get(f['index'],set()):
                    checksum=p.digest(chunk)
                    _,ack=self.call(identity,base+'/files/'+str(f['index'])+'/parts/'+str(index),method='PUT',body=chunk,headers={'Content-Type':'application/octet-stream','X-Part-Sha256':checksum},continuing=continuing)
                    p.fields(ack,'schema clientDatasetId fileIndex partIndex bytes sha256')
                    expected=dict(schema=p.SCHEMA,clientDatasetId=data['clientDatasetId'],fileIndex=f['index'],partIndex=index,bytes=len(chunk),sha256=checksum)
                    p.require(ack==expected and all(type(ack[k]) is int for k in ('fileIndex','partIndex','bytes')),'PART_RECEIPT')
                done+=len(chunk);progress(done,total)
        try:status,result=self.call(identity,base+'/complete',method='POST',body=b'{}',continuing=continuing)
        except DatasetFailure as error:
            if error.status and error.status<500:raise
            receipt=self.receipt(identity,data,continuing)
            if receipt is not None:return receipt
            raise
        if status==202:validate_state(result,data);return None
        return validate_receipt(result,data)

    def governance(self,identity,data,receipt,continuing=lambda:True):
        _,value=self.call(identity,'/'+data['clientDatasetId']+'/governance',continuing=continuing)
        return validate_governance(value,data,receipt)

    def cancel(self,identity,data):
        _,state=self.call(identity,'/'+data['clientDatasetId']+'/cancel',method='POST',body=b'{}')
        validate_state(state,data);return state
