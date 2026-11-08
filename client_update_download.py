"""Signed, user-requested application downloads. No account credentials or install."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from client_update import MAX_RESPONSE_BYTES, UpdateProtocolError, version_tuple
from client_update_manifest import read_update_trust, verify
from client_update_lock import FileLock
from client_update_transport import NoRedirect
from device_identity import atomic_write

PLATFORM='windows-x86_64'
MAX_PACKAGE_BYTES=512*1024*1024


def runtime_trust_path():
    # Only build-pinned update keys; an adjacent downloaded key is never trusted.
    base=Path(sys._MEIPASS) if getattr(sys,'frozen',False) else Path(__file__).parent
    return base/'client-update-trust.json'


def trust_status():
    try:value=read_update_trust(runtime_trust_path())
    except FileNotFoundError:return {'state':'missing','installation_available':False}
    except (OSError,ValueError):return {'state':'invalid','installation_available':False}
    return {'state':'configured','installation_available':True,'origin':value['origin'],
            'key_ids':sorted(value['keys']),
            'configuration_sha256':hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()}


def managed(root,*parts):
    root=Path(root).resolve();current=root
    for part in parts:
        if not isinstance(part,str) or part in ('','.','..') or '/' in part or '\\' in part:raise ValueError('invalid_managed_path')
        current=current/part
        if current.is_symlink() or current.is_junction():raise ValueError('managed_path_is_link')
    if not current.resolve().is_relative_to(root):raise ValueError('managed_path_escape')
    return current


def read_json(path,limit=MAX_RESPONSE_BYTES):
    if Path(path).stat().st_size>limit:raise UpdateProtocolError('metadata_too_large')
    return json.loads(Path(path).read_text(encoding='utf-8'))


def file_hash(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def validate_package(path,payload):
    path=Path(path)
    if path.stat().st_size!=payload['bytes'] or file_hash(path)!=payload['sha256']:
        raise UpdateProtocolError('package_integrity')
    with path.open('rb') as stream:
        header=stream.read(64)
        if len(header)!=64 or header[:2]!=b'MZ':raise UpdateProtocolError('package_not_pe')
        offset=struct.unpack_from('<I',header,60)[0]
        if not 64<=offset<=min(1024*1024,payload['bytes']-6):raise UpdateProtocolError('package_pe_header')
        stream.seek(offset);signature=stream.read(6)
        if signature!=b'PE\0\0\x64\x86':raise UpdateProtocolError('package_architecture')


class ManifestStore:
    """Security high-water mark is NEVER reset along with the disposable UI cache."""
    def __init__(self,root,trust,channel):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.trust=trust;self.channel=channel
        name=hashlib.sha256((trust['origin']+'/'+channel+'/'+PLATFORM).encode()).hexdigest()
        self.path=managed(self.root,'security',name+'.json')
        self.path.parent.mkdir(parents=True,exist_ok=True)

    def accept(self,envelope):
        with FileLock(self.path.with_suffix('.lock')):
            previous=read_json(self.path) if self.path.exists() else {'sequence':0,'payload_digest':None}
            if (not isinstance(previous,dict) or type(previous.get('sequence')) is not int
                    or previous['sequence']<0):raise UpdateProtocolError('invalid_update_high_water')
            value=verify(envelope,self.trust,channel=self.channel,platform=PLATFORM,now=int(time.time()),
                highest_sequence=previous['sequence'],previous_digest=previous.get('payload_digest'))
            atomic_write(self.path,json.dumps({'sequence':value['sequence'],'payload_digest':value['payload_digest']}).encode())
            return value


def manifest(store,opener):
    url=store.trust['origin']+'/api/director/client-updates/manifest?channel='+store.channel+'&platform='+PLATFORM
    request=urllib.request.Request(url,headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'YuziDirector-Update/1'})
    with opener.open(request,timeout=5) as response:
        if response.status!=200:raise UpdateProtocolError('manifest_unavailable')
        raw=response.read(MAX_RESPONSE_BYTES+1)
    if len(raw)>MAX_RESPONSE_BYTES:raise UpdateProtocolError('manifest_too_large')
    envelope=json.loads(raw.decode('utf-8-sig'))
    if not isinstance(envelope,dict) or envelope.get('success') is not True or envelope.get('code')!='OK':
        raise UpdateProtocolError('manifest_unavailable')
    signed=envelope.get('data');return store.accept(signed),signed


def identity(payload):
    return tuple(payload[key] for key in ('origin','channel','platform','release_id','version','sha256','bytes'))


def download_core(root,trust_path,channel,current_version,progress=lambda **_:None,opener=None):
    trust=read_update_trust(trust_path);store=ManifestStore(root,trust,channel)
    opener=opener or urllib.request.build_opener(NoRedirect())
    progress(stage='manifest',bytes=0)
    payload,envelope=manifest(store,opener)
    current,target=version_tuple(current_version),version_tuple(payload['version'])
    if target<=current:raise UpdateProtocolError('update_not_newer')
    if target[0]!=current[0]:raise UpdateProtocolError('major_upgrade_requires_manual_install')
    if payload['bytes']>MAX_PACKAGE_BYTES:raise UpdateProtocolError('package_too_large')
    folder=managed(root,'downloads',payload['sha256']);folder.mkdir(parents=True,exist_ok=True)
    with FileLock(managed(folder,'download.lock'),timeout=.1):
        partial=managed(folder,'package.part');ready=managed(folder,'package.exe')
        count=partial.stat().st_size if partial.exists() else 0
        if count>payload['bytes']:partial.unlink();count=0
        if ready.exists():
            try:validate_package(ready,payload)
            except UpdateProtocolError:ready.unlink()
        if not ready.exists():
            if shutil.disk_usage(folder).free < payload['bytes']-count+16*1024*1024:raise OSError('update_disk_space')
            if count<payload['bytes']:
                headers={'Accept':'application/octet-stream','Accept-Encoding':'identity','User-Agent':'YuziDirector-Update/1'}
                if count:headers.update(Range=f'bytes={count}-',**{'If-Range':'"'+payload['sha256']+'"'})
                request=urllib.request.Request(trust['origin']+'/api/director/client-releases/'+payload['release_id']+'/file',headers=headers)
                progress(stage='download',bytes=count,total=payload['bytes'])
                with opener.open(request,timeout=5) as response:
                    if response.headers.get('Content-Encoding','identity').lower()!='identity':raise UpdateProtocolError('encoded_package')
                    if response.headers.get('ETag')!='"'+payload['sha256']+'"':raise UpdateProtocolError('package_etag')
                    if response.status==200:count=0
                    elif response.status!=206 or not count:raise UpdateProtocolError('package_range_status')
                    elif response.headers.get('Content-Range')!=f'bytes {count}-{payload["bytes"]-1}/{payload["bytes"]}':
                        raise UpdateProtocolError('package_content_range')
                    if response.headers.get('Content-Length')!=str(payload['bytes']-count):raise UpdateProtocolError('package_content_length')
                    with partial.open('ab' if count else 'wb') as output:
                        while True:
                            block=response.read(min(256*1024,payload['bytes']-count+1))
                            if not block:break
                            if count+len(block)>payload['bytes']:raise UpdateProtocolError('package_overlong')
                            output.write(block);count+=len(block)
                            progress(stage='download',bytes=count,total=payload['bytes'])
                        output.flush();os.fsync(output.fileno())
            progress(stage='verify',bytes=count,total=payload['bytes'])
            try:validate_package(partial,payload)
            except UpdateProtocolError:
                partial.unlink(missing_ok=True);raise
            os.replace(partial,ready)
        progress(stage='revalidate',bytes=payload['bytes'],total=payload['bytes'])
        latest,latest_envelope=manifest(store,opener)
        if identity(latest)!=identity(payload):raise UpdateProtocolError('release_changed')
        atomic_write(managed(folder,'manifest.json'),json.dumps(latest_envelope).encode())
        return {'state':'ready','folder':str(folder),'payload':latest}


def download(root,trust_path,channel,current_version,cancel,progress=lambda _:None):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='transfer-',dir=root) as temp:
        status=Path(temp)/'status.json';result=Path(temp)/'result.json'
        command=([sys.executable,'--client-update-download'] if getattr(sys,'frozen',False)
                 else [sys.executable,str(Path(__file__).resolve()),'--download'])
        command += [str(root),str(trust_path),channel,current_version,str(status),str(result)]
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        started=last_activity=time.monotonic();last=None
        try:
            while process.poll() is None:
                if cancel.wait(.1):return {'state':'cancelled'}
                now=time.monotonic()
                try:value=read_json(status,4096) if status.exists() else None
                except (ValueError,OSError):value=None
                if value!=last and value is not None:last=value;last_activity=now;progress(value)
                limit=15 if last and last.get('stage')=='download' else 20
                if now-last_activity>limit or now-started>1800:return {'state':'failed','error':'transfer_timeout'}
            return read_json(result) if not process.returncode and result.exists() else {'state':'failed','error':'transfer_worker'}
        finally:
            if process.poll() is None:process.kill()
            process.wait(timeout=5)


def worker_main(arguments):
    if len(arguments)!=6:return 2
    root,trust,channel,current,status,result=arguments
    last=[0.]
    def progress(**value):
        now=time.monotonic()
        if now-last[0]>.2 or value.get('stage')!='download':
            atomic_write(status,json.dumps(value).encode());last[0]=now
    try:value=download_core(root,trust,channel,current,progress)
    except FileNotFoundError:value={'state':'failed','error':'update_trust_or_file_missing'}
    except (OSError,ValueError,TimeoutError,urllib.error.URLError) as error:
        # Do not expose remote response text, proxy credentials or private paths.
        value={'state':'failed','error':str(error) if isinstance(error,UpdateProtocolError) else 'transfer_unavailable'}
    atomic_write(result,json.dumps(value).encode());return 0


if __name__=='__main__':raise SystemExit(worker_main(sys.argv[2:]) if sys.argv[1:2]==['--download'] else 2)
