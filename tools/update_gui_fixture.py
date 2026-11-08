"""Opt-in, local TLS fixture for two actual GUIs; no upstream traffic or real data.

Close both GUI windows normally to finish, or create OUTPUT/STOP to end the
fixture server. A process-local CA is used; Windows trust/settings are untouched.
"""
import argparse,datetime,json,os,socketserver,ssl,subprocess,tempfile,threading,time
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--exe',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args();output=args.output.resolve()
output.mkdir(parents=True,exist_ok=False)
requests=[];host='www.yuzibei.cn'
with tempfile.TemporaryDirectory(prefix='process-only-ca-',dir=output) as temp:
    temp=Path(temp);key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,host)]);now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]),critical=False)
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    certificate=temp/'certificate.pem';private=temp/'private.pem'
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(certificate,private)
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
                self.request.settimeout(3)
                if not headers(self.request).startswith(('CONNECT '+host+':443 ').encode()):return
                self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                with tls.wrap_socket(self.request,server_side=True) as sock:
                    request=headers(sock).decode('iso-8859-1');path=request.split(' ')[1]
                    anonymous='authorization:' not in request.lower() and 'cookie:' not in request.lower()
                    requests.append({'path':path,'anonymous':anonymous,'time':time.time()})
                    ident='10000000-0000-0000-0000-000000000015'
                    data=[] if 'channel=beta' in path else [dict(id=ident,version='1.16.0',channel='stable',enabled=True,artifactHash='b'*64,artifactBytes=187000000,
                        revision=1,notes='隔离界面验收，不可发行。\n'+'长更新说明，滚轮应能逐行查看。\n'*75,downloadPath=f'/api/director/client-releases/{ident}/file')]
                    if path.startswith('/api/director/client-releases?'):
                        body=json.dumps(dict(success=True,code='OK',data=data)).encode();status='200 OK'
                    else:body=b'{}';status='404 Not Found'
                    sock.sendall(('HTTP/1.1 '+status+'\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body)
            except (OSError,ssl.SSLError):pass
    class Server(socketserver.ThreadingTCPServer):daemon_threads=True
    with Server(('127.0.0.1',0),Handler) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        env=dict(os.environ,APPDATA=str(output/'data'),HTTPS_PROXY=f'http://127.0.0.1:{server.server_address[1]}',HTTP_PROXY='',ALL_PROXY='',NO_PROXY='',SSL_CERT_FILE=str(certificate),PYINSTALLER_RESET_ENVIRONMENT='1')
        director=subprocess.Popen([str(args.exe.resolve())],env=env)
        trainer=subprocess.Popen([str(args.exe.resolve()),'--trainer'],env=env)
        start=time.monotonic()
        try:
            while time.monotonic()-start<1200:
                result={'scope':'two actual frozen GUIs with isolated APPDATA and local TLS fixture, no upstream',
                    'seconds':round(time.monotonic()-start,2),'requests':list(requests),'pids':[director.pid,trainer.pid],
                    'both_closed':director.poll() is not None and trainer.poll() is not None}
                (output/'requests.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                if result['both_closed'] or (output/'STOP').exists():break
                time.sleep(.5)
        finally:server.shutdown();thread.join(timeout=2)
print(json.dumps(result))
