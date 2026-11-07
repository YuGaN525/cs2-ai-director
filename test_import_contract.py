"""Frozen contract digest and boundary fixtures (jsonschema is a QA dependency)."""
import hashlib
import json
from pathlib import Path
import unittest


class ImportContractTests(unittest.TestCase):
    def test_manifest_and_route_coverage(self):
        root = Path(__file__).parent / 'contracts/director-demo-import-v1'
        manifest = json.loads((root / 'manifest.json').read_text())
        for name, digest in manifest.items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)
        paths = json.loads((root / 'openapi.json').read_text())['paths']
        for suffix in ('claim', 'files/{id}/heartbeat', 'files/{id}/parts/{index}',
                       'files/{id}/results/{hash}', 'files/{id}/finish'):
            self.assertIn('/api/internal/director-imports/' + suffix, paths)

    def test_fixtures(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:
            self.skipTest('Run with the contract QA environment (jsonschema)')
        root = Path(__file__).parent / 'contracts/director-demo-import-v1'
        for fixture in json.loads((root / 'fixtures.json').read_text()):
            schema = json.loads((root / (fixture['schema'] + '.schema.json')).read_text())
            Draft202012Validator.check_schema(schema)
            errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(fixture['value']))
            self.assertEqual(not errors, fixture['valid'])
