"""Real delivery API/PG/DPAPI checks. Local isolated harness only; no production credentials."""
import concurrent.futures,gzip,hashlib,json,os,random,subprocess,sys,urllib.request,urllib.error,uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from account_client import AccountClient,AccountError,DesktopAccount
from device_identity import unb64,canonical
from model_runtime import ModelError,probability
from upload_outbox import UploadOutbox
from upload_worker import TrainingUploadClient,UploadFailure
from upload_protocol import encode_csv,verify_receipt
from test_uploads import row

repo=Path(__file__).resolve().parents[2];folder=Path(os.environ.get('DIRECTOR_IDENTITY_EVIDENCE','D:/Yuzibei/source/local-data/task78-20261107'))
actors=json.loads((folder/'actors.json').read_text());trust=json.loads((folder/'trust.json').read_text());assert trust['server_origin']=='http://127.0.0.1:5136'
client=AccountClient(trust['server_origin']);keys={k:unb64(v) for k,v in trust['trusted_public_keys'].items()};model_keys={k:unb64(v) for k,v in trust['trusted_model_keys'].items()};checks=[]
run_folder=folder/('run-'+(folder/'schema.txt').read_text()[-12:]);run_folder.mkdir(exist_ok=True)
# Exercise the site's real password/BCrypt/refresh issuance path with isolated QA users.
for name,actor in actors.items():
    request=urllib.request.Request(trust['server_origin']+'/api/auth/login',data=canonical({'username':'task6_'+name,'password':'local-qa-password-912','admin':False,'rememberMe':True}),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request) as response:actor['token']=json.loads(response.read())['data']['accessToken']
(folder/'actors.json').write_text(json.dumps(actors))


def check(value,name):assert value,name;checks.append(name);print('PASS '+name,flush=True)
def call(path,data=None,actor='admin'):return client.call('../admin/'+path if path.startswith(('director-models','director/accounts')) else path,data,actors[actor]['token'])
def fail(work,status,name):
    try:work();raise AssertionError(name)
    except (AccountError,UploadFailure) as e:check(e.status==status,name)

def multipart(path,file,fields,actor='reviewer'):
    boundary='qa_'+uuid.uuid4().hex;body=b''
    for k,v in fields.items():body+=f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    filename='asset.csv.gz' if file[:2]==b'\x1f\x8b' else 'asset.csv'
    body+=f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+file+f'\r\n--{boundary}--\r\n'.encode()
    req=urllib.request.Request(trust['server_origin']+'/api/'+path,data=body,headers={'Authorization':'Bearer '+actors[actor]['token'],'Content-Type':'multipart/form-data; boundary='+boundary})
    try:
        with urllib.request.urlopen(req,timeout=120) as response:return json.loads(response.read())['data']
    except urllib.error.HTTPError as e:
        status=e.code
        try:code=json.loads(e.read()).get('code','error')
        except ValueError:code='http_'+str(status)
        finally:e.close()
        raise AccountError(code,status)

def grant(actor):
    state=client.call('../admin/director/accounts?uid='+str(actors[actor]['uid']),token=actors['admin']['token']);now=datetime.now(timezone.utc)
    client.call('../admin/director/accounts/'+state['accountId']+'/grants',{'requestId':str(uuid.uuid4()),'expectedVersion':state['version'],'startsAt':now.isoformat().replace('+00:00','Z'),'endsAt':(now+timedelta(days=20)).isoformat().replace('+00:00','Z')},actors['admin']['token'])

def login(name,actor='alpha'):
    account=DesktopAccount(run_folder/name,trust['server_origin'],keys,run_folder/name/'model.json.gz',model_keys=model_keys)
    pending=account.begin_login(open_browser=False);client.call('device-auth/approve',{'userCode':pending['user_code'],'approve':True},actors[actor]['token']);assert account.complete_login();return account
