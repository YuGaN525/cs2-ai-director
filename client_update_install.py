"""Portable EXE update transaction. No elevation, process killing or data rollback."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
from client_update import UpdateProtocolError, version_tuple
from client_update_download import (ManifestStore,file_hash,identity,managed,read_json,
                                    runtime_trust_path,validate_package)
from client_update_lock import FileLock
from client_update_manifest import read_update_trust
from client_update_transport import check
from client_version import VERSION
from device_identity import atomic_write


def update_root():
    from decision_model import decision_model_path
    return decision_model_path().parent/'client-install'


def runtime_lock(root,target,*,shared=False,timeout=2):
    identity=os.path.normcase(str(Path(target).resolve()))
    name=hashlib.sha256(identity.encode()).hexdigest()+'.lock'
    return FileLock(managed(root,'runtime',name),shared=shared,timeout=timeout)


def application_guard(arguments):
    if not getattr(sys,'frozen',False) or arguments[:1]==['--client-update-install']:return nullcontext()
    return runtime_lock(update_root(),sys.executable,shared=True)


def fresh_manifest(root,trust,channel):
    response=check(trust['origin'],channel,threading.Event(),kind='manifest')
    envelope=response.get('envelope')
    if not isinstance(envelope,dict) or envelope.get('success') is not True or envelope.get('code')!='OK':
        raise UpdateProtocolError('update_revalidation_unavailable')
    signed=envelope.get('data')
    return ManifestStore(root,trust,channel).accept(signed),signed


def save_job(path,job,phase,**changes):
    job.update(changes,phase=phase,updated_at=time.time())
    atomic_write(path,json.dumps(job).encode())


def prepare(root,ready,target,*,trust_path=None,current_version=VERSION,role='director'):
    if role not in ('director','trainer'):raise ValueError('invalid_update_role')
    root=Path(root).resolve();target=Path(target).resolve(strict=True)
    if target.suffix.lower()!='.exe' or not target.is_file():raise ValueError('update_target_not_exe')
    trust=read_update_trust(trust_path or runtime_trust_path())
    payload,signed=fresh_manifest(root,trust,ready['payload']['channel'])
    if identity(payload)!=identity(ready['payload']):raise UpdateProtocolError('release_changed')
    current,new=version_tuple(current_version),version_tuple(payload['version'])
    if new<=current or new[0]!=current[0]:raise UpdateProtocolError('incompatible_update_version')
    folder=managed(root,'downloads',payload['sha256'])
    package=managed(folder,'package.exe');validate_package(package,payload)
    size=target.stat().st_size
    if shutil.disk_usage(target.parent).free < payload['bytes']+size+16*1024*1024:
        raise OSError('update_target_disk_space')
    if shutil.disk_usage(root).free < payload['bytes']+size+16*1024*1024:
        raise OSError('update_staging_disk_space')
    token=secrets.token_hex(16);jobdir=managed(root,'jobs',token);jobdir.mkdir(parents=True)
    old_hash=file_hash(target)
    staged=jobdir/'candidate.exe';helper=jobdir/'update-helper.exe'
    shutil.copyfile(package,staged);validate_package(staged,payload)
    shutil.copyfile(target,helper)
    if file_hash(helper)!=old_hash:raise UpdateProtocolError('helper_integrity')
    job={'schema':1,'token':token,'target':str(target),'old_hash':old_hash,'old_version':current_version,
         'manifest':signed,'payload':payload,'created_at':time.time(),'role':role}
    path=jobdir/'job.json';save_job(path,job,'prepared')
    (jobdir/'恢复说明.txt').write_text('如果更新中断且主程序无法打开，请关闭同一程序的其他窗口，然后运行本目录的 update-helper.exe。\n原程序备份位于安装目录，恢复过程只处理程序文件，不还原账号或采集数据库。\n',encoding='utf-8')
    return path


def validated_job(path,root):
    root=Path(root).resolve();path=Path(path).resolve(strict=True)
    token=path.parent.name
    if len(token)!=32 or any(c not in '0123456789abcdef' for c in token):raise ValueError('invalid_update_job')
    if path!=managed(root,'jobs',token,'job.json'):raise ValueError('update_job_outside_root')
    job=read_json(path)
    if job.get('schema')!=1 or job.get('token')!=token:raise ValueError('invalid_update_job')
    target=Path(job['target'])
    if not target.is_absolute() or target.suffix.lower()!='.exe' or target.is_symlink() or target.is_junction():
        raise ValueError('invalid_update_target')
    if target.parent.resolve()!=target.parent:raise ValueError('update_target_parent_link')
    return job,target


def launch_helper(path,recovery=False):
    path=Path(path)
    job,target=validated_job(path,update_root())
    if target!=Path(sys.executable).resolve():raise ValueError('update_wrong_target')
    helper=path.parent/'update-helper.exe'
    if file_hash(helper)!=job['old_hash'] or (not recovery and file_hash(target)!=job['old_hash']):raise ValueError('update_helper_changed')
    env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1')
    command=[str(helper),'--client-update-install',str(path)]+(['recover'] if recovery else [])
    return subprocess.Popen(command,env=env,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)


def pending_job(root,target):
    folder=managed(root,'jobs')
    if not folder.exists():return None
    phases={'staging','backing_up','backed_up','replacing','awaiting_health','rollback_pending'}
    for path in sorted(folder.glob('*/job.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]:
        try:
            job,jobtarget=validated_job(path,root)
            if jobtarget==Path(target).resolve() and job['phase'] in phases:return path
        except (OSError,ValueError,KeyError,TypeError):continue
    return None


def last_result(root,target):
    path=managed(root,'last-result.json')
    if not path.exists():return None
    value=read_json(path,4096)
    return value.get('state') if value.get('target')==str(Path(target).resolve()) else None


def before_destroy(app):
    path=getattr(app,'_update_job',None)
    if path:
        try:launch_helper(path,getattr(app,'_update_recovery',False))
        except (OSError,ValueError):
            from tkinter import messagebox
            messagebox.showerror('更新未启动','更新助手未能启动，原程序文件未替换。记录已保存，可重新打开程序后再试。',parent=app.root)


def _paths(path,job,target):
    suffix='.update-'+job['token']
    backup=target.with_name(target.name+suffix+'.backup')
    staged=target.with_name(target.name+suffix+'.new')
    for value in (backup,staged):
        if value.is_symlink() or value.is_junction():raise ValueError('update_target_link')
    return path.parent/'candidate.exe',backup,staged,path.parent/'ready.json'


def rollback(path,job,target,backup,staged):
    """Restore only this transaction's old EXE; never restore application databases."""
    if not backup.exists() or file_hash(backup)!=job['old_hash']:raise ValueError('update_backup_missing_or_changed')
    if target.exists() and file_hash(target) not in (job['old_hash'],job['payload']['sha256']):
        raise ValueError('update_target_changed')
    os.replace(backup,target)
    staged.unlink(missing_ok=True)
    save_job(path,job,'rolled_back')


