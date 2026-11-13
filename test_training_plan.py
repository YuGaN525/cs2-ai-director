import csv
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dataset_protocol as p
from training_plan import prepare_batches, preflight_demos, legacy_notice


class TrainingPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.input = self.root/'input.csv'
        fixture = Path(__file__).parent/'contracts/director-training-dataset-v1/samples/minimal.0.csv.gz'
        self.rows = list(csv.DictReader(gzip.decompress(fixture.read_bytes()).decode().splitlines()))

    def write_sources(self, count):
        rows=[]
        # Interleave each recording's two candidates to catch contiguous-only splitting.
        for candidate in self.rows:
            for index in range(count):
                row=dict(candidate,match_source_id=f'{index+1:064x}')
                rows.append(row)
        self.input.write_bytes(p.csv_bytes(rows,p.BASE+p.EXTRA))

    def test_interleaved_complete_sources_no_rows_lost_and_each_package_valid(self):
        self.write_sources(3);before=self.input.read_bytes()
        plan=prepare_batches(self.input,self.root/'plan',target_rows=3)
        self.assertEqual(self.input.read_bytes(),before)
        self.assertEqual([b['rows'] for b in plan['batches']],[2,2,2])
        seen=[]
        for batch in plan['batches']:
            rows=list(csv.DictReader(Path(batch['csv']).read_text().splitlines()))
            self.assertEqual({r['target_slot'] for r in rows},{'1','2'})
            self.assertEqual(len({r['match_source_id'] for r in rows}),1)
            seen.append(rows[0]['match_source_id'])
            self.assertTrue(batch['uploadReady'])
        self.assertEqual(len(set(seen)),3)
        self.assertEqual(json.loads((self.root/'plan/plan.json').read_text('utf-8')),plan)

    def test_soft_target_does_not_split_one_source(self):
        self.write_sources(1)
        plan=prepare_batches(self.input,self.root/'plan',target_rows=1)
        self.assertEqual(len(plan['batches']),1)
        self.assertEqual(Path(plan['batches'][0]['csv']).read_bytes(),self.input.read_bytes())

    def test_single_source_over_hard_limit_fails_before_training_and_keeps_original(self):
        self.write_sources(1);before=self.input.read_bytes()
        with patch.dict(p.LIMITS,maxRows=1):
            with self.assertRaisesRegex(ValueError,'单个完整来源'):
                prepare_batches(self.input,self.root/'plan',target_rows=1)
        self.assertEqual(self.input.read_bytes(),before)
        self.assertFalse((self.root/'plan').exists())

    def test_partial_metadata_and_incomplete_candidate_group_rejected(self):
        self.write_sources(1)
        rows=list(csv.DictReader(self.input.read_text().splitlines()))
        rows[0]['source_tick']=''
        self.input.write_bytes(p.csv_bytes(rows,p.BASE+p.EXTRA))
        with self.assertRaisesRegex(ValueError,'元数据不完整'):
            prepare_batches(self.input,self.root/'badmeta')
        self.input.write_bytes(p.csv_bytes(self.rows[:1],p.BASE+p.EXTRA))
        with self.assertRaisesRegex(ValueError,'GROUP_SIZE'):
            prepare_batches(self.input,self.root/'badgroup')
        self.assertFalse((self.root/'badgroup/plan.json').exists())

    def test_quota_and_disk_fail_before_destination_and_never_parse(self):
        self.write_sources(1)
        with self.assertRaisesRegex(ValueError,'quota'):
            prepare_batches(self.input,self.root/'plan',check_space=Mock(side_effect=ValueError('quota')))
        self.assertFalse((self.root/'plan').exists())
        demo=self.root/'sample.dem';demo.write_bytes(b'original')
        with patch('training_plan.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError,'磁盘空间不足'):
                preflight_demos([demo],self.root)
        self.assertEqual(demo.read_bytes(),b'original')

    def test_input_mutation_prevents_committed_plan(self):
        self.write_sources(1)
        with patch('training_plan.sha_file',side_effect=['a','b']):
            with self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):
                prepare_batches(self.input,self.root/'changed')
        self.assertFalse((self.root/'changed/plan.json').exists())

    def test_legacy_local_training_preserved_and_notice_not_based_on_ambiguity(self):
        columns=p.FEATURES+['label']
        self.input.write_bytes(p.csv_bytes([{k:'1' for k in columns}],columns))
        plan=prepare_batches(self.input,self.root/'legacy')
        self.assertFalse(plan['batches'][0]['uploadReady'])
        self.assertIn('旧格式',legacy_notice(plan['batches'][0]['sourceKind']))
        self.assertEqual(legacy_notice('demo-derived'),'')

    def test_progress_explains_verification_backoff_and_not_model_release(self):
        from dataset_runtime import describe, progress_summary
        row=dict(id='a',state='pending',paused=0,governance=None,error='SERVER_VERIFYING',next_at=120)
        self.assertIn('20 秒',describe(row,now=100))
        self.assertIn('收到回执',describe(row,now=100))
        row.update(state='uploading',error='')
        summary=progress_summary([row],{'a':(1048576,2097152)})
        self.assertIn('1.0/2.0 MiB',summary)
        self.assertIn('不等于纳入或发布',summary)


if __name__=='__main__': unittest.main()
