"""Anonymous HTTPS GET in a disposable process, including a hard DNS deadline."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from client_update import CHANNELS, MAX_RESPONSE_BYTES, public_origin


def executable_hash():
    """Called only in a worker or explicit CLI; source has no release artifact."""
    if not getattr(sys,'frozen',False):return None
    with open(sys.executable,'rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def report(path, channel):
    """Explicit user-requested network check, never part of offline --check."""
    import threading
    from client_update import decide
    from client_version import VERSION
    from public_trust import read_public_trust, runtime_trust_path
    try:
        origin=read_public_trust(runtime_trust_path())['server_origin']
        response=check(origin,channel,threading.Event())
        value=decide(response['envelope'],VERSION,channel,executable_hash()).as_dict() if 'envelope' in response else {'state':'check_failed','error':response['error']}
    except FileNotFoundError:value={'state':'source_unconfigured'}
    except (ValueError,OSError,RuntimeError):value={'state':'check_failed'}
    value.update(client_version=VERSION,channel=channel,checked_at=time.time(),frozen=bool(getattr(sys,'frozen',False)))
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if value['state'] in ('update_available','up_to_date','ahead_of_channel','no_release') else 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_delay(value, now=None):
    """Retry-After supports seconds and HTTP dates, always bounded to six hours."""
    if not isinstance(value,str) or len(value)>128:return None
    try:
        delay = int(value) if value.isascii() and value.isdigit() else parsedate_to_datetime(value).timestamp()-(time.time() if now is None else now)
        return min(21600,max(1,int(delay)))
    except (ValueError,TypeError,OverflowError):
        return None


def fetch_once(origin, channel):
    origin = public_origin(origin)
    if channel not in CHANNELS:
        raise ValueError('Invalid channel')
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(origin+'/api/director/client-releases?channel='+channel,
                                    headers={'Accept':'application/json', 'User-Agent':'YuziDirector-Update/1'})
    try:
        with opener.open(request, timeout=3) as response:
            if response.status != 200:
                return {'error':'http_error'}
            data = response.read(MAX_RESPONSE_BYTES+1)
            if len(data) > MAX_RESPONSE_BYTES:
                return {'error':'response_too_large'}
            return {'envelope':json.loads(data.decode('utf-8-sig'))}
    except urllib.error.HTTPError as error:
        value={'error':'service_unavailable' if error.code == 503 else 'rate_limited' if error.code == 429 else 'http_error'}
        retry=error.headers.get('Retry-After','') if error.headers else ''
        if error.code in (429,503) and (delay:=retry_delay(retry)) is not None:
            value['retry_after']=delay
        error.close()
        return value
    except (OSError, ValueError, urllib.error.URLError):
        return {'error':'network_or_protocol_error'}


def check(origin, channel, cancel, timeout=10):
    origin = public_origin(origin)
    if channel not in CHANNELS or not 0 < timeout <= 10:
        raise ValueError('Invalid update request')
    if cancel.is_set():
        return {'error':'cancelled'}
    with tempfile.TemporaryDirectory(prefix='director-update-') as temporary:
        output = Path(temporary)/'result.json'
        command = ([sys.executable,'--client-update-fetch'] if getattr(sys,'frozen',False)
                   else [sys.executable,str(Path(__file__).resolve()),'--fetch'])
        command += [origin,channel,str(output)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        started = time.monotonic()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            while process.poll() is None:
                if cancel.wait(.05):
                    return {'error':'cancelled'}
                if time.monotonic()-started >= timeout:
                    return {'error':'timeout'}
            if process.returncode or not output.exists() or output.stat().st_size > MAX_RESPONSE_BYTES*6+1024:
                return {'error':'worker_failed'}
            return json.loads(output.read_text(encoding='utf-8'))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def worker_main(arguments):
    if len(arguments) != 3:
        return 2
    origin, channel, output = arguments
    value = fetch_once(origin, channel)
    Path(output).write_text(json.dumps(value), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(worker_main(sys.argv[2:]) if sys.argv[1:2] == ['--fetch'] else 2)
