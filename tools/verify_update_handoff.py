"""Read-only cross-language update contract and signed handoff validation."""
import argparse,hashlib,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from client_update_manifest import read_update_trust,verify

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--website-contract',type=Path,required=True)
parser.add_argument('--evidence',type=Path,required=True)
parser.add_argument('--report',type=Path,required=True)
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]/'contracts/director-client-update-v1'
hashes=json.loads((root/'manifest.json').read_text())
for name,digest in hashes.items():
    for folder in (root,args.website_contract):
        assert hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest,name
trust=read_update_trust(args.evidence/'application-public-trust.json')
signed=json.loads((args.evidence/'signed-manifest.json').read_text(encoding='utf-8-sig'))
payload=verify(signed,trust,channel='stable',platform='windows-x86_64',now=int(time.time()))
result={'scope':'website .NET signature verified by program Python; original golden files byte identical; offline, no real download or installation',
        'passed':True,'files_checked':hashes,'payload_digest':payload['payload_digest'],
        'origin':payload['origin'],'version':payload['version'],'bytes':payload['bytes'],
        'production_trust_delivered':False,'real_executable':False}
args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
