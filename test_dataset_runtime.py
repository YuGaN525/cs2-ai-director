import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dataset_runtime import DatasetRuntime
from dataset_transport import DatasetFailure,retry_seconds
import test_dataset_store
from test_dataset_transport import MemoryServer
from upload_worker import UploadIdentity


class DatasetRuntimeTests(unittest.TestCase):
    setUp=test_dataset_store.DatasetStoreTests.setUp
    success=test_dataset_store.DatasetStoreTests.success
    def runtime(self,run_id):
        identity=UploadIdentity(self.binding,'fixture')
        data=json.loads(self.store.get(run_id)['descriptor'])
        self.client=MemoryServer(identity,data)
        account=SimpleNamespace(upload_identity=lambda:self.client.current,binding=lambda:self.binding)
        return DatasetRuntime(account,self.store.root,self.client,autostart=False)

    def test_runtime_persists_lost_complete_receipt_after_restart(self):
        rid=self.success();runtime=self.runtime(rid);self.client.lose_complete=True
        self.assertTrue(runtime.run_once())
        self.assertEqual(self.store.get(rid)['state'],'received')
        runtime=DatasetRuntime(runtime.account,self.store.root,self.client,autostart=False)
        runtime.run_once()
        self.assertEqual(sum(route.endswith('/complete') for _,route in self.client.calls),1)

    def test_packaging_success_marker_recovers_but_training_does_not(self):
        rid=self.success();runtime=self.runtime(rid)
        other=self.store.freeze(self.input,self.binding);self.store.start_training(other)
        with self.store.transaction() as db:db.execute("UPDATE runs SET state='packaging',descriptor=NULL WHERE id=?",(rid,))
        runtime.run_once()
        self.assertEqual(self.store.get(rid)['state'],'received')
        self.assertEqual(self.store.get(other)['state'],'training')

    def test_server_retry_after_is_persisted(self):
        rid=self.success();runtime=self.runtime(rid)
        with patch.object(runtime.client,'upload',side_effect=DatasetFailure('RATE_LIMITED',429,900)):
            runtime.run_once()
        self.assertGreaterEqual(self.store.get(rid)['next_at']-self.store.clock(),899)
        self.assertEqual(retry_seconds('120'),120);self.assertEqual(retry_seconds('broken'),0)

    def test_refreshed_device_credential_resumes_reauth_blocked_run(self):
        rid=self.success();runtime=self.runtime(rid)
        with patch.object(runtime.client,'upload',side_effect=DatasetFailure('DIRECTOR_REAUTH_REQUIRED',401)):
            runtime.run_once()
        self.assertEqual(self.store.get(rid)['state'],'blocked')
        runtime.client.current=UploadIdentity(self.binding,'refreshed-fixture')
        self.assertTrue(runtime.run_once())
        self.assertEqual(self.store.get(rid)['state'],'received')

    def test_cancelled_replacement_keeps_old_evidence_and_samples(self):
        rid=self.success();old=self.store.get(rid)
        self.store.cancelled(rid,self.binding);new_id=self.store.renew(rid,self.binding)
        self.assertNotEqual(new_id,rid)
        new=json.loads(self.store.get(new_id)['descriptor']);original=json.loads(old['descriptor'])
        self.assertNotEqual(new['clientDatasetId'],original['clientDatasetId'])
        self.assertEqual(new['manifest'],original['manifest'])
        self.assertEqual(self.store.get(rid)['state'],'cancelled')
        self.assertTrue(self.store.input_path(rid).is_file())


if __name__=='__main__':unittest.main()
