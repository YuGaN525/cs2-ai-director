"""Non-blocking capture, bounded persistence, crash markers and background retry."""
from collections import deque
import json
import logging
import os
from pathlib import Path
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid

from client_version import VERSION
from diagnostics_client import DiagnosticClient, DiagnosticFailure
from diagnostics_privacy import event_line, exception_lines, sanitize_log, project_line, read_own_history
from diagnostics_store import DiagnosticStore, binding_key, TTL
from device_identity import atomic_write
from preferences import Preferences

_ACTIVE = None


def _lock(path):
    if path.is_symlink() or path.is_junction(): raise OSError('diagnostic_link')
    stream = path.open('a+b')
    try:
        if os.fstat(stream.fileno()).st_size == 0: stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except Exception:
        stream.close()
        raise


class SessionMarker:
    def __init__(self, root, store):
        self.root = Path(root) / 'sessions'
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.path = self.root / (str(uuid.uuid4())+'.json')
        self.handle = None

    def recover(self):
        for path in list(self.root.glob('*.json'))[:40]:
            if path == self.path or path.is_symlink() or path.is_junction(): continue
            lock_path = path.with_suffix('.lock')
            try: handle = _lock(lock_path)
            except OSError: continue  # Another running director/trainer owns it.
            try:
                if path.stat().st_size > 65536: continue
                value = json.loads(path.read_text('utf-8'))
                if time.time()-float(value['time']) < TTL:
                    log = sanitize_log(value.get('log', ''))
                    log += '\n'+event_line('runtime.unclean_exit', 'ERROR')
                    self.store.enqueue(value.get('owner', ''), 'crash', value['version'], log,
                        fingerprint='unclean:'+path.stem)
            except (OSError, ValueError, TypeError, KeyError):
                pass
            finally:
                path.unlink(missing_ok=True)
                handle.close()
                lock_path.unlink(missing_ok=True)

    def update(self, owner, log):
        if self.handle is None:
            if len(list(self.root.glob('*.json'))) >= 20: return
            self.handle = _lock(self.path.with_suffix('.lock'))
        atomic_write(self.path, json.dumps(dict(time=time.time(), version=VERSION,
            owner=owner, log=sanitize_log(log)[-16000:]), ensure_ascii=False).encode())

    def close(self):
        if self.handle is not None:
            self.path.unlink(missing_ok=True)
            self.handle.close(); self.handle = None
            self.path.with_suffix('.lock').unlink(missing_ok=True)


class CaptureHandler(logging.Handler):
    def __init__(self, runtime):
        super().__init__(); self.runtime = runtime

    def emit(self, record):
        try:
            self.runtime.observe(record.getMessage())
            if record.levelno >= logging.ERROR and not record.getMessage().startswith('GUI 回调异常'):
                exc_type, _, tb = record.exc_info or (RuntimeError, None, None)
                self.runtime.capture('error', exc_type, tb)
        except Exception:
            pass


