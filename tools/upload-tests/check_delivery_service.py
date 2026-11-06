"""Primary real localhost delivery checks; helpers never accept production hosts."""
from delivery_test_support import *

metadata=json.loads((repo/'models/bootstrap/metadata.json').read_text());baseline=(repo/'models/bootstrap/baseline.json.gz').read_bytes()
fail(lambda:multipart('admin/director-models',baseline,{'metadata':json.dumps(metadata)},'alpha'),403,'ordinary account cannot register candidate')
model=multipart('admin/director-models',baseline,{'metadata':json.dumps(metadata)})
check(model['status']=='candidate','existing evaluated RF registered as candidate')
fail(lambda:call('director-models/'+model['id']+'/publish',{'requestId':str(uuid.uuid4()),'expectedSequence':0,'reason':'QA'},'publisher'),409,'unreviewed candidate cannot publish')
fail(lambda:call('director-models/'+model['id']+'/review',{'expectedVersion':0},'publisher'),403,'publisher cannot bypass separate review permission')
reviewed=call('director-models/'+model['id']+'/review',{'expectedVersion':0},'reviewer');check(reviewed['status']=='reviewed','candidate structure vectors and evaluation reviewed')
fail(lambda:call('director-models/'+model['id']+'/publish',{'requestId':str(uuid.uuid4()),'expectedSequence':0,'reason':'QA'},'reviewer'),403,'reviewer cannot publish models')
def publish(id,sequence):return call('director-models/'+id+'/publish',{'requestId':str(uuid.uuid4()),'expectedSequence':sequence,'reason':'Local QA baseline; no competitive benefit claim'},'publisher')
with concurrent.futures.ThreadPoolExecutor(2) as pool:
    futures=[pool.submit(publish,model['id'],0) for _ in range(2)];success=0;conflict=0
    for f in futures:
        try:f.result();success+=1
        except AccountError as e:conflict+=e.status==409
check((success,conflict)==(1,1),'channel publication CAS single winner')
grant('alpha');grant('beta');account=login('desktop-a');account.sync_once();account.check_local()
check(account.allowed(),'first login downloads signed approved model and obtains device lease')
check(account.model.current_hash()==hashlib.sha256(baseline).hexdigest(),'downloaded bytes match official artifact hash')
envelope=account.client.call('models/manifest',token=account.tokens['access_token']);check(account.model_manager.verify(envelope)['release_sequence']==1,'C# manifest signature verifies in Python')
ticket=account.client.call('models/'+model['id']+'/ticket',{},account.tokens['access_token'])
req=urllib.request.Request(trust['server_origin']+ticket['path'],headers={'Authorization':'Bearer '+account.tokens['access_token'],'X-Director-Download-Ticket':ticket['ticket'],'Range':'bytes=0-63'})
with urllib.request.urlopen(req) as response:check(response.status==206 and response.read()==baseline[:64],'authenticated model download supports bounded byte range')

def next_model(revision):
    data=json.loads(gzip.decompress(baseline));data['delivery_revision']=revision;packed=gzip.compress(canonical(data),mtime=0)
    info=dict(metadata,name='QA release '+str(revision),artifactSha256=hashlib.sha256(packed).hexdigest());result=multipart('admin/director-models',packed,{'metadata':json.dumps(info)})
    call('director-models/'+result['id']+'/review',{'expectedVersion':0},'reviewer');return result,packed

