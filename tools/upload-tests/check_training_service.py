"""Real PostgreSQL and actual independent worker; isolated localhost only."""
from delivery_test_support import *
import cloud_worker
schema=(folder/'schema.txt').read_text();assert schema.startswith('director6_') and len(schema)==42
psql=Path(os.environ['TEMP'])/'pgsql16/pgsql/bin/psql.exe'
def sql(value):return subprocess.check_output([str(psql),'-h','127.0.0.1','-p','55446','-U','postgres','-d','yuzibei_concurrency_director6','-v','ON_ERROR_STOP=1','-At','-c',f"SET timezone='UTC';SET search_path TO {schema};"+value],text=True)
admin=actors['admin']['token'];worker=cloud_worker.WorkerClient(trust['server_origin'],'local-task912-worker-key-isolated-only')
def train(path,data=None):return client.call('../admin/director-training/'+path,data,admin)
fail(lambda:client.call('../admin/director-training/jobs',token=actors['alpha']['token']),403,'product user cannot control cloud training')
sql("UPDATE director_training_jobs SET status='cancelled',fence=fence+1 WHERE status IN ('queued','running')")
nonce=uuid.uuid4().hex[:8]
selected=[]
for match in range(12):
    rows=[]
    for i in range(32):
        r=row(str(i%2));r['map']='de_qa_'+nonce+'_'+str(match);r['group']='event_'+str(i//4)
        for f in ('aim_self','aim_enemy','win','enemy_low_hp','self_low_hp','proximity','enemy_damaged','hp_advantage'):r[f]=str(.8 if i%2 else .2)
        r['timestamp']=str(1000+match*100+i);rows.append(r)
    source=multipart('admin/director-data/imports',encode_csv(rows),{'source':'offline','requestId':str(uuid.uuid4())},'dataEditor')
    selected.append(source['id']);train('sources/csv/'+source['id']+'/review',{'expectedVersion':0,'state':'ready','matchHash':hashlib.sha256(('match-'+str(match)).encode()).hexdigest(),'note':'Isolated synthetic QA match; both labels; no production-effect evidence'})
check(len(selected)==12,'12 synthetic match sources independently quality-reviewed')
snapshot=train('snapshots',{'name':'QA frozen 12-match input','batchIds':selected,'demoIds':[]});again=train('snapshots',{'name':'same immutable input','batchIds':list(reversed(selected)),'demoIds':[]})
check(snapshot['manifestHash']==again['manifestHash'] and snapshot['id']==again['id'],'snapshot content immutable and selection order independent')
request={'requestId':str(uuid.uuid4()),'snapshotId':snapshot['id'],'parameters':{'trees':10,'maxDepth':4,'seed':42,'maxSeconds':120,'maxMemoryMb':2048},'baselineModelId':None}
job=train('jobs',request);check(train('jobs',request)['id']==job['id'],'job request replay does not duplicate training')
cancelled=train('jobs/'+job['id']+'/cancel',{});check(cancelled['status']=='cancelled','queued job cancellation persists');train('jobs/'+job['id']+'/retry',{})
lease=worker.call('claim',{'workerId':'qa-stale'});check(lease['jobId']==job['id'],'worker claims queued frozen snapshot')
sql(f"UPDATE director_training_jobs SET lease_until=now()-interval '1 minute' WHERE id='{job['id']}'")
new=worker.call('claim',{'workerId':'qa-new'});check(new['fence']>lease['fence'],'expired worker is reclaimed with strictly newer fence')
try:worker.call('jobs/'+job['id']+'/heartbeat',{'fence':lease['fence'],'leaseToken':lease['leaseToken'],'progress':50});raise AssertionError('stale worker')
except urllib.error.HTTPError as error:check(error.code==409,'stale worker heartbeat rejected');error.close()
train('jobs/'+job['id']+'/cancel',{});train('jobs/'+job['id']+'/retry',{})
workroot=folder/'real-worker';workroot.mkdir(exist_ok=True)
check(cloud_worker.run_once(worker,workroot,'qa-real-worker'),'actual worker process executed one task')
result=next(j for j in train('jobs') if j['id']==job['id']);check(result['status']=='succeeded', 'actual worker model and final-test report accepted: '+str(result.get('errorCode')))
model=next(m for m in call('director-models')['models'] if m['id']==result['modelId']);check(model['status']=='candidate','worker output remains candidate with no automatic publish')
report=result['report'];check(report['selectionUsesFinal'] is False and report['partitions']['final_test']>0,'fixed held-out final split excluded from candidate selection')
check(report['executionCoverage']['available'] is False,'classification report does not invent camera coverage benefit')
train('sources/csv/'+selected[0]+'/review',{'expectedVersion':1,'state':'deleted','matchHash':'','note':'QA deletion invalidates dependent snapshot'})
changed=dict(request,requestId=str(uuid.uuid4()));fail(lambda:train('jobs',changed),409,'deleting snapshot input prevents new dependent training')
(folder/'training-checks.json').write_text(json.dumps({'passed':len(checks),'checks':checks,'snapshot':snapshot['id'],'job':job['id'],'report':report},indent=2))
print('TOTAL',len(checks),'training checks passed')
