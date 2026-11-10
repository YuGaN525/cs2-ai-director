import copy
from contextlib import closing
import json
import unittest
from unittest.mock import Mock
import uuid
import test_uploads
from upload_governance import CaptureGovernance,SCHEMA


class CaptureGovernanceTests(unittest.TestCase):
    setUp=test_uploads.UploadTests.setUp
    add=test_uploads.UploadTests.add
    batch=test_uploads.UploadTests.batch

    def prepare(self):
        batch=self.batch();receipt=test_uploads.receipt(batch);self.box.acknowledge(batch,receipt)
        value=dict(schema=SCHEMA,clientBatchId=batch['id'],transportStatus='confirmed',governanceStatus='pending-review',sources=[dict(sourceId=receipt['id'],revision=1,status='pending-review',blockers=['SOURCE_UNVERIFIED'])])
        client=Mock();caps={'governance':True,'governanceSchema':SCHEMA,'authentication':'device-entitlement'}
        client._request.side_effect=lambda req:copy.deepcopy(caps if req.full_url.endswith('/capabilities') else value)
        return batch,value,client,CaptureGovernance(self.box,client)

    def test_ordinary_uses_original_permission_and_keeps_receipt(self):
        batch,value,client,governance=self.prepare();governance.refresh(self.identity)
        self.assertIn('待管理员筛选',governance.summary(self.binding))
        urls=[call.args[0].full_url for call in client._request.call_args_list]
        self.assertTrue(all('/data/uploads/' in url for url in urls))
        with closing(self.box.connect()) as db:before=db.execute('SELECT receipt FROM batches').fetchone()[0]
        value['governanceStatus']='included';value['sources'][0].update(status='included',revision=2,blockers=[])
        governance.next_at=0;governance.refresh(self.identity)
        self.assertIn('已纳入',governance.summary(self.binding))
        with closing(self.box.connect()) as db:self.assertEqual(before,db.execute('SELECT receipt FROM batches').fetchone()[0])
        other=test_uploads.Binding(str(uuid.uuid4()),self.binding.origin,self.binding.device)
        self.assertIn('暂无筛选状态',governance.summary(other))

    def test_stale_or_conflicting_governance_cannot_replace_current(self):
        batch,value,client,governance=self.prepare();governance.refresh(self.identity)
        value['governanceStatus']='included';value['sources'][0]['status']='included'
        governance.next_at=0;governance.refresh(self.identity)
        self.assertIn('待管理员筛选',governance.summary(self.binding))
        self.assertNotIn('已纳入，等待',governance.summary(self.binding))


if __name__=='__main__':unittest.main()
