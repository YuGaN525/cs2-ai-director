import gzip
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import uuid

from dataset_runtime import DatasetRuntime
from upload_outbox import Binding


class DatasetTrainerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.csv=self.root/'input.csv';self.csv.write_bytes(gzip.decompress((Path(__file__).parent/'contracts/director-training-dataset-v1/samples/minimal.0.csv.gz').read_bytes()))
        binding=Binding(str(uuid.uuid4()),'http://127.0.0.1:5149',str(uuid.uuid4()))
        account=SimpleNamespace(binding=lambda:binding,upload_identity=lambda:None)
        from trainer_gui import TrainerApp
        self.app=TrainerApp.__new__(TrainerApp);self.app.datasets=DatasetRuntime(account,self.root/'queue',autostart=False)
        self.app.training_data=Mock();self.app._events=queue.Queue();self.app._dropped_log_lines=0
        self.binding=binding

    @staticmethod
    def inline_thread(*args,**kwargs):return SimpleNamespace(start=kwargs['target'])

    def test_success_uses_frozen_input_and_queues_after_training(self):
        seen=[]
        def train(csv,out,metrics,**kwargs):
            self.assertNotEqual(csv,self.csv);self.assertEqual(csv.read_bytes(),self.csv.read_bytes())
            self.assertIsNone(self.app.datasets.store.next(self.binding));seen.append(out)
            self.csv.write_text('overwritten after snapshot')
            return dict(model_json=str(out.with_suffix('.json.gz')),samples=2)
        with patch('trainer_gui.threading.Thread',side_effect=self.inline_thread),patch('trainer_gui.train_from_csv',side_effect=train):
            self.app._run_worker(self.csv,self.root/'decision_model.pkl',self.root/'metrics.json',(1,2,2,.25))
        row=self.app.datasets.store.next(self.binding);self.assertIsNotNone(row)
        self.assertIn(row['id'],str(seen[0]));self.app.training_data.capture_training_file.assert_not_called()
        self.assertIn('done',[x[0] for x in list(self.app._events.queue)])

    def test_failure_does_not_queue_and_does_not_touch_old_pipeline(self):
        with patch('trainer_gui.threading.Thread',side_effect=self.inline_thread),patch('trainer_gui.train_from_csv',side_effect=RuntimeError('expected')):
            self.app._run_worker(self.csv,self.root/'decision_model.pkl',self.root/'metrics.json',(1,2,2,.25))
        self.assertIsNone(self.app.datasets.store.next(self.binding))
        self.assertEqual(self.app.datasets.store.list(self.binding)[0]['state'],'failed')
        self.app.training_data.capture_training_file.assert_not_called()
        self.assertIn('error',[x[0] for x in list(self.app._events.queue)])

    def test_export_error_does_not_misreport_success_as_training_failure(self):
        with patch('trainer_gui.threading.Thread',side_effect=self.inline_thread),patch('trainer_gui.train_from_csv',return_value={'model_json':'local.json.gz','samples':2}),patch.object(self.app.datasets,'succeeded',side_effect=OSError('disk')):
            self.app._run_worker(self.csv,self.root/'decision_model.pkl',self.root/'metrics.json',(1,2,2,.25))
        kinds=[x[0] for x in list(self.app._events.queue)];self.assertIn('done',kinds);self.assertNotIn('error',kinds)

    def test_cli_freezes_equal_form_arguments_and_isolates_two_runs(self):
        import json
        from trainer_gui import _csv_cli
        outputs=[]
        def main(args):
            csv=Path(args[args.index('--csv')+1]);out=Path(args[args.index('--out')+1]);metrics=Path(args[args.index('--metrics')+1])
            self.assertNotEqual(csv,self.csv);self.assertEqual(csv.read_bytes(),self.csv.read_bytes())
            outputs.append(out);metrics.write_text(json.dumps({'samples':2}));return 0
        with patch('train_decision_model.main',side_effect=main):
            for _ in range(2):
                code=_csv_cli(['--csv='+str(self.csv),'--out='+str(self.root/'model.pkl')],self.app.datasets)
                self.assertEqual(code,0)
        self.assertNotEqual(outputs[0],outputs[1]);self.assertEqual(len(self.app.datasets.store.list(self.binding)),2)

    def test_cli_failure_does_not_enqueue(self):
        from trainer_gui import _csv_cli
        with patch('train_decision_model.main',return_value=1):
            self.assertEqual(_csv_cli(['--csv',str(self.csv),'--out',str(self.root/'model.pkl')],self.app.datasets),1)
        self.assertIsNone(self.app.datasets.store.next(self.binding))

    def test_batch_failure_preserves_prior_receipt_queue_and_stops_later_training(self):
        import csv
        import dataset_protocol as p
        from training_plan import prepare_batches
        rows=list(csv.DictReader(self.csv.read_text().splitlines()))
        self.csv.write_bytes(p.csv_bytes([dict(row,match_source_id=f'{i:064x}') for i in range(1,4) for row in rows],p.BASE+p.EXTRA))
        def plan(*args,**kwargs):return prepare_batches(*args,**kwargs,target_rows=3)
        def train(csv,out,metrics,**kwargs):
            if '2' == str(len(self.app.datasets.store.list(self.binding))):raise RuntimeError('second batch failed')
            return dict(model_json=str(out.with_suffix('.json.gz')),samples=2)
        with patch('trainer_gui.threading.Thread',side_effect=self.inline_thread),patch('training_plan.prepare_batches',side_effect=plan),patch('trainer_gui.train_from_csv',side_effect=train) as fit:
            self.app._run_worker(self.csv,self.root/'model.pkl',self.root/'metrics.json',(1,2,2,.25))
        self.assertEqual(fit.call_count,2)
        self.assertCountEqual([r['state'] for r in self.app.datasets.store.list(self.binding)],['pending','failed'])
        error=next(v for kind,v in list(self.app._events.queue) if kind=='error')
        self.assertIn('已完成 1 批',error)
        completed=list((self.root/'batch-plans').glob('*/completed.json'))
        self.assertEqual(len(completed),1)

    def test_ai_search_uses_same_plan_and_preserves_independent_outputs(self):
        import csv
        import dataset_protocol as p
        from training_plan import prepare_batches
        rows=list(csv.DictReader(self.csv.read_text().splitlines()))
        self.csv.write_bytes(p.csv_bytes([dict(row,match_source_id=f'{i:064x}') for i in range(1,3) for row in rows],p.BASE+p.EXTRA))
        def plan(*args,**kwargs):return prepare_batches(*args,**kwargs,target_rows=3)
        outputs=[]
        def train(csv,out,metrics,**kwargs):
            self.assertFalse(kwargs['use_ai']);outputs.append(out)
            return {'outputs':{'model_json':str(out/'model.json.gz')}}
        with patch('trainer_gui.threading.Thread',side_effect=self.inline_thread),patch('training_plan.prepare_batches',side_effect=plan),patch('trainer_gui.ai_assist.run_ai_training',side_effect=train):
            self.app._run_worker(self.csv,self.root/'model.pkl',self.root/'metrics.json',(1,2,2,.25),ai_mode=False)
        self.assertEqual(len(set(outputs)),2)
        report=next(v for kind,v in list(self.app._events.queue) if kind=='ai_done')
        self.assertEqual(report['batch_count'],2)
        self.assertEqual(len(self.app.datasets.store.list(self.binding)),2)


if __name__=='__main__':unittest.main()
