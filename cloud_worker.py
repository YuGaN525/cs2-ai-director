"""Isolated polling worker. No DB, signing keys, publish or arbitrary code endpoints."""
import argparse,csv,gzip,hashlib,json,os,signal,subprocess,sys,time,urllib.request,urllib.error,uuid
from pathlib import Path
from device_identity import atomic_write,canonical,server_origin
from upload_worker import NoRedirect


class WorkerClient:
    def __init__(self,origin,key,allow_internal=False):self.origin=origin if allow_internal and origin=="http://api:8080" else server_origin(origin);self.key=key;self.opener=urllib.request.build_opener(NoRedirect())
    def call(self,path,data=None):
        req=urllib.request.Request(self.origin+'/api/internal/director-training/'+path,data=canonical(data) if data is not None else None,headers={'X-Director-Worker-Key':self.key,'Content-Type':'application/json'})
        with self.opener.open(req,timeout=30) as response:
            raw=response.read(1048577)
            if len(raw)>1048576:raise ValueError('worker_response_budget')
            return json.loads(raw)['data']
    def download(self,lease,item,path,keepalive=lambda:None):
        route=f"jobs/{lease['jobId']}/inputs/{item['kind']}/{item['id']}?fence={lease['fence']}"
        req=urllib.request.Request(self.origin+'/api/internal/director-training/'+route,headers={'X-Director-Worker-Key':self.key,'X-Director-Job-Lease':lease['leaseToken']})
        limit=512*1024*1024 if item['kind']=='demo' else 32*1024*1024
        with self.opener.open(req,timeout=60) as response,path.open('wb') as output:
            total=0
            while True:
                keepalive()
                chunk=response.read(65536)
                if not chunk:break
                total+=len(chunk)
                if total>limit:raise ValueError('worker_input_budget')
                output.write(chunk)
        with (path.open('rb') if item['kind']=='demo' else gzip.open(path,'rb')) as verified:
            digest=hashlib.file_digest(verified,'sha256').hexdigest()
        if digest!=item['artifactHash']:raise ValueError('worker_input_hash')
    def complete(self,lease,result,model=None):
        boundary='worker_'+uuid.uuid4().hex;body=f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode()+canonical(dict(result,fence=lease['fence'],leaseToken=lease['leaseToken']))+b'\r\n'
        if model is not None:body+=f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="candidate.json.gz"\r\nContent-Type: application/gzip\r\n\r\n'.encode()+Path(model).read_bytes()+b'\r\n'
        body+=f'--{boundary}--\r\n'.encode();req=urllib.request.Request(self.origin+'/api/internal/director-training/jobs/'+lease['jobId']+'/complete',data=body,headers={'X-Director-Worker-Key':self.key,'Content-Type':'multipart/form-data; boundary='+boundary})
        with self.opener.open(req,timeout=90) as response:return json.loads(response.read(1048576))['data']


def train_child(folder):
    spec=json.loads((folder/'spec.json').read_text());budget=spec['request']['parameters'];limit=int(budget['maxMemoryMb'])*1024*1024
    if os.name!='nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(limit,limit));resource.setrlimit(resource.RLIMIT_CPU,(budget['maxSeconds'],budget['maxSeconds']+2))
    from upload_protocol import COLUMNS
    output=folder/'dataset.csv';stats=[];seen=set();count=0
    with output.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=COLUMNS+['match_source_id']);writer.writeheader()
        for item in spec['snapshot']['manifest']['items']:
            path=folder/(item['id']+('.dem' if item['kind']=='demo' else '.csv.gz'))
            if item['kind']=='demo':
                from demo_live_dataset import build_live_csv
                derived=folder/(item['id']+'.csv');stats.append(build_live_csv(path,derived));incoming=derived.open('rt',encoding='utf-8-sig',newline='')
            else:incoming=gzip.open(path,'rt',encoding='utf-8-sig',newline='')
            with incoming:
                for row in csv.DictReader(incoming):
                    row={k:row.get(k,'') for k in COLUMNS};row['match_source_id']=item['matchHash']
                    # The canonical match identity is independent of uploader/account and all windows stay together.
                    key=hashlib.sha256(canonical(row)).hexdigest()
                    if key in seen:continue
                    seen.add(key);writer.writerow(row);count+=1
                    if count>200000:raise ValueError('worker_row_budget')
    from train_decision_model import train_from_csv
    metrics=train_from_csv(output,folder/'candidate.pkl',folder/'metrics.json',min_samples=100,trees=budget['trees'],max_depth=budget['maxDepth'])
    candidate=json.loads((folder/'candidate.submission.json').read_text());candidate['name']='Cloud candidate '+spec['jobId'][:8]
    report={key:metrics[key] for key in ('samples','positive','negative','algorithm','comparison','partitions','split_hash','dataset_hash','auc','top1','final_test','model_json_bytes','model_nodes') if key in metrics}
    report.update(snapshotHash=spec['snapshot']['manifestHash'],parserStats=stats,selectionUsesFinal=False,executionCoverage={'available':False,'reason':'needs full event/scheduler replay with fixed latency; classification AUC is insufficient'},environment={'python':sys.version.split()[0],'core':'0.1.0','threads':1})
    atomic_write(folder/'result.json',canonical({'candidate':candidate,'report':report,'errorCode':None}))