class Diagnostics:
    def __init__(self, root, *, account=None, start=True):
        self.root = Path(root)
        self.preferences = Preferences(root)
        self.store = DiagnosticStore(root)
        self.account = account
        self.enabled = bool(self.preferences.read()['auto_report_diagnostics'])
        self.events = queue.Queue(maxsize=32)
        self.manual = queue.Queue(maxsize=20)
        self.ring = deque(maxlen=256)
        self.ring_lock = threading.Lock()
        self.ring_owner = self.owner()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.marker = SessionMarker(self.store.root, self.store)
        self.marker_lock = threading.RLock()
        self.last_status = '尚未提交日志'
        self.thread = None
        self.handler = CaptureHandler(self)
        logging.getLogger('cs2director').addHandler(self.handler)
        self.observe('INFO event=runtime.started')
        if start:
            self.thread = threading.Thread(target=self.run, name='diagnostics-upload', daemon=True)
            self.thread.start()

    def owner(self):
        try: return binding_key(self.account.binding()) if self.account else ''
        except (ValueError, AttributeError): return ''

    def identity(self):
        try: return self.account.upload_identity() if self.account else None
        except (ValueError, AttributeError): return None

    def observe(self, message):
        if not self.enabled: return
        safe = project_line(message)
        if safe:
            owner = self.owner()
            with self.ring_lock:
                if self.ring_owner != owner:
                    self.ring.clear(); self.ring_owner = owner
                self.ring.append(safe)

    def recent(self):
        owner = self.owner()
        with self.ring_lock:
            if self.ring_owner != owner:
                self.ring.clear(); self.ring_owner = owner
            return sanitize_log('\n'.join(self.ring))

    def capture(self, kind, exc_type=RuntimeError, tb=None, *, durable=False):
        if not self.enabled or exc_type in (KeyboardInterrupt, SystemExit): return
        lines = exception_lines(exc_type, tb, 'runtime.crash' if kind == 'crash' else 'runtime.error')
        fingerprint = re.sub(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ', '', '\n'.join(lines))
        item = (self.owner(), kind, sanitize_log(self.recent()+'\n'+'\n'.join(lines)), fingerprint)
        if durable:
            # Final Python exception: one bounded local write, never network here.
            try: self._persist(item)
            except Exception: pass  # The unfinished-session marker remains.
        else:
            try: self.events.put_nowait(item)
            except queue.Full: pass
            self.wake.set()

    def _persist(self, item):
        owner, kind, log, fingerprint = item
        return self.store.enqueue(owner, kind, VERSION, log, fingerprint=fingerprint)

    def preview(self):
        # Explicit manual action can read own allowlisted history even when auto is off.
        return sanitize_log(read_own_history(self.root)+'\n'+self.recent()+'\n'+event_line('diagnostics.manual'))

    def submit_manual(self, preview, owner_at_preview):
        if self.owner() != owner_at_preview: raise ValueError('账号状态已变化，请重新预览')
        ident = self.store.enqueue(owner_at_preview, 'manual', VERSION, preview)
        if owner_at_preview:
            self.manual.put_nowait(ident)
        self.last_status = '已加入当前账号上传队列' if owner_at_preview else '已保存在本机；登录后请预览确认'
        self.wake.set()
        return ident

    def confirm_existing(self, ident, owner_at_preview):
        if not owner_at_preview or self.owner() != owner_at_preview: raise ValueError('请先登录并重新预览')
        rows = self.store.rows(owner_at_preview, include_unbound=True)
        row = next((r for r in rows if r['id'] == ident), None)
        if not row or row['state'] != 'pending': raise ValueError('报告状态已变化')
        if not row['owner']: self.store.bind_unbound(ident, owner_at_preview)
        self.last_status = '已确认，按服务器重试时间提交'
        self.manual.put_nowait(ident); self.wake.set()

    def set_enabled(self, value):
        self.enabled = bool(value)  # Stop main-thread capture immediately.
        self.preferences.update(auto_report_diagnostics=self.enabled)
        if not self.enabled:
            with self.ring_lock: self.ring.clear()
            with self.marker_lock: self.marker.close()
        self.wake.set()

    def drain(self):
        for _ in range(32):
            try: item = self.events.get_nowait()
            except queue.Empty: break
            if self.enabled:
                try: self._persist(item)
                except (OSError, ValueError): self.last_status = '本机诊断队列已满或暂不可写'
                except sqlite3.Error:
                    try: self.events.put_nowait(item)
                    except queue.Full: pass
                    break

    def send_one(self, manual_id=None):
        identity = self.identity()
        if not identity or (not self.enabled and not manual_id): return
        owner = binding_key(identity.binding)
        row = self.store.claim(owner, manual_id)
        if row is None: return
        def permitted():
            if self.stop.is_set(): return False
            return bool(manual_id) or self.preferences.read()['auto_report_diagnostics']
        client = DiagnosticClient(self.identity, permitted=permitted)
        try:
            receipt = client.upload(identity, row)
            self.store.finish(row, receipt=receipt)
            self.last_status = '服务器已接收，保留 30 天'
        except DiagnosticFailure as error:
            from account_security import notify_account
            security_message=notify_account(self.account,error,identity)
            self.store.finish(row, status=error.status, retry_after=error.retry_after)
            self.last_status = security_message or ('报告被拒绝，请查看队列' if error.status in (400, 409, 413, 422) else '暂未上传，已保留本地等待重试')
        except (OSError, ValueError, TypeError):
            self.store.finish(row, status=0)
            self.last_status = '暂未上传，已保留本地等待重试'

    def run(self):
        last_marker = 0
        recovered = False
        while not self.stop.is_set():
            try:
                self.enabled = self.preferences.read()['auto_report_diagnostics']
                self.drain()
                with self.marker_lock:
                    if self.enabled:
                        if not recovered: self.marker.recover(); recovered = True
                        if time.monotonic()-last_marker > 5:
                            self.marker.update(self.owner(), self.recent()); last_marker = time.monotonic()
                    else:
                        self.marker.close()
                        with self.ring_lock: self.ring.clear()
                try: ident = self.manual.get_nowait()
                except queue.Empty: ident = None
                self.send_one(ident)
            except Exception:
                self.last_status = '诊断服务暂不可用，导播继续运行'
            self.wake.wait(1); self.wake.clear()
        self.drain()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(12)
        if self.thread and self.thread.is_alive(): raise TimeoutError('诊断传输正在退出，请稍后重试')
        self.marker.close()
        logging.getLogger('cs2director').removeHandler(self.handler)


def bootstrap(root):
    global _ACTIVE
    if _ACTIVE is not None: return _ACTIVE
    try: runtime = _ACTIVE = Diagnostics(root)
    except (OSError, ValueError, sqlite3.Error): return None
    old_sys, old_thread = sys.excepthook, threading.excepthook
    def fatal(exc_type, value, tb):
        runtime.capture('crash', exc_type, tb, durable=True)
        old_sys(exc_type, value, tb)
    def thread_error(args):
        runtime.capture('error', args.exc_type, args.exc_traceback)
        old_thread(args)
    sys.excepthook, threading.excepthook = fatal, thread_error
    return runtime


def attach(app):
    app.diagnostics = bootstrap(app.preferences.path.parent)
    if app.diagnostics is not None: app.diagnostics.account = app.account


def capture_gui(app, exc_type, tb):
    runtime = getattr(app, 'diagnostics', None) or _ACTIVE
    if runtime is not None: runtime.capture('error', exc_type, tb)


def capture_background(exc_type, tb):
    if _ACTIVE is not None: _ACTIVE.capture('error', exc_type, tb)
