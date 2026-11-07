import csv
import gzip
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from demo_import_archive import ImportFailure, safe_name, inspect_archive, extract_entry
from demo_import_parser import package_csv


class ImportArchiveTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('DIRECTOR_RAR_FIXTURES') and os.environ.get('DIRECTOR_TEST_7Z'), 'external RAR fixtures and pinned 7-Zip required')
    def test_real_rar4_rar5_solid_and_rejections(self):
        fixtures=Path(os.environ['DIRECTOR_RAR_FIXTURES'])
        executable=Path(os.environ['DIRECTOR_TEST_7Z'])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('rar3-solid.rar','rar5-solid.rar','rar5-crc.rar','rar3-subdirs.rar','rar5-subdirs.rar','unicode.rar'):
                with self.subTest(name=name):
                    plan=inspect_archive(fixtures/name,'rar',executable,max_entries=100,max_expanded=10_000_000)
                    for index,entry in enumerate(plan):
                        if not entry.directory:
                            target=root/(name+str(index))
                            extract_entry(fixtures/name,entry,target,executable,20)
                            self.assertEqual(target.stat().st_size,entry.size)
            for name in ('rar3-comment-hpsw.rar','rar5-hpsw.rar','rar5-psw.rar','rar3-vols.part1.rar','rar5-vols.part1.rar','rar3-symlink-unix.rar','rar5-hlink.rar','rar5-symlink-win.rar','rar5-evil-symlink-traversal.rar'):
                with self.subTest(name=name),self.assertRaises(ImportFailure):
                    inspect_archive(fixtures/name,'rar',executable,max_entries=100,max_expanded=10_000_000)

    def test_paths_reject_escape_and_ambiguity(self):
        for value in ('../a.dem', '/a.dem', 'a\\b.dem', 'a:stream', 'a//b', 'a./b',
                      'NUL.dem', 'a\nb.dem', 'a/' * 17 + 'b.dem'):
            with self.subTest(value=value), self.assertRaises(ImportFailure):
                safe_name(value)
        self.assertEqual(safe_name('比赛/第一图.dem'), '比赛/第一图.dem')

    def test_result_parts_are_deterministic_complete_and_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'source.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['label', 'feature'])
                writer.writerows((i % 2, 'row-' + str(i) + '-中文' * 30) for i in range(8000))
            parts = package_csv(path, root, 8 * 1024 * 1024)
            self.assertGreater(len(parts), 1)
            self.assertEqual(parts, package_csv(path, root, 8 * 1024 * 1024))
            count = 0
            for part in parts:
                raw = (root / part['sha256']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), part['sha256'])
                rows = list(csv.DictReader(io.StringIO(gzip.decompress(raw).decode('utf-8'))))
                self.assertEqual(len(rows), part['rows'])
                count += len(rows)
            self.assertEqual(count, 8000)

    def test_result_budget_never_truncates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'data.csv').write_text('label,feature\n1,1.25\n', encoding='utf-8')
            with self.assertRaises(ImportFailure):
                package_csv(root / 'data.csv', root, 1)

    @unittest.skipUnless(os.environ.get('DIRECTOR_TEST_7Z'), 'explicit pinned 7-Zip required')
    def test_real_zip_and_7z_chinese_and_malicious_paths(self):
        executable = Path(os.environ['DIRECTOR_TEST_7Z']).resolve(strict=True)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '比赛').mkdir()
            payload = b'PBDEMS2\0' + b'0123456789' * 100
            (root / '比赛' / '测试.dem').write_bytes(payload)
            for kind in ('zip', '7z'):
                archive = root / ('test.' + kind)
                subprocess.run([str(executable), 'a', '-t' + kind, str(archive), '比赛'],
                               cwd=root, stdout=subprocess.DEVNULL, check=True)
                plan = inspect_archive(archive, kind, executable, max_entries=10, max_expanded=100000)
                item = next(e for e in plan if not e.directory)
                target = root / (kind + '.raw')
                extract_entry(archive, item, target, executable, 10)
                self.assertEqual(target.read_bytes(), payload)
                with self.assertRaises(ImportFailure):
                    inspect_archive(archive, kind, executable, max_entries=10, max_expanded=10)
            evil = root / 'evil.zip'
            with zipfile.ZipFile(evil, 'w') as archive:
                archive.writestr('../escape.dem', payload)
            with self.assertRaises(ImportFailure):
                inspect_archive(evil, 'zip', executable, max_entries=10, max_expanded=100000)
            encrypted = root / 'encrypted.7z'
            subprocess.run([str(executable), 'a', '-t7z', '-ptest-only', '-mhe=on', str(encrypted), '比赛'],
                           cwd=root, stdout=subprocess.DEVNULL, check=True)
            with self.assertRaises(ImportFailure):
                inspect_archive(encrypted, '7z', executable, max_entries=10, max_expanded=100000)


if __name__ == '__main__':
    unittest.main()
