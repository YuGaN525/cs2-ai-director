"""Additional real-PG edge cases. Only the explicit isolated task6 DB/schema is writable."""
import argparse
from datetime import datetime,timedelta,timezone
import json,os,subprocess,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from account_client import AccountClient,AccountError,DesktopAccount
from device_identity import unb64
from upload_worker import TrainingUploadClient,UploadIdentity
from upload_outbox import UploadOutbox
from test_uploads import row
parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True);args=parser.parse_args()
folder=args.evidence.resolve();actors=json.loads((folder/'actors.json').read_text());trust=json.loads((folder/'trust.json').read_text());schema=(folder/'schema.txt').read_text()
assert schema.startswith('director6_') and all(c.isalnum() or c=='_' for c in schema)
assert trust['server_origin']=='http://127.0.0.1:5136'
psql=Path(os.environ['TEMP'])/'pgsql16/pgsql/bin/psql.exe'
def sql(value):
    return subprocess.check_output([str(psql),'-h','127.0.0.1','-p','55446','-U','postgres','-d','yuzibei_concurrency_director6','-v','ON_ERROR_STOP=1','-At','-c',f"SET timezone='UTC'; SET search_path TO {schema};"+value],text=True)
client=AccountClient(trust['server_origin']);keys={k:unb64(v) for k,v in trust['trusted_public_keys'].items()}
checks=[]
def check(value,name):assert value,name;checks.append(name);print('PASS '+name,flush=True)
def call(path,data=None,actor='admin'):return client.call(path,data,actors[actor]['token'])
def manage(path,data=None,actor='admin'):return call('../admin/director/'+path,data,actor)
def fail(work,status,name):
    try:work();raise AssertionError(name)
    except AccountError as e:check(e.status==status,name)
def setup(actor):
    state=manage('accounts?uid='+str(actors[actor]['uid']));now=datetime.now(timezone.utc)
    manage(f"accounts/{state['accountId']}/grants",{'requestId':str(uuid.uuid4()),'expectedVersion':state['version'],'startsAt':now.isoformat().replace('+00:00','Z'),'endsAt':(now+timedelta(hours=1)).isoformat().replace('+00:00','Z')})
def login(name,actor):
    desktop=DesktopAccount(folder/name,trust['server_origin'],keys,folder/'verified-model.json.gz')
    pending=desktop.begin_login(open_browser=False);call('device-auth/approve',{'userCode':pending['user_code'],'approve':True},actor)
    check(desktop.complete_login(),name+' interactive login');return desktop
# The concurrent CDK test may legitimately choose beta as its winner. Isolate the
# short-grant boundary instead of accidentally retaining that longer valid grant.
sql(f"UPDATE director_grants SET revoked=true WHERE account_id='{actors['beta']['id']}'")
setup('beta');beta=login('edge-beta','beta');beta.sync_once();beta.check_local()
lease=beta.session.check(beta.tokens['account_id'],beta.model.current_hash())
check(0<lease.remaining_seconds<=3600,'known earlier entitlement end caps offline deadline')
pending=beta.begin_login(open_browser=False);call('device-auth/approve',{'userCode':pending['user_code'],'approve':False},'beta')
fail(lambda:beta.complete_login(),400,'rejected device flow cannot obtain token');beta.pending=None
pending=beta.begin_login(open_browser=False)
sql("UPDATE director_device_auth SET expires_at=now()-interval '1 minute' WHERE state=0")
fail(lambda:beta.complete_login(),400,'expired device code cannot obtain token');beta.pending=None
fresh=beta.begin_login(open_browser=False)
fail(lambda:client.call('device-auth/token',{'deviceCode':fresh['device_code'],'proof':'invalid'},raw=True),400,'invalid device proof denied')
call('device-auth/approve',{'userCode':fresh['user_code'],'approve':False},'beta');beta.pending=None
active=login('edge-beta-revoke','beta');token=active.tokens['access_token'];device=active.tokens['device_id']
fail(lambda:client.call('me/devices/'+device,{},actors['alpha']['token']),405,'device mutation requires DELETE method')
# Use DELETE for the actual self-service resource route.
import urllib.request,urllib.error
def delete_device(actor,id):
    req=urllib.request.Request(trust['server_origin']+'/api/director/me/devices/'+id,method='DELETE',headers={'Authorization':'Bearer '+actors[actor]['token']})
    try:
        with urllib.request.urlopen(req) as response:return response.status
    except urllib.error.HTTPError as e:status=e.code;e.close();return status
