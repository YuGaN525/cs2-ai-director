import base64
import copy
import unittest
import hashlib
import json
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from client_update_manifest import canonical,verify,PRODUCT,SCHEMA
from client_update import UpdateProtocolError


class ManifestTests(unittest.TestCase):
    def test_shared_golden_vectors_and_canonical_bytes(self):
        root=Path(__file__).parent/'contracts/director-client-update-v1'
        for name,digest in json.loads((root/'manifest.json').read_text()).items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest)
        trust=json.loads((root/'test-public-trust.json').read_text())
        data=json.loads((root/'fixtures.json').read_text(encoding='utf-8'))
        self.assertEqual(canonical(data['fixtures'][0]['envelope']['payload']),(root/'payload.canonical.txt').read_bytes())
        for fixture in data['fixtures']:
            with self.subTest(name=fixture['name']):
                try:
                    verify(fixture['envelope'],trust,channel=data['channel'],platform=data['platform'],now=data['now'])
                    valid=True
                except UpdateProtocolError:valid=False
                self.assertEqual(valid,fixture['valid'])

    def setUp(self):
        self.key=Ed25519PrivateKey.generate()
        self.trust={'schema':SCHEMA,'origin':'https://example.org','keys':{'update-test':base64.b64encode(self.key.public_key().public_bytes_raw()).decode()}}
        self.payload=dict(schema=SCHEMA,product=PRODUCT,origin='https://example.org',channel='stable',
            release_id='10000000-0000-0000-0000-000000000001',version='1.14.0',platform='windows-x86_64',
            sha256='a'*64,bytes=200000000,sequence=2,issued_at=100,expires_at=1000,minimum_updater=1,notes='test')

    def signed(self,payload=None):
        payload=copy.deepcopy(self.payload if payload is None else payload)
        return dict(key_id='update-test',payload=payload,signature=base64.b64encode(self.key.sign(canonical(payload))).decode())

    def check(self,value,**kw):
        return verify(value,self.trust,channel='stable',platform='windows-x86_64',now=200,**kw)

    def test_valid_repeat_and_replay(self):
        value=self.signed();result=self.check(value)
        self.assertEqual(self.check(value,highest_sequence=2,previous_digest=result['payload_digest']),result)
        with self.assertRaises(UpdateProtocolError):self.check(value,highest_sequence=3)
        with self.assertRaises(UpdateProtocolError):self.check(value,highest_sequence=2,previous_digest='b'*64)

    def test_tamper_unknown_key_and_wrong_purpose(self):
        for field,value in [('version','1.15.0'),('product','identity'),('origin','https://evil.invalid')]:
            signed=self.signed();signed['payload'][field]=value
            with self.assertRaises(UpdateProtocolError):self.check(signed)
        signed=self.signed();signed['key_id']='model-web'
        with self.assertRaises(UpdateProtocolError):self.check(signed)

    def test_signed_but_wrong_target_time_or_shape(self):
        for field,value in [('channel','beta'),('platform','linux-x86_64'),('product','identity'),
            ('issued_at',201),('expires_at',199),('expires_at',9999999),('bytes',True),
            ('version','v2'),('minimum_updater',999),('sequence',False)]:
            payload=copy.deepcopy(self.payload);payload[field]=value
            with self.subTest(field=field),self.assertRaises(UpdateProtocolError):self.check(self.signed(payload))


if __name__=='__main__':unittest.main()
