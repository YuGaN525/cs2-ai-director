import gzip
import hashlib
import json
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from device_identity import canonical
from director_core.model_format import DECISION_FEATURES
from model_submission import write_submission
from training_submission import SubmissionClient, SubmissionError, load_result, explain_error
from upload_outbox import Binding
from upload_worker import UploadIdentity


def result_fixture(root):
    model = dict(n_features=8,features=DECISION_FEATURES,feature_version='pov-v1',classes=[0,1],
                 trees=[dict(left=[-1],right=[-1],feature=[-2],threshold=[-2.],value=[[1.,1.]])])
    path = Path(root)/'decision_model.json.gz'
    path.write_bytes(gzip.compress(canonical(model),mtime=0))
    write_submission(path,[[0.]*8,[1.]*8],[0,1],'a'*64)
    return path


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.model = result_fixture(self.temp.name)
        self.receipts = {}
        self.posts = []
        self.gets = []
        self.mode = ''
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def send(self,status,value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                fixture.gets.append(self.path)
                if fixture.mode=='forbidden':
                    return self.send(403,dict(success=False,code='FORBIDDEN'))
                if fixture.mode=='redirect':
                    self.send_response(302)
                    self.send_header('Location','/unexpected')
                    self.end_headers()
                    return
                if self.path.endswith('/capabilities'):
                    return self.send(200,dict(success=True,data=dict(schema='director-training-submission-v1',enabled=True,
                        modelFormat='forest-json-v1',featureVersion='pov-v1',maxFileBytes=20971520,
                        maxMetadataBytes=65536,maxRequestBytes=23068672,candidateOnly=True,idempotencyScope='account-device-requestId')))
                key = self.path.rsplit('/',1)[-1]
                if key not in fixture.receipts:
                    return self.send(404,dict(success=False,code='DIRECTOR_SUBMISSION_NOT_FOUND'))
                return self.send(200,dict(success=True,data=fixture.receipts[key]))
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                message = BytesParser().parsebytes(('Content-Type: '+self.headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                parts = {part.get_param('name',header='Content-Disposition'):part.get_payload(decode=True) for part in message.get_payload()}
                key = parts['requestId'].decode()
                fixture.posts.append((key,parts,self.headers['Authorization']))
                if fixture.mode=='reject':
                    return self.send(422,dict(success=False,code='DIRECTOR_MODEL_INVALID'))
                receipt = dict(requestId=key,modelId=str(uuid.uuid4()),artifactSha256=hashlib.sha256(parts['file']).hexdigest(),
                    artifactBytes=len(parts['file']),status='candidate',existingModel=False,submittedAt='2026-11-09T02:00:00Z')
                if fixture.mode=='bad_receipt': receipt['artifactSha256']='b'*64
                if fixture.mode!='drop_before_commit': fixture.receipts.setdefault(key,receipt)
                if fixture.mode in ('drop_after_commit','drop_before_commit'):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send(200,dict(success=True,data=fixture.receipts[key]))
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        origin='http://127.0.0.1:'+str(self.server.server_port)
        self.identity=UploadIdentity(Binding(str(uuid.uuid4()),origin,str(uuid.uuid4())),'d1_fixture')
        self.account=mock.Mock(root=Path(self.temp.name),tokens={'local_generation':'generation1'})
        self.account.upload_identity.return_value=self.identity
        self.client=SubmissionClient(self.account,timeout=2)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_success_and_restart_reuse_immutable_request_and_receipt(self):
        receipt=self.client.submit(self.model)
        again=SubmissionClient(self.account,timeout=2).submit(self.model)
        self.assertEqual(receipt,again)
        self.assertEqual(len(self.posts),1)
        _,parts,auth=self.posts[0]
        self.assertEqual(auth,'Bearer d1_fixture')
        self.assertEqual(set(parts),{'requestId','metadata','file'})
        saved=next(self.client.root.glob('*.json')).read_text()
        self.assertNotIn('d1_fixture',saved)
        self.assertNotIn(str(self.model),saved)
        self.assertIn('metadata',json.loads(saved)['context'])

    def test_lost_post_response_is_reconciled_without_second_post(self):
        self.mode='drop_after_commit'
        receipt=self.client.submit(self.model)
        self.assertEqual(receipt['status'],'candidate')
        self.assertEqual(len(self.posts),1)

    def test_unknown_uncommitted_request_reuses_id_after_restart(self):
        self.mode='drop_before_commit'
        with self.assertRaises(SubmissionError):self.client.submit(self.model)
        self.mode=''
        receipt=SubmissionClient(self.account,timeout=2).submit(self.model)
        self.assertEqual([item[0] for item in self.posts],[receipt['requestId']]*2)

    def test_forbidden_and_redirect_do_not_send_result(self):
        for mode in ('forbidden','redirect'):
            self.mode=mode
            with self.assertRaises(SubmissionError):self.client.submit(self.model)
        self.assertEqual(self.posts,[])
        self.assertNotIn('/unexpected',self.gets)

    def test_account_or_login_generation_change_blocks_late_request(self):
        original=self.client._request
        def change(*args,**kwargs):
            result=original(*args,**kwargs)
            self.account.tokens['local_generation']='generation2'
            return result
        with mock.patch.object(self.client,'_request',side_effect=change):
            with self.assertRaisesRegex(SubmissionError,'identity_changed'):self.client.submit(self.model)
        self.assertEqual(self.posts,[])

    def test_wrong_receipt_never_marks_confirmed(self):
        self.mode='bad_receipt'
        with self.assertRaisesRegex(SubmissionError,'invalid_receipt'):self.client.submit(self.model)
        state=json.loads(next(self.client.root.glob('*.json')).read_bytes())
        self.assertNotIn('receipt',state)

    def test_rejected_model_is_not_repeated(self):
        self.mode='reject'
        with self.assertRaises(SubmissionError):self.client.submit(self.model)
        self.assertEqual(len(self.posts),1)

    def test_metadata_whitespace_does_not_create_another_submission(self):
        first=self.client.submit(self.model)
        path=self.model.with_suffix('').with_suffix('.submission.json')
        path.write_bytes(canonical(json.loads(path.read_bytes())))
        self.assertEqual(self.client.submit(self.model),first)
        self.assertEqual(len(self.posts),1)

    def test_tampered_metadata_and_foreign_file_never_make_network_request(self):
        path=self.model.with_suffix('').with_suffix('.submission.json')
        data=json.loads(path.read_bytes())
        data['artifactSha256']='b'*64
        path.write_bytes(canonical(data))
        with self.assertRaises(ValueError):self.client.submit(self.model)
        with self.assertRaises(ValueError):self.client.submit(self.model.with_suffix('.pkl'))
        self.assertEqual(self.gets,[])

    def test_nonfinite_duplicate_or_extra_metadata_rejected(self):
        path=self.model.with_suffix('').with_suffix('.submission.json')
        original=path.read_bytes()
        for raw in (b'{"name":"a","name":"b"}',original.replace(b'0.5',b'NaN',1),original[:-1]+b',"secret":"never upload"}'):
            path.write_bytes(raw)
            with self.assertRaises(ValueError):load_result(self.model)

    def test_existing_model_receipt_preserves_original_state(self):
        artifact,meta=load_result(self.model)
        value=dict(requestId=str(uuid.uuid4()),modelId=str(uuid.uuid4()),artifactSha256=meta['artifactSha256'],
                   artifactBytes=len(artifact),status='published',existingModel=True,submittedAt='2026-11-09T00:00:00Z')
        self.assertEqual(self.client._receipt(value,value['requestId'],value),value)
        value['existingModel']=False
        with self.assertRaises(SubmissionError):self.client._receipt(value,value['requestId'],value)

    def test_permission_error_text_does_not_echo_server_or_credentials(self):
        self.assertNotIn('d1_private',explain_error(SubmissionError('d1_private',403)))


if __name__=='__main__':unittest.main()