def recover(path,root,timeout=.1):
    """Called explicitly on an interrupted job; never silently rerun an installation."""
    job,target=validated_job(path,root)
    with runtime_lock(root,target,timeout=timeout):
        _,backup,staged,_=_paths(path,job,target)
        if job['phase'] in ('backed_up','replacing','awaiting_health','rollback_pending'):
            rollback(path,job,target,backup,staged)
        elif job['phase']=='backing_up':
            if backup.exists():rollback(path,job,target,backup,staged)
            elif target.exists() and file_hash(target)==job['old_hash']:
                staged.unlink(missing_ok=True);save_job(path,job,'cancelled')
            else:raise ValueError('update_manual_recovery_required')
        elif job['phase'] in ('prepared','validated','staging'):
            if target.exists() and file_hash(target)==job['old_hash']:
                staged.unlink(missing_ok=True);save_job(path,job,'cancelled')
            else:raise ValueError('update_manual_recovery_required')
        return job['phase']


def perform(path,root,trust_path,*,launcher=None,health_timeout=45):
    job,target=validated_job(path,root)
    if job['phase']!='prepared':raise ValueError('update_job_already_consumed')
    if not 0<=time.time()-job['created_at']<=300:raise ValueError('update_job_expired')
    # Another director, trainer or CLI process prevents replacement. Never kill it.
    with runtime_lock(root,target,timeout=60):
        if file_hash(target)!=job['old_hash']:raise ValueError('update_target_changed')
        trust=read_update_trust(trust_path)
        current,_=fresh_manifest(root,trust,job['payload']['channel'])
        if identity(current)!=identity(job['payload']):raise UpdateProtocolError('release_changed')
        package,backup,staged,ack=_paths(path,job,target)
        validate_package(package,current)
        if backup.exists() or staged.exists():raise ValueError('update_target_files_exist')
        if shutil.disk_usage(target.parent).free<current['bytes']+16*1024*1024:raise OSError('update_target_disk_space')
        save_job(path,job,'staging')
        with package.open('rb') as src,staged.open('xb') as dst:
            shutil.copyfileobj(src,dst,1024*1024);dst.flush();os.fsync(dst.fileno())
        validate_package(staged,current)
        save_job(path,job,'backing_up')
        os.replace(target,backup)
        try:
            save_job(path,job,'backed_up')
            save_job(path,job,'replacing')
            os.replace(staged,target)
            save_job(path,job,'awaiting_health')
        except (OSError,ValueError):
            rollback(path,job,target,backup,staged);raise
    # Release the exclusive lock before the new GUI acquires its shared lease.
    process=None
    try:
        if launcher is None:
            env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1')
            launcher=lambda command:subprocess.Popen(command,env=env)
        process=launcher([str(target),'--client-update-health',str(path)]+(['--trainer'] if job.get('role')=='trainer' else []))
        end=time.monotonic()+health_timeout
        while time.monotonic()<end:
            if ack.exists():
                value=read_json(ack,4096)
                if (value.get('token')==job['token'] and value.get('version')==job['payload']['version']
                        and value.get('sha256')==job['payload']['sha256'] and process.poll() is None):
                    save_job(path,job,'complete');return 'complete'
            if process.poll() is not None:break
            time.sleep(.1)
        # Ask our own new app to close through its normal persistence path.
        atomic_write(path.parent/'cancel-health',b'cancel')
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            save_job(path,job,'rollback_pending',error='new_process_still_running');return 'rollback_pending'
    except (OSError,ValueError):
        if process is not None and process.poll() is None:
            atomic_write(path.parent/'cancel-health',b'cancel')
            save_job(path,job,'rollback_pending',error='new_process_still_running');return 'rollback_pending'
    with runtime_lock(root,target,timeout=.1):rollback(path,job,target,backup,staged)
    return 'rolled_back'


