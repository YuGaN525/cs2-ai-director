import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import client_update_download as download
from client_update_manifest import canonical,SCHEMA,PRODUCT


def pe(marker=b'a'):
    data=bytearray(marker*512);data[:2]=b'MZ';struct.pack_into('<I',data,60,64);data[64:70]=b'PE\0\0\x64\x86'
    return bytes(data)


class Response(io.BytesIO):
    def __init__(self,data,status=200,headers=None):
        super().__init__(data);self.status=status;self.headers=headers or {}


class DownloadFixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.data=pe();self.key=Ed25519PrivateKey.generate()
        self.trust=dict(schema=SCHEMA,origin='https://example.org',keys={'update-test':base64.b64encode(self.key.public_key().public_bytes_raw()).decode()})
        self.trust_path=self.root/'trust.json';self.trust_path.write_text(json.dumps(self.trust))
        now=int(time.time())
        self.payload=dict(schema=SCHEMA,product=PRODUCT,origin=self.trust['origin'],channel='stable',
            platform='windows-x86_64',release_id='10000000-0000-0000-0000-000000000001',version='1.15.0',
            sha256=hashlib.sha256(self.data).hexdigest(),bytes=len(self.data),sequence=1,
            issued_at=now-1,expires_at=now+3600,minimum_updater=1,notes='test')
        self.requests=[];self.manifest_count=0;self.mode='normal'

    def signed(self,payload=None):
        value=copy.deepcopy(payload or self.payload)
        return dict(key_id='update-test',payload=value,signature=base64.b64encode(self.key.sign(canonical(value))).decode())

    def open(self,request,timeout):
        self.requests.append(request)
        if '/manifest?' in request.full_url:
            self.manifest_count+=1
            if self.mode=='revoked' and self.manifest_count>1:raise OSError('withdrawn')
            payload=copy.deepcopy(self.payload)
            if self.mode=='changed' and self.manifest_count>1:
                payload.update(sequence=2,sha256='b'*64)
            return Response(json.dumps(dict(success=True,code='OK',data=self.signed(payload))).encode())
        start=int(request.get_header('Range').split('=')[1][:-1]) if request.has_header('Range') else 0
        if self.mode=='range-ignored':start=0
        headers={'ETag':'"'+self.payload['sha256']+'"','Content-Length':str(len(self.data)-start)}
        if start:headers['Content-Range']=f'bytes {start}-{len(self.data)-1}/{len(self.data)}'
        body=self.data[start:]
        if self.mode=='wrong-range':headers['Content-Range']='bytes 1-4/5'
        if self.mode=='bad-hash':body=body[:-1]+b'z'
        if self.mode=='truncated':body=body[:-8]
        if self.mode=='overlong':body+=b'excess'
        if self.mode=='changed-etag':headers['ETag']='"other"'
        return Response(body,206 if start else 200,headers)

    def run_download(self):
        return download.download_core(self.root,self.trust_path,'stable','1.14.0',opener=self)

    def partial(self):
        folder=self.root/'downloads'/self.payload['sha256'];folder.mkdir(parents=True)
        (folder/'package.part').write_bytes(self.data[:100]);return folder

class DownloadTests(DownloadFixture,unittest.TestCase):
    def test_complete_signed_download_and_second_manifest_check(self):
        result=self.run_download()
        self.assertEqual(result['state'],'ready');self.assertEqual(self.manifest_count,2)
        self.assertEqual((Path(result['folder'])/'package.exe').read_bytes(),self.data)
        for request in self.requests:
            self.assertFalse(request.has_header('Authorization'));self.assertFalse(request.has_header('Cookie'))

    def test_resume_and_server_ignoring_range_restart(self):
        for mode in ('normal','range-ignored'):
            with self.subTest(mode=mode):
                self.mode=mode;folder=self.partial()
                result=self.run_download();self.assertEqual((folder/'package.exe').read_bytes(),self.data)
                request=next(r for r in reversed(self.requests) if r.full_url.endswith('/file'))
                self.assertEqual(request.get_header('Range'),'bytes=100-')
                (folder/'package.exe').unlink();(folder/'manifest.json').unlink();(folder/'download.lock').unlink();folder.rmdir()

    def test_integrity_etag_and_truncation_never_become_ready(self):
        for mode in ('bad-hash','truncated','overlong','changed-etag'):
            self.mode=mode
            with self.subTest(mode=mode),self.assertRaises((ValueError,OSError)):self.run_download()
            ready=self.root/'downloads'/self.payload['sha256']/'package.exe'
            self.assertFalse(ready.exists())
            ready.with_name('package.part').unlink(missing_ok=True)

    def test_wrong_content_range_rejected(self):
        self.partial();self.mode='wrong-range'
        with self.assertRaisesRegex(ValueError,'content_range'):self.run_download()

    def test_withdrawn_after_download_not_ready(self):
        self.mode='revoked'
        with self.assertRaises(OSError):self.run_download()
        self.assertFalse((self.root/'downloads'/self.payload['sha256']/'manifest.json').exists())

    def test_changed_release_after_download_rejected(self):
        self.mode='changed'
        with self.assertRaisesRegex(ValueError,'release_changed'):self.run_download()

    def test_replay_and_corrupt_security_ledger_are_not_reset(self):
        store=download.ManifestStore(self.root,self.trust,'stable');store.accept(self.signed())
        newer=copy.deepcopy(self.payload);newer['sequence']=2;store.accept(self.signed(newer))
        with self.assertRaises(ValueError):store.accept(self.signed())
        store.path.write_text('corrupt')
        with self.assertRaises(ValueError):store.accept(self.signed(newer))
        self.assertEqual(store.path.read_text(),'corrupt')

    def test_insufficient_space_and_wrong_architecture(self):
        with patch.object(download.shutil,'disk_usage',return_value=type('Usage',(),{'free':0})()):
            with self.assertRaisesRegex(OSError,'disk_space'):self.run_download()
        wrong=bytearray(self.data);wrong[68:70]=b'\x4c\x01';self.data=bytes(wrong)
        self.payload.update(sha256=hashlib.sha256(self.data).hexdigest(),sequence=2)
        with self.assertRaisesRegex(ValueError,'architecture'):self.run_download()


if __name__=='__main__':unittest.main()
