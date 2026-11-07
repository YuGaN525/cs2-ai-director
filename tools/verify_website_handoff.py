"""Verify a user-provided website handoff without changing trust or accounts."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_trust import read_public_trust


def verify(folder, repo):
    folder, repo = Path(folder).resolve(), Path(repo).resolve()
    manifest = json.loads((folder/'SHA256SUMS.json').read_text(encoding='utf-8-sig'))
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('Empty or invalid handoff manifest')
    contracts = 0
    for name, digest in manifest.items():
        path = (folder/name).resolve()
        if not path.is_relative_to(folder):
            raise ValueError('Manifest path escapes handoff')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Handoff hash mismatch: '+name)
        if name.startswith('contracts/'):
            target = (repo/name).resolve()
            if not target.is_relative_to(repo) or target.read_bytes() != path.read_bytes():
                raise ValueError('Contract mismatch: '+name)
            contracts += 1
    if 'identity-server.json' not in manifest or 'website-status.json' not in manifest or not contracts:
        raise ValueError('Required handoff evidence missing')
    trust = read_public_trust(folder/'identity-server.json')
    normalized = json.dumps(trust, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'files_verified': len(manifest), 'contract_files_identical': contracts,
            'handoff_manifest_sha256': hashlib.sha256((folder/'SHA256SUMS.json').read_bytes()).hexdigest(),
            'trust_file_sha256': manifest['identity-server.json'],
            'trust_configuration_sha256': hashlib.sha256(normalized).hexdigest(),
            'server_origin': trust['server_origin'],
            'identity_key_ids': sorted(trust['trusted_public_keys']),
            'model_key_ids': sorted(trust['trusted_model_keys']),
            'scope': 'local integrity and compatibility; manifest is not a digital signature; no live server verification'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('handoff', type=Path)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.handoff, args.repo)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
