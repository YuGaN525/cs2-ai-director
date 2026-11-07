"""Trusted host-side launcher. The parser never receives the Docker socket."""
import os
from pathlib import Path
import re
import subprocess


class DockerParserSandbox:
    def __init__(self, image, cpus=1.0):
        if os.name == 'nt' or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise ValueError('import_fixed_linux_image_required')
        if not 0.1 <= float(cpus) <= 2:
            raise ValueError('import_cpu_budget')
        self.image, self.cpus = image, str(float(cpus))
        self.uid = os.getuid() if hasattr(os, 'getuid') else 10001
        self.gid = os.getgid() if hasattr(os, 'getgid') else 10001
        if self.uid <= 0:
            raise ValueError('import_non_root_host_required')

    @staticmethod
    def name(folder):
        import hashlib
        return 'director-import-' + hashlib.sha256(str(Path(folder).resolve()).encode()).hexdigest()[:24]

    @staticmethod
    def owner(root):
        import hashlib
        return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()

    def stop_owned(self, root):
        # Recover only containers from this private worker root after a crash.
        result = subprocess.run(['docker', 'ps', '-aq', '--filter',
                                 'label=org.yuzibei.import-root=' + self.owner(root)],
                                check=True, capture_output=True, text=True, timeout=15)
        for ident in result.stdout.split():
            if not re.fullmatch(r'[a-f0-9]{12,64}', ident):
                raise RuntimeError('import_container_identity')
            subprocess.run(['docker', 'rm', '-f', ident], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)

    def command(self, folder, spec, probe=False):
        folder = Path(folder).resolve(strict=True)
        if folder.is_symlink() or ',' in str(folder):
            raise ValueError('import_scratch_path')
        memory = int(spec['maxMemoryMb'])
        if not 256 <= memory <= 4096:
            raise ValueError('import_memory_budget')
        # No production volume, secret directory, network or Docker socket is
        # mounted in the untrusted parser. Only this job's scratch is writable.
        command = ['docker', 'run', '--rm', '--name', self.name(folder),
                   '--label', 'org.yuzibei.import-root=' + self.owner(folder.parent),
                   '--network', 'none', '--read-only', '--user', f'{self.uid}:{self.gid}',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--cpus', self.cpus, '--memory', str(memory) + 'm',
                   '--memory-swap', str(memory) + 'm', '--pids-limit', '128',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m',
                   '--env', 'RAYON_NUM_THREADS=1', '--env', 'POLARS_MAX_THREADS=1',
                   '--env', 'MALLOC_CONF=background_thread:false,narenas:2',
                   '--mount', 'type=bind,source=' + str(folder) + ',target=/work',
                   '--entrypoint', 'python', self.image,
                   '/opt/director/demo_import_parser.py', '--folder', '/work',
                   '--seven-zip', '/opt/director/7zz']
        return command + (['--probe'] if probe else [])

    def prepare(self, folder):
        # Same non-root UID permits private readback/cleanup, including files
        # created with umask 077. No world-writable scratch or host secrets.
        Path(folder).chmod(0o700)
        for name in ('input.raw', 'spec.json'):
            path = Path(folder) / name
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise ValueError('import_input_file')
                path.chmod(0o400)

    def stop(self, folder):
        # Killing the docker CLI alone would leave the parser container alive.
        subprocess.run(['docker', 'rm', '-f', self.name(folder)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15)

    def probe(self, root):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='sandbox-probe-', dir=root) as temp:
            folder = Path(temp)
            self.prepare(folder)
            try:
                result = subprocess.run(self.command(folder, {'maxMemoryMb': 256}, probe=True),
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
                if result.returncode or result.stdout.strip() != b'director-import-sandbox-ready':
                    raise RuntimeError('import_sandbox_unavailable')
            finally:
                self.stop(folder)
