"""Bounded nonblocking handoff to a durable local journal; no network on the decision thread."""
import csv
import hashlib
import queue
import threading
import uuid
from upload_protocol import parse_csv, validate_row, DECISION_FEATURES, ProtocolError


class DurableRecorder:
    def __init__(self,outbox,binding_provider,csv_path=None,max_buffer=2048):
        self.outbox,self.binding_provider,self.csv_path=outbox,binding_provider,csv_path
        self.source_id=str(uuid.uuid4());self.buffer=queue.Queue(maxsize=max_buffer)
        self.stop_event=threading.Event();self.thread=None
        self.persisted=0;self.rejected=0;self.error='';self._current=None

    def offer(self,row):
        # Binding is captured at collection time and never reassigned after login/switch.
        try:item=(self.binding_provider(),dict(row),str(uuid.uuid4()))
        except Exception:
            self.rejected+=1;self.error='当前采集身份不可用，本次未接收；请检查登录状态';return False
        try:self.buffer.put_nowait(item);return True
        except queue.Full:self.rejected+=1;self.error='本地写入缓冲已满；本次未接收，详见未接收计数';return False

    def _write(self,item):
        binding,row,record_id=item
        inserted=self.outbox.append(binding,'online',self.source_id,[row],[record_id])
        self.persisted+=inserted
        self.error=''
        if self.csv_path and inserted:
            # Legacy CSV is a convenience derivative; SQLite is the recovery source.
            try:
                from decision_model import CSV_COLUMNS
                with self.csv_path.open('a',newline='',encoding='utf-8-sig') as stream:
                    csv.DictWriter(stream,fieldnames=CSV_COLUMNS).writerow(row)
            except OSError:self.error='受管记录已保存，兼容CSV写入失败；可从本地队列导出'

    def _loop(self):
        while not self.stop_event.is_set() or self._current or not self.buffer.empty():
            if self._current is None:
                try:self._current=self.buffer.get(timeout=.1)
                except queue.Empty:continue
            try:
                self._write(self._current);self._current=None
            except ProtocolError:
                self.rejected+=1;self.error='记录特征或成熟标签无效，本条未入队；详见未接收计数';self._current=None
            except Exception:
                self.error='本地写盘失败/容量不足，记录等待重试；请检查磁盘'
                if self.stop_event.wait(1):break

    def start(self):
        self.thread=threading.Thread(target=self._loop,name='training-recorder',daemon=True);self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=3)
        return self._current is None and self.buffer.empty()

    def stats(self):
        return {'persisted':self.persisted,'buffered':self.buffer.qsize()+int(self._current is not None),
                'not_accepted':self.rejected,'error':self.error}


def import_explicit_csv(outbox,binding,path,source='offline'):
    if path.name.lower().endswith('.gz'):
        # Compressed imports are bounded; large plain CSVs are read in limited chunks.
        data=path.read_bytes() if path.stat().st_size<=8*1024*1024 else b''
        rows=parse_csv(data,compressed=True)
        source_id=hashlib.sha256(data).hexdigest()
        return outbox.append(binding,source,source_id,rows,
            [hashlib.sha256((str(binding)+':'+source_id+':'+str(i)).encode()).hexdigest() for i in range(len(rows))],cursor=len(rows))
    with path.open('rb') as stream:source_id=hashlib.file_digest(stream,'sha256').hexdigest()
    added=0;pending=[];ids=[];count=0
    with path.open('r',encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream,strict=True)
        header=reader.fieldnames or []
        if len(set(header))!=len(header) or not set(DECISION_FEATURES+['label'])<=set(header):raise ProtocolError('CSV表头无效')
        for row in reader:
            if None in row or any(v is None or len(v)>2048 for v in row.values()):raise ProtocolError('CSV列数或字段长度无效')
            pending.append(validate_row(row));ids.append(hashlib.sha256((str(binding)+':'+source_id+':'+str(count)).encode()).hexdigest());count+=1
            if len(pending)>=500:
                added+=outbox.append(binding,source,source_id,pending,ids,cursor=count);pending=[];ids=[]
        if pending:added+=outbox.append(binding,source,source_id,pending,ids,cursor=count)
    if count==0:raise ProtocolError('没有成熟样本')
    return added
