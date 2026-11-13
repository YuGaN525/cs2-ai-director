import gzip
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from dataset_store import DatasetStore
from upload_outbox import Binding
import dataset_protocol as p


class DatasetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.input=self.root/'input.csv';self.input.write_bytes(gzip.decompress((Path(__file__).parent/'contracts/director-training-dataset-v1/samples/minimal.0.csv.gz').read_bytes()))
        self.binding=Binding(str(uuid.uuid4()),'https://www.yuzibei.cn',str(uuid.uuid4()))
        self.store=DatasetStore(self.root/'state')

    def success(self):
        rid=self.store.freeze(self.input,self.binding);self.store.start_training(rid)
        self.store.training_succeeded(rid,{'samples':2});return rid

    def test_two_runs_do_not_overwrite_and_restart_preserves_queue(self):
        a=self.success();raw=self.store.input_path(a).read_bytes()
        b=self.success();self.assertNotEqual(a,b)
        self.input.write_text('changed')
        self.assertEqual(self.store.input_path(a).read_bytes(),raw)
        other=DatasetStore(self.store.root)
        self.assertEqual(len(other.list(self.binding)),2)
        self.assertEqual(other.next(self.binding)['id'],a)
        self.assertEqual(other.get(a)['state'],'pending')

    def test_failed_or_interrupted_training_is_never_queued(self):
        rid=self.store.freeze(self.input,self.binding);self.store.start_training(rid)
        self.assertIsNone(DatasetStore(self.store.root).next(self.binding))
        self.store.fail_training(rid)
        self.assertIsNone(self.store.next(self.binding));self.assertEqual(self.store.get(rid)['state'],'failed')

    def test_input_changed_after_snapshot_rejected(self):
        rid=self.store.freeze(self.input,self.binding);self.store.input_path(rid).write_text('corrupt')
        with self.assertRaisesRegex(p.Invalid,'INPUT_CHANGED'):self.store.start_training(rid)

    def test_packaging_failure_does_not_erase_success(self):
        rid=self.store.freeze(self.input,self.binding);self.store.start_training(rid)
        self.store.input_path(rid).write_text('corrupt')
        self.assertIsNone(self.store.training_succeeded(rid,{'samples':2}))
        row=self.store.get(rid);self.assertEqual(row['state'],'package-error');self.assertEqual(json.loads(row['result'])['samples'],2)
        self.assertIsNone(self.store.next(self.binding))

    def test_pause_cross_account_and_binding(self):
        rid=self.success();other=Binding(str(uuid.uuid4()),self.binding.origin,self.binding.device)
        self.assertIsNone(self.store.next(other))
        with self.assertRaisesRegex(p.Invalid,'IDENTITY_CHANGED'):self.store.set_paused(rid,other,True)
        self.store.set_paused(rid,self.binding,True);self.assertIsNone(self.store.next(self.binding))
        self.store.retry(rid,self.binding);self.assertEqual(self.store.next(self.binding)['id'],rid)
        with self.assertRaisesRegex(p.Invalid,'IDENTITY_CHANGED'):self.store.bind_local(rid,self.binding,other)

    def test_unbound_claim_requires_explicit_local_actor_and_no_attempts(self):
        local=Binding('unbound','local','installation')
        rid=self.store.freeze(self.input,local);self.store.start_training(rid);self.store.training_succeeded(rid,{})
        self.assertIsNone(self.store.next(self.binding))
        self.store.bind_local(rid,local,self.binding);self.assertEqual(self.store.next(self.binding)['id'],rid)
        with self.assertRaisesRegex(p.Invalid,'IDENTITY_CHANGED'):self.store.bind_local(rid,local,self.binding)

    def test_receipt_and_governance_are_separate_immutable_evidence(self):
        rid=self.success();receipt={'id':'test-receipt'};self.store.received(rid,receipt)
        self.store.received(rid,receipt)
        with self.assertRaisesRegex(p.Invalid,'RECEIPT_CHANGED'):self.store.received(rid,{'id':'different'})
        self.store.save_governance(rid,{'revision':2,'status':'included'})
        with self.assertRaisesRegex(p.Invalid,'GOVERNANCE_ROLLBACK'):self.store.save_governance(rid,{'revision':1,'status':'pending-review'})
        with self.assertRaisesRegex(p.Invalid,'GOVERNANCE_CHANGED'):self.store.save_governance(rid,{'revision':2,'status':'excluded'})
        self.assertEqual(json.loads(self.store.get(rid)['receipt']),receipt)

    def test_resume_preserves_server_backoff_and_package_retry_clears_pause(self):
        rid=self.success()
        self.store.upload_failed(rid,'pending','NETWORK_UNAVAILABLE',120)
        due=self.store.get(rid)['next_at']
        self.store.set_paused(rid,self.binding,True)
        self.store.retry(rid,self.binding)
        self.assertEqual(self.store.get(rid)['next_at'],due)
        self.assertFalse(self.store.get(rid)['paused'])
        self.assertIsNone(self.store.next(self.binding))
        with self.store.transaction() as db:
            db.execute("UPDATE runs SET state='package-error',paused=1 WHERE id=?",(rid,))
        self.assertIsNotNone(self.store.retry(rid,self.binding))
        self.assertFalse(self.store.get(rid)['paused'])
        self.assertEqual(self.store.get(rid)['state'],'pending')


if __name__=='__main__':unittest.main()
