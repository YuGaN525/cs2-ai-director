from delivery_test_support import *
import sqlite3,copy
from upload_protocol import parse_csv
from model_runtime import ModelError
summary=json.loads((folder/'delivery-checks.json').read_text());db=sqlite3.connect(run_folder/'outbox.sqlite3');db.row_factory=sqlite3.Row;batch=dict(db.execute('select * from batches where id=?',(summary['batch_id'],)).fetchone());db.close()
account=DesktopAccount(run_folder/'desktop-a',trust['server_origin'],keys,run_folder/'desktop-a/model.json.gz',model_keys=model_keys);account.sync_once();check(account.allowed(),'restart restores signed model and DPAPI lease')
uploader=TrainingUploadClient();identity=account.upload_identity();payload_rows=parse_csv(batch['payload'],True);description=json.loads(batch['descriptor'])
def subset(indices,change=False):
    value=dict(batch,id=str(uuid.uuid4()));rows=[dict(payload_rows[i]) for i in indices];samples=[dict(description['samples'][i]) for i in indices]
    if change:rows[0]['aim_self']='0.1234567'
    packed=gzip.compress(encode_csv(rows),mtime=0);value.update(payload=packed,upload_hash=hashlib.sha256(packed).hexdigest(),row_count=len(rows),positive_count=sum(r['label']=='1' for r in rows))
    for sample,r in zip(samples,rows):sample['rowSha256']=hashlib.sha256(encode_csv([r]).split(b'\n',1)[1]).hexdigest()
    d=dict(description,batchId=value['id'],uploadSha256=value['upload_hash'],uploadBytes=len(packed),partCount=(len(packed)+262143)//262144,samples=samples);value['descriptor']=json.dumps(d,sort_keys=True,separators=(',',':'));return value
resegmented=subset([0,1,2]);receipt=uploader.upload(identity,resegmented);check(receipt['acceptedRows']==0 and receipt['duplicateRows']==3,'resegmentation preserves sample identities and avoids duplicate samples')
fail(lambda:uploader.upload(identity,subset([0,1],True)),409,'same source/sequence with changed row is rejected')
request=json.loads(subset([4,5])['descriptor']);state=client.call('data/uploads',request,identity.access_token)
fail(lambda:client.call('data/uploads/'+request['batchId']+'/complete',{},identity.access_token),409,'incomplete upload cannot be confirmed')
other=login('same-account-other-device');other.sync_once();fail(lambda:client.call('data/uploads/'+batch['id'],token=other.tokens['access_token']),404,'different device of same account cannot query original upload')
schema=(folder/'schema.txt').read_text();assert schema.startswith('director6_') and len(schema)==42
psql=Path(os.environ['TEMP'])/'pgsql16/pgsql/bin/psql.exe'
def sql(q):return subprocess.check_output([str(psql),'-h','127.0.0.1','-p','55446','-U','postgres','-d','yuzibei_concurrency_director6','-At','-v','ON_ERROR_STOP=1','-c',f"SET timezone='UTC';SET search_path TO {schema};"+q],text=True)
original=json.loads(batch['receipt']);data_id=original['dataBatchId'];entry=sql(f"SELECT owner_id||'/'||source||'/'||content_hash FROM director_data_batches WHERE id='{data_id}'").strip().splitlines()[-1].split('/')
path=folder/'objects'/entry[0].replace('-','')/entry[1]/(entry[2]+'.csv.gz');before=path.read_bytes();path.write_bytes(b'corrupt')
fail(lambda:client.call('data/uploads/'+batch['id'],token=identity.access_token),503,'receipt query refuses missing or corrupted confirmed data')
repaired=uploader.upload(identity,batch);check(repaired==original and gzip.decompress(path.read_bytes())==gzip.decompress(before),'immutable parts repair storage before returning original receipt')
admin_blob=(repo/'contracts/director-data-v1/valid-minimal.csv').read_bytes();rid=str(uuid.uuid4())
a=multipart('admin/director-data/imports',admin_blob,{'source':'offline','requestId':rid},'dataEditor');b=multipart('admin/director-data/imports',admin_blob,{'source':'offline','requestId':rid},'dataEditor');check(a['id']==b['id'] and b['duplicate'],'manual retry uses stable request identity')
fail(lambda:multipart('admin/director-data/imports',admin_blob+b'\n',{'source':'offline','requestId':rid},'dataEditor'),409,'manual reused request with different bytes conflicts')
# Releasing to one account never changes public stable pointer or other accounts.
baseline=(repo/'models/bootstrap/baseline.json.gz').read_bytes();data=json.loads(gzip.decompress(baseline));data['qa_canary']=uuid.uuid4().hex;packed=gzip.compress(canonical(data),mtime=0);metadata=json.loads((repo/'models/bootstrap/metadata.json').read_text());metadata.update(name='QA account canary',artifactSha256=hashlib.sha256(packed).hexdigest());model=multipart('admin/director-models',packed,{'metadata':json.dumps(metadata)});call('director-models/'+model['id']+'/review',{'expectedVersion':0},'reviewer')
head=call('director-models');body={'expectedVersion':0,'modelId':model['id'],'reason':'QA one-account canary','requestId':str(uuid.uuid4())};r=call('director-models/rollouts/'+actors['alpha']['id'],body,'publisher');check(r['modelId']==model['id'],'publisher can scope reviewed candidate to one account')
account.sync_once();check(account.allowed() and account.model_manager.active_metadata['model_id']==model['id'],'target device receives signed account-canary catalogue and lease')
beta=DesktopAccount(run_folder/'desktop-b',trust['server_origin'],keys,run_folder/'desktop-b/model.json.gz',model_keys=model_keys);beta.sync_once();check(beta.model_manager.active_metadata['model_id']==head['modelId'],'other account remains on stable model')
fail(lambda:beta.client.call('models/'+model['id']+'/ticket',{},beta.tokens['access_token']),404,'other account cannot download unpublished canary')
call('director-models/rollouts/'+actors['alpha']['id'],{'expectedVersion':r['version'],'modelId':None,'reason':'QA end canary','requestId':str(uuid.uuid4())},'publisher');account.sync_once();check(account.allowed() and account.model_manager.active_metadata['model_id']==head['modelId'],'ending canary restores stable via newer signed catalogue')
apps=call('director-models/applications','' if False else None,'publisher');check(any(a['deviceId']==account.tokens['device_id'] and a['lastValidatedAt'] for a in apps),'actual device model reports show validation timestamp')
fail(lambda:client.call('../admin/director-models',token=account.tokens['access_token']),401,'device bearer has no website admin authentication')
(folder/'fault-checks.json').write_text(json.dumps({'passed':len(checks),'checks':checks},indent=2));print('TOTAL',len(checks),'fault and canary checks passed')
