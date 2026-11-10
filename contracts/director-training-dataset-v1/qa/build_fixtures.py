"""Rebuild synthetic fixtures/schemas only. No user data or network access."""
import copy
import gzip
import json
from pathlib import Path
import sys
import uuid
from contract import BASE,EXTRA,FEATURES,LABEL,MISSING,LIMITS,HEX,UUID,SCHEMA,ROOT,canonical,csv_bytes,digest,decimal_text
from decimal import Decimal


def write(name,value):
    path=ROOT/name;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(value if isinstance(value,bytes) else (json.dumps(value,indent=2,ensure_ascii=True)+'\n').encode('ascii'))


def obj(properties):return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)
def integer(low=0,high=9007199254740991):return dict(type='integer',minimum=low,maximum=high)
def string(regex=None,enum=None):
    v=dict(type='string')
    if regex is not None:v['pattern']='^'+regex+'$'
    if enum is not None:v['enum']=enum
    return v
def nullable(v):return dict(anyOf=[v,dict(type='null')])
def array(v,low=1,high=1024):return dict(type='array',items=v,minItems=low,maxItems=high)
def const(v):return dict(const=v)
def schema(name,value):write(name+'.schema.json',{'$schema':'https://json-schema.org/draft/2020-12/schema',**value})


def make_schemas():
    uid=string(UUID);sha=string(HEX);utc=string(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
    sampling=obj(dict(method=const('periodic-live'),stepTicks=integer(1,1000000),horizonTicks=integer(1,1000000),tickrateAssumption=const(64),labelDefinition=const(LABEL),missingFields=const(MISSING)))
    source=obj(dict(sourceId=uid,kind=string(enum=['demo-derived','legacy-offline','legacy-selected-only']),rowSchema=string(enum=['demo-live36-v1','legacy26-v1']),rawDemoSha256=nullable(sha),recordingId=sha,declaredMatchGroup=nullable(sha),identityQuality=string(enum=['recording-only','filename-inferred','user-declared','unknown']),parserVersion=nullable(const('demo-v2-live.1')),sampling=nullable(sampling),rows=integer(1,200000),positiveRows=integer(0,200000)))
    source['allOf']=[{'if':{'properties':{'kind':const('demo-derived')}},'then':{'properties':dict(rowSchema=const('demo-live36-v1'),rawDemoSha256=sha,parserVersion=const('demo-v2-live.1'),sampling=sampling,identityQuality=string(enum=['recording-only','filename-inferred','user-declared']))},'else':{'properties':dict(rowSchema=const('legacy26-v1'),rawDemoSha256=const(None),parserVersion=const(None),sampling=const(None),identityQuality=const('unknown'))}}]
    file=obj(dict(index=integer(0,1023),sourceId=uid,sha256=sha,contentSha256=sha,bytes=integer(1,8388608),expandedBytes=integer(1,33554432),rows=integer(1,50000),positiveRows=integer(0,50000),partCount=integer(1,32)))
    totals=obj(dict(sources=integer(1,256),files=integer(1,1024),rows=integer(1,200000),positiveRows=integer(0,200000),compressedBytes=integer(1,134217728),expandedBytes=integer(1,536870912)))
    manifest=obj(dict(format=const(SCHEMA),version=const(1),producerVersion=string(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'),featureVersion=const('pov-v1'),sources=array(source,1,256),files=array(file),totals=totals))
    init=obj(dict(schema=const(SCHEMA),clientDatasetId=uid,runId=uid,manifestSha256=sha,manifest=manifest));schema('Initialize',init)
    schema('Manifest',manifest)
    caps=obj(dict(schema=const(SCHEMA),enabled=dict(type='boolean'),rowSchemas=array(string(enum=['demo-live36-v1','legacy26-v1']),2,2),featureVersion=const('pov-v1'),canonicalization=const('ascii-integer-json-v1'),authentication=const('device-entitlement-train'),initialGovernanceStatus=const('pending-review'),limits=obj({k:integer(1,v) for k,v in LIMITS.items()})))
    caps['properties']['limits']['properties']['partBytes']=const(262144)
    schema('Capabilities',caps)
    receipt=obj(dict(schema=const(SCHEMA),clientDatasetId=uid,datasetId=uid,manifestSha256=sha,requestHash=sha,receivedFiles=integer(1,1024),receivedRows=integer(1,200000),acceptedRows=integer(0,200000),duplicateRows=integer(0,200000),receivedAt=utc,initialGovernanceStatus=const('pending-review')));schema('Receipt',receipt)
    part=obj(dict(schema=const(SCHEMA),clientDatasetId=uid,fileIndex=integer(0,1023),partIndex=integer(0,31),bytes=integer(1,262144),sha256=sha));schema('PartReceipt',part)
    transport=obj(dict(schema=const(SCHEMA),clientDatasetId=uid,manifestSha256=sha,requestHash=sha,transportStatus=string(enum=['receiving','verifying','received','cancelled','expired','rejected']),expiresAt=utc,receivedParts=array(obj(dict(fileIndex=integer(0,1023),parts=array(integer(0,31),0,32))),0,1024),hasReceipt=dict(type='boolean')));schema('TransportState',transport)
    blocker=string(enum=['SOURCE_UNVERIFIED','SOURCE_MISSING','READER_UNAVAILABLE','OBJECT_UNAVAILABLE','QUALITY_REJECTED','LEGACY_PROVENANCE_INCOMPLETE'])
    governance=obj(dict(schema=const(SCHEMA),clientDatasetId=uid,datasetId=uid,revision=integer(1),governanceStatus=string(enum=['pending-review','partially-included','included','excluded','blocked']),sources=array(obj(dict(sourceId=uid,status=string(enum=['pending-review','included','excluded','blocked']),blockers=array(blocker,0,6))),1,256)));schema('GovernanceState',governance)
    schema('EmptyCommand',obj({}))
    error=obj(dict(success=const(False),code=string(r'[A-Z][A-Z0-9_]{0,79}'),message=dict(type='string',maxLength=500),data=const(None)));schema('Error',error)
    write('capabilities.example.json',dict(schema=SCHEMA,enabled=False,rowSchemas=['demo-live36-v1','legacy26-v1'],featureVersion='pov-v1',canonicalization='ascii-integer-json-v1',authentication='device-entitlement-train',initialGovernanceStatus='pending-review',limits=LIMITS))
    write('columns.json',dict(featureVersion='pov-v1',features=FEATURES,schemas={'demo-live36-v1':BASE+EXTRA,'legacy26-v1':BASE}))
    responses={200:{'description':'Success envelope; data uses the indicated schema'},400:{'description':'Malformed contract'},401:{'description':'Device authentication needed'},403:{'description':'Entitlement/train permission denied'},404:{'description':'Out of scope or not found'},409:{'description':'Identity/content/state conflict'},413:{'description':'Size limit; proxy may return non-JSON'},422:{'description':'Invalid dataset content'},429:{'description':'Quota/concurrency; honor Retry-After'},503:{'description':'Feature disabled or storage unavailable'}}
    paths={}
    base='/api/director/training/datasets'
    routes=[('get','/capabilities',None,'Capabilities'),('post','', 'Initialize','TransportState'),('get','/{clientDatasetId}',None,'TransportState'),('put','/{clientDatasetId}/files/{fileIndex}/parts/{partIndex}','binary','PartReceipt'),('post','/{clientDatasetId}/complete','EmptyCommand','Receipt'),('get','/{clientDatasetId}/receipt',None,'Receipt'),('post','/{clientDatasetId}/cancel','EmptyCommand','TransportState'),('get','/{clientDatasetId}/governance',None,'GovernanceState')]
    for method,path,request,response in routes:
        operation=dict(operationId=method+'_'+(path or 'initialize').replace('/','_').replace('{','').replace('}',''),security=[{'deviceToken':[]}],responses=copy.deepcopy(responses),description='Contract only; not an enabled endpoint. Success data: '+response+'.schema.json')
        envelope=obj(dict(success=const(True),code=const('OK'),message=dict(type='string'),data={'$ref':response+'.schema.json'}))
        operation['responses'][200]['content']={'application/json':{'schema':envelope}}
        if path=='':operation['responses'][201]=copy.deepcopy(operation['responses'][200])
        if path.endswith('/complete'):operation['responses'][202]={'description':'Still verifying; query state/receipt','content':{'application/json':{'schema':obj(dict(success=const(True),code=const('OK'),message=dict(type='string'),data={'$ref':'TransportState.schema.json'}))}}}
        parameters=[]
        for name,s in [('clientDatasetId',uid),('fileIndex',integer(0,1023)),('partIndex',integer(0,31))]:
            if '{'+name+'}' in path:parameters.append(dict(name=name,**{'in':'path'},required=True,schema=s))
        if request=='binary':parameters.append(dict(name='X-Part-Sha256',**{'in':'header'},required=True,schema=sha))
        if parameters:operation['parameters']=parameters
        if request:operation['requestBody']={'required':True,'content':{'application/octet-stream' if request=='binary' else 'application/json':{'schema':{'type':'string','format':'binary'} if request=='binary' else {'$ref':request+'.schema.json'}}}}
        paths.setdefault(base+path,{})[method]=operation
    write('openapi.json',dict(openapi='3.1.0',info=dict(title='Director training dataset offline contract',version='1.0.0'),paths=paths,components=dict(securitySchemes=dict(deviceToken=dict(type='http',scheme='bearer',bearerFormat='d1 device token')))))


def uid(n):return str(uuid.UUID(int=n))
def rows_for(recording,group,tick=128,legacy=False):
    out=[]
    for slot in range(2):
        r={k:'' for k in BASE+(EXTRA if not legacy else [])}
        r.update(timestamp='0' if legacy else decimal_text(Decimal(tick)/64),map='de_dust2',round='1',action='hold' if legacy else 'demo',target_slot='' if legacy else str(slot+1),group='g_'+digest(('candidate-group-v1:'+recording+':'+str(group)).encode()),group_size='1' if legacy else '2',label=str(slot),rule_score='0.5')
        r.update({k:'0.5' for k in FEATURES})
        if not legacy:r.update(match_source_id=recording,source_tick=str(tick),label_horizon_ticks='192',sampling_weight='1',missing_fields=MISSING,parser_version='demo-v2-live.1',sampling_method='periodic-live',sample_step_ticks='128',tickrate_assumption='64',label_definition=LABEL,label_damage=str(slot))
        out.append(r)
    return out


def source(n,legacy=False,group=1):
    recording=digest(('synthetic-demo-'+str(n)).encode()) if not legacy else digest(('legacy-recording-v1:'+digest(b'synthetic private-free legacy input')).encode())
    return dict(sourceId=uid(n+100),kind='legacy-selected-only' if legacy else 'demo-derived',rowSchema='legacy26-v1' if legacy else 'demo-live36-v1',rawDemoSha256=None if legacy else recording,recordingId=recording,declaredMatchGroup=None if legacy else digest(('declared-synthetic-bo-'+str(group)).encode()),identityQuality='unknown' if legacy else 'user-declared',parserVersion=None if legacy else 'demo-v2-live.1',sampling=None if legacy else dict(method='periodic-live',stepTicks=128,horizonTicks=192,tickrateAssumption=64,labelDefinition=LABEL,missingFields=MISSING),rows=2,positiveRows=1)


def package(specs,run=1):
    sources=[];files=[];payloads=[]
    for i,(s,rows) in enumerate(specs):
        columns=BASE+(EXTRA if s['rowSchema']=='demo-live36-v1' else [])
        raw=csv_bytes(rows,columns);compressed=gzip.compress(raw,compresslevel=6,mtime=0)
        s=copy.deepcopy(s);s['rows']=len(rows);s['positiveRows']=sum(r['label']=='1' for r in rows);sources.append(s)
        files.append(dict(index=i,sourceId=s['sourceId'],sha256=digest(compressed),contentSha256=digest(raw),bytes=len(compressed),expandedBytes=len(raw),rows=len(rows),positiveRows=s['positiveRows'],partCount=(len(compressed)+262143)//262144));payloads.append(compressed)
    m=dict(format=SCHEMA,version=1,producerVersion='1.15.4',featureVersion='pov-v1',sources=sources,files=files,totals=dict(sources=len(sources),files=len(files),rows=sum(f['rows'] for f in files),positiveRows=sum(f['positiveRows'] for f in files),compressedBytes=sum(f['bytes'] for f in files),expandedBytes=sum(f['expandedBytes'] for f in files)))
    return dict(schema=SCHEMA,clientDatasetId=uid(run),runId=uid(run+10000),manifestSha256=digest(canonical(m)),manifest=m),payloads


def make_fixtures():
    cases=[]
    def save(name,data,payloads,expected=None,raw=None):
        if raw is None:data['manifestSha256']=digest(canonical(data['manifest']));raw=canonical(data)
        write('samples/'+name+'.initialize.json',raw)
        names=[]
        for i,b in enumerate(payloads):
            p='samples/'+name+'.'+str(i)+'.csv.gz';write(p,b);names.append(p)
        case=dict(id=name,initialize='samples/'+name+'.initialize.json',files=names,valid=expected is None,error=expected)
        if expected is None:case.update(manifestSha256=data['manifestSha256'],requestHash=digest(canonical(data)),initialGovernanceStatus='pending-review')
        cases.append(case)
    s=source(1);minimal,parts=package([(s,rows_for(s['recordingId'],1))]);save('minimal',copy.deepcopy(minimal),parts)
    multi=[]
    for n in range(1,4):
        s=source(n,group=1 if n<3 else 2);multi.append((s,rows_for(s['recordingId'],n)))
    data,p=package(multi,2);save('multi-bo',data,p)
    s=source(10,True);data,p=package([(s,rows_for(s['recordingId'],10,legacy=True))],3);save('legacy',data,p)
    save('same-request-reordered',copy.deepcopy(minimal),parts,raw=json.dumps(minimal,indent=4,sort_keys=True).encode())
    for name,fn,error in [
        ('wrong-schema',lambda d:d.update(schema='director-candidate-v2'),'SCHEMA'),
        ('extra-field',lambda d:d.update(accountId='123'),'FIELDS'),
        ('nil-id',lambda d:d.update(clientDatasetId=uid(0)),'STRING'),
        ('bad-file-index',lambda d:d['manifest']['files'][0].update(index=1),'INTEGER'),
        ('bad-part-count',lambda d:d['manifest']['files'][0].update(partCount=2),'PART_COUNT'),
        ('wrong-total',lambda d:d['manifest']['totals'].update(rows=3),'TOTALS'),
        ('source-without-file',lambda d:(d['manifest']['sources'].append(source(9)),d['manifest']['totals'].update(sources=2)),'SOURCE_TOTALS'),
        ('unsafe-source',lambda d:d['manifest']['sources'][0].update(identityQuality='verified'),'SOURCE_IDENTITY'),
        ('wrong-source-hash',lambda d:d['manifest']['sources'][0].update(recordingId='0'*64),'SOURCE_IDENTITY'),
        ('row-budget',lambda d:d['manifest']['totals'].update(rows=200001),'INTEGER')]:
        d=copy.deepcopy(minimal);fn(d);save(name,d,parts,error)
    d=copy.deepcopy(minimal);d['manifestSha256']='0'*64;save('manifest-hash',d,parts,'MANIFEST_HASH',raw=canonical(d))
    original=canonical(minimal)
    save('duplicate-key',minimal,parts,'DUPLICATE_KEY',raw=original.replace(b'{',b'{"schema":"director-training-dataset-v1",',1))
    save('fractional-json',minimal,parts,'INTEGER_TOKEN',raw=original.replace(b'"version":1',b'"version":1.0'))
    save('non-ascii-json',minimal,parts,'ASCII',raw=original.replace(b'"producerVersion":"1.15.4"',b'"producerVersion":"\\u4e2d"'))
    save('corrupt-file-hash',copy.deepcopy(minimal),[parts[0][:-1]+bytes([parts[0][-1]^1])],'FILE_HASH')
    for name,fn,error in [
        ('nan-feature',lambda rows:rows[0].update(aim_self='NaN'),'CSV_NUMBER'),
        ('missing-group',lambda rows:rows[0].update(group=''),'STRING'),
        ('bad-label',lambda rows:rows[0].update(label='2'),'CSV_INTEGER'),
        ('wrong-tick-time',lambda rows:rows[0].update(timestamp='3'),'CSV_TIME'),
        ('duplicate-target',lambda rows:rows[1].update(target_slot='1'),'GROUP_TARGET'),
        ('partial-candidate-group',lambda rows:rows.pop(),'GROUP_SIZE')]:
        s=source(1);r=rows_for(s['recordingId'],1);fn(r);d,p=package([(s,r)]);save(name,d,p,error)
    # Valid framing and metadata hashes, but invalid object types / CSV layout.
    for name,raw,error in [('model-misroute',b'{"trees":[]}\n','CSV_COLUMNS'),('private-column',b'target_name\nsynthetic-player\n','CSV_COLUMNS')]:
        d=copy.deepcopy(minimal);p=[gzip.compress(raw,mtime=0)];f=d['manifest']['files'][0];f.update(sha256=digest(p[0]),contentSha256=digest(raw),bytes=len(p[0]),expandedBytes=len(raw));d['manifest']['totals'].update(compressedBytes=len(p[0]),expandedBytes=len(raw));save(name,d,p,error)
    d=copy.deepcopy(minimal);p=[parts[0]+parts[0]];d['manifest']['files'][0].update(sha256=digest(p[0]),bytes=len(p[0]));d['manifest']['totals']['compressedBytes']=len(p[0]);save('concatenated-gzip',d,p,'GZIP')
    d=copy.deepcopy(minimal);p=[parts[0][:-4]];d['manifest']['files'][0].update(sha256=digest(p[0]),bytes=len(p[0]));d['manifest']['totals']['compressedBytes']=len(p[0]);save('truncated-gzip',d,p,'GZIP')
    d=copy.deepcopy(minimal);expanded=b'a'*(33554432+1);p=[gzip.compress(expanded,mtime=0)];d['manifest']['files'][0].update(sha256=digest(p[0]),bytes=len(p[0]),contentSha256=digest(expanded),expandedBytes=33554432);d['manifest']['totals'].update(compressedBytes=len(p[0]),expandedBytes=33554432);save('expanded-budget',d,p,'EXPANDED_LIMIT')
    write('fixtures.json',cases)
    vectors=[]
    for name,raw in [('ordered',b'{"z":2,"a":{"b":true,"a":null},"s":"a/b<>&"}'),('escaped',b'{"quote":"\\\"","slash":"\\\\"}'),('safe-integer',b'{"n":9007199254740991}'),('reordered',b' { "a": 1, "b": [0,2] } '),('duplicate',b'{"a":1,"a":2}'),('negative-zero',b'{"a":-0}'),('exponent',b'{"a":1e0}'),('fraction',b'{"a":1.0}'),('unsafe-integer',b'{"a":9007199254740992}'),('unicode',b'{"a":"\\u4e2d"}'),('control',b'{"a":"\\n"}'),('nan',b'{"a":NaN}'),('bom',b'\xef\xbb\xbf{}')]:
        from contract import loads,Invalid
        p='samples/canonical-'+name+'.json';write(p,raw);v=dict(id=name,path=p)
        try:c=canonical(loads(raw));v.update(valid=True,canonical=c.decode(),sha256=digest(c))
        except Invalid as e:v.update(valid=False,error=str(e))
        vectors.append(v)
    write('canonical-vectors.json',vectors)
    receipt=dict(schema=SCHEMA,clientDatasetId=minimal['clientDatasetId'],datasetId=uid(500),manifestSha256=minimal['manifestSha256'],requestHash=digest(canonical(minimal)),receivedFiles=1,receivedRows=2,acceptedRows=2,duplicateRows=0,receivedAt='2026-11-10T00:00:00Z',initialGovernanceStatus='pending-review')
    write('receipt.example.json',receipt)
    duplicate=copy.deepcopy(receipt);duplicate.update(clientDatasetId=uid(4),datasetId=uid(501),acceptedRows=0,duplicateRows=2)
    duplicate_init=copy.deepcopy(minimal);duplicate_init['clientDatasetId']=uid(4);duplicate_init['runId']=uid(10004);duplicate['requestHash']=digest(canonical(duplicate_init));save('same-content-new-command',duplicate_init,parts)
    conflict_init=copy.deepcopy(minimal);conflict_init['runId']=uid(10005);save('same-id-different-request',conflict_init,parts)
    write('duplicate-receipt.example.json',duplicate);write('fixtures.json',cases)
    write('governance.example.json',dict(schema=SCHEMA,clientDatasetId=minimal['clientDatasetId'],datasetId=uid(500),revision=1,governanceStatus='pending-review',sources=[dict(sourceId=minimal['manifest']['sources'][0]['sourceId'],status='pending-review',blockers=['SOURCE_UNVERIFIED'])]))
    write('transport.example.json',dict(schema=SCHEMA,clientDatasetId=minimal['clientDatasetId'],manifestSha256=minimal['manifestSha256'],requestHash=digest(canonical(minimal)),transportStatus='receiving',expiresAt='2026-11-17T00:00:00Z',receivedParts=[],hasReceipt=False))
    write('idempotency-vectors.json',[
        dict(first='minimal',next='same-request-reordered',scope='same-account-device-origin',expected='same-session-and-receipt'),
        dict(first='minimal',next='same-content-new-command',scope='same-account-device-origin',expected='new-pending-review-receipt-object-reuse-only'),
        dict(first='minimal',next='same-id-different-request',scope='same-account-device-origin',expected='409-DIRECTOR_DATASET_CONFLICT'),
        dict(first='minimal',next='minimal',scope='different-account-device-origin',expected='different-ownership-scope-no-receipt-disclosure')])


def main():
    make_schemas();make_fixtures()
    codes=[(400,'DIRECTOR_DATASET_INVALID','Fix malformed request; never blind retry'),(401,'DIRECTOR_DEVICE_UNAUTHORIZED','Restore device authentication'),(403,'DIRECTOR_NO_ENTITLEMENT','Restore product entitlement'),(403,'FORBIDDEN','Current train permission required only on dataset endpoint'),(404,'DIRECTOR_DATASET_NOT_FOUND','Missing or outside current identity scope'),(404,'DIRECTOR_DATASET_RECEIPT_NOT_FOUND','Query transport; keep original ID and payload'),(409,'DIRECTOR_DATASET_CONFLICT','Same ID with different request or part content'),(409,'DIRECTOR_DATASET_INCOMPLETE','Resume missing parts'),(409,'DIRECTOR_DATASET_STATE','Terminal/received state cannot be changed by cancel/upload'),(410,'DIRECTOR_DATASET_EXPIRED','Keep local original; create new explicit attempt when appropriate'),(413,'DIRECTOR_DATASET_LIMIT','Honor negotiated and hard limits; proxy may return non-JSON'),(422,'DIRECTOR_DATASET_CONTENT_INVALID','Fix invalid content; preserve diagnostic and original'),(429,'DIRECTOR_DATASET_QUOTA','Honor Retry-After; do not duplicate reservation'),(503,'DIRECTOR_DATASET_DISABLED','Feature unavailable; never send samples to model endpoint'),(503,'DIRECTOR_DATASET_STORAGE_UNAVAILABLE','Query original operation before retry')]
    write('errors.json',[dict(http=h,code=c,handling=t) for h,c,t in codes])
    write('actual-size-evidence.json',dict(scope='Read-only historical derived files; no upload or real sample export',sourceDirectory='D:/Yuzibei/local-data/adw-opt-20261108/retrain-real-20261108',sources=41,files=154,rows=167517,positiveRows=12483,compressedBytes=8300611,expandedBytes=69501209,maxFileBytes=65188,maxExpandedFileBytes=524705,maxFileRows=1285,legacyProjectedDatasetBytes=47413715,containsPrivateSampleRows=False))
    print('Synthetic schemas and fixtures generated. Run verify.py, then refresh manifest.json after final QA script changes.')


if __name__=='__main__':main()
