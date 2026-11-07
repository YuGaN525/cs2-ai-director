import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from demo_import_parser import package_csv
from derived_training_input import rows, manifest


class DerivedInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        raw = self.root / 'parsed.csv'
        with raw.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['label', 'feature'])
            writer.writerows((n % 2, '样本-' + str(n) + '-x' * 80) for n in range(8000))
        parts = package_csv(raw, self.root, 8 * 1024 * 1024)
        for part in parts:
            (self.root / part['sha256']).rename(self.root / ('test-' + part['sha256'] + '.csv.gz'))
        self.data = dict(format='director-derived-v1', version=1, parserVersion='demo-v2-live.1',
                         featureVersion='pov-v1', rawHash='a' * 64, rows=8000, parts=parts)
        self.path = self.root / 'test.csv.gz'
        self.item = dict(id='test', artifactHash='')
        self.commit()
        raw.unlink()  # Only durable parts and manifest survive, no raw Demo/CSV.

    def commit(self):
        content = json.dumps(self.data, separators=(',', ':')).encode()
        self.path.write_bytes(content)
        self.item['artifactHash'] = hashlib.sha256(content).hexdigest()

    def test_reads_all_parts_after_original_removed(self):
        result = list(rows(self.path, self.item, ['label', 'feature']))
        self.assertEqual(len(result), 8000)
        self.assertTrue(result[-1]['feature'].startswith('样本-7999-'))

    def test_rejects_corrupt_result_even_with_original_absent(self):
        part = self.data['parts'][0]
        path = self.root / ('test-' + part['sha256'] + '.csv.gz')
        payload = bytearray(path.read_bytes())
        payload[-12] ^= 1
        path.write_bytes(payload)
        with self.assertRaisesRegex(ValueError, 'derived_part_hash'):
            list(rows(self.path, self.item, ['label']))

    def test_rejects_manifest_mutation(self):
        self.path.write_bytes(self.path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'derived_manifest_hash'):
            list(rows(self.path, self.item, ['label']))

    def test_rejects_declared_row_count_mismatch(self):
        self.data['parts'][-1]['rows'] += 1
        self.data['rows'] += 1
        self.commit()
        with self.assertRaisesRegex(ValueError, 'derived_row_count'):
            list(rows(self.path, self.item, ['label']))

    def test_rejects_unknown_format_parser_and_missing_columns(self):
        for key, value in [('version', 2), ('parserVersion', 'future'), ('featureVersion', 'pov-v2')]:
            original = self.data[key]
            self.data[key] = value
            self.commit()
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'derived_manifest_version'):
                manifest(self.path, self.item['artifactHash'])
            self.data[key] = original
        self.commit()
        with self.assertRaisesRegex(ValueError, 'derived_columns'):
            list(rows(self.path, self.item, ['absent']))


if __name__ == '__main__':
    unittest.main()
