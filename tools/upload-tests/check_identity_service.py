"""Real localhost HTTP/PG task6 checks. Never accepts production hosts or real credentials."""
import argparse
import concurrent.futures
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from account_client import DesktopAccount,AccountClient,AccountError
from device_identity import unb64,DeviceSession

parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True)
args=parser.parse_args();folder=args.evidence.resolve()
actors=json.loads((folder/'actors.json').read_text());trust=json.loads((folder/'trust.json').read_text())
assert trust['server_origin']=='http://127.0.0.1:5136'
client=AccountClient(trust['server_origin']);keys={k:unb64(v) for k,v in trust['trusted_public_keys'].items()}
checks=[]
def check(value,name):
    assert value,name
    checks.append(name);print('PASS '+name,flush=True)
def call(path,body=None,actor='admin',raw=False):return client.call(path,body,actors[actor]['token'],raw)
# Website admin paths use the same bounded HTTP client but have a distinct prefix.
admin=AccountClient(trust['server_origin']);old_call=admin.call
def management(path,body=None,actor='admin'):
    return old_call('../admin/director/'+path,body,actors[actor]['token'])
def fail(work,status,name):
    try:work();raise AssertionError(name)
    except AccountError as error:check(error.status==status,name)
def uid(actor):return str(actors[actor]['uid'])
def account(actor):return management('accounts?uid='+uid(actor))
def grant(actor,days=10):
    state=account(actor);now=datetime.now(timezone.utc)
    request={'requestId':str(uuid.uuid4()),'expectedVersion':state['version'],'startsAt':now.isoformat().replace('+00:00','Z'),'endsAt':(now+timedelta(days=days)).isoformat().replace('+00:00','Z')}
    return management('accounts/'+state['accountId']+'/grants',request),request
def cdk(days=30):
    body={'requestId':str(uuid.uuid4()),'label':'local-test','count':1,'days':days,'expiresAt':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat().replace('+00:00','Z')}
    return management('cdk-batches',body),body

fail(lambda:management('accounts?uid='+uid('alpha'),actor='alpha'),403,'ordinary account cannot manage qualifications')
fail(lambda:management('accounts?uid='+uid('alpha'),actor='viewer'),403,'view-only module cannot modify/read private license workspace')
check(management('accounts?uid='+uid('alpha'),actor='license')['accountId']==actors['alpha']['id'],'module grants authorize dedicated license admin')
check(not call('me/entitlement',actor='alpha')['enabled'],'website login alone grants no product')
given,request=grant('alpha')
check(given['enabled'],'manual grant enables product')
same=management('accounts/'+actors['alpha']['id']+'/grants',request)
check(same['version']==given['version'],'grant command replay idempotent')
changed=dict(request,endsAt=(datetime.now(timezone.utc)+timedelta(days=11)).isoformat().replace('+00:00','Z'))
fail(lambda:management('accounts/'+actors['alpha']['id']+'/grants',changed),409,'same request identity different payload conflicts')
created,body=cdk();replay=management('cdk-batches',body)
check(created['rawAvailable'] and not replay['rawAvailable'] and replay['codes']==[],'raw CDK available exactly once')
code=created['codes'][0]
def redeem(actor):
    try:return call('me/cdk/redeem',{'requestId':str(uuid.uuid4()),'code':code},actor)['enabled']
    except AccountError as e:return e.status
with concurrent.futures.ThreadPoolExecutor(2) as pool:out=list(pool.map(redeem,['alpha','beta']))
check(sum(v is True for v in out)==1 and 409 in out,'concurrent CDK redemption only one account succeeds')
new,body=cdk();rid=str(uuid.uuid4());req={'requestId':rid,'code':new['codes'][0]}
e=call('me/cdk/redeem',req,'alpha');again=call('me/cdk/redeem',req,'alpha')
check(e['version']==again['version'],'CDK request replay does not create additional grant')
state=account('alpha');target=state['grants'][0]
management(f"accounts/{state['accountId']}/grants/{target['id']}/revoke",{'requestId':str(uuid.uuid4()),'expectedVersion':state['version']})
check(call('me/entitlement',actor='alpha')['enabled'],'revoking one grant preserves other grants')
batch=next(b for b in management('cdk-batches') if b['id']==created['batchId'])
fail(lambda:management(f"cdk-batches/{batch['id']}/state",{'requestId':str(uuid.uuid4()),'expectedVersion':99,'disabled':True}),409,'batch CAS rejects stale version')
unused,body=cdk();management(f"cdk-batches/{unused['batchId']}/state",{'requestId':str(uuid.uuid4()),'expectedVersion':0,'disabled':True})
fail(lambda:call('me/cdk/redeem',{'requestId':str(uuid.uuid4()),'code':unused['codes'][0]},'beta'),409,'disabled batch rejects redemption')

