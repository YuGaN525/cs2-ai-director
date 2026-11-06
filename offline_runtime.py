"""Authenticated composition of an immutable verified model and an execution gate."""
import gzip
import hashlib
import json
from pathlib import Path
from device_identity import AuthorizationGate
from director_core.model_format import validate_forest


class CachedModel:
    def __init__(self, path):
        self.path = Path(path)
        self.data = None
        self._stamp = self._digest = None

    def _stat(self):
        st = self.path.stat()
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def verified_hash(self):
        try:
            stamp = self._stat()
            # Re-check actual bytes rather than trusting a mutable path or old mtime.
            with self.path.open('rb') as stream:
                raw = stream.read(20 * 1024 * 1024 + 1)
            if len(raw) > 20 * 1024 * 1024:
                return ''
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                unpacked = stream.read(64 * 1024 * 1024 + 1)
            if len(unpacked) > 64 * 1024 * 1024:
                return ''
            data = validate_forest(json.loads(unpacked))
            if stamp != self._stat():
                return ''
            self.data = data
            self._stamp, self._digest = stamp, hashlib.sha256(raw).hexdigest()
            return self._digest
        except Exception:
            return ''

    def current_hash(self):
        # The real-time path only checks the identity of the prepared immutable cache.
        # Updates need a new verification/preparation outside the decision loop.
        try:
            return self._digest if self._stamp == self._stat() else ''
        except OSError:
            return ''


def create_local_engine(session, account_id, model_path, cfg, bus, **adapters):
    """Explicit authenticated constructor; no credential means no automatic actions.

    The GUI acquires the shared activation barrier before calling this constructor.
    """
    from cs2_ai_director import DirectorEngine
    from decision_model import ModelArbiter
    model = CachedModel(model_path)
    if not model.verified_hash():
        raise ValueError('模型缓存验证失败')
    gate = AuthorizationGate(session, account_id, model.current_hash)
    class PreparedArbiter(ModelArbiter):
        def _load_calibration(self):
            self.calibration = None  # Local calibration is not part of this signed artifact.
        def load(self):
            self.forest, self.loaded, self.format = model.data, True, 'json'
            return True

        def reload_if_changed(self):
            return False  # Switching needs an independently verified signed lease.
    arbiter = PreparedArbiter(cfg, Path(model_path).with_suffix('').with_suffix('.pkl'))
    if not arbiter.loaded:
        raise ValueError('模型不能由本地推理器加载')
    engine = DirectorEngine(cfg, bus, authorization_gate=gate, model_arbiter=arbiter, **adapters)
    return engine, gate


def main(argv=None):
    """Diagnostic of a locally installed session; persists the clock high-water mark.

    The config's public-key map is for local integration; production trust-key
    provisioning belongs to AID-07/09, not to untrusted login-file contents.
    """
    import argparse
    from dataclasses import asdict
    from device_identity import DeviceSession, unb64
    parser = argparse.ArgumentParser(description='本地设备离线凭据诊断（服务器接入待完成）')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--report', type=Path, help='保存脱敏诊断结果（也适用于无控制台exe）')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        session = DeviceSession(config['session_root'], config['server_origin'],
                                {k: unb64(v) for k, v in config['trusted_public_keys'].items()})
        model = CachedModel(config['model_path'])
        state = session.check(config['account_id'], model.verified_hash())
        result = asdict(state)
        if args.report:
            from device_identity import atomic_write, canonical
            atomic_write(args.report, canonical(result))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if state.allowed else 1
    except Exception:
        result = {'allowed': False, 'reason': 'configuration_or_login_invalid'}
        if args.report:
            from device_identity import atomic_write, canonical
            atomic_write(args.report, canonical(result))
        print(json.dumps(result))
        return 1
