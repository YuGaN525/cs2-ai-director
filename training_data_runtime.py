"""Common desktop composition for recording and upload status, with identity injection."""
import json
import os
from pathlib import Path
import uuid
from device_identity import atomic_write, canonical
from training_recorder import DurableRecorder, import_explicit_csv
from upload_outbox import UploadOutbox, Binding
from upload_worker import UploadWorker


class TrainingDataRuntime:
    def __init__(self,root=None,csv_path=None,identity_provider=lambda:None,binding_provider=None):
        self.root=Path(root or Path(os.environ.get('APPDATA') or Path.home())/'CS2AIDirector'/'training-data')
        self.root.mkdir(parents=True,exist_ok=True)
        self.outbox=UploadOutbox(self.root/'outbox.sqlite3')
        # A transaction gives director/trainer processes the same installation namespace.
        # This is never an authentication credential or a server binding.
        with self.outbox.transaction() as db:
            db.execute("INSERT OR IGNORE INTO local_settings VALUES('installation',?)",(str(uuid.uuid4()),))
            self.device=db.execute("SELECT value FROM local_settings WHERE name='installation'").fetchone()[0]
        self.identity_provider=identity_provider
        self.binding_provider=binding_provider
        self.import_error=''
        self.recorder=DurableRecorder(self.outbox,self.binding,csv_path)
        self.uploader=UploadWorker(self.outbox,identity_provider)
        self.recorder.start();self.uploader.start()

    def binding(self):
        if self.binding_provider is not None:
            # An initialized offline session keeps its actor even when API tokens expire.
            return self.binding_provider()
        identity=self.identity_provider()
        return identity.binding if identity is not None else Binding('unbound','local',self.device)

    def import_file(self,path,source='offline'):
        count=import_explicit_csv(self.outbox,self.binding(),Path(path),source)
        self.uploader.wake_event.set();return count

    def status_text(self):
        stats=self.recorder.stats()
        return (f"本次已保存 {stats['persisted']} · 写入中 {stats['buffered']} · 未接收 {stats['not_accepted']}\n"
                + (stats['error'] or self.import_error or self.uploader.status))

    def capture_training_file(self,path,source='offline'):
        try:
            count=self.import_file(path,source);self.import_error='';return count
        except Exception:
            self.import_error='训练数据未完全入队，请检查格式/磁盘并重新选择文件重试；已有记录保留'
            return None

    def summary(self):
        stats=self.outbox.stats(self.binding());states=stats['states']
        text=(f"\n\n本设备受管记录：{stats['records']} 条\n"
              f"待上传 {states.get('pending',0)} 批 · 上传中 {states.get('inflight',0)} 批\n"
              f"已确认 {states.get('confirmed',0)} 批 · 待处理 {states.get('blocked',0)} 批\n"
              f"数据被拒绝 {states.get('rejected',0)} 批（记录保留）")
        return self.status_text()+text

    def close(self):
        flushed=self.recorder.close()
        if not flushed:
            self.recorder.stop_event.clear()
            if not self.recorder.thread.is_alive():self.recorder.start()
            return False
        self.uploader.stop();return True


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='本设备本地训练记录与上传队列诊断')
    parser.add_argument('--root',type=Path)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--export',type=Path,help='导出当前本地未绑定记录，保留队列原数据')
    args=parser.parse_args(argv)
    runtime=TrainingDataRuntime(root=args.root)
    try:
        result=runtime.outbox.stats(runtime.binding())
        if args.export:result['exported_rows']=runtime.outbox.export_csv(runtime.binding(),args.export)
        if args.report:atomic_write(args.report,canonical(result))
        print(json.dumps(result,ensure_ascii=False));return 0
    finally:runtime.close()
