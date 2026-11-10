"""Local training runs, immutable inputs and identity-scoped durable upload queue."""
from contextlib import contextmanager,closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from client_update_lock import FileLock
from device_identity import atomic_write
from upload_outbox import Binding
from dataset_export import export_csv,sha_file
import dataset_protocol as p


class DatasetStore:
    def __init__(self,root,clock=time.time,max_bytes=2*1024**3):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'datasets.sqlite3';self.clock=clock;self.max_bytes=max_bytes
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY,account TEXT NOT NULL,origin TEXT NOT NULL,device TEXT NOT NULL,
                    state TEXT NOT NULL,input_hash TEXT NOT NULL DEFAULT '',input_bytes INTEGER NOT NULL DEFAULT 0,
                    package_name TEXT,package_bytes INTEGER NOT NULL DEFAULT 0,descriptor TEXT,request_hash TEXT,
                    result TEXT,error TEXT NOT NULL DEFAULT '',receipt TEXT,governance TEXT,
                    paused INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_dataset_actor ON runs(account,origin,device,state,next_at);
            ''')

    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def transaction(self):
        db=self.connect()
        try:db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    @staticmethod
    def actor(binding):return binding.account,binding.origin,binding.device

    def check_space(self,additional):
        # Count retained failed attempts and previews too, not only committed package sizes.
        used=sum(path.stat().st_size for path in self.root.rglob('*') if path.is_file())
        p.require(used+additional<=self.max_bytes,'LOCAL_QUOTA')
        p.require(shutil.disk_usage(self.root).free>additional+64*1024**2,'DISK_SPACE')

    def folder(self,run_id):
        p.pattern(run_id,p.UUID);path=self.root/'runs'/run_id
        p.require(path.resolve().is_relative_to(self.root),'LOCAL_PATH')
        return path

    def get(self,run_id,binding=None):
        p.pattern(run_id,p.UUID)
        with closing(self.connect()) as db:
            row=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if row is None:raise ValueError('run_not_found')
        row=dict(row)
        if binding is not None:p.require(tuple(row[k] for k in ('account','origin','device'))==self.actor(binding),'IDENTITY_CHANGED')
        return row

    def list(self,binding,limit=100):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM runs WHERE account=? AND origin=? AND device=? ORDER BY created DESC LIMIT ?',(*self.actor(binding),limit))]

    def input_path(self,run_id):return self.folder(run_id)/'input.csv'

    def import_bundle(self,source_folder,binding):
        """Explicit historical bundle import; preserve request ID and authenticated ownership."""
        source_folder=Path(source_folder).resolve();data=p.validate_directory(source_folder)
        run_id=data['runId'];encoded=p.canonical(data);request_hash=p.digest(encoded)
        with FileLock(self.root/'bundle-import.lock',timeout=2):
            with closing(self.connect()) as db:old=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            if old is not None:
                p.require(tuple(old[k] for k in ('account','origin','device'))==self.actor(binding) and old['request_hash']==request_hash,'IMPORT_CONFLICT')
                if old['state']!='importing':return run_id
                target=self.package_path(dict(old))
            else:
                name='package-'+uuid.uuid4().hex;target=self.folder(run_id)/name
                size=data['manifest']['totals']['compressedBytes'];now=self.clock()
                with self.transaction() as db:
                    self.check_space(size)
                    used=db.execute('SELECT COALESCE(SUM(input_bytes+package_bytes),0) FROM runs').fetchone()[0]
                    p.require(used+size<=self.max_bytes,'LOCAL_QUOTA')
                    p.require(shutil.disk_usage(self.root).free>size+64*1024**2,'DISK_SPACE')
                    db.execute('INSERT INTO runs(id,account,origin,device,state,package_name,package_bytes,descriptor,request_hash,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(run_id,*self.actor(binding),'importing',name,size,encoded.decode(),request_hash,now,now))
            target.mkdir(parents=True,exist_ok=True)
            for f in data['manifest']['files']:
                raw=p.read_bounded(p.file_path(source_folder,f['index']),p.LIMITS['maxFileBytes'])
                p.require(p.digest(raw)==f['sha256'],'FILE_CHANGED');atomic_write(p.file_path(target,f['index']),raw)
            atomic_write(target/'initialize.json',encoded);p.validate_directory(target)
            with self.transaction() as db:db.execute("UPDATE runs SET state='pending',updated=? WHERE id=? AND state='importing'",(self.clock(),run_id))
        return run_id

    def freeze(self,path,binding):
        path=Path(path);size=path.stat().st_size
        p.require(0<size<=p.LIMITS['maxExpandedBytes'],'INPUT_LIMIT')
        run_id=str(uuid.uuid4());folder=self.folder(run_id);folder.mkdir(parents=True,exist_ok=False)
        with self.transaction() as db:
            self.check_space(size+p.LIMITS['maxCompressedBytes'])
            used=db.execute('SELECT COALESCE(SUM(input_bytes+package_bytes),0) FROM runs').fetchone()[0]
            p.require(used+size+p.LIMITS['maxCompressedBytes']<=self.max_bytes,'LOCAL_QUOTA')
            p.require(shutil.disk_usage(self.root).free>size+p.LIMITS['maxCompressedBytes']+64*1024**2,'DISK_SPACE')
            now=self.clock()
            db.execute('INSERT INTO runs(id,account,origin,device,state,input_bytes,created,updated) VALUES(?,?,?,?,?,?,?,?)',(run_id,*self.actor(binding),'freezing',size,now,now))
        try:
            original=sha_file(path);target=self.input_path(run_id);copied=0;checksum=hashlib.sha256()
            with path.open('rb') as incoming,target.open('xb') as output:
                while chunk:=incoming.read(65536):
                    copied+=len(chunk);p.require(copied<=size,'INPUT_CHANGED');checksum.update(chunk);output.write(chunk)
                output.flush();os.fsync(output.fileno())
            p.require(copied==size and checksum.hexdigest()==original and sha_file(path)==original,'INPUT_CHANGED')
            atomic_write(folder/'input.manifest.json',p.canonical(dict(schema='director-local-training-input-v1',runId=run_id,sha256=original,bytes=size)))
            with self.transaction() as db:db.execute("UPDATE runs SET input_hash=?,state='ready',updated=? WHERE id=?",(original,self.clock(),run_id))
        except BaseException:
            self.fail_training(run_id,'input_snapshot_failed');raise
        return run_id

    def start_training(self,run_id):
        row=self.get(run_id)
        p.require(sha_file(self.input_path(run_id))==row['input_hash'],'INPUT_CHANGED')
        with self.transaction() as db:
            changed=db.execute("UPDATE runs SET state='training',updated=? WHERE id=? AND state='ready'",(self.clock(),run_id)).rowcount
            p.require(changed==1,'RUN_STATE')

    def fail_training(self,run_id,error='training_failed'):
        with self.transaction() as db:db.execute("UPDATE runs SET state='failed',error=?,updated=? WHERE id=? AND state IN ('freezing','ready','training')",(error[:120],self.clock(),run_id))

    def training_succeeded(self,run_id,result,declared_groups=None):
        # Persist success before packaging. A failed export must not report training failure.
        safe={k:result[k] for k in ('samples','positive','negative','auc','top1','model_json','split_hash','dataset_hash','model','model_json_bytes') if k in result}
        with self.transaction() as db:
            changed=db.execute("UPDATE runs SET state='packaging',result=?,error='',updated=? WHERE id=? AND state IN ('training','ready','package-error')",(json.dumps(safe,allow_nan=False),self.clock(),run_id)).rowcount
            p.require(changed==1,'RUN_STATE')
        return self.package(run_id,declared_groups)

    def package(self,run_id,declared_groups=None):
        with FileLock(self.folder(run_id)/'package.lock',timeout=0):
            row=self.get(run_id);p.require(row['state'] in ('packaging','package-error'),'RUN_STATE')
            try:
                if row['package_name']:
                    destination=self.package_path(row)
                    try:
                        descriptor=p.validate_directory(destination)
                        p.require(descriptor['runId']==run_id,'RUN_STATE')
                    except (OSError,ValueError):descriptor=None
                else:descriptor=None
                if descriptor is None:
                    self.check_space(min(p.LIMITS['maxExpandedBytes']+p.LIMITS['maxCompressedBytes'],row['input_bytes']*3+1024**2))
                    name='package-'+uuid.uuid4().hex
                    with self.transaction() as db:db.execute("UPDATE runs SET package_name=?,state='packaging',updated=? WHERE id=?",(name,self.clock(),run_id))
                    destination=self.folder(run_id)/name
                    descriptor=export_csv(self.input_path(run_id),destination,run_id,declared_groups=declared_groups,expected_hash=row['input_hash'])
                encoded=p.canonical(descriptor);request_hash=p.digest(encoded)
                with self.transaction() as db:
                    db.execute("UPDATE runs SET state='pending',descriptor=?,request_hash=?,package_bytes=?,error='',next_at=0,updated=? WHERE id=?",(encoded.decode(),request_hash,descriptor['manifest']['totals']['compressedBytes'],self.clock(),run_id))
                return descriptor
            except Exception as error:
                with self.transaction() as db:db.execute("UPDATE runs SET state='package-error',error=?,updated=? WHERE id=?",(safe_error(error),self.clock(),run_id))
                return None

    def package_path(self,row):
        p.pattern(row['package_name'],r'package-[a-f0-9]{32}')
        path=self.folder(row['id'])/row['package_name'];p.require(path.resolve().is_relative_to(self.root),'LOCAL_PATH');return path

    def next(self,binding):
        with closing(self.connect()) as db:
            row=db.execute("SELECT * FROM runs WHERE account=? AND origin=? AND device=? AND paused=0 AND state IN ('pending','uploading') AND next_at<=? ORDER BY created LIMIT 1",(*self.actor(binding),self.clock())).fetchone()
            return dict(row) if row else None

    def set_paused(self,run_id,binding,paused):
        self.get(run_id,binding)
        with self.transaction() as db:db.execute('UPDATE runs SET paused=?,updated=? WHERE id=?',(int(paused),self.clock(),run_id))

    def retry(self,run_id,binding):
        row=self.get(run_id,binding)
        if row['state'] in ('package-error','packaging'):return self.package(run_id)
        with self.transaction() as db:
            db.execute("UPDATE runs SET paused=0,next_at=0,error='',state=CASE WHEN state IN ('blocked','rejected') THEN 'pending' ELSE state END,updated=? WHERE id=?",(self.clock(),run_id))

    def cancelled(self,run_id,binding):
        self.get(run_id,binding)
        with self.transaction() as db:
            db.execute("UPDATE runs SET state='cancelled',paused=1,error='',updated=? WHERE id=? AND receipt IS NULL",(self.clock(),run_id))

    def renew(self,run_id,binding):
        """Explicit terminal-session replacement, retaining every old receipt and source file."""
        row=self.get(run_id,binding)
        p.require(row['state']=='cancelled' or (row['state']=='rejected' and row['error']=='DIRECTOR_DATASET_EXPIRED'),'RUN_STATE')
        data=p.validate_directory(self.package_path(row));new_id=str(uuid.uuid4())
        data.update(runId=new_id,clientDatasetId=str(uuid.uuid5(uuid.UUID(new_id),'training-data')))
        # Only temporary copies are cleaned; original run and immutable files are retained.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='renew-',dir=self.root) as temporary:
            folder=Path(temporary)
            for f in data['manifest']['files']:shutil.copyfile(p.file_path(self.package_path(row),f['index']),p.file_path(folder,f['index']))
            atomic_write(folder/'initialize.json',p.canonical(data))
            return self.import_bundle(folder,binding)

    def bind_local(self,run_id,local_binding,new_binding):
        p.require(local_binding.origin=='local' and new_binding.origin!='local','IDENTITY_CHANGED')
        row=self.get(run_id,local_binding)
        p.require(row['state'] in ('pending','package-error') and not row['receipt'] and row['attempts']==0,'RUN_STATE')
        with self.transaction() as db:
            changed=db.execute('UPDATE runs SET account=?,origin=?,device=?,updated=? WHERE id=? AND origin=? AND attempts=0',(*self.actor(new_binding),self.clock(),run_id,'local')).rowcount
            p.require(changed==1,'IDENTITY_CHANGED')

    def begin_upload(self,run_id,binding):
        self.get(run_id,binding)
        with self.transaction() as db:
            n=db.execute("UPDATE runs SET state='uploading',attempts=attempts+1,updated=? WHERE id=? AND paused=0 AND state IN ('pending','uploading')",(self.clock(),run_id)).rowcount
            p.require(n==1,'RUN_PAUSED')

    def upload_failed(self,run_id,state,error,delay=30):
        p.require(state in ('pending','blocked','rejected'),'RUN_STATE')
        with self.transaction() as db:db.execute('UPDATE runs SET state=?,error=?,next_at=?,updated=? WHERE id=? AND receipt IS NULL',(state,error[:120],self.clock()+delay,self.clock(),run_id))

    def received(self,run_id,receipt):
        encoded=json.dumps(receipt,sort_keys=True,separators=(',',':'))
        with self.transaction() as db:
            old=db.execute('SELECT receipt FROM runs WHERE id=?',(run_id,)).fetchone()
            p.require(old is not None and (old[0] is None or old[0]==encoded),'RECEIPT_CHANGED')
            db.execute("UPDATE runs SET state='received',receipt=?,error='',updated=? WHERE id=?",(encoded,self.clock(),run_id))

    def save_governance(self,run_id,value):
        with self.transaction() as db:
            row=db.execute('SELECT governance FROM runs WHERE id=?',(run_id,)).fetchone()
            old=json.loads(row[0]) if row and row[0] else None
            if old is not None:
                p.require(value['revision']>=old['revision'],'GOVERNANCE_ROLLBACK')
                p.require(value['revision']!=old['revision'] or value==old,'GOVERNANCE_CHANGED')
            db.execute('UPDATE runs SET governance=?,updated=? WHERE id=?',(json.dumps(value,sort_keys=True),self.clock(),run_id))


def safe_error(error):
    code=str(error)
    return code[:120] if code and all(c.isupper() or c.isdigit() or c=='_' for c in code) else type(error).__name__
