"""Training dataset lifecycle and a single shared-store upload worker."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from client_update_lock import FileLock
from dataset_store import DatasetStore,safe_error
from dataset_transport import DatasetClient,DatasetFailure
import dataset_protocol as p


STATES={'freezing':'正在保存训练输入','ready':'输入已保存','training':'训练中或上次训练未完成','failed':'训练未完成，不自动上传','packaging':'正在整理训练数据','package-error':'训练已完成，数据整理需处理','pending':'等待上传','uploading':'正在上传','blocked':'登录或权限待恢复','rejected':'数据被拒绝，原件保留','received':'已接收，等待管理员筛选','cancelled':'上传已取消，本地数据保留'}
ERRORS={'MISSING_GROUP':'缺少候选分组，请从原录像重新解析','CSV_NUMBER':'样本含无效特征或标签','INPUT_CHANGED':'输入快照发生变化，已停止提交','LOCAL_QUOTA':'本地数据存储空间达到限制','DISK_SPACE':'磁盘空间不足','DIRECTOR_DATASET_LIMIT':'样本规模超过服务器当前限额','DIRECTOR_DATASET_DISABLED':'服务器尚未启用训练数据接收','FORBIDDEN':'当前账号没有训练数据提交权限','DIRECTOR_NO_ENTITLEMENT':'产品资格不可用','NETWORK_UNAVAILABLE':'网络不可用，数据已保留等待重试','IDENTITY_CHANGED':'登录状态已变化，提交已暂停','DIRECTOR_DATASET_EXPIRED':'服务器会话已过期，请保留原件并重新发起历史提交'}


def describe(row):
    text=STATES.get(row['state'],row['state'])
    if row['paused']:text='已暂停 · '+text
    if row['governance']:
        status=json.loads(row['governance'])['governanceStatus']
        text={'pending-review':'已接收，等待管理员筛选','included':'已纳入训练库，等待管理员主动训练','partially-included':'部分来源已纳入，其余待处理','excluded':'管理员未纳入本批数据','blocked':'管理员处理前需补充来源或兼容信息'}.get(status,text)
    if row['error']:text+=' · '+ERRORS.get(row['error'],'需要处理：'+row['error'])
    return text


class DatasetRuntime:
    def __init__(self,account,root=None,client=None,autostart=True):
        self.account=account
        root=root or Path(os.environ.get('APPDATA') or Path.home())/'CS2AIDirector'/'training-datasets'
        self.store=DatasetStore(root);self.client=client or DatasetClient(account.upload_identity)
        self.stop=threading.Event();self.wake=threading.Event();self.thread=None
        self.progress={};self._fingerprint=None;self._governance_at={};self.status='训练数据保存在本机'
        if autostart:self.start()

    def begin_training(self,path):
        run_id=self.store.freeze(path,self.account.binding());self.store.start_training(run_id)
        return run_id,self.store.input_path(run_id)

    def succeeded(self,run_id,result):
        data=self.store.training_succeeded(run_id,result);self.wake.set()
        return data

    def failed(self,run_id):self.store.fail_training(run_id)

    def cancel(self,run_id,binding):
        self.store.set_paused(run_id,binding,True)
        with FileLock(self.store.root/'upload-worker.lock',timeout=2):
            row=self.store.get(run_id,binding)
            if row['receipt']:return
            identity=self.account.upload_identity()
            if identity is None or identity.binding!=binding:raise DatasetFailure('IDENTITY_CHANGED',401)
            data=p.loads(row['descriptor'].encode())
            receipt=self.client.receipt(identity,data)
            if receipt is not None:self.store.received(run_id,receipt);return
            try:state=self.client.cancel(identity,data)
            except DatasetFailure as error:
                if error.status!=404 or error.code!='DIRECTOR_DATASET_NOT_FOUND':raise
            else:
                if state['transportStatus']=='received':
                    receipt=self.client.receipt(identity,data)
                    p.require(receipt is not None,'RECEIPT_MISSING');self.store.received(run_id,receipt);return
                p.require(state['transportStatus'] in ('cancelled','expired'),'CANCEL_NOT_CONFIRMED')
            self.store.cancelled(run_id,binding)

    def continuing(self,run_id,binding):
        if self.stop.is_set():return False
        row=self.store.get(run_id,binding)
        return not row['paused'] and row['state'] in ('pending','uploading','received')

    def run_once(self):
        identity=self.account.upload_identity()
        if identity is None or identity.binding.origin=='local':self.status='未登录，训练数据保留在本机';return False
        fingerprint=hashlib.sha256((str(identity.binding)+identity.access_token).encode()).hexdigest()
        if fingerprint!=self._fingerprint:
            with self.store.transaction() as db:
                db.execute("UPDATE runs SET state='pending',next_at=0 WHERE account=? AND origin=? AND device=? AND state='blocked' AND error IN ('IDENTITY_CHANGED','DIRECTOR_DEVICE_UNAUTHORIZED','DIRECTOR_REAUTH_REQUIRED','FORBIDDEN','DIRECTOR_NO_ENTITLEMENT')",self.store.actor(identity.binding))
            self._fingerprint=fingerprint
        try:
            with FileLock(self.store.root/'upload-worker.lock',timeout=0):
                # Packaging has a persisted success marker; never auto-submit an interrupted training.
                for item in self.store.list(identity.binding):
                    if item['state']=='packaging' and not item['paused']:
                        try:self.store.package(item['id'])
                        except TimeoutError:pass  # Another trainer is still packaging this run.
                row=self.store.next(identity.binding)
                if row is None:return self.refresh_one(identity)
                run_id=row['id'];self.store.begin_upload(run_id,identity.binding)
                try:
                    folder=self.store.package_path(row)
                    data=p.validate_directory(folder)
                    p.require(p.canonical(data).decode()==row['descriptor'] and p.digest(p.canonical(data))==row['request_hash'],'PACKAGE_CHANGED')
                    receipt=self.client.upload(identity,data,folder,lambda:self.continuing(run_id,identity.binding),lambda done,total:self.progress.update({run_id:(done,total)}))
                    if receipt is None:
                        self.store.upload_failed(run_id,'pending','SERVER_VERIFYING',15);return False
                    self.store.received(run_id,receipt);self.status='服务器已接收，等待管理员筛选';return True
                except DatasetFailure as error:
                    state='blocked' if error.status in (401,403) or error.code.startswith('PROTOCOL_') else 'rejected' if error.status in (400,409,410,413,415,422) or 300<=error.status<400 else 'pending'
                    delay=max(error.retry_after,min(3600,5*2**min(row['attempts'],9)))
                    self.store.upload_failed(run_id,state,error.code,delay)
                    self.status=ERRORS.get(error.code,'上传未完成，原件保留');return False
                except (ValueError,OSError) as error:
                    self.store.upload_failed(run_id,'blocked',safe_error(error),60);self.status='数据校验未通过，原件保留';return False
        except TimeoutError:return False

    def refresh_one(self,identity):
        now=time.monotonic()
        for row in self.store.list(identity.binding):
            if row['state']!='received' or row['paused'] or now-self._governance_at.get(row['id'],0)<60:continue
            self._governance_at[row['id']]=now
            try:
                data=p.loads(row['descriptor'].encode());receipt=json.loads(row['receipt'])
                value=self.client.governance(identity,data,receipt,lambda:self.continuing(row['id'],identity.binding))
                self.store.save_governance(row['id'],value)
            except (DatasetFailure,ValueError,OSError):pass  # Durable receipt is unchanged; retry read later.
            return False
        return False

    def _loop(self):
        while not self.stop.is_set():
            try:progressed=self.run_once()
            except Exception:self.status='本地训练数据队列暂不可用，记录保留';progressed=False
            if not progressed:self.wake.wait(2);self.wake.clear()

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop.clear();self.thread=threading.Thread(target=self._loop,name='dataset-uploader',daemon=True);self.thread.start()

    def close(self):
        self.stop.set();self.wake.set()
        if self.thread:self.thread.join(timeout=3)
        return self.thread is None or not self.thread.is_alive()
