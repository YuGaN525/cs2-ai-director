import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from account_client import AccountClient, AccountError, DesktopAccount
from account_security import ERROR_STATUS, MESSAGES, response_code
import test_account_client as fixtures
from upload_worker import UploadIdentity


class AccountSecurityTests(unittest.TestCase):
    setUp=fixtures.AccountClientTests.setUp
    tokens=fixtures.AccountClientTests.tokens
    install=fixtures.AccountClientTests.install
    def test_four_denials_persist_without_renewal_or_key_rotation(self):
        for code,status in ERROR_STATUS.items():
            with self.subTest(code=code):
                self.now+=1
                self.install()
                key=self.account.session.device_public_key()
                envelope=self.account.session._read(self.account.session.path)['envelope']
                self.client.call.reset_mock();self.client.call.side_effect=AccountError(code,status)
                self.account.sync_once()
                self.assertEqual(self.account.security_code,code)
                self.assertFalse(self.account.allowed());self.assertIsNone(self.account.upload_identity())
                self.assertFalse(self.account.status_snapshot()['logged_in'])
                self.assertEqual(self.account.status_snapshot()['reason_code'],code)
                self.assertEqual(self.account.status,MESSAGES[code])
                protected=self.account.session._read(self.account.auth_path)
                self.assertNotIn('access_token',protected);self.assertNotIn('refresh_token',protected)
                self.account.sync_once();self.account.check_local()
                self.assertEqual(self.client.call.call_count,1)
                other=DesktopAccount(self.root,'https://test.invalid',{'test':self.public},self.path,
                    client=self.client,clock=lambda:self.now,protector=self.protector)
                self.assertEqual(other.security_code,code);self.assertFalse(other.allowed())
                other.sync_once();self.assertEqual(self.client.call.call_count,1)
                self.account.logout();self.assertEqual(self.account.security_code,code)
                self.assertEqual(self.account.session.device_public_key(),key)
                self.assertEqual(self.account.session._read(self.account.session.path)['envelope'],envelope)

    def test_lease_stage_denial_and_late_rejection_cannot_clear_new_session(self):
        self.install();old=self.account.upload_identity()
        self.client.call.side_effect=lambda path,*args,**kwargs: {'enabled':True} if path=='me/entitlement' else (_ for _ in ()).throw(AccountError('SESSION_REVOKED',401))
        self.account.sync_once();self.assertEqual(self.account.security_code,'SESSION_REVOKED')
        self.now+=1;self.install()
        with self.account.session._transaction():self.account._save_tokens(dict(self.tokens(),access_token='d1_new'))
        self.assertFalse(self.account.handle_security_error('ACCOUNT_SECURITY_FROZEN',403,old))
        self.assertEqual(self.account.tokens['access_token'],'d1_new')
        self.assertEqual(self.account.security_code,'')

    def test_manual_reauthorization_keeps_key_and_requires_server_success(self):
        self.install();identity=self.account.upload_identity();key=self.account.session.device_public_key()
        self.account.handle_security_error('EMAIL_REAUTH_REQUIRED',403,identity)
        self.client.call.side_effect=None
        self.client.call.return_value={'verification_uri':'https://test.invalid/tools/ai-director/activate',
            'device_code':'challenge','user_code':'ABCDE','expires_in':300,'interval':5}
        self.account.begin_login(open_browser=False)
        self.assertEqual(self.client.call.call_args.args[1]['publicKey'],key)
        self.assertIsNone(self.account.upload_identity())
        self.client.call.return_value=self.tokens()
        self.assertTrue(self.account.complete_login())
        self.assertEqual(self.account.security_code,'')
        self.assertEqual(self.account.session.device_public_key(),key)
        # New tokens alone do not clear the old lease tombstone; a signed lease is required.
        self.account.check_local();self.assertFalse(self.account.allowed())

    def test_login_rejection_stops_polling_and_preserves_frozen_state(self):
        self.install();key=self.account.session.device_public_key()
        def call(path,*args,**kwargs):
            if path=='device-auth/start':return {'verification_uri':'https://test.invalid/tools/ai-director/activate',
                'device_code':'challenge','user_code':'ABCDE','expires_in':300,'interval':5}
            raise AccountError('DEVICE_SECURITY_BLOCKED',403)
        self.client.call.side_effect=call
        with mock.patch('account_client.webbrowser.open'),mock.patch('account_client.threading.Thread') as thread:
            self.account.cancel_event.wait=mock.Mock(return_value=False)
            self.account.login_async();thread.call_args.kwargs['target']()
        self.assertEqual(self.client.call.call_count,2)
        self.assertEqual(self.account.security_code,'DEVICE_SECURITY_BLOCKED')
        self.assertEqual(self.account.session.device_public_key(),key)

    def test_http_code_is_preferred_over_untrusted_error_message(self):
        self.assertEqual(response_code({'code':'EMAIL_REAUTH_REQUIRED','error':'full server message'},403),'EMAIL_REAUTH_REQUIRED')
        self.assertEqual(response_code({'code':{},'error':['secret']},403),'http_403')
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403);self.end_headers()
                self.wfile.write(json.dumps({'success':False,'code':'ACCOUNT_SECURITY_FROZEN','error':'do not display'}).encode())
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=AccountClient(f'http://127.0.0.1:{server.server_port}')
            with self.assertRaises(AccountError) as caught:client.call('me/entitlement',token='synthetic')
            self.assertEqual((caught.exception.code,caught.exception.status),('ACCOUNT_SECURITY_FROZEN',403))
        finally:server.shutdown();server.server_close();thread.join()

    def test_dataset_upload_denial_clears_current_identity_preserving_queue(self):
        from dataset_runtime import DatasetRuntime
        from dataset_transport import DatasetFailure
        self.install();binding=self.account.binding()
        runtime=DatasetRuntime(self.account,self.root/'datasets',autostart=False)
        from pathlib import Path
        import gzip
        path=self.root/'input.csv'
        path.write_bytes(gzip.decompress((Path(__file__).parent/'contracts/director-training-dataset-v1/samples/minimal.0.csv.gz').read_bytes()))
        run,_=runtime.begin_training(path);runtime.succeeded(run,{})
        runtime.client=mock.Mock();runtime.client.upload.side_effect=DatasetFailure('ACCOUNT_SECURITY_FROZEN',403)
        self.assertFalse(runtime.run_once())
        self.assertEqual(runtime.store.get(run)['state'],'blocked')
        self.assertEqual(runtime.store.get(run)['account'],binding.account)
        self.assertTrue(runtime.store.input_path(run).exists())
        runtime.run_once();self.assertEqual(runtime.client.upload.call_count,1)
        self.assertEqual(self.account.security_code,'ACCOUNT_SECURITY_FROZEN')


    def challenge(self,action='director.start',**overrides):
        from datetime import datetime,timezone
        value={'challengeId':'cccccccc-cccc-cccc-cccc-cccccccccccc','nonce':'0123456789abcdef'*4,
               'action':action,'expiresAt':datetime.fromtimestamp(self.now+120,timezone.utc).isoformat()}
        value['signingPayload']=value['challengeId']+':'+value['nonce']+':'+action
        return dict(value,**overrides)

    def test_activity_signs_exact_purpose_and_does_not_change_lease(self):
        from device_identity import unb64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        self.install();before=self.account.session.path.read_bytes()
        public=Ed25519PublicKey.from_public_bytes(unb64(self.account.session.device_public_key()))
        receipt={'recorded':True,'recordedAt':'2026-11-13T12:00:05Z'}
        for action in ('director.start','trainer.start'):
            def call(path,data,token):
                self.assertEqual(token,'d1_access')
                if path=='security/activity/challenge':
                    self.assertEqual(data,{'action':action});return self.challenge(action)
                self.assertEqual(path,'security/activity/confirm')
                self.assertEqual(set(data),{'challengeId','proof'})
                public.verify(unb64(data['proof']),('yuzi-director-activity-v1\0'+self.challenge(action)['signingPayload']).encode())
                return receipt
            self.client.call.side_effect=call
            self.assertEqual(self.account.record_activity(action),receipt)
            self.assertEqual(self.account.session.path.read_bytes(),before)

    def test_activity_rejects_malformed_or_expired_challenge_before_signing(self):
        self.install()
        invalid=[{'action':'trainer.start'},{'signingPayload':'arbitrary text'},{'nonce':'a:b'},
                 {'challengeId':'CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC'},
                 {'expiresAt':'2000-01-01T00:00:00Z'},{'expiresAt':'2099-01-01T00:00:00Z'},
                 {'expiresAt':'2026-11-13T12:00:00'},{'nonce':{}},{}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.client.call.reset_mock()
                self.client.call.return_value=self.challenge(**changes) if changes else []
                with mock.patch.object(self.account.session,'sign_proof') as sign:
                    self.assertEqual(self.account.record_activity('director.start')['reason'],'invalid_response')
                    sign.assert_not_called()
                self.assertEqual(self.client.call.call_count,1)
        with self.assertRaises(ValueError):self.account.record_activity('heartbeat')

    def test_activity_old_server_and_network_do_not_claim_recording_or_extend_lease(self):
        self.install();before=self.account.session.path.read_bytes()
        for error,reason in ((AccountError('http_404',404),'unsupported'),(AccountError('network_unavailable'),'network_unavailable')):
            self.client.call.reset_mock();self.client.call.side_effect=error
            self.assertEqual(self.account.record_activity('trainer.start'),{'recorded':False,'reason':reason})
            self.assertEqual(self.client.call.call_count,1)
            self.assertTrue(self.account.allowed())
            self.assertEqual(self.account.session.path.read_bytes(),before)
        self.now+=604800
        self.assertFalse(self.account.allowed())

    def test_activity_denial_clears_identity_and_future_calls_do_not_retry(self):
        self.install()
        self.client.call.side_effect=[self.challenge(),AccountError('EMAIL_REAUTH_REQUIRED',403)]
        result=self.account.record_activity('director.start')
        self.assertFalse(result['recorded']);self.assertEqual(self.account.security_code,'EMAIL_REAUTH_REQUIRED')
        self.account.record_activity('director.start');self.account.sync_once()
        self.assertEqual(self.client.call.call_count,2)

    def test_invalid_confirm_receipt_never_claims_success(self):
        self.install()
        for receipt in ({'recorded':False,'recordedAt':'2026-11-13T12:00:00Z'},
                        {'recorded':True,'recordedAt':'bad-time'},
                        {'recorded':True,'recordedAt':'2026-11-13T12:00:00'},None):
            self.client.call.side_effect=[self.challenge(),receipt]
            self.assertEqual(self.account.record_activity('director.start'),{'recorded':False,'reason':'invalid_response'})

    def test_invalid_http_envelope_is_a_safe_error(self):
        client=AccountClient('https://test.invalid')
        for body in ([],{'success':True},{'success':False,'message':'private detail'}):
            response=io.BytesIO(json.dumps(body).encode())
            with mock.patch.object(client.opener,'open',return_value=response):
                with self.assertRaises(AccountError) as caught:client.call('me/entitlement')
            self.assertEqual(caught.exception.code,'invalid_response')

    def test_logout_between_challenge_and_confirm_never_confirms(self):
        self.install()
        def call(path,*args,**kwargs):
            if path=='security/activity/challenge':
                self.account.logout();return self.challenge()
            self.assertEqual(path,'logout')
        self.client.call.side_effect=call
        self.assertEqual(self.account.record_activity('director.start')['reason'],'local_session_changed')
        self.assertEqual(self.client.call.call_count,2)

    def test_async_activity_is_bounded_and_does_not_follow_new_identity(self):
        self.install()
        with mock.patch('account_client.threading.Thread') as thread:
            self.assertTrue(self.account.record_activity_async('director.start'))
            self.assertFalse(self.account.record_activity_async('trainer.start'))
            with self.account.session._transaction():
                self.account._save_tokens(dict(self.tokens(),access_token='d1_new',local_generation='new'))
            thread.call_args.kwargs['target']()
        self.client.call.assert_not_called();self.assertFalse(self.account._activity_running)

    def test_background_sync_and_validation_never_emit_activity(self):
        self.install();self.client.call.side_effect=AccountError('network_unavailable')
        self.account.sync_once();self.account.check_local();self.account.status_snapshot()
        self.assertEqual([c.args[0] for c in self.client.call.call_args_list],['me/entitlement'])

    def test_security_error_wrong_status_and_stale_identity_are_not_applied(self):
        self.install();identity=self.account.upload_identity()
        self.assertFalse(self.account.handle_security_error('ACCOUNT_SECURITY_FROZEN',500,identity))
        self.assertFalse(self.account.handle_security_error('SESSION_REVOKED',401,None))
        self.assertTrue(self.account.allowed())

    def test_denial_stops_input_before_waiting_for_credential_file_lock(self):
        from contextlib import contextmanager
        self.install();identity=self.account.upload_identity()
        waiting=threading.Event();release=threading.Event();errors=[]
        @contextmanager
        def blocked_transaction():
            waiting.set();release.wait(2);yield
        def reject():
            try:self.account.handle_security_error('DEVICE_SECURITY_BLOCKED',403,identity)
            except Exception as error:errors.append(error)
        with mock.patch.object(self.account.session,'_transaction',blocked_transaction):
            worker=threading.Thread(target=reject);worker.start()
            try:
                self.assertTrue(waiting.wait(2));self.assertFalse(self.account.allowed())
                self.assertIsNone(self.account.upload_identity())
            finally:release.set();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.account.security_code,'DEVICE_SECURITY_BLOCKED')

    def test_diagnostic_security_reply_stops_retries_and_preserves_report(self):
        from diagnostics_runtime import Diagnostics
        from diagnostics_client import DiagnosticFailure
        self.install();runtime=Diagnostics(self.root/'diagnostics',account=self.account,start=False)
        self.addCleanup(runtime.close)
        owner=runtime.owner();runtime.capture('error',ValueError);runtime.drain()
        with mock.patch('diagnostics_runtime.DiagnosticClient') as client:
            client.return_value.upload.side_effect=DiagnosticFailure(403,0,'ACCOUNT_SECURITY_FROZEN')
            runtime.send_one();runtime.send_one()
            client.return_value.upload.assert_called_once()
        self.assertEqual(self.account.security_code,'ACCOUNT_SECURITY_FROZEN')
        self.assertEqual(len(runtime.store.rows(owner)),1)
        self.assertEqual(runtime.last_status,MESSAGES['ACCOUNT_SECURITY_FROZEN'])

    def test_diagnostic_http_parser_keeps_only_stable_code(self):
        from diagnostics_client import DiagnosticClient,DiagnosticFailure
        self.install();identity=self.account.upload_identity()
        client=DiagnosticClient(self.account.upload_identity)
        client.opener=mock.Mock()
        client.opener.open.side_effect=urllib.error.HTTPError('https://test.invalid',403,'Forbidden',{},
            io.BytesIO(json.dumps({'success':False,'code':'EMAIL_REAUTH_REQUIRED','message':'private detail'}).encode()))
        with self.assertRaises(DiagnosticFailure) as caught:client._request(identity,'/capabilities')
        self.assertEqual(caught.exception.code,'EMAIL_REAUTH_REQUIRED')
        self.assertNotIn('private',str(caught.exception))

    def test_capture_upload_security_reply_preserves_batch_and_stops_retry(self):
        from upload_worker import UploadWorker,UploadFailure
        self.install();outbox=mock.Mock();client=mock.Mock()
        batch={'attempts':0};outbox.claim.return_value=batch
        client.upload.side_effect=UploadFailure(401,'SESSION_REVOKED')
        worker=UploadWorker(outbox,self.account.upload_identity,client)
        self.assertFalse(worker.run_once());self.assertFalse(worker.run_once())
        client.upload.assert_called_once();outbox.acknowledge.assert_not_called()
        self.assertTrue(outbox.fail.call_args.kwargs['blocked'])
        self.assertEqual(self.account.security_code,'SESSION_REVOKED')

if __name__=='__main__':unittest.main()
