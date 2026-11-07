"""Fenced Demo import transport. Parser receives neither credentials nor network.

Linux requires bubblewrap or the fixed-image host Docker launcher; unsupported sandboxes
fail before claiming. The explicit local Windows test option is never production.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from cloud_worker import stop_child
from device_identity import canonical, server_origin
from upload_worker import NoRedirect
from worker_limits import attach


class ImportClient:
    def __init__(self, origin, key, allow_internal=False):
        self.origin = origin if allow_internal and origin in ('http://api:8080', 'http://127.0.0.1:8002') else server_origin(origin)
        self.key = key
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, route, data=None, *, method=None, lease=None):
        headers = {'X-Director-Import-Key': self.key}
        if isinstance(data, dict):
            data = canonical(data)
            headers['Content-Type'] = 'application/json'
        else:
            headers['Content-Type'] = 'application/octet-stream'
        if lease is not None:
            headers['X-Director-Import-Lease'] = lease['leaseToken']
        return urllib.request.Request(self.origin + '/api/internal/director-imports/' + route,
                                      data=data, headers=headers, method=method)

    def call(self, route, data=None, *, method=None, lease=None):
        with self.opener.open(self.request(route, data, method=method, lease=lease), timeout=60) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('import_response_limit')
            return json.loads(raw)['data']

    def download(self, lease, target, keepalive):
        digest, total = hashlib.sha256(), 0
        with target.open('xb') as output:
            for index, part in enumerate(lease['parts']):
                if part['index'] != index or not 0 < part['bytes'] <= 8 * 1024 * 1024:
                    raise ValueError('import_part_manifest')
                keepalive()
                route = f"files/{lease['fileId']}/parts/{index}?fence={lease['fence']}"
                part_hash, size = hashlib.sha256(), 0
                with self.opener.open(self.request(route, lease=lease), timeout=60) as response:
                    while True:
                        keepalive()
                        block = response.read(65536)
                        if not block:
                            break
                        size += len(block)
                        total += len(block)
                        if size > part['bytes'] or total > lease['bytes']:
                            raise ValueError('import_length')
                        part_hash.update(block)
                        digest.update(block)
                        output.write(block)
                if size != part['bytes'] or part_hash.hexdigest() != part['sha256']:
                    raise ValueError('import_part_hash')
            output.flush()
            os.fsync(output.fileno())
        if total != lease['bytes'] or digest.hexdigest() != lease['sha256']:
            raise ValueError('import_file_hash')


def parser_command(folder, seven_zip, development=False):
    script = Path(__file__).with_name('demo_import_parser.py').resolve()
    if os.name == 'nt':
        if not development:
            raise RuntimeError('production_import_requires_linux_sandbox')
        return [sys.executable, str(script), '--folder', str(folder), '--seven-zip', str(seven_zip)]
    executable = shutil.which('bwrap')
    if not executable:
        raise RuntimeError('import_sandbox_unavailable')
    code = script.parent
    # Only runtime libraries, code and this one scratch directory are visible.
    # /run/secrets, parent config, host sockets and other jobs are not mounted.
    command = [executable, '--unshare-all', '--die-with-parent', '--new-session', '--clearenv']
    for path in ('/usr', '/lib', '/lib64', '/bin'):
        if Path(path).exists():
            command += ['--ro-bind', path, path]
    command += ['--dir', '/etc']
    if Path('/etc/ld.so.cache').exists():
        command += ['--ro-bind', '/etc/ld.so.cache', '/etc/ld.so.cache']
    command += ['--ro-bind', str(code), '/app', '--ro-bind', str(seven_zip), '/sevenzip',
                '--bind', str(folder), '/work', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                '--chdir', '/work', '--setenv', 'PATH', '/usr/local/bin:/usr/bin:/bin',
                '--setenv', 'OMP_NUM_THREADS', '1', '--setenv', 'OPENBLAS_NUM_THREADS', '1',
                '--setenv', 'RAYON_NUM_THREADS', '1', '--setenv', 'POLARS_MAX_THREADS', '1',
                '--setenv', 'MALLOC_CONF', 'background_thread:false,narenas:2',
                '--setenv', 'PYTHONUNBUFFERED', '1', sys.executable, '/app/demo_import_parser.py',
                '--folder', '/work', '--seven-zip', '/sevenzip']
    return command


def verify_sandbox(root, seven_zip, development=False):
    command = parser_command(root, seven_zip, development) + ['--probe']
    env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'PATH') if key in os.environ}
    env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, env=env,
                                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('import_sandbox_unavailable') from error
    if result.returncode or result.stdout.strip() != b'director-import-sandbox-ready':
        raise RuntimeError('import_sandbox_unavailable')


def safe_result_file(folder, path):
    folder, path = Path(folder).resolve(), Path(path)
    if '..' in path.parts:
        raise ValueError('import_result_path')
    for node in (path, *path.parents):
        if node == folder:
            break
        if node.is_symlink():
            raise ValueError('import_result_link')
    path = path.resolve()
    if not path.is_relative_to(folder):
        raise ValueError('import_result_path')
    if not path.is_file():
        raise ValueError('import_result_missing')
    return path


def run_once(client, root, worker, seven_zip, *, development=False, sandbox=None):
    lease = client.call('claim', {'workerId': worker})
    if lease is None:
        return False
    ident = str(uuid.UUID(lease['fileId']))
    fence = int(lease['fence'])
    if fence < 1 or lease['bytes'] < 16 or lease['bytes'] >= 50_000_000_000:
        raise ValueError('import_lease')
    root = Path(root).resolve(strict=True)
    folder = root / (ident + '-' + str(fence) + '-' + uuid.uuid4().hex)
    folder.mkdir(exist_ok=False)
    started, next_beat = time.monotonic(), 0.0
    def keepalive():
        nonlocal next_beat
        if time.monotonic() - started > lease['maxSeconds']:
            raise TimeoutError('import_timeout')
        if time.monotonic() >= next_beat:
            if not client.call('files/' + ident + '/heartbeat',
                               dict(fence=fence, leaseToken=lease['leaseToken'], progress=0)):
                raise RuntimeError('import_lease_expired')
            next_beat = time.monotonic() + 15
    try:
        keepalive()
        if shutil.disk_usage(folder).free < lease['bytes'] + lease['maxExpandedBytes'] + lease['maxDerivedBytes'] + 128 * 1024 * 1024:
            raise ValueError('import_scratch_capacity')
        client.download(lease, folder / 'input.raw', keepalive)
        spec = {key: lease[key] for key in ('bytes', 'sha256', 'kind', 'maxEntries', 'maxExpandedBytes', 'maxDerivedBytes', 'maxSeconds', 'maxMemoryMb')}
        (folder / 'spec.json').write_bytes(canonical(spec))
        if sandbox is not None:
            sandbox.prepare(folder)
        command = sandbox.command(folder, spec) if sandbox is not None else parser_command(folder, seven_zip, development)
        env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'PATH') if key in os.environ}
        env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', TEMP=str(folder), TMP=str(folder))
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=env, start_new_session=os.name != 'nt',
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        release = attach(child, lease['maxMemoryMb'])
        try:
            while child.poll() is None:
                keepalive()
                time.sleep(.25)
            if child.returncode != 0:
                raise ValueError('import_parser_process_failed')
        finally:
            stop_child(child)
            if sandbox is not None:
                sandbox.stop(folder)
            release()
        receipt_path = safe_result_file(folder, folder / 'receipt.json')
        if receipt_path.stat().st_size > 7 * 1024 * 1024:
            raise ValueError('import_receipt_limit')
        result = json.loads(receipt_path.read_text('utf-8'))
        sent = set()
        for entry in result['entries']:
            for part in entry['parts']:
                digest = part['sha256']
                if not re.fullmatch('[a-f0-9]{64}', digest):
                    raise ValueError('result_hash')
                if digest in sent:
                    continue
                keepalive()
                path = safe_result_file(folder, folder / 'results' / digest)
                if path.stat().st_size != part['bytes'] or part['bytes'] > 8 * 1024 * 1024:
                    raise ValueError('result_length')
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('result_hash')
                client.call(f'files/{ident}/results/{digest}?fence={fence}', data, method='PUT', lease=lease)
                sent.add(digest)
        keepalive()
        receipt = dict(result, fence=fence, leaseToken=lease['leaseToken'])
        # One exact retry is safe: receipt identity and payload are server-checked.
        for attempt in range(2):
            try:
                client.call(f'files/{ident}/finish', receipt)
                break
            except (TimeoutError, urllib.error.URLError):
                if attempt:
                    raise
        return True
    except Exception as error:
        if isinstance(error, urllib.error.HTTPError) and error.code in (401, 403, 409):
            error.close()
            return True
        # Unknown completion may already have succeeded: a mismatched failed
        # receipt is rejected by the server and never replaces committed results.
        try:
            code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'import_[a-z_]{1,100}', str(error)) else 'import_' + type(error).__name__.lower()
            client.call(f'files/{ident}/finish', dict(fence=fence, leaseToken=lease['leaseToken'], entries=[],
                                                    errorCode=code))
        except Exception:
            pass
        return True
    finally:
        resolved = folder.resolve()
        if resolved.parent != root or not resolved.name.startswith(ident + '-') or folder.is_symlink():
            raise RuntimeError('unsafe_scratch_cleanup')
        shutil.rmtree(resolved)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--local-windows-test', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text('utf-8'))
    key = Path(config['worker_key_file']).read_text('utf-8').strip()
    if len(key) < 32:
        raise ValueError('worker_key_length')
    seven_zip = Path(config['seven_zip']).resolve(strict=True)
    with seven_zip.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != config['seven_zip_sha256']:
            raise ValueError('seven_zip_digest')
    root = Path(config['work_root']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Execute before claim: unavailable user/network namespaces fail closed.
    sandbox = None
    backend = config.get('parser_backend', 'bubblewrap')
    if backend == 'docker-host':
        from import_docker_sandbox import DockerParserSandbox
        sandbox = DockerParserSandbox(config['parser_image'], config.get('parser_cpus', 1))
        # This service is single-instance per private work_root.
        sandbox.stop_owned(root)
        sandbox.probe(root)
    elif backend == 'bubblewrap':
        verify_sandbox(root, seven_zip, args.local_windows_test)
    else:
        raise ValueError('import_sandbox_backend')
    client = ImportClient(config['server_origin'], key, config.get('allow_internal_api_http', False))
    def terminate(signum, frame):
        raise SystemExit(0)  # unwind run_once and stop the actual parser container
    signal.signal(signal.SIGTERM, terminate)
    while True:
        run_once(client, root, config.get('worker_id', 'director-import'), seven_zip,
                 development=args.local_windows_test, sandbox=sandbox)
        if args.once:
            break
        time.sleep(3)


if __name__ == '__main__':
    main()