def health_ready(app,path):
    job,target=validated_job(path,update_root())
    if target!=Path(sys.executable).resolve() or job['phase']!='awaiting_health' or VERSION!=job['payload']['version']:
        raise ValueError('invalid_update_health')
    if file_hash(target)!=job['payload']['sha256']:raise ValueError('update_health_hash')
    from desktop_ui import schedule
    def ready():
        if (path.parent/'cancel-health').exists():app._on_close();return
        atomic_write(path.parent/'ready.json',json.dumps({'token':job['token'],'version':VERSION,'sha256':job['payload']['sha256']}).encode())
    schedule(app.root,1000,ready)
    def cancellation():
        if (path.parent/'cancel-health').exists():app._on_close();return
        schedule(app.root,500,cancellation)
    schedule(app.root,500,cancellation)


def helper_main(arguments):
    if len(arguments) not in (1,2) or (len(arguments)==2 and arguments[1]!='recover'):return 2
    path=Path(arguments[0]);root=update_root()
    try:
        job,_=validated_job(path,root)
        if Path(sys.executable).resolve()!=path.parent/'update-helper.exe' or file_hash(sys.executable)!=job['old_hash']:
            raise ValueError('invalid_update_helper')
        if len(arguments)==2:state=recover(path,root,timeout=60)
        else:state=perform(path,root,runtime_trust_path())
        atomic_write(managed(root,'last-result.json'),json.dumps({'target':job['target'],'state':state}).encode())
        if state=='rolled_back':
            subprocess.Popen([job['target']]+(['--trainer'] if job.get('role')=='trainer' else []),env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1'))
        return 0
    except (OSError,ValueError,TimeoutError):
        # Preserve the last durable phase so a restart can recover it safely.
        try:
            job,target=validated_job(path,root)
            atomic_write(managed(root,'last-result.json'),json.dumps({'target':str(target),'state':'failed'}).encode())
            if target.exists() and file_hash(target)==job['old_hash']:
                subprocess.Popen([str(target)]+(['--trainer'] if job.get('role')=='trainer' else []),env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1'))
        except (OSError,ValueError,KeyError,TypeError):pass
        return 1
