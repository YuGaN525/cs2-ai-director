import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import csv

import dataset_protocol as p
from dataset_reader import manifest,part_path,rows


class DatasetReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        fixtures=Path(__file__).parent/'contracts/director-training-dataset-v1/samples'
        data=json.loads((fixtures/'multi-bo.initialize.json').read_text());self.manifest=data['manifest']
        self.item=dict(id=str(uuid.uuid4()),kind='dataset',artifactHash=data['manifestSha256'],matchHash='c'*64,sourceId=data['manifest']['sources'][0]['sourceId'],readerVersion='training-dataset-v1',rows=2)
        self.path=self.root/(self.item['id']+'.csv.gz');self.path.write_bytes(p.canonical(self.manifest))
        for f in self.manifest['files']:
            if f['sourceId']==self.item['sourceId']:part_path(self.path,self.item,f['index']).write_bytes((fixtures/('multi-bo.'+str(f['index'])+'.csv.gz')).read_bytes())

    def test_reads_only_selected_source_without_other_source_files(self):
        output=list(rows(self.path,self.item));self.assertEqual(len(output),2)
        self.assertEqual(list(output[0])[:36],p.BASE+p.EXTRA)
        self.assertEqual(output[0]['recording_source_id'],self.manifest['sources'][0]['recordingId'])

    def test_server_match_and_reader_identity_required(self):
        for field,value in [('sourceId',str(uuid.uuid4())),('readerVersion','future'),('matchHash','filename-team-A'),('rows',3)]:
            item=dict(self.item,**{field:value})
            with self.subTest(field=field),self.assertRaises(ValueError):list(rows(self.path,item))

    def test_manifest_and_file_tamper_rejected(self):
        target=part_path(self.path,self.item,0);target.write_bytes(target.read_bytes()+b'x')
        with self.assertRaisesRegex(p.Invalid,'FILE_LENGTH'):list(rows(self.path,self.item))
        self.path.write_bytes(self.path.read_bytes()+b' ')
        with self.assertRaisesRegex(p.Invalid,'DATASET_MANIFEST_HASH'):manifest(self.path,self.item)

    def test_cannot_escape_source_folder(self):
        with self.assertRaises(p.Invalid):part_path(self.path,dict(self.item,id='../other'),0)

    def test_worker_uses_verified_match_and_deduplicates_recorded_samples(self):
        import cloud_worker
        other=dict(self.item,id=str(uuid.uuid4()))
        other_path=self.root/(other['id']+'.csv.gz');other_path.write_bytes(self.path.read_bytes())
        for f in self.manifest['files']:
            if f['sourceId']==self.item['sourceId']:part_path(other_path,other,f['index']).write_bytes(part_path(self.path,self.item,f['index']).read_bytes())
        spec=dict(jobId=str(uuid.uuid4()),request={'parameters':dict(maxMemoryMb=1024,maxSeconds=30,trees=50,maxDepth=4)},snapshot=dict(manifestHash='f'*64,manifest=dict(schemaVersion=3,items=[self.item,other])))
        (self.root/'spec.json').write_text(json.dumps(spec));(self.root/'candidate.submission.json').write_text('{}')
        with patch('train_decision_model.train_from_csv',return_value={}):cloud_worker.train_child(self.root)
        with (self.root/'dataset.csv').open(newline='') as stream:output=list(csv.DictReader(stream))
        self.assertEqual(len(output),2)
        self.assertEqual({r['match_source_id'] for r in output},{'c'*64})
        report=json.loads((self.root/'result.json').read_text())['report']
        self.assertEqual(report['duplicateRows'],2)
        self.assertEqual(report['parserStats'][0]['reader'],'training-dataset-v1')


if __name__=='__main__':unittest.main()
