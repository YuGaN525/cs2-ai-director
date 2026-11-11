"""Device-authenticated diagnostics transport. No redirects or response-body logs."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from diagnostics_privacy import MAX_LOG_BYTES, SUMMARIES, sanitize_log
from diagnostics_store import binding_key
from upload_worker import NoRedirect


class DiagnosticFailure(Exception):
    def __init__(self, status=0, retry_after=0):
        self.status, self.retry_after = status, retry_after
        super().__init__('diagnostic_http_'+str(status))


def validate_payload(value):
    if set(value) != {'requestId', 'kind', 'appVersion', 'occurredAt', 'summary', 'log', 'buildCommit', 'osVersion'}:
        raise DiagnosticFailure(400)
    try:
        uuid.UUID(value['requestId'])
        if value['kind'] not in SUMMARIES or value['summary'] != SUMMARIES[value['kind']]:
            raise ValueError()
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?', value['appVersion']) or len(value['appVersion']) > 48:
            raise ValueError()
        when = datetime.fromisoformat(value['occurredAt'].replace('Z', '+00:00'))
        if when.utcoffset() != timezone.utc.utcoffset(when):
            raise ValueError()
        if value['buildCommit'] is not None and not re.fullmatch('[0-9a-f]{7,64}', value['buildCommit']):
            raise ValueError()
        if value['osVersion'] is not None and not re.fullmatch(r'Windows [0-9.]{1,40}', value['osVersion']):
            raise ValueError()
        if not value['log'] or value['log'] != sanitize_log(value['log']) or len(value['log'].encode('utf-8')) > MAX_LOG_BYTES:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DiagnosticFailure(400) from None


class DiagnosticClient:
    def __init__(self, identity_provider, *, opener=None, permitted=lambda: True, timeout=5):
        self.identity_provider, self.permitted, self.timeout = identity_provider, permitted, timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def _check(self, identity):
        current = self.identity_provider()
        if not self.permitted() or current is None or current.binding != identity.binding or current.access_token != identity.access_token:
            raise DiagnosticFailure(401)

    def _request(self, identity, suffix, data=None):
        self._check(identity)
        request = urllib.request.Request(identity.binding.origin+'/api/director/diagnostics'+suffix,
            data=data, method='POST' if data is not None else 'GET',
            headers={'Authorization': 'Bearer '+identity.access_token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(65537)
                if len(raw) > 65536: raise ValueError()
                body = json.loads(raw)
                if body.get('success') is not True or not isinstance(body.get('data'), dict): raise ValueError()
                return body['data']
        except urllib.error.HTTPError as error:
            retry = 0
            header = str(error.headers.get('Retry-After', ''))[:100]
            try:
                retry = max(0, min(30*86400, int(header))) if header.isdigit() else max(0, min(30*86400, parsedate_to_datetime(header).timestamp()-time.time()))
            except (ValueError, TypeError, OverflowError): pass
            error.close()
            raise DiagnosticFailure(error.code, retry) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise DiagnosticFailure() from None
        except (ValueError, TypeError, KeyError):
            raise DiagnosticFailure(502) from None

    def upload(self, identity, row):
        if binding_key(identity.binding) != row['owner']:
            raise DiagnosticFailure(403)
        value = json.loads(row['payload'])
        validate_payload(value)
        cap = self._request(identity, '/capabilities')
        if cap.get('enabled') is not True: raise DiagnosticFailure(503)
        if cap.get('schemaVersion') != 1 or cap.get('protocol') != 'director-diagnostics-v1': raise DiagnosticFailure(503)
        if len(value['log'].encode()) > cap.get('maxLogBytes', 0): raise DiagnosticFailure(413)
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(data) > min(1048576, cap.get('maxRequestBytes', 0)): raise DiagnosticFailure(413)
        receipt = self._request(identity, '', data)
        try:
            uuid.UUID(receipt['id'])
            if receipt['status'] != 'received': raise ValueError()
            received = datetime.fromisoformat(receipt['receivedAt'].replace('Z', '+00:00'))
            expires = datetime.fromisoformat(receipt['expiresAt'].replace('Z', '+00:00'))
            if received.utcoffset() != timezone.utc.utcoffset(received) or expires.utcoffset() != timezone.utc.utcoffset(expires): raise ValueError()
            if not 0 < (expires-received).total_seconds() <= 30*86400+1: raise ValueError()
            return {key: receipt[key] for key in ('id', 'status', 'receivedAt', 'expiresAt')}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DiagnosticFailure(502) from None
