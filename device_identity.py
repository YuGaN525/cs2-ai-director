"""Local device credentials and signed 168-hour lease verification.

No token issuer or test signing keys are bundled here. Network identity adapters are
intentionally separate until the server contract is resumed.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption

MAX_OFFLINE_SECONDS = 604800
PURPOSE = b'yuzi-director-offline-lease-v1\x00'


class IdentityError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def b64(value):
    return base64.b64encode(value).decode('ascii')


def unb64(value):
    return base64.b64decode(value, validate=True)


def server_origin(url):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise IdentityError('站点必须为不含凭据/路径/查询参数的origin')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise IdentityError('正式站点必须使用HTTPS')
    if not parsed.hostname:
        raise IdentityError('缺少站点')
    return parsed.scheme + '://' + parsed.netloc.lower()


class WindowsProtector:
    """DPAPI current-user scope: ciphertext is bound to this Windows user/machine."""
    def _call(self, data, protect):
        if os.name != 'nt':
            raise IdentityError('正式设备凭据存储需要Windows DPAPI')
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source, target = Blob(len(data), buffer), Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise IdentityError('设备凭据无法由当前Windows用户/设备解密')
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(target.data)

    def protect(self, data):
        return self._call(data, True)

    def unprotect(self, data):
        return self._call(data, False)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@dataclass(frozen=True)
class AccessState:
    allowed: bool
    reason: str
    expires_at: float = 0
    remaining_seconds: float = 0


class DeviceSession:
    def __init__(self, root, origin, trusted_keys, clock=time.time, monotonic=time.monotonic, protector=None):
        self.root = Path(root)
        self.origin = server_origin(origin)
        # Public keys are supplied by the trusted application/configuration adapter,
        # never accepted from a lease or login file.
        self.trusted_keys = dict(trusted_keys)
        self.clock, self.monotonic = clock, monotonic
        self.protector = protector or WindowsProtector()
        self._lock = threading.RLock()
        self._last_wall = self._last_mono = None
        self._clock_invalid = False
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / (hashlib.sha256(self.origin.encode()).hexdigest() + '.login')

    @contextmanager
    def _transaction(self):
        with self._lock, (self.root / 'identity.lock').open('a+b') as lock:
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                if lock.read(1) == b'':
                    lock.write(b'0'); lock.flush()
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self, path):
        if path.stat().st_size > 128 * 1024:
            raise IdentityError('凭据文件过大')
        return json.loads(self.protector.unprotect(path.read_bytes()))

    def _write(self, path, payload):
        atomic_write(path, self.protector.protect(canonical(payload)))

    def _device(self):
        path = self.root / 'device.key'
        if path.exists():
            return Ed25519PrivateKey.from_private_bytes(unb64(self._read(path)['private_key']))
        key = Ed25519PrivateKey.generate()
        self._write(path, {'private_key': b64(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))})
        return key

    def device_public_key(self):
        with self._transaction():
            return b64(self._device().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def sign_proof(self, purpose, value):
        with self._transaction():
            return b64(self._device().sign((purpose+'\0'+value).encode('utf-8')))

    def _verify(self, envelope, account_id):
        payload = envelope['payload']
        key = self.trusted_keys[payload['key_id']]
        Ed25519PublicKey.from_public_bytes(key).verify(unb64(envelope['signature']), PURPOSE + canonical(payload))
        device = self._device().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if (type(payload['schema']) is not int or payload['schema'] != 1 or payload['product'] != 'cs2-ai-director'
                or payload['account_id'] != account_id or payload['server_origin'] != self.origin
                or payload['device_key_thumbprint'] != hashlib.sha256(device).hexdigest()
                or not isinstance(payload['capabilities'], list)
                or 'direct' not in payload['capabilities']):
            raise IdentityError('账号、设备、站点或产品能力不匹配')
        start, end = payload['verified_at'], payload['offline_expires_at']
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)):
            raise IdentityError('无效凭据时间')
        if not 0 < end - start <= MAX_OFFLINE_SECONDS or payload['offline_max_seconds'] != MAX_OFFLINE_SECONDS:
            raise IdentityError('离线凭据超过168小时上限')
        if (not isinstance(payload['model_sha256'], str) or len(payload['model_sha256']) != 64
                or any(c not in '0123456789abcdef' for c in payload['model_sha256'])):
            raise IdentityError('凭据未绑定模型摘要')
        return payload

    def install_verified_lease(self, envelope, account_id):
        """Network adapter calls after authenticated renewal; signature is still mandatory."""
        with self._transaction():
            self._install_verified_lease_locked(envelope, account_id)

    def _install_verified_lease_locked(self, envelope, account_id):
        """Caller holds the shared transaction, including any credential guard."""
        payload = self._verify(envelope, account_id)
        now = self.clock()
        # A freshly signed server lease can arrive just ahead of the local clock.
        # Wait for actual validity rather than backdating it or extending expiry.
        # The verified payload is checked again after a bounded wait; larger skew
        # and a stationary/rolled-back clock still fail closed.
        ahead = payload['verified_at'] - now
        if 0 < ahead <= 2:
            time.sleep(ahead + 0.01)
            now = self.clock()
        if not payload['verified_at'] <= now < payload['offline_expires_at']:
            raise IdentityError('凭据尚未生效或已过期')
        if self.path.exists():
            old = self._read(self.path)
            old_start = old.get('envelope', {}).get('payload', {}).get('verified_at', 0)
            if payload['verified_at'] <= old_start:
                raise IdentityError('续签不能重放旧凭据')
        self._write(self.path, {'account_id': account_id, 'envelope': envelope,
                                'last_seen': now, 'blocked': ''})
        self._last_wall, self._last_mono = now, self.monotonic()
        self._clock_invalid = False

    def invalidate(self, reason='signed_out'):
        """Logout/revocation keeps a tombstone so deleting a login cannot revive it."""
        with self._transaction():
            state = self._read(self.path) if self.path.exists() else {}
            state['blocked'] = reason
            self._write(self.path, state)

    def check(self, account_id, model_sha256):
        try:
            with self._transaction():
                state = self._read(self.path)
                if state.get('blocked'):
                    return AccessState(False, state['blocked'])
                payload = self._verify(state['envelope'], account_id)
                now, mono = self.clock(), self.monotonic()
                if not math.isfinite(now) or not math.isfinite(mono):
                    raise IdentityError('无效时钟')
                floor = max(state['last_seen'], payload['verified_at'])
                if self._last_wall is not None:
                    self._clock_invalid |= mono < self._last_mono
                    floor = max(floor, self._last_wall + max(0, mono - self._last_mono))
                # A 2-second tolerance only handles clock rounding; effective time
                # never moves backwards and is persisted across process restarts.
                self._clock_invalid |= now < floor - 2
                effective = max(now, floor)
                self._last_wall, self._last_mono = effective, mono
                state['last_seen'] = effective
                if self._clock_invalid:
                    state['blocked'] = 'clock_rollback'
                self._write(self.path, state)
                if self._clock_invalid:
                    return AccessState(False, 'clock_rollback')
                end = payload['offline_expires_at']
                if effective >= end:
                    return AccessState(False, 'offline_expired', end)
                if model_sha256 != payload['model_sha256']:
                    return AccessState(False, 'model_missing_or_changed', end)
                return AccessState(True, 'offline_ready', end, end - effective)
        except Exception:
            # Do not expose decrypted credential data, signatures or internal paths.
            return AccessState(False, 'login_missing_or_invalid')


class AuthorizationGate:
    """Adapter boundary; denial stops automatic actions while preserving manual UI."""
    def __init__(self, session, account_id, model_sha256, on_denied=lambda: None):
        self.session, self.account_id = session, account_id
        self.model_sha256, self.on_denied = model_sha256, on_denied
        self.last_state = AccessState(False, 'not_checked')

    def allowed(self):
        state = self.session.check(self.account_id, self.model_sha256())
        self.last_state = state
        if not state.allowed:
            self.on_denied()
        return state.allowed
