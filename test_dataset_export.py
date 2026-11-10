import csv
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import dataset_protocol as p
from dataset_export import export_csv,inspect_csv,sha_file


ROOT=Path(__file__).parent/'contracts/director-training-dataset-v1'


class DatasetExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'input.csv';self.run=str(uuid.uuid4())
        self.path.write_bytes(gzip.decompress((ROOT/'samples/minimal.0.csv.gz').read_bytes()))

    def test_runtime_matches_every_frozen_package(self):
        for c in json.loads((ROOT/'fixtures.json').read_text()):
            with self.subTest(c=c['id']):
                raw=(ROOT/c['initialize']).read_bytes();files={i:(ROOT/f).read_bytes() for i,f in enumerate(c['files'])}
                if c['valid']:
                    value=p.validate_package(raw,files);self.assertEqual(value['requestHash'],c['requestHash'])
                else:
                    with self.assertRaisesRegex(p.Invalid,'^'+c['error']+'$'):p.validate_package(raw,files)

    def test_export_preserves_36_columns_and_group_completeness(self):
        before=self.path.read_bytes();out=self.root/'export'
        data=export_csv(self.path,out,self.run)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(p.validate_directory(out),data)
        raw=gzip.decompress(p.file_path(out,0).read_bytes())
        rows=list(csv.DictReader(raw.decode().splitlines()))
        self.assertEqual(list(rows[0]),p.BASE+p.EXTRA)
        self.assertEqual([r['target_slot'] for r in rows],['1','2'])
        self.assertEqual(len({r['group'] for r in rows}),1)
        self.assertEqual(data['manifest']['totals']['rows'],2)
        self.assertEqual(data['manifest']['sources'][0]['identityQuality'],'recording-only')

    def test_same_run_export_is_reproducible_and_new_run_separate(self):
        a=export_csv(self.path,self.root/'a',self.run)
        b=export_csv(self.path,self.root/'b',self.run)
        c=export_csv(self.path,self.root/'c',str(uuid.uuid4()))
        self.assertEqual(a,b)
        self.assertNotEqual(a['clientDatasetId'],c['clientDatasetId'])
        self.assertEqual(a['manifest']['files'][0]['sha256'],c['manifest']['files'][0]['sha256'])
        with self.assertRaises(FileExistsError):export_csv(self.path,self.root/'a',self.run)

    def test_no_personal_columns_leave_legacy_export(self):
        original=list(csv.DictReader(gzip.decompress((ROOT/'samples/legacy.0.csv.gz').read_bytes()).decode().splitlines()))
        for n,row in enumerate(original):
            row.update(target_name='private name',target_steamid='76561198000000000',reason='C:/private',timestamp=str(1700000000+n))
        with self.path.open('w',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(original[0]));writer.writeheader();writer.writerows(original)
        out=self.root/'legacy';data=export_csv(self.path,out,self.run)
        raw=gzip.decompress(p.file_path(out,0).read_bytes())
        for word in (b'private',b'7656119',b'1700000000',b'reason'):self.assertNotIn(word,raw)
        rows=list(csv.DictReader(raw.decode().splitlines()));self.assertEqual([r['timestamp'] for r in rows],['0','1'])
        self.assertEqual(data['manifest']['sources'][0]['identityQuality'],'unknown')
        self.assertEqual(list(rows[0]),p.BASE)

    def test_source_change_and_invalid_label_are_rejected(self):
        expected=sha_file(self.path);self.path.write_bytes(self.path.read_bytes()+b'\n')
        with self.assertRaisesRegex(p.Invalid,'INPUT_CHANGED'):export_csv(self.path,self.root/'bad',self.run,expected_hash=expected)
        raw=self.path.read_text().replace(',1,1,,',',2,1,,')
        self.path.write_text(raw)
        with self.assertRaises(p.Invalid):inspect_csv(self.path)

    def test_missing_source_columns_are_not_invented(self):
        rows=list(csv.DictReader(self.path.read_text().splitlines()))
        rows[0]['source_tick']=''
        self.path.write_bytes(p.csv_bytes(rows,p.BASE+p.EXTRA))
        with self.assertRaisesRegex(p.Invalid,'MIXED_ROW_SCHEMAS'):inspect_csv(self.path)


if __name__=='__main__':unittest.main()