desktop=DesktopAccount(folder/'desktop',trust['server_origin'],keys,folder/'verified-model.json.gz')
pending=desktop.begin_login(open_browser=False)
try:desktop.complete_login();raise AssertionError('pending')
except AccountError as error:check(error.code=='authorization_pending','poll returns standard authorization_pending')
try:desktop.complete_login();raise AssertionError('slowdown')
except AccountError as error:check(error.code=='slow_down','early poll returns slow_down')
view=call('device-auth/preview',{'userCode':pending['user_code'],'approve':False},'alpha')
check(view['deviceName']=='CS2 AI Director','website preview shows device without leaking device_code')
call('device-auth/approve',{'userCode':pending['user_code'],'approve':True},'alpha')
fail(lambda:call('device-auth/approve',{'userCode':pending['user_code'],'approve':True},'beta'),409,'second account cannot overwrite device approval')
time.sleep(10.2);check(desktop.complete_login(),'approved account binds device and obtains credentials')
check(desktop.tokens['account_id']==actors['alpha']['id'],'device bearer identity matches website approval')
check(desktop.client.call('me/entitlement',token=desktop.tokens['access_token'])['enabled'],'real device authentication reads own entitlement')
fail(lambda:management('accounts?uid='+uid('alpha'),actor='beta'),403,'ordinary website account still has no admin')
device_token=desktop.tokens['access_token']
fail(lambda:old_call('../admin/director/cdk-batches',token=device_token),401,'device credentials cannot authenticate to admin')
desktop.sync_once();desktop.check_local();check(desktop.allowed(),'C# signed offline lease accepted by Python and model cache')
state=desktop.session.check(desktop.tokens['account_id'],desktop.model.current_hash())
check(604790<state.remaining_seconds<=604800,'server-issued lease capped at 168 hours')
check(b'd1_' not in desktop.auth_path.read_bytes() and b'dr1_' not in desktop.auth_path.read_bytes(),'access and refresh credentials DPAPI-protected')
reopened=DesktopAccount(folder/'desktop',trust['server_origin'],keys,folder/'verified-model.json.gz')
check(reopened.allowed(),'offline initialized client survives process recreation')
refresh=desktop.tokens['refresh_token'];proof=desktop.session.sign_proof('yuzi-director-refresh-v1',refresh)
rotated=desktop.client.call('token/refresh',{'refreshToken':refresh,'proof':proof},raw=True)
check(rotated['refresh_token']!=refresh,'refresh rotates token')
fail(lambda:desktop.client.call('token/refresh',{'refreshToken':refresh,'proof':proof},raw=True),401,'old refresh replay rejected')
fail(lambda:desktop.client.call('me/entitlement',token=rotated['access_token']),401,'refresh replay revokes entire family')
devices=call('me/devices',actor='alpha');device=next(d for d in devices if d['id']==desktop.tokens['device_id'])
# Rebind with a fresh interactive flow after replay revoke.
pending=desktop.begin_login(open_browser=False);call('device-auth/approve',{'userCode':pending['user_code'],'approve':True},'alpha');check(desktop.complete_login(),'explicit device login recovers revoked token family')
fail(lambda:call('device-auth/token',{'deviceCode':pending['device_code'],'proof':desktop.session.sign_proof('yuzi-director-device-poll-v1',pending['device_code'])},raw=True),400,'device_code single consumption')
desktop.sync_once()
state=account('alpha');management(f"accounts/{state['accountId']}/state",{'requestId':str(uuid.uuid4()),'expectedVersion':state['version'],'disabled':True})
desktop.sync_once();check(not desktop.allowed(),'online explicit product suspension stops local gate')
state=account('alpha');management(f"accounts/{state['accountId']}/state",{'requestId':str(uuid.uuid4()),'expectedVersion':state['version'],'disabled':False})
desktop.sync_once();desktop.check_local();check(desktop.allowed(),'restored qualification can renew signed lease')
desktop.logout();check(not desktop.allowed() and desktop.tokens is None,'logout preserves tombstone and stops local gate')
(folder/'checks.json').write_text(json.dumps({'passed':len(checks),'checks':checks},indent=2),encoding='utf-8')
print('TOTAL',len(checks),'identity checks passed')
