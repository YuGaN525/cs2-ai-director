"""Verification of application updates, separate from identity/model signing."""
import base64
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from client_update import CHANNELS, public_origin, version_tuple, UpdateProtocolError

PRODUCT = 'yuzi-cs2-director'
SCHEMA = 'director-client-update-v1'
FIELDS = {'schema','product','origin','channel','release_id','version','platform',
          'sha256','bytes','sequence','issued_at','expires_at','minimum_updater','notes'}


def canonical(payload):
    return json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')


def read_update_trust(path):
    path=Path(path)
    if path.stat().st_size>16384:raise UpdateProtocolError('trust_too_large')
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict) or set(value)!={'schema','origin','keys'} or value['schema']!=SCHEMA:
        raise UpdateProtocolError('invalid_update_trust')
    value['origin']=public_origin(value['origin'])
    keys=value['keys']
    if not isinstance(keys,dict) or not 1<=len(keys)<=8:raise UpdateProtocolError('invalid_update_keys')
    for key,encoded in keys.items():
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',key) or not isinstance(encoded,str) or len(base64.b64decode(encoded,validate=True))!=32:
            raise UpdateProtocolError('invalid_update_key')
    return value


def verify(envelope,trust,*,channel,platform,now,highest_sequence=0,previous_digest=None,updater_version=1):
    """Return validated bytes identity; callers must persist sequence before applying."""
    if not isinstance(envelope,dict) or set(envelope)!={'key_id','payload','signature'}:
        raise UpdateProtocolError('invalid_signed_manifest')
    if channel not in CHANNELS or trust.get('schema')!=SCHEMA:
        raise UpdateProtocolError('invalid_update_context')
    payload=envelope['payload']
    if not isinstance(payload,dict) or set(payload)!=FIELDS:
        raise UpdateProtocolError('invalid_update_payload')
    try:
        raw=canonical(payload)
        if len(raw)>32768:raise ValueError('large_payload')
        key=base64.b64decode(trust['keys'][envelope['key_id']],validate=True)
        signature=base64.b64decode(envelope['signature'],validate=True)
        Ed25519PublicKey.from_public_bytes(key).verify(signature,raw)
    except (ValueError,TypeError,KeyError,InvalidSignature) as error:
        raise UpdateProtocolError('invalid_update_signature') from error
    if (payload['schema']!=SCHEMA or payload['product']!=PRODUCT
            or payload['origin']!=public_origin(trust['origin'])
            or payload['channel']!=channel or payload['platform']!=platform):
        raise UpdateProtocolError('wrong_update_target')
    try:
        if str(UUID(payload['release_id']))!=payload['release_id']:raise ValueError()
        version_tuple(payload['version'])
        for name in ('bytes','sequence','issued_at','expires_at','minimum_updater'):
            if type(payload[name]) is not int or not 0<payload[name]<=2**63-1:raise ValueError()
        if payload['minimum_updater']>updater_version:raise ValueError()
        if not isinstance(payload['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',payload['sha256']):raise ValueError()
        if not isinstance(payload['notes'],str) or len(payload['notes'])>2000:raise ValueError()
        if not payload['issued_at']<=now<payload['expires_at']:raise ValueError()
        if payload['expires_at']-payload['issued_at']>7*86400:raise ValueError()
    except (ValueError,TypeError,AttributeError) as error:
        raise UpdateProtocolError('invalid_update_constraints') from error
    digest=hashlib.sha256(raw).hexdigest()
    if payload['sequence']<highest_sequence or (payload['sequence']==highest_sequence and previous_digest!=digest):
        raise UpdateProtocolError('replayed_or_conflicting_update')
    return dict(payload,payload_digest=digest,key_id=envelope['key_id'])