check(delete_device('alpha',device)==404,'cross-account device revoke denied')
check(delete_device('beta',device)==200,'owner can revoke device')
fail(lambda:client.call('me/entitlement',token=token),401,'device revocation invalidates access immediately')
pw=login('edge-password','beta');token=pw.tokens['access_token'];refresh=pw.tokens['refresh_token']
sql(f"UPDATE users SET password_hash='changed-synthetic-{uuid.uuid4().hex}' WHERE id='{actors['beta']['id']}'")
fail(lambda:client.call('me/entitlement',token=token),401,'password change invalidates existing device access')
proof=pw.session.sign_proof('yuzi-director-refresh-v1',refresh)
fail(lambda:client.call('token/refresh',{'refreshToken':refresh,'proof':proof},raw=True),401,'password change invalidates refresh token')
ban=login('edge-ban','beta');token=ban.tokens['access_token']
sql(f"UPDATE users SET ban_type=1,ban_expires_at=NULL WHERE id='{actors['beta']['id']}'")
fail(lambda:client.call('me/entitlement',token=token),401,'account ban invalidates device access')
sql(f"UPDATE users SET ban_type=0 WHERE id='{actors['beta']['id']}'")
expiry=login('edge-expiry','beta');refresh=expiry.tokens['refresh_token'];proof=expiry.session.sign_proof('yuzi-director-refresh-v1',refresh)
sql("UPDATE director_credentials SET access_expires_at=now()-interval '1 second',refresh_expires_at=now()-interval '1 second'")
fail(lambda:client.call('me/entitlement',token=expiry.tokens['access_token']),401,'expired access rejected')
fail(lambda:client.call('token/refresh',{'refreshToken':refresh,'proof':proof},raw=True),401,'expired refresh rejected')
setup('alpha');upload=login('edge-upload','alpha');box=UploadOutbox(folder/'real-identity-outbox.sqlite3')
box.append(upload.binding(),'online',str(uuid.uuid4()),[row()]);box.prepare_batch(upload.binding());batch=box.claim(upload.binding())
receipt=TrainingUploadClient().upload(upload.upload_identity(),batch);box.acknowledge(batch,receipt)
check(box.stats(upload.binding())['states'].get('confirmed',0)>0,'actual device bearer authorizes upload and confirms durable queue')
fail(lambda:client.call('me/lease',{'modelSha256':'b'*64,'proof':upload.session.sign_proof('yuzi-director-lease-v1',upload.tokens['access_token']+':'+('b'*64))},upload.tokens['access_token']),503,'unapproved model cannot obtain signed execution lease')
grants=manage('accounts?uid='+str(actors['alpha']['uid']))
sql(f"UPDATE director_grants SET ends_at=now()-interval '1 minute' WHERE account_id='{actors['alpha']['id']}'")
check(not call('me/entitlement',actor='alpha')['enabled'],'expired grants no longer confer qualification')
fail(lambda:client.call('me/lease',{'modelSha256':upload.model.current_hash(),'proof':upload.session.sign_proof('yuzi-director-lease-v1',upload.tokens['access_token']+':'+upload.model.current_hash())},upload.tokens['access_token']),403,'expired qualification cannot renew offline lease')
# Revoke the dedicated module grant; pre-existing JWT cannot retain stale privileges.
sql("DELETE FROM role_permissions WHERE permission_id IN(SELECT id FROM permissions WHERE code='module:director-license:edit')")
fail(lambda:manage('accounts?uid='+str(actors['alpha']['uid']),actor='license'),403,'old admin JWT cannot bypass current permission withdrawal')
secrets=[upload.tokens['access_token'],upload.tokens['refresh_token']]
stored=sql('SELECT result_json FROM director_identity_commands; SELECT operation FROM director_identity_audit;')
check(all(value not in stored for value in secrets),'tokens never appear in command or audit payloads')
(folder/'edge-checks.json').write_text(json.dumps({'passed':len(checks),'checks':checks},indent=2),encoding='utf-8')
print('TOTAL',len(checks),'edge checks passed')
