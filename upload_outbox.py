"""SQLite/WAL managed records and immutable batches. No user-directory scanning."""
from contextlib import contextmanager, closing
from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
import csv
import os
import tempfile

from device_identity import server_origin
from upload_protocol import encode_csv, sanitize_row, verify_receipt


class QueueFull(OSError): pass


@dataclass(frozen=True)
class Binding:
    account: str
    origin: str
    device: str

    def __post_init__(self):
        if self.origin != 'local':
            uuid.UUID(self.account)
            object.__setattr__(self, 'origin', server_origin(self.origin))
        if not self.device or len(self.device) > 256: raise ValueError('缺少设备身份')


class UploadOutbox:
    def __init__(self, path, max_bytes=256*1024*1024, clock=time.time):
        self.path = Path(path);self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.clock = max_bytes, clock
        with closing(self.connect()) as db:
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS records(
                    seq INTEGER PRIMARY KEY, record_id TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL, origin TEXT NOT NULL, device TEXT NOT NULL,
                    source TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL,
                    batch_id TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS batches(
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, origin TEXT NOT NULL, device TEXT NOT NULL,
                    source TEXT NOT NULL, payload BLOB NOT NULL, upload_hash TEXT NOT NULL,
                    row_count INTEGER NOT NULL, positive_count INTEGER NOT NULL,
                    seq_start INTEGER NOT NULL, seq_end INTEGER NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    lease_id TEXT, error TEXT NOT NULL DEFAULT '', receipt TEXT, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_record_pending ON records(account,origin,device,batch_id,seq);
                CREATE INDEX IF NOT EXISTS ix_batch_pending ON batches(account,origin,device,state,next_at);
                CREATE TABLE IF NOT EXISTS source_progress(
                    account TEXT,origin TEXT,device TEXT,source_id TEXT,cursor INTEGER NOT NULL,
                    PRIMARY KEY(account,origin,device,source_id));
                CREATE TABLE IF NOT EXISTS queue_budget(id INTEGER PRIMARY KEY CHECK(id=1),used INTEGER NOT NULL);
                INSERT OR IGNORE INTO queue_budget SELECT 1,
                    (SELECT COALESCE(SUM(length(payload)),0) FROM records)+(SELECT COALESCE(SUM(length(payload)),0) FROM batches);
                CREATE TRIGGER IF NOT EXISTS record_budget_insert AFTER INSERT ON records BEGIN
                    UPDATE queue_budget SET used=used+length(new.payload) WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS batch_budget_insert AFTER INSERT ON batches BEGIN
                    UPDATE queue_budget SET used=used+length(new.payload) WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS record_budget_delete AFTER DELETE ON records BEGIN
                    UPDATE queue_budget SET used=used-length(old.payload) WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS batch_budget_delete AFTER DELETE ON batches BEGIN
                    UPDATE queue_budget SET used=used-length(old.payload) WHERE id=1; END;
                CREATE TABLE IF NOT EXISTS local_settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
                COMMIT;
            ''')
            if 'descriptor' not in {r[1] for r in db.execute('PRAGMA table_info(batches)')}:
                db.execute('ALTER TABLE batches ADD COLUMN descriptor TEXT')

    def connect(self):
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except Exception: db.rollback();raise
        finally: db.close()

    @staticmethod
    def _who(binding): return (binding.account,binding.origin,binding.device)

    def append(self,binding,source,source_id,rows,record_ids=None,cursor=None):
        if source not in ('online','offline'): raise ValueError('来源无效')
        payloads=[json.dumps(sanitize_row(row,source_id),ensure_ascii=True,separators=(',',':')) for row in rows]
        ids=record_ids or [str(uuid.uuid4()) for _ in rows]
        if len(ids)!=len(rows): raise ValueError('记录身份数量不一致')
        with self.transaction() as db:
            used=db.execute('SELECT used FROM queue_budget WHERE id=1').fetchone()[0]
            fresh=[]
            for record_id,payload in zip(ids,payloads):
                existing=db.execute('SELECT * FROM records WHERE record_id=?',(record_id,)).fetchone()
                if existing:
                    if tuple(existing[k] for k in ('account','origin','device','source','source_id','payload')) != (*self._who(binding),source,source_id,payload):
                        raise ValueError('同一记录身份内容冲突')
                else:fresh.append((record_id,payload))
            if used+sum(len(v) for _,v in fresh)>self.max_bytes: raise QueueFull('本地训练记录容量已满，待传数据已保留')
            for record_id,payload in fresh:
                db.execute('INSERT INTO records(record_id,account,origin,device,source,source_id,payload,created) VALUES(?,?,?,?,?,?,?,?)',
                           (record_id,*self._who(binding),source,source_id,payload,self.clock()))
            inserted=len(fresh)
            if cursor is not None:
                db.execute('INSERT INTO source_progress VALUES(?,?,?,?,?) ON CONFLICT(account,origin,device,source_id) DO UPDATE SET cursor=MAX(cursor,excluded.cursor)',(*self._who(binding),source_id,cursor))
            return inserted

    def prepare_batch(self,binding,limit=500):
        with self.transaction() as db:
            rows=db.execute('SELECT * FROM records WHERE account=? AND origin=? AND device=? AND batch_id IS NULL ORDER BY seq LIMIT ?',(*self._who(binding),limit)).fetchall()
            if not rows:return None
            source=rows[0]['source'];rows=[r for r in rows if r['source']==source]
            payload=gzip.compress(encode_csv([json.loads(r['payload']) for r in rows]),mtime=0)
            if len(payload)>1024*1024: raise QueueFull('批次超过本地1MiB上限，请减小分片')
            used=db.execute('SELECT used FROM queue_budget WHERE id=1').fetchone()[0]
            if used+len(payload)>self.max_bytes:raise QueueFull('队列分片空间不足，原始记录已保留')
            batch_id=str(uuid.uuid4());now=self.clock()
            db.execute('INSERT INTO batches(id,account,origin,device,source,payload,upload_hash,row_count,positive_count,seq_start,seq_end,state,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (batch_id,*self._who(binding),source,payload,hashlib.sha256(payload).hexdigest(),len(rows),sum(json.loads(r['payload'])['label']=='1' or float(json.loads(r['payload'])['label'])==1 for r in rows),rows[0]['seq'],rows[-1]['seq'],'pending',now))
            db.executemany('UPDATE records SET batch_id=? WHERE seq=?',[(batch_id,r['seq']) for r in rows])
            return batch_id

    def claim(self,binding,lease_seconds=60):
        now=self.clock()
        with self.transaction() as db:
            row=db.execute("SELECT * FROM batches WHERE account=? AND origin=? AND device=? AND ((state='pending' AND next_at<=?) OR (state='inflight' AND lease_until<=?)) ORDER BY created,id LIMIT 1",(*self._who(binding),now,now)).fetchone()
            if not row:return None
            if row['descriptor'] is None:
                records=db.execute('SELECT * FROM records WHERE batch_id=? ORDER BY seq',(row['id'],)).fetchall()
                # Migration preserves the old immutable payload and its exact record identities.
                if gzip.decompress(row['payload'])!=encode_csv([json.loads(r['payload']) for r in records]):raise ValueError('旧批次与受管记录不一致')
                samples=[]
                for r in records:
                    line=encode_csv([json.loads(r['payload'])]).split(b'\n',1)[1]
                    samples.append({'recordId':hashlib.sha256(r['record_id'].encode()).hexdigest(),'sourceId':hashlib.sha256(r['source_id'].encode()).hexdigest(),'sequence':r['seq'],'rowSha256':hashlib.sha256(line).hexdigest()})
                descriptor={'batchId':row['id'],'source':row['source'],'uploadSha256':row['upload_hash'],'uploadBytes':len(row['payload']),
                            'partCount':(len(row['payload'])+262143)//262144,'samples':samples}
                db.execute('UPDATE batches SET descriptor=? WHERE id=?',(json.dumps(descriptor,sort_keys=True,separators=(',',':')),row['id']))
                row=db.execute('SELECT * FROM batches WHERE id=?',(row['id'],)).fetchone()
            lease=str(uuid.uuid4())
            db.execute("UPDATE batches SET state='inflight',lease_id=?,lease_until=?,attempts=attempts+1 WHERE id=?",(lease,now+lease_seconds,row['id']))
            result=dict(row);result.update(lease_id=lease,attempts=row['attempts']+1);return result

    def acknowledge(self,batch,receipt):
        with self.transaction() as db:
            actual=db.execute("SELECT * FROM batches WHERE id=? AND state='inflight' AND lease_id=?",(batch['id'],batch['lease_id'])).fetchone()
            if actual is None:raise ValueError('过期上传租约')
            verify_receipt(receipt,actual)
            changed=db.execute("UPDATE batches SET state='confirmed',receipt=?,error='',lease_until=0 WHERE id=? AND state='inflight' AND lease_id=?",(json.dumps(receipt),batch['id'],batch['lease_id'])).rowcount
            if changed!=1:raise ValueError('过期上传租约，不能覆盖当前批次')

    def fail(self,batch,error,retry_after=None,blocked=False,rejected=False):
        state='rejected' if rejected else 'blocked' if blocked else 'pending'
        with self.transaction() as db:
            db.execute('UPDATE batches SET state=?,error=?,next_at=?,lease_until=0 WHERE id=? AND state=\'inflight\' AND lease_id=?',
                       (state,error[:160],self.clock()+(retry_after or 5),batch['id'],batch['lease_id']))

    def resume_authenticated(self,binding):
        with self.transaction() as db:
            db.execute("UPDATE batches SET state='pending',error='',next_at=0 WHERE account=? AND origin=? AND device=? AND state='blocked'",self._who(binding))

    def stats(self,binding=None):
        where=' WHERE account=? AND origin=? AND device=?' if binding else ''
        args=self._who(binding) if binding else ()
        with closing(self.connect()) as db:
            states={r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM batches'+where+' GROUP BY state',args)}
            count=db.execute('SELECT COUNT(*) FROM records'+where,args).fetchone()[0]
            error=db.execute("SELECT error FROM batches"+where+(" AND" if binding else " WHERE")+" error<>'' ORDER BY created DESC LIMIT 1",args).fetchone()
            return {'records':count,'states':states,'last_error':error[0] if error else ''}

    def export_csv(self,binding,path):
        from upload_protocol import COLUMNS
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        handle,temp=tempfile.mkstemp(dir=path.parent,prefix='.training-export-')
        count=0
        try:
            with os.fdopen(handle,'w',encoding='utf-8',newline='') as stream,closing(self.connect()) as db:
                writer=csv.DictWriter(stream,fieldnames=COLUMNS,lineterminator='\n');writer.writeheader()
                for row in db.execute('SELECT payload FROM records WHERE account=? AND origin=? AND device=? ORDER BY seq',self._who(binding)):
                    writer.writerow(json.loads(row['payload']));count+=1
                stream.flush();os.fsync(stream.fileno())
            os.replace(temp,path);return count
        finally:
            if os.path.exists(temp):os.unlink(temp)