def stop_child(child):
    if child.poll() is None:
        if os.name!='nt':os.killpg(child.pid,signal.SIGTERM)
        else:child.terminate()
        try:child.wait(timeout=5)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)


def run_once(client,root,worker):
    lease=client.call('claim',{'workerId':worker})
    if lease is None:return False
    folder=root/(lease['jobId']+'-'+str(lease['fence']));folder.mkdir(parents=True,exist_ok=False)
    # Lease tokens and worker secrets stay in parent memory, never child files or logs.
    spec={k:lease[k] for k in ('jobId','snapshot','request')};atomic_write(folder/'spec.json',canonical(spec))
    started=time.monotonic();next_beat=started
    def keepalive():
        nonlocal next_beat
        now=time.monotonic()
        if now-started>lease['request']['parameters']['maxSeconds']:raise TimeoutError('training_timeout')
        if now>=next_beat:
            if not client.call('jobs/'+lease['jobId']+'/heartbeat',{'fence':lease['fence'],'leaseToken':lease['leaseToken'],'progress':10}):raise ValueError('lease_or_budget_expired')
            next_beat=now+15
    try:
        for item in lease['snapshot']['manifest']['items']:client.download(lease,item,folder/(item['id']+('.dem' if item['kind']=='demo' else '.csv.gz')),keepalive)
        env={**os.environ,'DIRECTOR_CLOUD_BUDGET':'1','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1'}
        with (folder/'process.log').open('wb') as log:
            child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--child',str(folder)],stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=os.name!='nt',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            from worker_limits import attach
            release_budget=attach(child,lease['request']['parameters']['maxMemoryMb'])
            next_beat=time.monotonic()
            try:
                while child.poll() is None:
                    if time.monotonic()-started>lease['request']['parameters']['maxSeconds']:raise TimeoutError('training_timeout')
                    if time.monotonic()>=next_beat:
                        okay=client.call('jobs/'+lease['jobId']+'/heartbeat',{'fence':lease['fence'],'leaseToken':lease['leaseToken'],'progress':25})
                        if not okay:raise ValueError('lease_or_budget_expired')
                        next_beat=time.monotonic()+15
                    if (folder/'process.log').stat().st_size>4*1024*1024:raise ValueError('log_budget')
                    time.sleep(.25)
                if child.returncode!=0:raise ValueError('training_process_failed')
            finally:
                stop_child(child);release_budget()
        result=json.loads((folder/'result.json').read_text());client.complete(lease,result,folder/'candidate.json.gz');return True
    except Exception as error:
        if isinstance(error,urllib.error.HTTPError) and error.code in (401,403,409):error.close();return True
        try:client.complete(lease,{'candidate':None,'report':{'phase':'failed','errorType':type(error).__name__},'errorCode':'worker_'+type(error).__name__.lower()})
        except Exception:pass
        return True


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path);p.add_argument('--once',action='store_true');p.add_argument('--child',type=Path);a=p.parse_args()
    if a.child is not None:train_child(a.child);return
    if a.config is None:p.error('--config required')
    config=json.loads(a.config.read_text());key=Path(config['worker_key_file']).read_text().strip()
    if len(key)<32:raise ValueError('worker key too short')
    client=WorkerClient(config['server_origin'],key,config.get('allow_internal_api_http',False));root=Path(config['work_root']).resolve();root.mkdir(parents=True,exist_ok=True)
    while True:
        run_once(client,root,config.get('worker_id','director-worker'))
        if a.once:break
        time.sleep(3)
if __name__=='__main__':main()
