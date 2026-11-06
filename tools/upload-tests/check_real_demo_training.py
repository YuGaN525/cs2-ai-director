"""Run user-provided real CS2 demos through private intake and the actual cloud worker."""
from delivery_test_support import *
import http.client,cloud_worker,time
source_root=Path('D:/demo');sources=json.loads((repo/'build/task11-real/sources.json').read_text())['sources'][:8];assert len(sources)==8
worker=cloud_worker.WorkerClient(trust['server_origin'],'local-task912-worker-key-isolated-only')
def train(path,data=None):return client.call('../admin/director-training/'+path,data,actors['admin']['token'])
ids=[];started=time.monotonic()
for source in sources:
 path=source_root/source['source_name'];assert path.parent==source_root and path.is_file();boundary='qa_'+uuid.uuid4().hex
 prefix=f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="match.dem"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode();suffix=f'\r\n--{boundary}--\r\n'.encode()
 conn=http.client.HTTPConnection('127.0.0.1',5136,timeout=120);conn.putrequest('POST','/api/admin/director-training/demos');conn.putheader('Authorization','Bearer '+actors['admin']['token']);conn.putheader('Content-Type','multipart/form-data; boundary='+boundary);conn.putheader('Content-Length',str(len(prefix)+path.stat().st_size+len(suffix)));conn.endheaders();conn.send(prefix)
 with path.open('rb') as stream:
  while chunk:=stream.read(65536):conn.send(chunk)
 conn.send(suffix);response=conn.getresponse();raw=response.read();assert response.status==200,(response.status,raw[:300]);d=json.loads(raw)['data'];conn.close();assert d['hash']==source['source_sha256'];ids.append(d['id'])
 train('sources/demo/'+d['id']+'/review',{'expectedVersion':d['version'],'state':'ready','matchHash':source['canonical_series_sha256'],'note':'User-supplied real Demo; conservative series grouping; GSI alignment and programme coverage unverified'})
 print('PASS streamed actual Demo',source['source_name'],flush=True)
snapshot=train('snapshots',{'name':'Real Demo QA 8 independent series','batchIds':[],'demoIds':ids});job=train('jobs',{'requestId':str(uuid.uuid4()),'snapshotId':snapshot['id'],'parameters':{'trees':40,'maxDepth':8,'seed':42,'maxSeconds':300,'maxMemoryMb':2048},'baselineModelId':None})
workroot=folder/'demo-worker';workroot.mkdir(exist_ok=True);assert cloud_worker.run_once(worker,workroot,'qa-real-demo-worker');result=next(j for j in train('jobs') if j['id']==job['id']);assert result['status']=='succeeded',result;model=next(m for m in call('director-models')['models'] if m['id']==result['modelId']);assert model['status']=='candidate';assert result['report']['executionCoverage']['available'] is False
(folder/'real-demo-cloud-checks.json').write_text(json.dumps({'sources':sources,'snapshot':snapshot['id'],'job':result,'elapsed_seconds':time.monotonic()-started,'default_scheduler':'legacy','published':False},indent=2));print('PASS real Demo private intake -> frozen snapshot -> isolated worker -> candidate; no auto-publish',flush=True)