second,second_bytes=next_model(2);check(account.acquire_execution(),'director owns model activation barrier')
publish(second['id'],1);old=account.model.current_hash();account.sync_once();check(account.model.current_hash()==old and account.allowed(),'running director keeps immutable approved generation')
account.release_execution();account.sync_once();check(account.allowed() and account.model.current_hash()==hashlib.sha256(second_bytes).hexdigest(),'stopped director activates verified pending model and matching lease')
check(publish(model['id'],2)==3,'rollback creates higher release sequence')
call('director-models/'+second['id']+'/revoke',{'requestId':str(uuid.uuid4()),'expectedSequence':3,'reason':'QA revoked secondary'},'publisher')
account.sync_once();check(account.allowed() and account.model.current_hash()==hashlib.sha256(baseline).hexdigest(),'non-revoked older artifact can be selected by explicit higher-sequence rollback')
fail(lambda:publish(second['id'],4),409,'revoked model cannot be republished')
call('director-models/'+model['id']+'/revoke',{'requestId':str(uuid.uuid4()),'expectedSequence':4,'reason':'QA active revoke'},'publisher');account.sync_once();check(not account.allowed(),'signed active revocation immediately stops local execution')
try:account.model_manager.verify(dict(envelope,signature='invalid'));raise AssertionError('signature')
except ModelError:check(True,'tampered model manifest rejected')
third,third_bytes=next_model(3);publish(third['id'],5);account.sync_once();check(account.allowed(),'new reviewed model restores formal initialization after revocation')

# Durable account-bound scoped upload, followed by a receipt-loss replay.
box=UploadOutbox(run_folder/'outbox.sqlite3');source=str(uuid.uuid4());rng=random.Random(78)
records=[]
for _ in range(1800):
    item=row()
    for feature in ('aim_self','aim_enemy','win','enemy_low_hp','self_low_hp','proximity','enemy_damaged','hp_advantage'):item[feature]=str(rng.random())
    records.append(item)
box.append(account.binding(),'online',source,records);box.prepare_batch(account.binding(),limit=5000);batch=box.claim(account.binding());uploader=TrainingUploadClient()
description=json.loads(batch['descriptor']);start=client.call('data/uploads',description,account.tokens['access_token'])
part=batch['payload'][:262144];url=trust['server_origin']+'/api/director/data/uploads/'+batch['id']+'/parts/0'
request=urllib.request.Request(url,data=part,method='PUT',headers={'Authorization':'Bearer '+account.tokens['access_token'],'X-Part-Sha256':hashlib.sha256(part).hexdigest(),'Content-Type':'application/octet-stream'})
with urllib.request.urlopen(request) as response:assert response.status==200
check(client.call('data/uploads/'+batch['id'],token=account.tokens['access_token'])['parts']==[0],'server retains exact received part progress')
receipt=uploader.upload(account.upload_identity(),batch);box.acknowledge(batch,receipt)
check(receipt['acceptedRows']==1800 and receipt['duplicateRows']==0,'scoped automatic upload commits stable sample identities')
again=uploader.upload(account.upload_identity(),batch);check(again==receipt,'lost final response replays identical immutable receipt')
check(box.stats(account.binding())['states']['confirmed']==1,'scoped receipt durably confirms original local batch')
changed=dict(description,uploadSha256='a'*64);fail(lambda:client.call('data/uploads',changed,account.tokens['access_token']),409,'batch identity cannot be reused with changed hash')
other=login('desktop-b','beta');fail(lambda:client.call('data/uploads/'+batch['id'],token=other.tokens['access_token']),404,'cross-account upload status is isolated')
manual=multipart('admin/director-data/imports',(repo/'contracts/director-data-v1/valid-minimal.csv').read_bytes(),{'source':'offline','requestId':str(uuid.uuid4())},'dataEditor')
check(manual['rowCount']==1,'manual history import shares validated private intake')
fail(lambda:multipart('admin/director-data/imports',b'broken',{'source':'offline'},'alpha'),403,'ordinary account cannot use admin manual intake')
(folder/'delivery-checks.json').write_text(json.dumps({'passed':len(checks),'checks':checks,'model_id':third['id'],'model_sha256':hashlib.sha256(third_bytes).hexdigest(),'batch_id':batch['id']},indent=2))
(folder/'approved-model.json.gz').write_bytes(third_bytes)
print('TOTAL',len(checks),'delivery checks passed')
