"""Offline dataset inspection/export; explicit authenticated upload is added by the runtime CLI."""
import argparse
import csv
import json
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dataset_protocol as p
from dataset_export import export_csv,inspect_csv


from dataset_history import export_derived

def main():
    parser=argparse.ArgumentParser(description='训练数据离线检查与脱敏导出，不联网、不改变模型')
    sub=parser.add_subparsers(dest='command',required=True)
    inspect=sub.add_parser('inspect');inspect.add_argument('csv',type=Path)
    export=sub.add_parser('export-csv');export.add_argument('csv',type=Path);export.add_argument('destination',type=Path)
    derived=sub.add_parser('export-derived');derived.add_argument('folder',type=Path);derived.add_argument('destination',type=Path);derived.add_argument('--first-source',action='store_true')
    verify=sub.add_parser('verify');verify.add_argument('folder',type=Path)
    upload=sub.add_parser('upload-local',help='仅localhost隔离服务；不接受生产origin')
    upload.add_argument('folder',type=Path);upload.add_argument('--identity-config',type=Path,required=True)
    upload.add_argument('--state-root',type=Path,required=True);upload.add_argument('--steps',type=int,default=1)
    for command in (export,derived):command.add_argument('--run-id',default=None)
    args=parser.parse_args()
    if args.command=='upload-local':
        from urllib.parse import urlsplit
        from types import SimpleNamespace
        from upload_outbox import Binding
        from upload_worker import UploadIdentity
        from dataset_runtime import DatasetRuntime
        from dataset_transport import DatasetClient,DatasetFailure
        private=json.loads(p.read_bounded(args.identity_config,65536))
        origin=private['origin'];p.require(urlsplit(origin).hostname in ('localhost','127.0.0.1','::1'),'LOCALHOST_REQUIRED')
        identity=UploadIdentity(Binding(private['accountId'],origin,private['deviceId']),private['accessToken'])
        account=SimpleNamespace(upload_identity=lambda:identity,binding=lambda:identity.binding)
        trace=[]
        class LocalClient(DatasetClient):
            def call(self,identity,suffix='',method='GET',**kwargs):
                try:
                    status,value=super().call(identity,suffix,method,**kwargs)
                    trace.append(dict(method=method,path=suffix or '/',status=status));return status,value
                except DatasetFailure as error:
                    trace.append(dict(method=method,path=suffix or '/',status=error.status,code=error.code));raise
        runtime=DatasetRuntime(account,args.state_root,client=LocalClient(account.upload_identity),autostart=False)
        rid=runtime.store.import_bundle(args.folder,identity.binding)
        runtime.store.retry(rid,identity.binding)
        for _ in range(max(1,min(args.steps,10))):runtime.run_once()
        row=runtime.store.get(rid,identity.binding)
        print(json.dumps(dict(runId=rid,state=row['state'],error=row['error'],trace=trace,receipt=json.loads(row['receipt']) if row['receipt'] else None,governance=json.loads(row['governance']) if row['governance'] else None),indent=2))
        return 0 if row['state']=='received' else 2
    if args.command=='inspect':print(json.dumps(inspect_csv(args.csv),indent=2));return
    if args.command=='verify':data=p.validate_directory(args.folder)
    elif args.command=='export-csv':data=export_csv(args.csv,args.destination,args.run_id or str(uuid.uuid4()),producer='1.16.0')
    else:data=export_derived(args.folder,args.destination,args.run_id or str(uuid.uuid4()),args.first_source)
    print(json.dumps(dict(runId=data['runId'],clientDatasetId=data['clientDatasetId'],manifestSha256=data['manifestSha256'],requestHash=p.digest(p.canonical(data)),totals=data['manifest']['totals'],scope='local export/verification only; no upload or training'),indent=2))


if __name__=='__main__':raise SystemExit(main())
