import concurrent.futures
from contextlib import closing
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from director_core.model_format import DECISION_FEATURES
from upload_protocol import parse_csv,encode_csv,accept_candidate_preview,ProtocolError,API_VERSION
from upload_outbox import UploadOutbox,Binding,QueueFull
from upload_worker import UploadIdentity,UploadWorker,TrainingUploadClient,UploadFailure
from training_recorder import DurableRecorder,import_explicit_csv
from training_data_runtime import TrainingDataRuntime


def row(label='1'):
    return dict(zip(DECISION_FEATURES,['0.5']*8),label=label,group='private/demo.dem#1',
                map='de_mirage',action='hold',target_name='SECRET_NAME',target_steamid='76561190000000001')


def receipt(batch):
    return {'id':str(uuid.uuid4()),'uploadHash':batch['upload_hash'],'contentHash':'a'*64,
            'rowCount':batch['row_count'],'positiveCount':batch['positive_count'],
            'source':batch['source'],'channel':'automatic',
            'quality':'legacy-selected-only' if batch['source']=='online' else 'legacy-offline',
            'createdAt':'2026-11-07T00:00:00Z','duplicate':False}


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=1000.
        self.binding=Binding('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','http://127.0.0.1:5125','test-device')
        self.box=UploadOutbox(self.root/'outbox.sqlite3',clock=lambda:self.now)
        self.identity=UploadIdentity(self.binding,'synthetic-a')

    def add(self,n=1):
        return self.box.append(self.binding,'online','session1',[row() for _ in range(n)])

    def batch(self):
        self.add();self.box.prepare_batch(self.binding);return self.box.claim(self.binding)

    def test_shared_contract_fixtures(self):
        path=Path('contracts/director-data-v1');data=json.loads((path/'fixtures.json').read_text())
        for case in data['legacy']:
            with self.subTest(case=case['id']):
                if case['accepted']:self.assertTrue(parse_csv((path/case['file']).read_bytes()))
                else:
                    with self.assertRaises(ProtocolError):parse_csv((path/case['file']).read_bytes())
        for case in data['candidate_preview']:
            self.assertEqual(accept_candidate_preview(case['value']),case['accepted'])

    def test_records_strip_names_ids_and_paths(self):
        batch=self.batch();text=gzip.decompress(batch['payload']).decode()
        for value in ('SECRET_NAME','7656119','private/demo.dem'):self.assertNotIn(value,text)

    def test_restart_preserves_batch_identity_and_bytes(self):
        batch=self.batch();self.now+=61
        reopened=UploadOutbox(self.box.path,clock=lambda:self.now);again=reopened.claim(self.binding)
        self.assertEqual(again['id'],batch['id']);self.assertEqual(again['payload'],batch['payload'])

    def test_process_crash_after_durable_claim_recovers(self):
        code='''import sys,os
from upload_outbox import UploadOutbox,Binding
from test_uploads import row
b=Binding('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','http://127.0.0.1:5125','test-device')
box=UploadOutbox(sys.argv[1],clock=lambda:1000.)
box.append(b,'online','session',[row()]);box.prepare_batch(b);box.claim(b)
os._exit(23)
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.box.path)])
        self.assertEqual(result.returncode,23)
        self.assertIsNone(self.box.claim(self.binding))
        self.now+=61;self.assertIsNotNone(self.box.claim(self.binding))

    def test_receipt_persisted_before_confirm_and_reopen(self):
        batch=self.batch();self.box.acknowledge(batch,receipt(batch))
        self.assertEqual(UploadOutbox(self.box.path).stats()['states'],{'confirmed':1})
        self.assertIsNone(self.box.claim(self.binding))
        self.assertEqual(self.box.stats()['records'],1)

    def test_mismatched_receipt_never_confirms(self):
        batch=self.batch();bad=receipt(batch);bad['uploadHash']='b'*64
        with self.assertRaises(ProtocolError):self.box.acknowledge(batch,bad)
        self.assertEqual(self.box.stats()['states'],{'inflight':1})

    def test_old_lease_cannot_confirm_new_attempt(self):
        batch=self.batch();self.now+=61;new=self.box.claim(self.binding)
        with self.assertRaises(ValueError):self.box.acknowledge(batch,receipt(batch))
        self.box.acknowledge(new,receipt(new))

    def test_actor_device_environment_isolation(self):
        self.add();self.box.prepare_batch(self.binding)
        for binding in [Binding(str(uuid.uuid4()),self.binding.origin,self.binding.device),
                        Binding(self.binding.account,self.binding.origin,'another-device'),
                        Binding(self.binding.account,'https://other.invalid',self.binding.device)]:
            self.assertIsNone(self.box.claim(binding));self.assertEqual(self.box.stats(binding)['records'],0)

    def test_concurrent_claim_only_one_worker(self):
        self.add();self.box.prepare_batch(self.binding)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(lambda _:self.box.claim(self.binding),range(5)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_source_cursor_and_duplicate_ids_atomic(self):
        values=[row()];ids=['stable-record']
        self.assertEqual(self.box.append(self.binding,'offline','source',values,ids,cursor=1),1)
        self.assertEqual(self.box.append(self.binding,'offline','source',values,ids,cursor=1),0)
        with closing(self.box.connect()) as db:self.assertEqual(db.execute('SELECT cursor FROM source_progress').fetchone()[0],1)
        with self.assertRaises(ValueError):self.box.append(self.binding,'offline','source',[row('0')],ids,cursor=2)
        self.assertEqual(self.box.stats()['records'],1)

    def test_quota_failure_preserves_committed_rows(self):
        self.add();self.box.max_bytes=1
        with self.assertRaises(QueueFull):self.add()
        self.assertEqual(self.box.stats()['records'],1)
        with self.assertRaises(QueueFull):self.box.prepare_batch(self.binding)

    def test_explicit_import_idempotent_and_no_directory_scan(self):
        path=self.root/'selected.csv';path.write_bytes(encode_csv([row()]))
        # encode_csv only accepts allowlisted fields; use a sanitized row
        self.assertEqual(import_explicit_csv(self.box,self.binding,path),1)
        self.assertEqual(import_explicit_csv(self.box,self.binding,path),0)
        self.assertEqual(self.box.stats()['records'],1)

    def test_network_error_retries_and_preserves_payload(self):
        self.add();client=mock.Mock();client.upload.side_effect=UploadFailure(0,'network_unavailable')
        worker=UploadWorker(self.box,lambda:self.identity,client)
        self.assertFalse(worker.run_once());self.assertEqual(self.box.stats()['states'],{'pending':1})
        self.now+=5000;client.upload.side_effect=None;client.upload.side_effect=lambda identity,batch:receipt(batch)
        self.assertTrue(worker.run_once());self.assertEqual(self.box.stats()['states'],{'confirmed':1})

    def test_401_pauses_until_credential_changes(self):
        self.add();client=mock.Mock();client.upload.side_effect=UploadFailure(401,'login_required')
        worker=UploadWorker(self.box,lambda:self.identity,client)
        worker.run_once();worker.run_once();self.assertEqual(client.upload.call_count,1)
        self.identity=UploadIdentity(self.binding,'new-token')
        worker.run_once();self.assertEqual(client.upload.call_count,2)

    def test_permanent_validation_error_not_retried_forever(self):
        self.add();client=mock.Mock();client.upload.side_effect=UploadFailure(422,'invalid_data')
        worker=UploadWorker(self.box,lambda:self.identity,client);worker.run_once();self.now+=5000;worker.run_once()
        self.assertEqual(client.upload.call_count,1);self.assertEqual(self.box.stats()['states'],{'rejected':1})

    def test_invalid_receipt_blocked_without_data_loss(self):
        self.add();client=mock.Mock();client.upload.side_effect=lambda identity,batch:dict(receipt(batch),uploadHash='0'*64)
        worker=UploadWorker(self.box,lambda:self.identity,client);worker.run_once()
        self.assertEqual(self.box.stats()['states'],{'blocked':1});self.assertEqual(self.box.stats()['records'],1)

    def test_identity_switch_before_network_never_transmits(self):
        self.add();other=UploadIdentity(Binding(str(uuid.uuid4()),self.binding.origin,self.binding.device),'other')
        client=mock.Mock();values=iter([self.identity,other])
        UploadWorker(self.box,lambda:next(values),client).run_once();client.upload.assert_not_called()

    def test_buffer_full_visible_and_nonblocking(self):
        recorder=DurableRecorder(self.box,lambda:self.binding,max_buffer=1)
        self.assertTrue(recorder.offer(row()));self.assertFalse(recorder.offer(row()))
        self.assertEqual(recorder.stats()['not_accepted'],1)
        recorder.start();self.assertTrue(recorder.close());self.assertEqual(self.box.stats()['records'],1)

    def test_async_disk_failure_does_not_drop_current_record(self):
        recorder=DurableRecorder(self.box,lambda:self.binding)
        recorder.offer(row())
        with mock.patch.object(self.box,'append',side_effect=OSError('disk full')):
            recorder.start();time.sleep(.1);self.assertFalse(recorder.close())
            self.assertEqual(recorder.stats()['buffered'],1)
        recorder.stop_event.clear();recorder.start()
        self.assertTrue(recorder.close());self.assertEqual(self.box.stats()['records'],1)

    def test_invalid_row_counted_and_does_not_block_following_records(self):
        recorder=DurableRecorder(self.box,lambda:self.binding)
        recorder.offer(dict(row(),aim_self='NaN'));recorder.offer(row())
        recorder.start();self.assertTrue(recorder.close())
        self.assertEqual(recorder.stats()['not_accepted'],1)
        self.assertEqual(self.box.stats()['records'],1)

    def test_queue_export_retains_original_records(self):
        self.add(2);path=self.root/'export.csv'
        self.assertEqual(self.box.export_csv(self.binding,path),2)
        self.assertEqual(len(parse_csv(path.read_bytes())),2)
        self.assertEqual(self.box.stats()['records'],2)

    def test_unbound_records_are_not_reassigned_after_login(self):
        local=Binding('unbound','local','installation')
        self.box.append(local,'online','session',[row()])
        self.assertIsNone(self.box.prepare_batch(self.binding));self.assertEqual(self.box.stats(local)['records'],1)

    def test_capture_binding_survives_api_token_unavailability(self):
        runtime=TrainingDataRuntime(self.root/'runtime',binding_provider=lambda:self.binding)
        try:self.assertEqual(runtime.binding(),self.binding)
        finally:self.assertTrue(runtime.close())

    def test_redirect_rejected_without_forwarding_authorization(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.send_response(302);self.send_header('Location','http://127.0.0.1:1/steal');self.end_headers()
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            binding=Binding(self.binding.account,f'http://127.0.0.1:{server.server_port}',self.binding.device)
            with self.assertRaises(UploadFailure) as result:TrainingUploadClient().capabilities(binding)
            self.assertEqual(result.exception.status,302)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
