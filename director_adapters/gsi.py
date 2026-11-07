"""GSI transport and snapshot mailbox; independent of the GUI."""
from __future__ import annotations
import json
import threading
import time
from typing import Any, Optional, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from director_core import Snapshot, parse_snapshot

class SnapshotStore:
    """GSI 服务端与导播线程之间的共享状态。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap: Optional[Snapshot] = None
        self._count = 0
        self._last_at: Optional[float] = None

    def update(self, snap: Snapshot) -> None:
        with self._lock:
            self._count += 1
            snap.seq = self._count
            self._snap = snap
            self._last_at = time.monotonic()

    def get(self) -> Optional[Snapshot]:
        with self._lock:
            return self._snap

    def stats(self) -> tuple[int, Optional[float]]:
        with self._lock:
            return self._count, self._last_at

    def has_positions(self) -> bool:
        with self._lock:
            snap = self._snap
            return bool(
                snap and any(p.pos for p in snap.players.values() if p.health > 0)
            )


class _BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        request.settimeout(2)
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _GSIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CS2AIDirectorGSI"

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 1024*1024:
                self.close_connection = True
                self._reply(413, b'payload size out of range')
                return
            body = self.rfile.read(length) if length > 0 else b""
            payload = json.loads(body.decode("utf-8", "replace"))
            snap = parse_snapshot(payload, time.monotonic())
            if snap is None:
                self._reply(400, b"bad payload")
                return
            # 同一端口可能被多个 GSI 配置共用（CS2 会把同一份数据发给所有
            # URI）。其他工具的精简包缺少 allplayers_position，绝不能让它
            # 覆盖掉本程序的完整快照，否则会误报“缺少 allplayers_position”。
            accepted, why = self.server.accept(snap)  # type: ignore[attr-defined]
            if not accepted:
                self.server.note_rejected(why)  # type: ignore[attr-defined]
                self._reply(200, b"ignored")
                return
            self.server.store.update(snap)  # type: ignore[attr-defined]
            self._reply(200, b"ok")
        except Exception as e:
            try:
                self.server.log_fn(f"GSI 解析失败：{type(e).__name__}: {e}")  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                self._reply(400, b"error")
            except Exception:
                pass

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        # 静默：GSI 的心跳会刷屏
        return


class GSIServer:
    """只监听 127.0.0.1 的 GSI HTTP 接收端。"""

    def __init__(self, host: str, port: int, store: SnapshotStore,
                 log_fn: Callable[[str], None]) -> None:
        self.host = host
        self.port = port
        self.store = store
        self.log_fn = log_fn
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._reject_lock = threading.Lock()
        self._reject_count = 0
        self._last_reject_log = 0.0

    def accept(self, snap: Snapshot) -> tuple[bool, str]:
        """只接受本程序 GSI 配置发来的完整快照，过滤同端口的其他工具包。"""
        if not snap.provider_steamid:
            return False, "缺少 provider（很可能来自其他 GSI 配置）"
        alive = [p for p in snap.players.values() if p.health > 0]
        if alive and not any(p.pos for p in alive) and self.store.has_positions():
            return False, "缺少 allplayers_position（很可能与其他 GSI 配置共用端口）"
        return True, ""

    def note_rejected(self, why: str) -> None:
        with self._reject_lock:
            self._reject_count += 1
            now = time.monotonic()
            if now - self._last_reject_log < 10.0:
                return
            self._last_reject_log = now
            total = self._reject_count
        self.log_fn(
            f"已忽略 {total} 条不完整的 GSI 数据：{why}。"
            f"如果有其他 HUD/导播 GSI 配置指向同一端口，请改用不同端口。"
        )

    def start(self) -> None:
        if self._httpd is not None:
            return
        try:
            httpd = _BoundedHTTPServer((self.host, self.port), _GSIHandler)
        except OSError as e:
            raise RuntimeError(
                f"GSI 端口 {self.port} 无法监听：{e}。"
                f"请换一个端口并重新“一键安装 GSI”，或关闭占用该端口的程序。"
            ) from e
        httpd.daemon_threads = True
        httpd.store = self.store  # type: ignore[attr-defined]
        httpd.log_fn = self.log_fn  # type: ignore[attr-defined]
        httpd.accept = self.accept  # type: ignore[attr-defined]
        httpd.note_rejected = self.note_rejected  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .1}, name="gsi-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
