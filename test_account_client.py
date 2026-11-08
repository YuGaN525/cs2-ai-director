import gzip,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest import mock
from datetime import datetime,timezone,timedelta
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from account_client import DesktopAccount,AccountError
from device_identity import b64,unb64,canonical,PURPOSE
from director_core.model_format import DECISION_FEATURES
from test_task23 import Protector


class AccountClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.now=time.time();self.signer=Ed25519PrivateKey.generate();self.protector=Protector()
        self.public=self.signer.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        self.client=mock.Mock();self.path=self.root/'model.json.gz'
        data={'n_features':8,'feature_version':'pov-v1','features':DECISION_FEATURES,'classes':[0,1],
              'trees':[{'left':[-1],'right':[-1],'feature':[-2],'threshold':[-2.],'value':[[.4,.6]]}]}
        with gzip.open(self.path,'wt') as stream:json.dump(data,stream)
        self.account=DesktopAccount(self.root,'https://test.invalid',{'test':self.public},self.path,
                                   client=self.client,clock=lambda:self.now,protector=self.protector)

    def tokens(self):
        iso=lambda n:datetime.fromtimestamp(n,timezone.utc).isoformat().replace('+00:00','Z')
        return {'account_id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','device_id':'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                'server_origin':'https://test.invalid','access_token':'d1_access','refresh_token':'dr1_refresh',
                'access_expires_at':iso(self.now+900),'refresh_expires_at':iso(self.now+2592000)}

    def install(self):
        with self.account.session._transaction():self.account._save_tokens(self.tokens())
        device=unb64(self.account.session.device_public_key())
        p={'schema':1,'key_id':'test','product':'cs2-ai-director','account_id':self.tokens()['account_id'],
           'device_key_thumbprint':hashlib.sha256(device).hexdigest(),'server_origin':'https://test.invalid',
           'model_sha256':self.account.model.current_hash(),'capabilities':['direct'],
           'verified_at':self.now,'offline_expires_at':self.now+604800,'offline_max_seconds':604800}
        envelope={'payload':p,'signature':b64(self.signer.sign(PURPOSE+canonical(p)))}
        self.account.session.install_verified_lease(envelope,self.tokens()['account_id']);self.account.check_local()

    def test_status_reason_distinguishes_pending_revoked_and_expired(self):
        self.account.pending={'user_code':'fixture'}
        self.assertEqual(self.account.status_snapshot()['reason_code'],'login_pending')
        self.account.pending=None
        self.install()
        self.now+=604800
        self.account.check_local()
        self.assertEqual(self.account.status_snapshot()['reason_code'],'lease_expired')
        self.now+=1
        self.install()
        self.client.call.return_value={'enabled':False}
        self.account.sync_once()
        self.assertEqual(self.account.status_snapshot()['reason_code'],'revoked')

    def test_status_snapshot_does_not_read_credentials_or_call_network(self):
        self.install()
        with mock.patch.object(self.account.session,'_read',side_effect=AssertionError('disk')):
            self.assertEqual(self.account.status_snapshot()['reason_code'],'ready')
        self.client.call.assert_not_called()

    def test_status_immediate_clock_rollback_and_model_change(self):
        self.install();self.now-=10
        self.assertEqual(self.account.status_snapshot()['reason_code'],'clock_rollback')
        self.now+=10;self.path.write_bytes(b'tampered')
        self.assertEqual(self.account.status_snapshot()['reason_code'],'model_changed')

    def test_empty_session_denies_and_unbound_capture_stays_local(self):
        self.assertFalse(self.account.allowed());self.assertEqual(self.account.binding().origin,'local')
        self.assertIsNone(self.account.upload_identity())

    def test_action_path_has_no_network_or_credential_reads(self):
        self.install()
        with mock.patch.object(self.account.session,'check',side_effect=AssertionError('disk read')):
            self.assertTrue(self.account.allowed());self.assertIsNotNone(self.account.upload_identity());self.account.binding()
        self.client.call.assert_not_called()

    def test_deadline_exact_without_background_poll(self):
        self.install();self.now+=604800
        self.assertFalse(self.account.allowed())

    def test_network_failure_preserves_valid_offline_permission(self):
        self.install();self.client.call.side_effect=AccountError('network_unavailable')
        self.account.sync_once();self.assertTrue(self.account.allowed())

    def test_explicit_rejection_invalidates_local_permission(self):
        self.install();self.client.call.side_effect=AccountError('DIRECTOR_REAUTH_REQUIRED',401)
        self.account.sync_once();self.assertFalse(self.account.allowed());self.assertIsNone(self.account.tokens)

    def test_no_entitlement_invalidates_without_losing_account_binding(self):
        self.install();binding=self.account.binding();self.client.call.return_value={'enabled':False}
        self.account.sync_once();self.assertFalse(self.account.allowed());self.assertEqual(self.account.binding(),binding)

    def test_logout_stops_permission_even_when_network_unreachable(self):
        self.install();self.client.call.side_effect=AccountError('network_unavailable')
        self.account.logout();self.assertFalse(self.account.allowed());self.assertIsNone(self.account.upload_identity())

    def test_wrong_verification_origin_never_opens_browser(self):
        self.client.call.return_value={'verification_uri':'https://attacker.invalid'}
        with mock.patch('account_client.webbrowser.open') as browser:
            with self.assertRaises(AccountError):self.account.begin_login()
            browser.assert_not_called()

    def test_token_origin_mismatch_rejected(self):
        with self.assertRaises(AccountError):self.account._save_tokens(dict(self.tokens(),server_origin='https://wrong.invalid'))

    def test_new_process_reads_protected_session_and_deadline(self):
        self.install();self.now+=60
        other=DesktopAccount(self.root,'https://test.invalid',{'test':self.public},self.path,
                             clock=lambda:self.now,protector=self.protector)
        self.assertTrue(other.allowed());self.assertEqual(other.binding(),self.account.binding())

    def test_model_tamper_immediately_stops_gate(self):
        self.install();self.path.write_bytes(b'tampered');self.assertFalse(self.account.allowed())

    def test_logout_in_other_process_cannot_be_undone_by_late_renewal(self):
        self.install()
        other=DesktopAccount(self.root,'https://test.invalid',{'test':self.public},self.path,
                             clock=lambda:self.now,protector=self.protector,client=mock.Mock())
        def response(path,*args,**kwargs):
            if path=='me/entitlement':return {'enabled':True}
            if path=='me/lease':other.logout();return {}
            raise AssertionError(path)
        self.client.call.side_effect=response
        self.account.sync_once();self.account.check_local()
        self.assertFalse(self.account.allowed());self.assertIsNone(self.account.tokens)

    def test_lease_rejection_stops_cached_execution_immediately(self):
        self.install()
        def response(path,*args,**kwargs):
            if path=='me/entitlement':return {'enabled':True}
            raise AccountError('DIRECTOR_NO_ENTITLEMENT',403)
        self.client.call.side_effect=response
        self.account.sync_once()
        self.assertFalse(self.account.allowed())

    def test_invalid_renewal_keeps_old_lease_without_crashing_or_extending_it(self):
        self.install()
        original=self.account.session._read(self.account.session.path)['envelope']
        payload=dict(original['payload'])
        payload.update(verified_at=self.now+3600,offline_expires_at=self.now+3600+604800)
        invalid={'payload':payload,'signature':b64(self.signer.sign(PURPOSE+canonical(payload)))}
        self.client.call.side_effect=lambda path,*args,**kwargs: {'enabled':True} if path=='me/entitlement' else invalid
        self.account.sync_once()
        self.assertTrue(self.account.allowed())
        self.assertEqual(self.account.session._read(self.account.session.path)['envelope'],original)
        self.assertIn('续签未通过验证',self.account.status)
        self.now+=604800+3601
        # Keep a valid access token so this specifically exercises lease expiry.
        with self.account.session._transaction():self.account._save_tokens(self.tokens())
        self.account.sync_once()
        self.assertFalse(self.account.allowed())
        self.assertTrue(self.account.tokens)

    def test_device_login_backs_off_on_http_throttle(self):
        self.account.begin_login=mock.Mock(return_value={'expires_in':600,'interval':5})
        self.account.complete_login=mock.Mock(side_effect=[AccountError('RATE_LIMITED',429),True])
        self.account.sync_once=mock.Mock();wait=mock.Mock(return_value=False)
        self.account.cancel_event.wait=wait
        with mock.patch('account_client.threading.Thread') as thread:
            self.account.login_async();thread.call_args.kwargs['target']()
        self.assertEqual(wait.call_args_list,[mock.call(5),mock.call(10)])
        self.account.sync_once.assert_called_once()

    def test_other_process_logout_rejects_late_device_login_response(self):
        self.install()
        self.client.call.return_value={'verification_uri':'https://test.invalid/tools/ai-director/activate','user_code':'12345-67890','device_code':'opaque'}
        self.account.begin_login(open_browser=False)
        other=DesktopAccount(self.root,'https://test.invalid',{'test':self.public},self.path,
                             clock=lambda:self.now,protector=self.protector,client=mock.Mock())
        def late_response(*args,**kwargs):other.logout();return self.tokens()
        self.client.call.side_effect=late_response
        with self.assertRaises(AccountError):self.account.complete_login()
        self.account.check_local();self.assertFalse(self.account.allowed());self.assertIsNone(self.account.tokens)

    def test_repeated_login_click_does_not_spawn_parallel_flows(self):
        with mock.patch('account_client.threading.Thread') as thread:
            self.account.login_async();self.account.login_async()
            self.assertEqual(thread.call_count,1)

    def test_explicit_unapproved_model_denial_blocks_offline_cache(self):
        self.install()
        def response(path,*args,**kwargs):
            if path=='me/entitlement':return {'enabled':True}
            raise AccountError('DIRECTOR_MODEL_REQUIRED',503)
        self.client.call.side_effect=response;self.account.sync_once()
        self.assertFalse(self.account.allowed());self.assertTrue(self.account.tokens)


if __name__=='__main__':unittest.main()
