"""Packaged account adapter against an explicitly isolated localhost service/profile."""
import argparse
from datetime import datetime,timedelta,timezone
import json,os,shutil,subprocess,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from account_client import DesktopAccount,AccountClient
from device_identity import unb64

p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True);p.add_argument('--offline',action='store_true');a=p.parse_args()
repo=Path(__file__).resolve().parents[2];folder=a.evidence.resolve();trust=json.loads((folder/'trust.json').read_text())
assert trust['server_origin']=='http://127.0.0.1:5136'
exe=repo/'dist/task6/Yuzi_CS2_AIDirector.exe';profile=repo/'build/task6/package-profile'
os.environ['APPDATA']=str(profile)
root=profile/'CS2AIDirector';root.mkdir(parents=True,exist_ok=True)
model=root/'verified-model.json.gz'
keys={k:unb64(v) for k,v in trust['trusted_public_keys'].items()}

def diagnostic(name):
    report=repo/'build/task6'/('package-'+name+'.json')
    subprocess.run([str(exe),'--account-check','--report',str(report)],check=True,timeout=60,cwd=exe.parent)
    return json.loads(report.read_text())

if not a.offline:
    shutil.copyfile(folder/'verified-model.json.gz',model)
    (exe.parent/'identity-server.json').write_text(json.dumps(trust),encoding='utf-8')
    actors=json.loads((folder/'actors.json').read_text());client=AccountClient(trust['server_origin'])
    state=client.call('../admin/director/accounts?uid='+str(actors['alpha']['uid']),token=actors['admin']['token'])
    now=datetime.now(timezone.utc)
    client.call(f"../admin/director/accounts/{state['accountId']}/grants",{'requestId':str(uuid.uuid4()),'expectedVersion':state['version'],
                'startsAt':now.isoformat().replace('+00:00','Z'),'endsAt':(now+timedelta(days=10)).isoformat().replace('+00:00','Z')},actors['admin']['token'])
    account=DesktopAccount(root/'identity',trust['server_origin'],keys,model)
    pending=account.begin_login(open_browser=False)
    client.call('device-auth/approve',{'userCode':pending['user_code'],'approve':True},actors['alpha']['token'])
    assert account.complete_login();account.sync_once();account.check_local();assert account.allowed()
    state=account.session._read(account.session.path)
    (folder/'signed-lease-public-fixture.json').write_text(json.dumps({'trusted_public_keys':trust['trusted_public_keys'],'envelope':state['envelope']},indent=2))
    result=diagnostic('online');assert result['logged_in'] and result['execution_allowed'],result
    assert result['binding']['account_id']==actors['alpha']['id']
    print('PASS packaged executable reads DPAPI device credentials and validates actual C# lease')
else:
    result=diagnostic('offline');assert result['logged_in'] and result['execution_allowed'],result
    account=DesktopAccount(root/'identity',trust['server_origin'],keys,model);account.logout()
    result=diagnostic('signed-out');assert not result['logged_in'] and not result['execution_allowed'],result
    print('PASS packaged offline restart preserves lease; logout tombstone prevents execution')
