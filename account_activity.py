"""One signed website activity per explicit start; no retry queue or heartbeat."""
from datetime import datetime, timezone
import uuid

ACTIONS = frozenset(('director.start', 'trainer.start'))
PURPOSE = 'yuzi-director-activity-v1'


def utc_timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid_activity_time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError('invalid_activity_time')
    return parsed.timestamp()


def validate_challenge(value, action, now):
    """Never sign arbitrary server text or reuse other device proof purposes."""
    if not isinstance(value, dict) or value.get('action') != action:
        raise ValueError('invalid_activity_challenge')
    challenge = value['challengeId']
    if not isinstance(challenge, str) or str(uuid.UUID(challenge)) != challenge:
        raise ValueError('invalid_activity_challenge')
    nonce = value['nonce']
    if (not isinstance(nonce, str) or not 16 <= len(nonce) <= 256
            or any(not ('!' <= char <= '~') or char == ':' for char in nonce)):
        raise ValueError('invalid_activity_challenge')
    payload = challenge + ':' + nonce + ':' + action
    if value.get('signingPayload') != payload:
        raise ValueError('invalid_activity_challenge')
    if not now < utc_timestamp(value['expiresAt']) <= now + 125:
        raise ValueError('expired_activity_challenge')
    return challenge, payload


def validate_receipt(value):
    if not isinstance(value, dict) or value.get('recorded') is not True:
        raise ValueError('invalid_activity_receipt')
    utc_timestamp(value['recordedAt'])
    return {'recorded': True, 'recordedAt': value['recordedAt']}
