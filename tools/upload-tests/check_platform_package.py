"""Real packaged online/offline/reconnect check. Explicit localhost QA profile only."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from account_client import DesktopAccount,AccountClient
from device_identity import unb64
from upload_outbox import UploadOutbox
p=argparse.ArgumentParser();p.add_argument('mode',choices=['online','offline','resume']);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args()
repo=Path(__file__).resolve().parents[2];folder=a.evidence;trust=json.loads((folder/'trust.json').read_text());assert trust['server_origin']=='http://127.0.0.1:5136'
profile=repo/'build/task912/package-profile';os.environ['APPDATA']=str(profile);root=profile/'CS2AIDirector';root.mkdir(parents=True,exist_ok=True)
exe=repo/'dist/task912-qa/Yuzi_CS2_AIDirector.exe';assert exe.is_file()
account=DesktopAccount(root/'identity',trust['server_origin'],{k:unb64(v) for k,v in trust['trusted_public_keys'].items()},root/'verified-model.json.gz',model_keys={k:unb64(v) for k,v in trust['trusted_model_keys'].items()})
def diagnostic(name):
 report=folder/('package-'+name+'.json');subprocess.run([str(exe),'--account-check','--report',str(report)],check=True,timeout=60);r=json.loads(report.read_text());assert r['execution_allowed'] and r['logged_in'],r;return r
def train():
 subprocess.run([str(exe),'--trainer','--cli','--csv',str(repo/'build/merge-smoke/decisions.csv'),'--out',str(repo/'build/task912/package-candidate.pkl'),'--metrics',str(repo/'build/task912/package-metrics.json'),'--min-samples','100','--trees','10','--max-depth','3'],check=True,timeout=180)
if a.mode=='online':
 actors=json.loads((folder/'actors.json').read_text());pending=account.begin_login(open_browser=False);AccountClient(trust['server_origin']).call('device-auth/approve',{'userCode':pending['user_code'],'approve':True},actors['alpha']['token']);assert account.complete_login();account.sync_once();account.check_local();assert account.allowed();r=diagnostic('online');assert r['binding']['account_id']==actors['alpha']['id'];print('PASS packaged first initialization uses bundled public keys, signed model and real device lease')
else:
 account.check_local();before=account.model.current_hash();diagnostic(a.mode+'-restart');train();account.check_local();assert account.model.current_hash()==before,'local candidate replaced formal model';box=UploadOutbox(root/'training-data/outbox.sqlite3');stats=box.stats(account.binding());assert stats['records']>=252,stats
 if a.mode=='offline':assert not stats['states'].get('confirmed'),stats
 else:
  for _ in range(10):
   stats=box.stats(account.binding())
   if stats['states'].get('confirmed'):break
   time.sleep(.5)
  assert stats['states'].get('confirmed'),stats
  account.logout();report=folder/'package-logout.json';subprocess.run([str(exe),'--account-check','--report',str(report)],check=True,timeout=60);r=json.loads(report.read_text());assert not r['execution_allowed'] and not r['logged_in'],r
 (folder/('package-'+a.mode+'-queue.json')).write_text(json.dumps({'stats':stats,'formal_model_sha256':before,'exe_sha256':hashlib.sha256(exe.read_bytes()).hexdigest()},indent=2));print('PASS packaged '+a.mode+' restart/training/queue; candidate does not replace formal model')
account.close()
