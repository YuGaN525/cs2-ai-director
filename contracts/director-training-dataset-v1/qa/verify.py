"""Read-only golden package validation. --refresh-manifest only records file hashes."""
import argparse
import json
from pathlib import Path
import sys
from contract import ROOT,Invalid,canonical,digest,loads,validate_package


def local(name):
    path=(ROOT/name).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():raise ValueError('Fixture outside contract/missing')
    return path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--refresh-manifest',action='store_true');args=parser.parse_args()
    if args.refresh_manifest:
        paths=sorted(p for p in ROOT.rglob('*') if p.is_file() and p.name!='manifest.json' and '__pycache__' not in p.parts)
        (ROOT/'manifest.json').write_text(json.dumps({p.relative_to(ROOT).as_posix():digest(p.read_bytes()) for p in paths},indent=2)+'\n',encoding='ascii')
    hashes=json.loads((ROOT/'manifest.json').read_text())
    for name,expected in hashes.items():assert digest(local(name).read_bytes())==expected,name
    actual={p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and p.name!='manifest.json' and '__pycache__' not in p.parts}
    assert actual==set(hashes),'Unlisted files'
    cases=json.loads((ROOT/'fixtures.json').read_text());results={};valid=0;rejected=0
    for c in cases:
        try:
            result=validate_package(local(c['initialize']).read_bytes(),{i:local(p).read_bytes() for i,p in enumerate(c['files'])})
        except Invalid as error:
            assert not c['valid'] and str(error)==c['error'],(c['id'],c['error'],str(error));rejected+=1
        else:
            assert c['valid'],c['id']
            assert all(result[k]==c[k] for k in ('manifestSha256','requestHash','initialGovernanceStatus')),c['id']
            results[c['id']]=result;valid+=1
    assert results['minimal']['requestHash']==results['same-request-reordered']['requestHash']
    assert results['minimal']['manifestSha256']==results['same-content-new-command']['manifestSha256']
    assert results['minimal']['requestHash']!=results['same-content-new-command']['requestHash']
    assert results['minimal']['requestHash']!=results['same-id-different-request']['requestHash']
    vectors=json.loads((ROOT/'canonical-vectors.json').read_text())
    for c in vectors:
        try:
            value=canonical(loads(local(c['path']).read_bytes()))
        except Invalid as error:assert not c['valid'] and str(error)==c['error'],c['id']
        else:assert c['valid'] and value.decode()==c['canonical'] and digest(value)==c['sha256'],c['id']
    for name in ('receipt.example.json','duplicate-receipt.example.json'):
        r=json.loads((ROOT/name).read_text());assert r['receivedRows']==r['acceptedRows']+r['duplicateRows'] and r['initialGovernanceStatus']=='pending-review'
    capabilities=json.loads((ROOT/'capabilities.example.json').read_text());assert capabilities['enabled'] is False
    # Schema meta-validation is optional locally; the web owner independently validates with .NET/Node.
    schema_qa='not-run-jsonschema-unavailable'
    try:
        from jsonschema import Draft202012Validator
    except ImportError:pass
    else:
        for path in ROOT.glob('*.schema.json'):Draft202012Validator.check_schema(json.loads(path.read_text()))
        for name,schema in [('capabilities.example.json','Capabilities'),('receipt.example.json','Receipt'),('duplicate-receipt.example.json','Receipt'),('transport.example.json','TransportState'),('governance.example.json','GovernanceState')]:Draft202012Validator(json.loads((ROOT/(schema+'.schema.json')).read_text())).validate(json.loads((ROOT/name).read_text()))
        validator=Draft202012Validator(json.loads((ROOT/'Initialize.schema.json').read_text()))
        for c in cases:
            if c['valid']:validator.validate(loads(local(c['initialize']).read_bytes()))
        schema_qa='passed'
    print(json.dumps(dict(scope='offline synthetic contract; no endpoint/queue/UI implementation',manifestSha256=digest((ROOT/'manifest.json').read_bytes()),filesChecked=len(hashes),validPackages=valid,rejectedPackages=rejected,canonicalVectors=len(vectors),hashIdempotencyRelations='passed',receiptArithmetic='passed',schemaMetaValidation=schema_qa),indent=2))


if __name__=='__main__':main()
