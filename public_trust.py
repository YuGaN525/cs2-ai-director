"""Only public, pinned trust information can enter a client release."""
import base64,json,re
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
