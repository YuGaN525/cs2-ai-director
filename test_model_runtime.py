import base64,gzip,hashlib,io,json,tempfile,time,unittest
from pathlib import Path
from unittest import mock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from device_identity import DeviceSession,canonical,b64
from model_runtime import ModelManager,ModelError,ModelNetworkError,PURPOSE,FEATURE_HASH,DECISION_FEATURES
from public_trust import read_public_trust
from test_account_client import Protector


class Response(io.BytesIO):
    def __init__(self,data,status=200,headers=None):super().__init__(data);self.status=status;self.headers=headers or {}


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.now=time.time();self.signer=Ed25519PrivateKey.generate();self.pub=self.signer.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        self.session=DeviceSession(self.root/'identity','https://test.invalid',{},protector=Protector())
        self.manager=ModelManager(self.session,self.root,{'model':self.pub},clock=lambda:self.now)
        self.client=mock.Mock(origin='https://test.invalid',timeout=1);self.client.opener.open.side_effect=self.open
        self.forest={'n_features':8,'feature_version':'pov-v1','features':DECISION_FEATURES,'classes':[0,1],
                     'trees':[{'left':[-1],'right':[-1],'feature':[-2],'threshold':[-2.],'value':[[1.,3.]]}]}
        self.bytes=gzip.compress(canonical(self.forest),mtime=0);self.ranges=[]
        self.envelope=self.signed();self.client.call.side_effect=self.call

    def signed(self,sequence=1,model_id='11111111-1111-1111-1111-111111111111',current=True,revoked=(),**changes):
        meta={'model_id':model_id,'artifact_sha256':hashlib.sha256(self.bytes).hexdigest(),'artifact_bytes':len(self.bytes),
              'model_format':'forest-json-v1','feature_version':'pov-v1','feature_manifest_sha256':FEATURE_HASH,'features':DECISION_FEATURES,
              'min_client_version':'1.11.0','core_major':0,'max_unpacked_bytes':64*1024*1024,
              'test_vectors':[{'features':['0']*8,'probability':'0.75'} for _ in range(8)],'published_at':int(self.now)}
        p={'manifest_schema':1,'key_id':'model','product':'cs2-ai-director','channel':'stable','server_origin':'https://test.invalid',
           'release_sequence':sequence,'issued_at':int(self.now),'metadata_expires_at':int(self.now+86400),'revoked_models':list(revoked),'current':meta if current else None}
        p.update(changes);return {'payload':p,'signature':b64(self.signer.sign(PURPOSE+canonical(p)))}

    def call(self,path,*args,**kwargs):
        if path=='models/manifest':return self.envelope
        if path.endswith('/ticket'):return {'ticket':'opaque','path':'/api/director/models/'+self.envelope['payload']['current']['model_id']+'/file'}
        raise AssertionError(path)

    def open(self,request,**kwargs):
        offset=int(request.headers.get('Range','bytes=0-').split('=')[1][:-1]);self.ranges.append(offset)
        return Response(self.bytes[offset:],206 if offset else 200,{'Content-Range':f'bytes {offset}-{len(self.bytes)-1}/{len(self.bytes)}'})

    def test_first_initialization_needs_signed_download(self):
        self.assertIsNone(self.manager.cached_model());self.manager.sync(self.client,'d1_test')
        self.assertEqual(self.manager.cached_model().current_hash(),hashlib.sha256(self.bytes).hexdigest())

    def test_unknown_signer_rejected(self):
        self.manager.keys={}
        with self.assertRaises(ModelError):self.manager.sync(self.client,'d1_test')
        self.assertIsNone(self.manager.cached_model())

    def test_expired_fresh_metadata_rejected(self):
        self.envelope=self.signed(issued_at=int(self.now-100000),metadata_expires_at=int(self.now-1000))
        with self.assertRaises(ModelError):self.manager.sync(self.client,'d1_test')

    def test_wrong_origin_rejected(self):
        self.envelope=self.signed(server_origin='https://wrong.invalid')
        with self.assertRaises(ModelError):self.manager.sync(self.client,'d1_test')

    def test_feature_order_and_client_version_enforced(self):
        for key,value in [('features',list(reversed(DECISION_FEATURES))),('min_client_version','99.0.0')]:
            envelope=self.signed();envelope['payload']['current'][key]=value
            envelope['signature']=b64(self.signer.sign(PURPOSE+canonical(envelope['payload'])))
            with self.assertRaises(ModelError):self.manager.verify(envelope)

    def test_lower_catalogue_sequence_cannot_roll_back(self):
        self.envelope=self.signed(sequence=2);self.manager.sync(self.client,'d1_test');self.envelope=self.signed(sequence=1)
        with self.assertRaises(ModelError):self.manager.sync(self.client,'d1_test')

    def test_revocation_survives_restart_and_old_signed_manifest(self):
        self.manager.sync(self.client,'d1_test');old=self.envelope
        self.envelope=self.signed(sequence=2,current=False,revoked=['11111111-1111-1111-1111-111111111111'])
        self.manager.sync(self.client,'d1_test');self.assertIsNone(self.manager.cached_model())
        other=ModelManager(self.session,self.root,{'model':self.pub},clock=lambda:self.now);self.assertIsNone(other.cached_model())
        self.envelope=old
        with self.assertRaises(ModelError):other.sync(self.client,'d1_test')

    def test_cached_model_can_outlive_metadata_until_account_lease_expires(self):
        self.manager.sync(self.client,'d1_test');self.now+=2*86400
        self.assertIsNotNone(self.manager.cached_model())

    def test_immutable_runtime_barrier_defers_new_generation(self):
        self.manager.sync(self.client,'d1_test');old=self.manager.cached_model().current_hash();self.assertTrue(self.manager.execution_lock.acquire())
        self.addCleanup(self.manager.execution_lock.release)
        self.forest['revision']=2;self.bytes=gzip.compress(canonical(self.forest),mtime=0);self.envelope=self.signed(2,'22222222-2222-2222-2222-222222222222')
        self.manager.sync(self.client,'d1_test');self.assertEqual(self.manager.cached_model().current_hash(),old)
        self.manager.execution_lock.release();self.manager.sync(self.client,'d1_test');self.assertNotEqual(self.manager.cached_model().current_hash(),old)

    def test_corrupted_update_keeps_good_active_model(self):
        self.manager.sync(self.client,'d1_test');old=self.manager.cached_model().current_hash()
        self.bytes=b'broken-model-file-123456';self.envelope=self.signed(2,'22222222-2222-2222-2222-222222222222')
        with self.assertRaises(ModelError):self.manager.sync(self.client,'d1_test')
        self.assertEqual(self.manager.cached_model().current_hash(),old)

    def test_cache_tamper_fails_closed(self):
        self.manager.sync(self.client,'d1_test');self.manager.cached_model().path.write_bytes(b'bad')
        self.assertIsNone(self.manager.cached_model())

    def test_interrupted_download_resumes_from_persisted_bytes(self):
        part=self.manager.root/(hashlib.sha256(self.bytes).hexdigest()+'.part');part.write_bytes(self.bytes[:40])
        self.manager.sync(self.client,'d1_test');self.assertEqual(self.ranges,[40]);self.assertIsNotNone(self.manager.cached_model())

    def test_explicit_download_auth_failure_has_status(self):
        import urllib.error
        self.client.opener.open.side_effect=urllib.error.HTTPError('https://test.invalid',401,'unauthorized',{},io.BytesIO(b'{}'))
        with self.assertRaises(ModelNetworkError) as caught:self.manager.sync(self.client,'d1_test')
        self.assertEqual(caught.exception.status,401)

    def test_release_trust_configuration_rejects_secrets_and_reused_keys(self):
        pub=base64.b64encode(self.pub).decode();other=base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
        value={'server_origin':'https://test.invalid','trusted_public_keys':{'identity':other},'trusted_model_keys':{'model':pub}};path=self.root/'public.json';path.write_text(json.dumps(value));self.assertEqual(read_public_trust(path)['server_origin'],'https://test.invalid')
        value['private_key']='never bundle';path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):read_public_trust(path)
        value.pop('private_key');value['trusted_public_keys']={'identity':pub};path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):read_public_trust(path)


if __name__=='__main__':unittest.main()
