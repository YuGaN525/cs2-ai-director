"""Director security integration against an explicitly supplied localhost-only fixture.

Uses only fixture actors user4/user5 and one super-admin session for scoped restrictions.
Secrets stay in memory or the isolated Windows-protected device directory, never the report.
"""
import argparse
import hashlib
import http.cookiejar
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from account_client import AccountClient, AccountError, DesktopAccount
from account_security import ERROR_STATUS, response_code
from device_identity import unb64
from upload_worker import NoRedirect


class CheckFailed(Exception): pass


class Run:
    def __init__(self, fixture, output):
        self.fixture = json.loads(fixture.read_text(encoding='utf-8-sig'))
        self.base = self.fixture['baseUrl']
        parsed = urllib.parse.urlparse(self.base)
        if (parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1')
                or parsed.username or parsed.password or parsed.path not in ('', '/')
                or '_concurrency_' not in self.fixture['connection']):
            raise CheckFailed('localhost isolated fixture required')
        self.output = output
        output.mkdir(parents=True, exist_ok=True)
        self.checks, self.accounts, self.restrictions, self.grants = [], [], [], []
        self.admin = None
        self.fixture_headers = {'X-Fixture-Key': self.fixture['fixtureKey']}
        self.actor_ids = {name: str(uuid.UUID(self.fixture['actors'][name]['id'])) for name in ('user4', 'user5')}
        self.run_id = str(uuid.uuid4())
        self.activity_replay = None

    def check(self, name, passed, *, fatal=True, detail=None):
        self.checks.append({'name': name, 'passed': bool(passed), **({'detail': detail} if detail is not None else {})})
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)
        if not passed and fatal: raise CheckFailed(name)

    def web(self, admin=False):
        return {'opener': urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())),
                'headers': {'X-Yuzi-Client': 'admin' if admin else 'web'}}

    def call(self, method, path, data=None, web=None, headers=None):
        request = urllib.request.Request(self.base + path, method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Content-Type': 'application/json', **((web or {}).get('headers', {})), **(headers or {})})
        opener = web['opener'] if web else urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, json.loads(response.read(2*1024*1024))
        except urllib.error.HTTPError as error:
            try: return error.code, json.loads(error.read(65536))
            finally: error.close()

    def sql(self, sql, read=False):
        status, body = self.call('POST', '/_fixture/sql' if read else '/_fixture/execute', {'sql': sql}, headers=self.fixture_headers)
        if status != 200: raise CheckFailed('isolated SQL failed')
        return body

    def state(self, actor='user4'):
        return self.sql("SELECT last_active_at,require_email FROM account_security_states WHERE user_id='" + self.actor_ids[actor] + "'", True)[0]

    def email_code(self, label):
        status, rows = self.call('GET', '/_fixture/mail', headers=self.fixture_headers)
        if status != 200: raise CheckFailed('synthetic mailbox unavailable')
        return next(row['payload']['code'] for row in rows if row['payload']['email'] == self.fixture['actors'][label]['email'] and 'code' in row['payload'])

    def login(self, label, *, verify_email=False):
        web = self.web(label == 'user0')
        actor = self.fixture['actors'][label]
        status, body = self.call('POST', '/api/auth/login',
            {'username': actor['username'], 'password': actor['password'], 'admin': label == 'user0'}, web)
        if status != 200: raise CheckFailed('synthetic website login rejected: '+str(status)+' '+response_code(body,status))
        if verify_email or body['data'].get('nextAction') == 'email_code':
            self.check('inactive website login demands email without issuing tokens',
                body['data'].get('nextAction') == 'email_code' and not body['data'].get('accessToken'))
            status, body = self.call('POST', '/api/auth/security/login-confirm',
                {'challengeId': body['data']['challengeId'], 'code': self.email_code(label)}, web)
            self.check('synthetic mailbox proof completes website login', status == 200 and bool(body['data'].get('accessToken')))
        token = body['data'].get('accessToken')
        if not token: raise CheckFailed('website session required')
        web['headers']['Authorization'] = 'Bearer ' + token
        web['sessionId'] = body['data']['sessionId']
        return web

    def authorize(self, account, label, web=None):
        web = web or self.login(label)
        pending = account.begin_login(open_browser=False)
        status, _ = self.call('POST', '/api/director/device-auth/approve',
            {'userCode': pending['user_code'], 'approve': True}, web)
        if status != 200: raise CheckFailed('synthetic device approval rejected')
        if not account.complete_login(): raise CheckFailed('synthetic signed device poll rejected')
        self.check('device authorization keeps requested synthetic owner', account.binding().account == self.actor_ids[label])
        return web

    def account(self, name, label):
        root = self.output / self.run_id / name
        account = DesktopAccount(root, self.base, {}, root/'unused-model.json.gz')
        self.accounts.append(account)
        self.authorize(account, label)
        return account

    def recent_admin(self):
        if self.admin is None:
            self.admin = self.login('user0')
        status, body = self.call('POST', '/api/account-security/email-challenge', {}, self.admin)
        if status != 200: raise CheckFailed('admin recent-email challenge failed: '+str(status)+' '+response_code(body,status))
        status, _ = self.call('POST', '/api/account-security/email-challenge/confirm',
            {'challengeId': body['data']['id'], 'code': self.email_code('user0')}, self.admin)
        if status != 200: raise CheckFailed('admin recent-email verification failed')

    def restriction(self, kind, target):
        rule = {'objectType': kind, 'target': target, 'actions': ['director'], 'level': 'freeze',
                'manualRelease': True, 'endsAt': None, 'reason': '程序侧隔离联验', 'evidence': ['user4 synthetic fixture only'], 'version': 0}
        status, body = self.call('POST', '/api/admin/account-security/restrictions/preview', rule, self.admin)
        if status != 200: raise CheckFailed('scoped restriction preview failed')
        status, body = self.call('POST', '/api/admin/account-security/restrictions',
            {'rule': rule, 'previewTicket': body['data']['previewTicket'], 'confirmBroad': True}, self.admin)
        if status != 200: raise CheckFailed('scoped restriction creation failed')
        self.restrictions.append(body['data'])
        return body['data']

    def release(self, restriction):
        status, _ = self.call('POST', '/api/admin/account-security/restrictions/'+restriction+'/revoke',
            {'version': 1, 'reason': '程序侧隔离联验完成'}, self.admin)
        self.check('super admin explicitly releases synthetic restriction', status == 200)
        self.restrictions.remove(restriction)

    def expect_denial(self, account, code):
        token = account.tokens['access_token']
        refresh = account.tokens['refresh_token']
        public = account.session.device_public_key()
        for path, data in (('me/entitlement', None), ('me/devices', None),
                           ('me/lease', {'modelSha256':'0'*64,'proof':account.session.sign_proof('yuzi-director-lease-v1',token+':'+'0'*64)}),
                           ('token/refresh', {'refreshToken':refresh,'proof':account.session.sign_proof('yuzi-director-refresh-v1',refresh)}),
                           ('security/activity/challenge', {'action': 'director.start'})):
            try: account.client.call(path, data, token)
            except AccountError as error: self.check(code+' on '+path, (error.code, error.status) == (code, ERROR_STATUS[code]), fatal=False, detail={'httpStatus':error.status,'code':error.code})
            else: self.check(code+' on '+path, False)
        result = account.record_activity('director.start')
        self.check(code+' invalidates current client identity', result == {'recorded': False, 'reason': code} and account.security_code == code and account.upload_identity() is None)
        original = account.client.call
        def no_network(*args, **kwargs): raise CheckFailed('denied client retried automatically')
        account.client.call = no_network
        try:
            account.sync_once(); account.check_local(); account.record_activity('trainer.start')
        finally: account.client.call = original
        other = DesktopAccount(account.root, self.base, {}, account.model.path)
        other.client.call = no_network
        other.sync_once()
        self.check(code+' persists across restart with same key', other.security_code == code and other.session.device_public_key() == public and other.upload_identity() is None)
        other.close()

    def main(self):
        status, body = self.call('GET', '/_fixture/ready', headers=self.fixture_headers)
        self.check('authenticated localhost fixture ready', status == 200 and body.get('ready') is True)
        for actor_id in self.actor_ids.values():
            grant = str(uuid.uuid4()); self.grants.append(grant)
            self.sql("INSERT INTO director_grants(id,created_at,account_id,source,source_id,starts_at,ends_at,revoked) VALUES('"+grant+"',timezone('UTC',now()),'"+actor_id+"','manual','"+grant+"',timezone('UTC',now()),timezone('UTC',now())+interval '1 hour',false)")
        account = self.account('primary', 'user4')
        second = self.account('same-owner-other-device', 'user4')
        other = self.account('other-owner', 'user5')
        key = account.session.device_public_key()
        # A real background entitlement check must not advance the website inactivity clock.
        self.sql("UPDATE account_security_states SET last_active_at=timezone('UTC',now())-interval '1 hour' WHERE user_id='"+self.actor_ids['user4']+"'")
        before = self.state()
        account.sync_once(); account.check_local()
        self.check('background entitlement and local validation do not count as activity', self.state() == before)
        captured = {}
        original = account.client.call
        def record(path, data=None, token='', raw=False):
            if path == 'security/activity/confirm': captured.update(data)
            return original(path, data, token, raw)
        account.client.call = record
        first = account.record_activity('director.start')
        self.check('DesktopAccount director.start signature accepted by real server', first.get('recorded') is True)
        first_state = self.state()
        replay = original('security/activity/confirm', dict(captured), account.tokens['access_token'])
        self.activity_replay = {'firstRecordedAt': first.get('recordedAt'), 'replayedRecordedAt': replay.get('recordedAt'),
                                'firstActivityAt': first_state.get('last_active_at'), 'replayedActivityAt': self.state().get('last_active_at')}
        self.check('replayed confirmation preserves first receipt and activity time', replay == first and self.state() == first_state, fatal=False)
        self.check('DesktopAccount trainer.start signature accepted by real server', account.record_activity('trainer.start').get('recorded') is True)
        account.client.call = original
        token = account.tokens['access_token']
        challenge = original('security/activity/challenge', {'action': 'director.start'}, token)
        confirm = {'challengeId': challenge['challengeId'], 'proof': account.session.sign_proof('yuzi-director-activity-v1', challenge['signingPayload'])}
        for target, name in ((second, 'cross-device'), (other, 'cross-account')):
            try: target.client.call('security/activity/confirm', confirm, target.tokens['access_token'])
            except AccountError as error: self.check(name+' confirmation is rejected', error.status == 404)
            else: self.check(name+' confirmation is rejected', False)
        try: original('security/activity/confirm', dict(confirm, proof=second.session.sign_proof('yuzi-director-activity-v1', challenge['signingPayload'])), token)
        except AccountError as error: self.check('wrong device signature is rejected', error.status == 403)
        else: self.check('wrong device signature is rejected', False)
        cid = str(uuid.UUID(challenge['challengeId']))
        self.sql("UPDATE account_director_activity SET expires_at=timezone('UTC',now())-interval '1 second' WHERE id='"+cid+"' AND user_id='"+self.actor_ids['user4']+"'")
        try: original('security/activity/confirm', confirm, token)
        except AccountError as error: self.check('expired unconsumed challenge is rejected', error.status == 409)
        else: self.check('expired unconsumed challenge is rejected', False)
        self.sql("UPDATE account_security_states SET last_active_at=timezone('UTC',now())-interval '14 days' WHERE user_id='"+self.actor_ids['user4']+"'")
        stale = self.state()
        self.expect_denial(account, 'EMAIL_REAUTH_REQUIRED')
        self.check('rejected background and start requests do not postpone 14-day threshold', self.state() == stale)
        verified = self.login('user4', verify_email=True)
        self.check('website email verification alone does not reactivate local client', account.security_code == 'EMAIL_REAUTH_REQUIRED' and account.upload_identity() is None)
        self.authorize(account, 'user4', verified)
        self.check('manual device reauthorization restores identity without key rotation', account.upload_identity() is not None and account.session.device_public_key() == key)
        self.recent_admin()
        restriction = self.restriction('account', self.actor_ids['user4'])
        frozen_token = account.tokens['access_token']
        self.expect_denial(account, 'ACCOUNT_SECURITY_FROZEN')
        self.sql("UPDATE security_restrictions SET ends_at=timezone('UTC',now())-interval '1 day' WHERE id='"+str(uuid.UUID(restriction))+"'")
        try: account.client.call('security/activity/challenge', {'action':'director.start'}, frozen_token)
        except AccountError as error: self.check('manual-release freeze does not expire or accept activity', error.code == 'ACCOUNT_SECURITY_FROZEN' and error.status == 403)
        else: self.check('manual-release freeze does not expire or accept activity', False)
        web = self.web(); actor = self.fixture['actors']['user4']
        status, body = self.call('POST', '/api/auth/login', {'username': actor['username'], 'password': actor['password']}, web)
        self.check('frozen account cannot get new website authorization', status == 403 and body.get('code') == 'ACCOUNT_SECURITY_FROZEN')
        self.release(restriction)
        self.authorize(account, 'user4')
        self.authorize(second, 'user4')
        thumb = hashlib.sha256(unb64(key)).hexdigest()
        restriction = self.restriction('director-device', 'director:'+thumb)
        self.expect_denial(account, 'DEVICE_SECURITY_BLOCKED')
        self.check('device-specific restriction leaves a different authorized device available',
            second.client.call('me/entitlement',token=second.tokens['access_token'])['enabled'] is True)
        self.check('restriction does not affect the other synthetic account',
            other.client.call('me/entitlement',token=other.tokens['access_token'])['enabled'] is True)
        web = self.login('user4'); pending = account.begin_login(open_browser=False)
        status, _ = self.call('POST', '/api/director/device-auth/approve', {'userCode': pending['user_code'], 'approve': True}, web)
        if status == 200:
            try: account.complete_login()
            except AccountError as error: self.check('device reauthorization cannot bypass blocked original key', error.code == 'DEVICE_SECURITY_BLOCKED')
            else: self.check('device reauthorization cannot bypass blocked original key', False)
        else: self.check('blocked key rejected before device poll', status == 403)
        self.check('device ban never rotates local key', account.session.device_public_key() == key)
        self.release(restriction)
        self.authorize(account, 'user4')
        device = str(uuid.UUID(account.binding().device))
        self.sql("UPDATE director_credentials SET revoked=true WHERE device_id='"+device+"' AND device_id IN(SELECT id FROM director_devices WHERE account_id='"+self.actor_ids['user4']+"')")
        self.expect_denial(account, 'SESSION_REVOKED')

    def cleanup(self):
        for restriction in list(self.restrictions):
            try: self.release(restriction)
            except Exception: self.checks.append({'name': 'scoped restriction cleanup', 'passed': False})
        for account in self.accounts:
            try: account.logout(); account.close()
            except Exception: self.checks.append({'name': 'synthetic device logout cleanup', 'passed': False})
        for grant in self.grants:
            try: self.sql("UPDATE director_grants SET revoked=true WHERE id='"+grant+"'")
            except Exception: self.checks.append({'name': 'synthetic grant cleanup', 'passed': False})
        if self.admin:
            try: self.call('DELETE', '/api/account-security/sessions/'+str(uuid.UUID(self.admin['sessionId'])), web=self.admin)
            except Exception: self.checks.append({'name': 'test admin session cleanup', 'passed': False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    run = Run(args.fixture, args.output)
    error_type = None
    error_check = None
    try: run.main()
    except Exception as error:
        error_type = type(error).__name__
        if isinstance(error,CheckFailed): error_check = str(error)
        print('Stopped: '+error_type, flush=True)
    finally:
        run.cleanup()
        report = {'runId': run.run_id, 'baseUrl': run.base, 'actors': ['user4', 'user5'],
                  'checks': run.checks, 'total': len(run.checks), 'passed': sum(c['passed'] for c in run.checks),
                  'errorType': error_type, 'failedCheck':error_check, 'productionChanged': False, 'realSignedModelOrLeaseTested': False}
        report['activityReplay'] = run.activity_replay
        (args.output/'program-security-http.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if error_type is None and all(c['passed'] for c in run.checks) else 1


if __name__ == '__main__': raise SystemExit(main())
