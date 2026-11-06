"""Explicit localhost-only .NET/PG compatibility roundtrip, using synthetic test auth."""
import argparse
import json
from pathlib import Path
import sys
import uuid
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from upload_outbox import UploadOutbox,Binding
from upload_worker import UploadWorker,TrainingUploadClient,UploadIdentity,UploadFailure
from director_core.model_format import DECISION_FEATURES

parser=argparse.ArgumentParser()
parser.add_argument('--url',required=True)
parser.add_argument('--evidence',type=Path,required=True)
args=parser.parse_args()
url=urlsplit(args.url)
if url.hostname not in ('127.0.0.1','localhost') or url.scheme!='http':raise SystemExit('Only explicit local test host allowed')
args.evidence.mkdir(parents=True,exist_ok=True)
now=[1000.]
box=UploadOutbox(args.evidence/(str(uuid.uuid4())+'.sqlite3'),clock=lambda:now[0])
binding=Binding('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',args.url,'local-test-device')
row=dict(zip(DECISION_FEATURES,['0.5']*8),label='1',map='de_mirage',group='synthetic-match',action='hold')
box.append(binding,'online',str(uuid.uuid4()),[row])
class LoseReceipt(TrainingUploadClient):
    def upload(self,identity,batch):
        self.first=super().upload(identity,batch)
        raise UploadFailure(0,'synthetic_receipt_lost')
client=LoseReceipt();worker=UploadWorker(box,lambda:UploadIdentity(binding,'synthetic-a'),client)
assert worker.run_once() is False
assert box.stats()['states']=={'pending':1}
first=client.first;now[0]+=5000;worker.client=TrainingUploadClient()
assert worker.run_once() is True
from contextlib import closing
with closing(box.connect()) as db:result=json.loads(db.execute('SELECT receipt FROM batches').fetchone()[0])
assert result['duplicate'] is True and result['id']==first['id']
assert result['uploadHash']==first['uploadHash'] and result['createdAt'].endswith('Z')
report={'state':box.stats(),'same_receipt_id':True,'duplicate_after_lost_receipt':True,'receipt':result,'authentication':'local-test-only-bearer'}
(args.evidence/'real-service-upload.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print('PASS actual .NET/PG/private-store upload and lost-receipt replay')
