"""Only public, pinned trust information can enter a client release."""
import base64,json,re,hashlib,sys
from pathlib import Path
from device_identity import server_origin


def read_public_trust(path):
    path=Path(path)
    if path.stat().st_size>16384:raise ValueError('Public trust configuration too large')
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict) or set(value)!={'server_origin','trusted_public_keys','trusted_model_keys'} or not isinstance(value['server_origin'],str):raise ValueError('Only origin and two public-key maps are allowed')
    value['server_origin']=server_origin(value['server_origin'])
    if not value['server_origin'].isascii():raise ValueError('Origin must be ASCII')
    for name in ('trusted_public_keys','trusted_model_keys'):
        keys=value[name]
        if not isinstance(keys,dict) or not 1<=len(keys)<=8:raise ValueError('A release needs pinned public keys')
        for key,raw in keys.items():
            if not isinstance(raw,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',key) or len(base64.b64decode(raw,validate=True))!=32:raise ValueError('Invalid public Ed25519 key')
    if set(value['trusted_public_keys'].values())&set(value['trusted_model_keys'].values()):raise ValueError('Identity/model signing keys must be separated')
    return value


def runtime_trust_path():
    """Use the same pinned source for account setup and diagnostic reports."""
    if not getattr(sys, 'frozen', False):
        return Path(__file__).parent/'identity-server.json'
    external = Path(sys.executable).parent/'identity-server.json'
    # An invalid explicit override must fail closed, not fall back silently.
    return external if external.exists() else Path(sys._MEIPASS)/'identity-server.json'


def public_trust_status():
    """Allowlisted local diagnostics; no account data or private paths."""
    path = runtime_trust_path()
    if not path.exists():
        return {'state': 'missing'}
    try:
        value = read_public_trust(path)
    except (ValueError, OSError):
        return {'state': 'invalid'}
    normalized = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'state': 'configured', 'server_origin': value['server_origin'],
            'configuration_sha256': hashlib.sha256(normalized).hexdigest(),
            'identity_key_ids': sorted(value['trusted_public_keys']),
            'model_key_ids': sorted(value['trusted_model_keys']),
            'formal_model_approval_verified': False}
