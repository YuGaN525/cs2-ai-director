import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import dataset_protocol as p
from dataset_transport import DatasetClient,DatasetFailure,validate_governance,validate_receipt,validate_state
from upload_outbox import Binding
from upload_worker import UploadIdentity

ROOT=Path(__file__).parent/'contracts/director-training-dataset-v1'


class MemoryServer(DatasetClient):
    def __init__(self,identity,data):
        self.current=identity;super().__init__(lambda:self.current)
        self.data=data;self.exists=False;self.finished=False;self.calls=[];self.parts={};self.lose_complete=False;self.lose_part=False;self.revoke_after_part=False

    def receipt_value(self):
        t=self.data['manifest']['totals']
        return dict(schema=p.SCHEMA,clientDatasetId=self.data['clientDatasetId'],datasetId=str(uuid.UUID(int=909)),manifestSha256=self.data['manifestSha256'],requestHash=p.digest(p.canonical(self.data)),receivedFiles=t['files'],receivedRows=t['rows'],acceptedRows=t['rows'],duplicateRows=0,receivedAt='2026-11-10T00:00:00Z',initialGovernanceStatus='pending-review')

    def state(self):
        return dict(schema=p.SCHEMA,clientDatasetId=self.data['clientDatasetId'],manifestSha256=self.data['manifestSha256'],requestHash=p.digest(p.canonical(self.data)),transportStatus='received' if self.finished else 'receiving',expiresAt='2026-11-17T00:00:00Z',receivedParts=[dict(fileIndex=i,parts=sorted(parts)) for i,parts in sorted(self.parts.items())],hasReceipt=self.finished)

    def call(self,identity,suffix='',method='GET',body=None,headers=None,continuing=lambda:True):
        self.check(identity,continuing);self.calls.append((method,suffix))
        if suffix=='/capabilities':
            caps=json.loads((ROOT/'capabilities.example.json').read_text());caps['enabled']=True;return 200,caps
        if suffix.endswith('/receipt'):
            if not self.finished:raise DatasetFailure('DIRECTOR_DATASET_RECEIPT_NOT_FOUND',404)
            return 200,self.receipt_value()
        if method=='POST' and suffix=='':
            self.exists=True;return 201,self.state()
        if '/files/' in suffix:
            pieces=suffix.split('/');file_index=int(pieces[-3]);part_index=int(pieces[-1])
            self.parts.setdefault(file_index,{})[part_index]=body
            if self.revoke_after_part:self.current=None
            if self.lose_part:self.lose_part=False;raise DatasetFailure('NETWORK_UNAVAILABLE')
            return 200,dict(schema=p.SCHEMA,clientDatasetId=self.data['clientDatasetId'],fileIndex=file_index,partIndex=part_index,bytes=len(body),sha256=p.digest(body))
        if suffix.endswith('/complete'):
            self.finished=True
            if self.lose_complete:self.lose_complete=False;raise DatasetFailure('NETWORK_UNAVAILABLE')
            return 200,self.receipt_value()
        if not self.exists:raise DatasetFailure('DIRECTOR_DATASET_NOT_FOUND',404)
        return 200,self.state()


class DatasetTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.data=json.loads((ROOT/'samples/minimal.initialize.json').read_text())
        raw=(ROOT/'samples/minimal.0.csv.gz').read_bytes();p.file_path(self.folder,0).write_bytes(raw)
        self.identity=UploadIdentity(Binding(str(uuid.uuid4()),'http://127.0.0.1:5149',str(uuid.uuid4())),'test-device-token')
        self.client=MemoryServer(self.identity,self.data)

    def large_transport(self):
        # Transport-only bytes; package semantic validation is covered by frozen package tests.
        raw=b'x'*(p.PART_BYTES+1);f=self.data['manifest']['files'][0]
        f.update(bytes=len(raw),sha256=p.digest(raw),partCount=2)
        self.data['manifest']['totals']['compressedBytes']=len(raw)
        self.data['manifestSha256']=p.digest(p.canonical(self.data['manifest']));p.file_path(self.folder,0).write_bytes(raw)

    def test_success_receipt_is_pending_review(self):
        receipt=self.client.upload(self.identity,self.data,self.folder)
        self.assertEqual(receipt['initialGovernanceStatus'],'pending-review')
        self.assertEqual(sum(method=='PUT' for method,_ in self.client.calls),1)
        self.client.upload(self.identity,self.data,self.folder)
        self.assertEqual(sum(method=='PUT' for method,_ in self.client.calls),1)
        self.assertEqual(sum(method=='POST' and route=='' for method,route in self.client.calls),1)

    def test_unknown_complete_queries_receipt_without_duplicate_post(self):
        self.client.lose_complete=True
        receipt=self.client.upload(self.identity,self.data,self.folder)
        self.assertIsNotNone(receipt)
        self.assertEqual(sum(route.endswith('/complete') for _,route in self.client.calls),1)
        self.assertTrue(self.client.calls[-1][1].endswith('/receipt'))

    def test_lost_part_response_resumes_original_id_without_resending_part(self):
        self.large_transport();self.client.lose_part=True
        with self.assertRaises(DatasetFailure):self.client.upload(self.identity,self.data,self.folder)
        self.client.upload(self.identity,self.data,self.folder)
        self.assertEqual(sum(method=='PUT' and route.endswith('/parts/0') for method,route in self.client.calls),1)
        self.assertEqual(sum(method=='PUT' and route.endswith('/parts/1') for method,route in self.client.calls),1)
        self.assertEqual(sum(method=='POST' and route=='' for method,route in self.client.calls),1)

    def test_revoked_identity_stops_before_next_part(self):
        self.large_transport();self.client.revoke_after_part=True
        with self.assertRaisesRegex(DatasetFailure,'IDENTITY_CHANGED'):self.client.upload(self.identity,self.data,self.folder)
        self.assertEqual(sum(method=='PUT' for method,_ in self.client.calls),1)

    def test_pause_stops_without_http(self):
        with self.assertRaisesRegex(DatasetFailure,'UPLOAD_PAUSED'):self.client.upload(self.identity,self.data,self.folder,lambda:False)
        self.assertEqual(self.client.calls,[])

    def test_wrong_receipt_and_state_rejected(self):
        receipt=self.client.receipt_value();receipt['acceptedRows']=0
        with self.assertRaisesRegex(p.Invalid,'RECEIPT_MISMATCH'):validate_receipt(receipt,self.data)
        state=self.client.state();state['receivedParts']=[dict(fileIndex=0,parts=[0,0])]
        with self.assertRaises(p.Invalid):validate_state(state,self.data)
        state=self.client.state();state['requestHash']='0'*64
        with self.assertRaisesRegex(p.Invalid,'STATE_MISMATCH'):validate_state(state,self.data)

    def test_included_receipt_is_invalid_and_governance_is_separate(self):
        receipt=self.client.receipt_value();receipt['initialGovernanceStatus']='included'
        with self.assertRaisesRegex(p.Invalid,'RECEIPT_MISMATCH'):validate_receipt(receipt,self.data)
        receipt=self.client.receipt_value()
        value=dict(schema=p.SCHEMA,clientDatasetId=self.data['clientDatasetId'],datasetId=receipt['datasetId'],revision=2,governanceStatus='included',sources=[dict(sourceId=self.data['manifest']['sources'][0]['sourceId'],status='included',blockers=[])])
        self.assertEqual(validate_governance(value,self.data,receipt),value)
        value['sources'][0]['sourceId']=str(uuid.uuid4())
        with self.assertRaisesRegex(p.Invalid,'GOVERNANCE_MISMATCH'):validate_governance(value,self.data,receipt)


if __name__=='__main__':unittest.main()
