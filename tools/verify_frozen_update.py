"""Opt-in Windows acceptance: two isolated QA builds and a local-only TLS server.

Uses a fresh in-memory signing key. Never forwards traffic or changes system CA,
real user data, production releases, or the working tree's version. The rollback
case leaves the restored QA GUI open for visual inspection and normal closing.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa,ed25519
from cryptography.x509.oid import NameOID


def wait_for(test,seconds=120):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=test()
        if value:return value
        time.sleep(.2)
    raise TimeoutError('QA condition not reached')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--identity-trust',type=Path,required=True)
    args=parser.parse_args()
    source=Path(__file__).resolve().parents[1];output=args.output.resolve()
    if output.exists():raise ValueError('Use a new output directory')
    output.mkdir(parents=True)
    sys.path.insert(0,str(source))
    from public_trust import read_public_trust
    from client_update_manifest import canonical,SCHEMA,PRODUCT
    from client_update_download import file_hash
    from client_update_install import prepare
    public=read_public_trust(args.identity_trust)
    origin=public['server_origin'];host=origin.removeprefix('https://')
    if '/' in host or ':' in host:raise ValueError('QA requires a plain HTTPS hostname')
    signing=ed25519.Ed25519PrivateKey.generate()
    key_id='isolated-qa-never-production'
    trust=output/'QA-ONLY-update-trust.json'
    trust.write_text(json.dumps(dict(schema=SCHEMA,origin=origin,keys={key_id:base64.b64encode(signing.public_key().public_bytes_raw()).decode()})))
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=source).decode().split('\0')
    additions=['client_update_download.py','client_update_install.py','client_update_lock.py']
    built={};evidence={'scope':'isolated QA keys, TLS and APPDATA; no production traffic','builds':{},'cases':[]}
    for name,version in (('old','1.14.0'),('new','1.15.0')):
        folder=output/name;folder.mkdir()
        for relative in sorted(set(tracked+additions)):
            if not relative or not (source/relative).is_file():continue
            destination=folder/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/relative,destination)
        (folder/'client_version.py').write_text('VERSION = '+repr(version)+'\n')
        env=dict(os.environ,DIRECTOR_PUBLIC_TRUST_CONFIG=str(args.identity_trust.resolve()),DIRECTOR_UPDATE_TRUST_CONFIG=str(trust),PYTHONUTF8='1')
        with (output/(name+'-build.log')).open('w',encoding='utf-8') as log:
            subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','Yuzi_CS2_AIDirector.spec'],cwd=folder,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=900)
        built[name]=folder/'dist/Yuzi_CS2_AIDirector.exe'
        evidence['builds'][name]={'version':version,'sha256':file_hash(built[name]),'bytes':built[name].stat().st_size}
        print('QA build ready: '+name,flush=True)
    with tempfile.TemporaryDirectory(prefix='local-tls-',dir=output) as temporary:
        temporary=Path(temporary);key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,host)])
        now=datetime.datetime.now(datetime.timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(hours=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]),critical=False)
            .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
        certfile=temporary/'ca.pem';keyfile=temporary/'key.pem'
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certfile,keyfile)
        active={};requests=[]
        def headers(sock):
            value=b''
            while b'\r\n\r\n' not in value and len(value)<8192:
                block=sock.recv(1)
                if not block:break
                value+=block
            return value
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(10)
                    if not headers(self.request).startswith(('CONNECT '+host+':443 ').encode()):return
                    self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                    with context.wrap_socket(self.request,server_side=True) as sock:
                        request=headers(sock).decode('iso-8859-1');path=request.split(' ')[1]
                        assert 'authorization:' not in request.lower() and 'cookie:' not in request.lower()
                        requests.append(path)
                        if path.startswith('/api/director/client-updates/manifest?'):
                            body=json.dumps(dict(success=True,code='OK',data=active['signed'])).encode()
                            sock.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
                        elif path==active['file_path']:
                            start=0
                            for line in request.split('\r\n'):
                                if line.lower().startswith('range: bytes='):start=int(line.split('=')[1].split('-')[0])
                            package=built['new'];length=package.stat().st_size
                            response='HTTP/1.1 '+('206 Partial Content' if start else '200 OK')+'\r\nETag: "'+active['payload']['sha256']+'"\r\nContent-Length: '+str(length-start)+'\r\n'
                            if start:response+=f'Content-Range: bytes {start}-{length-1}/{length}\r\n'
                            sock.sendall((response+'\r\n').encode())
                            with package.open('rb') as stream:
                                stream.seek(start)
                                while block:=stream.read(1024*1024):sock.sendall(block)
                            active['range_start']=start
                        else:sock.sendall(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n')
                except (OSError,ValueError,ssl.SSLError):pass
        class Server(socketserver.ThreadingTCPServer):daemon_threads=True
        with Server(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            previous=dict(os.environ)
            os.environ.update(HTTPS_PROXY=f'http://127.0.0.1:{server.server_address[1]}',HTTP_PROXY='',ALL_PROXY='',NO_PROXY='',SSL_CERT_FILE=str(certfile))
            try:
                for number,(case,version,role) in enumerate((('upgrade','1.15.0','director'),('trainer','1.15.0','trainer'),('rollback','1.15.1','director')),1):
                    folder=output/('中文 空格 '+case);folder.mkdir()
                    data=folder/'data';os.environ['APPDATA']=str(data)
                    root=data/'CS2AIDirector'/'client-install';root.mkdir(parents=True)
                    target=folder/'Director.exe';shutil.copyfile(built['old'],target)
                    sentinel=data/'preserve-user-record.bin';sentinel.write_bytes(os.urandom(64));sentinel_hash=file_hash(sentinel)
                    package=built['new'];release=str(uuid4());now=int(time.time())
                    payload=dict(schema=SCHEMA,product=PRODUCT,origin=origin,channel='stable',release_id=release,version=version,platform='windows-x86_64',
                        sha256=file_hash(package),bytes=package.stat().st_size,sequence=number,issued_at=now-5,expires_at=now+3600,minimum_updater=1,notes='隔离验收，不可发行')
                    active.update(payload=payload,signed=dict(key_id=key_id,payload=payload,signature=base64.b64encode(signing.sign(canonical(payload))).decode()),
                        file_path='/api/director/client-releases/'+release+'/file',range_start=None)
                    download=root/'downloads'/payload['sha256'];download.mkdir(parents=True)
                    with package.open('rb') as stream:(download/'package.part').write_bytes(stream.read(1048576))
                    result=folder/'download-result.json';status=folder/'download-status.json';started=time.monotonic()
                    subprocess.run([str(target),'--client-update-download',str(root),str(trust),'stable','1.14.0',str(status),str(result)],check=True,timeout=120)
                    ready=json.loads(result.read_text());assert ready['state']=='ready',ready
                    assert active['range_start']==1048576,active['range_start']
                    jobpath=prepare(root,ready,target,trust_path=trust,current_version='1.14.0',role=role)
                    helper=jobpath.parent/'update-helper.exe'
                    process=subprocess.Popen([str(helper),'--client-update-install',str(jobpath)],env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1'))
                    process.wait(timeout=150)
                    job=json.loads(jobpath.read_text());expected='rolled_back' if case=='rollback' else 'complete'
                    assert job['phase']==expected,job['phase']
                    assert file_hash(target)==evidence['builds']['old' if case=='rollback' else 'new']['sha256']
                    assert file_hash(sentinel)==sentinel_hash
                    if case!='rollback':
                        (jobpath.parent/'cancel-health').write_bytes(b'qa-normal-close')
                        from client_update_install import runtime_lock
                        def stopped():
                            try:
                                with runtime_lock(root,target,timeout=.01):return True
                            except TimeoutError:return False
                        wait_for(stopped,30)
                    evidence['cases'].append(dict(case=case,phase=expected,resumed_bytes=active['range_start'],seconds=round(time.monotonic()-started,3),data_unchanged=True,target=str(target),job=str(jobpath)))
                    (output/'verification.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
                    print('QA transaction passed: '+case,flush=True)
                evidence['anonymous_requests']=len(requests)
                (output/'verification.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
            finally:
                os.environ.clear();os.environ.update(previous);server.shutdown();thread.join(timeout=2)
    print(json.dumps(evidence,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
