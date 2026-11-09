import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from arbitration_runtime import (PersonalKeyStore,PersonalProvider,PlatformProvider,CloudSession,
                                 ArbitrationError,sanitize_config,migrate_config,owner_of)
from upload_outbox import Binding
from upload_worker import UploadIdentity

class Cipher:
    def protect(self,data):return b'cipher:'+data[::-1]
    def unprotect(self,data):return data[7:][::-1]

class Account:
    def __init__(self):
        self.identity=UploadIdentity(Binding(str(uuid.uuid4()),'https://www.yuzibei.cn',str(uuid.uuid4())),'synthetic-token')
        self.session=SimpleNamespace(origin=self.identity.binding.origin)
        self.valid=True
    def binding(self):return self.identity.binding
    def allowed(self):return self.valid
    def upload_identity(self):return self.identity

class Opener:
    def __init__(self,body=None,error=None):self.body=body;self.error=error;self.calls=0
    def open(self,request,timeout):
        self.calls+=1;self.request=request
        if self.error:raise self.error
        return io.BytesIO(self.body)

def request():return {'requestId':str(uuid.uuid4()),'sessionId':str(uuid.uuid4()),'generation':0,'roundKey':'round1','map':'de_mirage','candidates':[{'slot':1,'features':[0]*8}]}
def response(content='{"slot":1}',finish='stop'):
    return json.dumps({'choices':[{'message':{'content':content},'finish_reason':finish}]}).encode()

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.account=Account();self.store=PersonalKeyStore(self.temp.name,Cipher())
        self.owner=owner_of(self.account);self.store.set(self.owner,'synthetic-personal-key')
    def test_credentials_are_account_scoped_and_not_plaintext(self):
        self.assertEqual(self.store.get(self.owner),'synthetic-personal-key')
        self.assertEqual(self.store.get('another-account'),'')
        self.assertNotIn(b'synthetic-personal-key',self.store._path(self.owner).read_bytes())
        self.store.clear(self.owner);self.assertEqual(self.store.get(self.owner),'')

    def test_daily_and_legacy_budget_display_and_read_without_enabling_provider(self):
        from arbitration_runtime import describe_budget
        from unittest import mock
        budget={'unlimited':False,'dailyEnabled':True,'dailyLimit':'0.50000000',
                'dailyConsumed':'0.20000000','available':'0.30000000','nextResetAt':'2026-11-09T16:00:00Z'}
        text=describe_budget(budget)
        for part in ('0.3 元','每日 0.5 元','已用 0.2 元','11-10 00:00','北京时间'):
            self.assertIn(part,text)
        self.assertIn('无限',describe_budget(dict(budget,unlimited=True,available=None)))
        self.assertNotIn('每日',describe_budget({'unlimited':False,'available':'2.00000000'}))
        client=mock.Mock()
        client.call.return_value=budget
        provider=PlatformProvider(self.account,client)
        self.assertEqual(provider.read_budget(),budget)
        client.call.assert_called_once_with('arbitration/device-budget',token=self.account.identity.access_token)
        self.assertIsNone(provider.policy)
    def test_wrong_cipher_fails_closed(self):
        self.store._path(self.owner).write_bytes(b'invalid')
        with self.assertRaises(ArbitrationError):self.store.get(self.owner)
    def test_legacy_migration_encrypts_before_clearing(self):
        path=Path(self.temp.name)/'config.json'
        path.write_text(json.dumps({'deepseek_api_key':'legacy','ai_enabled':True,'obs_password':'keep'}))
        migrate_config(path,self.store);data=json.loads(path.read_bytes())
        self.assertEqual(data['deepseek_api_key'],'');self.assertFalse(data['ai_enabled']);self.assertEqual(data['obs_password'],'keep')
        self.store.adopt_legacy(self.owner);self.assertEqual(self.store.get(self.owner),'legacy');self.assertEqual(self.store.get('legacy-pending'),'')
    def test_migration_failure_preserves_source(self):
        path=Path(self.temp.name)/'config.json';raw=b'{"deepseek_api_key":"legacy"}';path.write_bytes(raw)
        self.store.protector.protect=lambda _: (_ for _ in ()).throw(OSError('locked'))
        with self.assertRaises(OSError):migrate_config(path,self.store)
        self.assertEqual(path.read_bytes(),raw)
    def test_sanitized_preferences_never_enable_paid_mode(self):
        result=sanitize_config({'deepseek_api_key':'secret','arbitration_mode':'platform','ai_enabled':True,'obs_password':'unchanged'})
        self.assertEqual(result['arbitration_mode'],'off');self.assertFalse(result['ai_enabled']);self.assertEqual(result['deepseek_api_key'],'');self.assertEqual(result['obs_password'],'unchanged')
    def personal(self,opener):return PersonalProvider(self.account,self.store,'deepseek-flash',opener)
    def test_personal_uses_valid_offline_gate_without_website_access_token(self):
        self.account.upload_identity=lambda:None
        self.assertEqual(self.personal(Opener(response())).ask(request()),1)
        self.account.valid=False
        with self.assertRaises(ArbitrationError):self.personal(Opener(response())).ask(request())

    def test_windows_dpapi_roundtrip(self):
        import os
        if os.name != 'nt':self.skipTest('Windows DPAPI only')
        real=PersonalKeyStore(Path(self.temp.name)/'real-dpapi')
        real.set(self.owner,'synthetic-dpapi-key')
        self.assertEqual(real.get(self.owner),'synthetic-dpapi-key')
        self.assertNotIn(b'synthetic-dpapi-key',real._path(self.owner).read_bytes())
        real.clear(self.owner)
        self.assertEqual(real.get(self.owner),'')

    def test_backup_never_exports_personal_key_even_when_obs_secret_included(self):
        import zipfile
        import backup_bundle
        archive=Path(self.temp.name)/'export.zip'
        backup_bundle.export_bundle(archive,{'deepseek_api_key':'fixture-key','obs_password':'fixture-obs','ai_enabled':True,'arbitration_mode':'personal'},Path(self.temp.name),'test',True)
        with zipfile.ZipFile(archive) as z:data=json.loads(z.read('config.json'))
        self.assertEqual(data['deepseek_api_key'],'');self.assertEqual(data['obs_password'],'fixture-obs');self.assertFalse(data['ai_enabled']);self.assertEqual(data['arbitration_mode'],'off')

    def test_old_backup_import_encrypts_key_before_install_and_stays_off(self):
        import zipfile
        from unittest.mock import patch
        import backup_bundle
        archive=Path(self.temp.name)/'old.zip';dest=Path(self.temp.name)/'import'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('manifest.json',json.dumps({'format':backup_bundle.FORMAT,'version':1,'files':['config.json']}))
            z.writestr('config.json',json.dumps({'deepseek_api_key':'legacy-import-key','ai_enabled':True,'obs_password':'fixture-obs'}))
        original=archive.read_bytes()
        with patch('arbitration_runtime.WindowsProtector',return_value=Cipher()):
            result=backup_bundle.import_bundle(archive,dest)
        self.assertEqual(PersonalKeyStore(dest,Cipher()).get('legacy-pending'),'legacy-import-key')
        self.assertEqual(result['config']['deepseek_api_key'],'');self.assertFalse(result['config']['ai_enabled']);self.assertEqual(archive.read_bytes(),original)

    def test_personal_no_entitlement_no_network(self):
        self.account.valid=False;opener=Opener(response())
        with self.assertRaises(ArbitrationError):self.personal(opener).ask(request())
        self.assertEqual(opener.calls,0)
    def test_personal_structured_request_fixed_origin_and_bounded_output(self):
        opener=Opener(response());self.assertEqual(self.personal(opener).ask(request()),1)
        self.assertEqual(opener.request.full_url,'https://api.deepseek.com/chat/completions')
        body=json.loads(opener.request.data);self.assertEqual(body['max_tokens'],128);self.assertFalse(body['stream'])
        self.assertNotIn('synthetic-personal-key',str(body))
    def test_personal_timeout_never_retries_or_leaks_error(self):
        opener=Opener(error=TimeoutError('synthetic-personal-key'))
        with self.assertRaises(ArbitrationError) as caught:self.personal(opener).ask(request())
        self.assertEqual(opener.calls,1);self.assertNotIn('synthetic-personal-key',str(caught.exception))
    def test_personal_invalid_results(self):
        for content in ('{"slot":2}','{"slot":true}','{"slot":1,"extra":2}','bad'):
            with self.subTest(content=content):
                try:result=self.personal(Opener(response(content))).ask(request());self.assertIsNone(result)
                except ArbitrationError:pass
    def test_personal_truncated_response_not_accepted(self):
        with self.assertRaises(ArbitrationError):self.personal(Opener(response(finish='length'))).ask(request())
    def test_personal_large_response_bounded(self):
        with self.assertRaises(ArbitrationError):self.personal(Opener(b' '*65537)).ask(request())
    def test_platform_empty_config_no_paid_call(self):
        client=FakeClient(configured=False);provider=PlatformProvider(self.account,client)
        with self.assertRaises(ArbitrationError):provider.ask(request())
        self.assertEqual(client.posts,0)
    def test_platform_receipt_context_and_no_personal_key(self):
        client=FakeClient();provider=PlatformProvider(self.account,client)
        self.assertEqual(provider.ask(request()),1);self.assertEqual(client.posts,1)
        self.assertNotIn('synthetic-personal-key',json.dumps(client.body))
    def test_platform_mismatched_receipt_rejected(self):
        client=FakeClient();client.bad=True
        with self.assertRaises(ArbitrationError):PlatformProvider(self.account,client).ask(request())
    def test_platform_unknown_latches_off_without_fallback(self):
        client=FakeClient();client.unknown=True
        session=CloudSession(self.account,self.store,'platform','',PlatformProvider(self.account,client))
        with self.assertRaises(ArbitrationError):session.ask(request())
        self.assertFalse(session.allowed());self.assertIsNone(session.ask(request()));self.assertEqual(client.posts,1)
    def test_account_and_device_change_disable_session(self):
        session=CloudSession(self.account,self.store,'personal','deepseek-flash')
        old=self.account.identity.binding
        self.account.identity=UploadIdentity(Binding(old.account,old.origin,str(uuid.uuid4())),'other')
        self.assertFalse(session.allowed())
        self.account.identity=UploadIdentity(old,'old');self.assertFalse(session.allowed())
    def test_round_change_and_manual_takeover_drop_response(self):
        class Provider:
            def ask(_,req):session.invalidate();return 1
        session=CloudSession(self.account,self.store,'personal','deepseek-flash',Provider())
        self.assertIsNone(session.ask(request()))
        session.sync(('map',2,'live'));self.assertIsNone(session.ask(request()))

class FakeClient:
    def __init__(self,configured=True):self.configured=configured;self.posts=0;self.bad=False;self.unknown=False
    def call(self,path,data=None,token=''):
        if path.endswith('policy'):return {'configured':self.configured,'enabled':True,'version':1}
        if path.endswith('device-budget'):return {'unlimited':True,'paused':False}
        self.posts+=1;self.body=data
        result=dict(data,state='unknown' if self.unknown else 'settled',code='ok',slot=1)
        if self.bad:result['sessionId']=str(uuid.uuid4())
        return result

if __name__=='__main__':unittest.main()
