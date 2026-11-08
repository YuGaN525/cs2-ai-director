"""Public client release decisions. No account, model or execution side effects."""
from dataclasses import asdict, dataclass
import re
import math
from urllib.parse import urlsplit
from uuid import UUID

CHANNELS = ('stable', 'beta')
MAX_RESPONSE_BYTES = 256 * 1024


class UpdateProtocolError(ValueError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}', value):
        raise UpdateProtocolError('unsupported_version')
    return tuple(map(int, value.split('.')))


def public_origin(value):
    if not isinstance(value, str) or len(value) > 2048 or not value.isascii():
        raise UpdateProtocolError('invalid_origin')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise UpdateProtocolError('invalid_origin') from exc
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or parts.path not in ('', '/') or '\\' in value
            or any(ord(c) <= 32 for c in value) or port == 0):
        raise UpdateProtocolError('invalid_origin')
    return 'https://' + parts.netloc.lower()


@dataclass(frozen=True)
class Release:
    id: str
    version: str
    channel: str
    artifact_hash: str
    artifact_bytes: int
    revision: int
    notes: str

    @classmethod
    def parse(cls, value, channel):
        if not isinstance(value, dict) or value.get('enabled') is not True or value.get('channel') != channel:
            raise UpdateProtocolError('invalid_release')
        try:
            release_id = value['id']
            if not isinstance(release_id, str) or str(UUID(release_id)) != release_id.lower():
                raise ValueError()
            version_tuple(value['version'])
            digest = value['artifactHash']
            if not isinstance(digest, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
                raise ValueError()
            size, revision = value['artifactBytes'], value['revision']
            if type(size) is not int or not 0 < size <= 2**63-1 or type(revision) is not int or not 0 <= revision <= 2**63-1:
                raise ValueError()
            notes = value['notes']
            if not isinstance(notes, str) or len(notes) > 2000 or '\x00' in notes:
                raise ValueError()
            # Never use a returned path to determine a network destination.
            if value.get('downloadPath') != f'/api/director/client-releases/{release_id}/file':
                raise ValueError()
            return cls(release_id.lower(), value['version'], channel, digest.lower(), size, revision, notes)
        except (ValueError, KeyError, TypeError) as exc:
            raise UpdateProtocolError('invalid_release') from exc

    def identity(self, origin):
        return (public_origin(origin), self.channel, self.id, self.version, self.artifact_hash, self.revision)


@dataclass(frozen=True)
class CheckResult:
    state: str
    release: Release | None = None

    def as_dict(self):
        return asdict(self)


def decide(envelope, current_version, channel, current_hash=None):
    current = version_tuple(current_version)
    if channel not in CHANNELS:
        raise UpdateProtocolError('invalid_channel')
    if not isinstance(envelope, dict) or envelope.get('success') is not True or envelope.get('code') != 'OK':
        raise UpdateProtocolError('invalid_envelope')
    rows = envelope.get('data')
    if not isinstance(rows, list) or len(rows) > 1:
        raise UpdateProtocolError('conflicting_releases')
    if not rows:
        return CheckResult('no_release')
    release = Release.parse(rows[0], channel)
    target = version_tuple(release.version)
    if target == current and current_hash is not None and current_hash.lower() != release.artifact_hash:
        return CheckResult('same_version_conflict', release)
    state = 'update_available' if target > current else 'ahead_of_channel' if target < current else 'up_to_date'
    return CheckResult(state, release)


def cached_result(value, origin, current_version, channel, current_hash=None):
    """Revalidate cached display data; malformed JSON structures force a recheck."""
    try:
        if not isinstance(value,dict):return None
        stamp=value['checked_at']
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<stamp<253402300800:return None
        release=value.get('release')
        if release is not None:
            row=dict(id=release['id'],version=release['version'],channel=release['channel'],
                artifactHash=release['artifact_hash'],artifactBytes=release['artifact_bytes'],
                revision=release['revision'],notes=release['notes'],enabled=True,
                downloadPath='/api/director/client-releases/'+release['id']+'/file')
            result=decide(dict(success=True,code='OK',data=[row]),current_version,channel,current_hash).as_dict()
        elif value['state'] in ('no_release','check_failed','service_unavailable'):
            result={'state':value['state']}
        else:return None
        return dict(result,origin=origin,checked_at=stamp)
    except (KeyError,ValueError,TypeError):
        return None
