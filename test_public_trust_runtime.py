import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import public_trust as trust


class RuntimeTrustTests(unittest.TestCase):
    def test_frozen_bundle_and_explicit_invalid_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root/'bundle'
            bundle.mkdir()
            value = {'server_origin': 'https://test.invalid',
                     'trusted_public_keys': {'identity': base64.b64encode(bytes(range(32))).decode()},
                     'trusted_model_keys': {'model': base64.b64encode(bytes(range(1,33))).decode()}}
            path = bundle/'identity-server.json'
            path.write_text(json.dumps(value))
            with mock.patch.object(trust.sys,'frozen',True,create=True), mock.patch.object(trust.sys,'_MEIPASS',str(bundle),create=True), mock.patch.object(trust.sys,'executable',str(root/'director.exe')):
                self.assertEqual(trust.runtime_trust_path(),path)
                status = trust.public_trust_status()
                self.assertEqual(status['state'],'configured')
                self.assertFalse(status['formal_model_approval_verified'])
                path.write_text(json.dumps(value,indent=2))
                self.assertEqual(trust.public_trust_status(),status)
                override = root/'identity-server.json'
                override.write_text('{}')
                self.assertEqual(trust.runtime_trust_path(),override)
                self.assertEqual(trust.public_trust_status(),{'state':'invalid'})
                from account_client import default_account
                with mock.patch('account_client.DesktopAccount') as account:
                    default_account()
                    self.assertEqual(account.call_args.args[2],{})
                    self.assertEqual(account.call_args.kwargs['model_keys'],{})

    def test_missing_configuration_does_not_claim_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(trust,'runtime_trust_path',return_value=Path(directory)/'missing.json'):
                self.assertEqual(trust.public_trust_status(),{'state':'missing'})
