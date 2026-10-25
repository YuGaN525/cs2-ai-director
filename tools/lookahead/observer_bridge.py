#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影子录制桥：托管启动 cs2-headless-observer，记录带本地接收时间的 JSONL。

只做记录，不改变导播任何决策。观察器是独立获取的 AGPL 组件，本脚本不包含
其源码，也不保存 GOTV 密码；密码只通过私有 stdin 进入观察器进程。

输出：
  <out>/lookahead.jsonl          每行 {"recv": epoch, "recv_iso": ..., "record": {...}}
  <out>/observer.stderr.log      观察器诊断日志
  <out>/bridge_report.json       记录数、序号范围、退出码、事件统计
  <out>/observer/                观察器自己的会话报告目录
"""

from __future__ import annotations

import argparse
import collections
import getpass
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

MAX_STDOUT_LINE = 65536
MAX_STDIN_CONFIG = 4096
STOP_GRACE_SECONDS = 15.0
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class BridgeError(Exception):
    pass


def _iso(epoch: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch or time.time()))


def load_manifest(package: Path) -> dict[str, Any]:
    path = package / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise BridgeError(f"无法读取观察器清单 {path}：{e}") from e
    if not isinstance(data, dict):
        raise BridgeError("manifest.json 不是对象")
    entrypoints = data.get("entrypoints")
    if not isinstance(entrypoints, dict):
        raise BridgeError("manifest.json 缺少 entrypoints")
    return data


def resolve_entry(package: Path, manifest: dict[str, Any], key: str,
                  fallbacks: tuple[str, ...], override: Optional[str] = None,
                  required: bool = True) -> Optional[Path]:
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise BridgeError(f"--{key} 不存在：{path}")
        return path
    candidates: list[Path] = []
    raw = manifest.get("entrypoints", {}).get(key)
    if isinstance(raw, str) and raw:
        candidates.append((package / raw).resolve())
    candidates.extend((package / rel).resolve() for rel in fallbacks)
    for path in candidates:
        if path.is_file():
            return path
    if required:
        raise BridgeError(f"观察器包里找不到 {key}（可手动指定 --{key}）")
    return None


def validate_endpoint(text: str) -> str:
    value = (text or "").strip()
    if not value:
        raise BridgeError("endpoint 不能为空")
    host, sep, port_text = value.rpartition(":")
    if not sep:
        raise BridgeError("endpoint 必须是数字 IP:port")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
    except ValueError as e:
        raise BridgeError(f"endpoint 必须是数字 IP，而不是主机名：{host}") from e
    try:
        port = int(port_text)
    except ValueError as e:
        raise BridgeError("endpoint 端口无效") from e
    if not 1 <= port <= 65535:
        raise BridgeError("endpoint 端口超出 1-65535")
    return value


def validate_password(text: str) -> str:
    try:
        raw = (text or "").encode("ascii")
    except UnicodeEncodeError as e:
        raise BridgeError("GOTV 密码只支持 ASCII") from e
    if b"\0" in raw:
        raise BridgeError("GOTV 密码不能包含 NUL")
    if len(raw) > 95:
        raise BridgeError("GOTV 密码最多 95 字节")
    return text or ""


def build_config(endpoint: str, password: str) -> bytes:
    line = json.dumps({"endpoint": endpoint, "password": password},
                      ensure_ascii=False).encode("utf-8") + b"\n"
    if len(line) > MAX_STDIN_CONFIG:
        raise BridgeError("配置行超过 4096 字节")
    return line


def _stderr_worker(stream: Any, path: Path, tail: collections.deque[str],
                   quiet: bool) -> None:
    try:
        with path.open("w", encoding="utf-8", errors="replace") as f:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                f.write(text + "\n")
                tail.append(text)
                if not quiet and text:
                    print(f"[observer] {text}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        tail.append(f"stderr-reader-failed: {type(e).__name__}: {e}")
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _write_report(path: Path, report: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _thread_env() -> dict[str, str]:
    env = dict(os.environ)
    env["SteamAppId"] = "730"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_bridge(args: argparse.Namespace) -> int:
    package = Path(args.package).expanduser().resolve()
    if not package.is_dir():
        raise BridgeError(f"观察器包目录不存在：{package}")
    manifest = load_manifest(package)
    python = resolve_entry(package, manifest, "python",
                           ("runtime/python.exe", "python.exe"),
                           args.python, required=False) or Path(sys.executable)
    driver = resolve_entry(package, manifest, "driver", ("client/driver.py",),
                           args.driver)
    native = resolve_entry(package, manifest, "native",
                           ("client/cs2_observer.dll",), args.native)
    worker = resolve_entry(package, manifest, "worker",
                           ("client/parser_worker.mjs",), args.worker)
    node = resolve_entry(package, manifest, "node",
                         ("runtime/node.exe",), args.node, required=False)
    if node is None:
        found = shutil.which("node")
        if not found:
            raise BridgeError("找不到 Node 24+（external-node 包需指定 --node）")
        node = Path(found).resolve()
    steam_api = Path(args.steam_api).expanduser().resolve()
    if not steam_api.is_file():
        raise BridgeError(f"steam_api64.dll 不存在：{steam_api}")
    endpoint = validate_endpoint(args.endpoint)
    if not 1 <= int(args.host_version) <= 0xFFFFFFFF:
        raise BridgeError("host-version 必须是正整数 uint32")
    password = os.environ.get(args.password_env, "")
    if not password:
        if not sys.stdin.isatty():
            raise BridgeError(f"非交互环境请用环境变量 {args.password_env} 提供密码")
        password = getpass.getpass("GOTV password（无密码直接回车）: ")
    validate_password(password)

    out_dir = Path(args.out).expanduser().resolve() if args.out else (
        Path.cwd() / "lookahead_runs" / time.strftime("%Y%m%d_%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "lookahead.jsonl"
    stderr_path = out_dir / "observer.stderr.log"
    report_path = out_dir / "bridge_report.json"
    observer_out = out_dir / "observer"
    observer_out.mkdir(parents=True, exist_ok=True)
    if events_path.exists() and not args.force:
        raise BridgeError(f"输出已存在：{events_path}（换 --out 或加 --force）")

    source_id = args.source_id or uuid.uuid4().hex
    command = [
        str(python), "-B", str(driver), "--managed",
        "--native", str(native), "--steam-api", str(steam_api),
        "--node", str(node), "--parser-worker", str(worker),
        "--source-role", args.source_role, "--source-id", source_id,
        "--host-version", str(int(args.host_version)),
        "--output", str(observer_out),
    ]
    if args.seconds:
        command += ["--seconds", str(int(args.seconds))]
    if args.check:
        print(json.dumps({
            "package": str(package),
            "python": str(python),
            "driver": str(driver),
            "native": str(native),
            "worker": str(worker),
            "node": str(node),
            "steam_api": str(steam_api),
            "endpoint": endpoint,
            "host_version": int(args.host_version),
            "out": str(out_dir),
        }, ensure_ascii=False, indent=2))
        return 0

    started_at = time.time()
    report: dict[str, Any] = {
        "tool": "lookahead-shadow-bridge/1",
        "started_at": _iso(started_at),
        "package": str(package),
        "endpoint": endpoint,
        "host_version": int(args.host_version),
        "source_role": args.source_role,
        "source_id": source_id,
        "observer_output": str(observer_out),
        "events": str(events_path),
        "stderr": str(stderr_path),
        "password_stored": False,
        "counts": {},
        "event_names": {},
        "discontinuities": 0,
        "invalid_sequences": 0,
        "records": 0,
        "warnings": [],
        "stop_reason": "running",
        "exit_code": None,
    }
    counts: collections.Counter[str] = collections.Counter()
    names: collections.Counter[str] = collections.Counter()
    stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
    started = time.time()
    process: Optional[subprocess.Popen[bytes]] = None
    reader: Optional[threading.Thread] = None
    last_sequence = 0
    try:
        process = subprocess.Popen(
            command, cwd=str(package), env=_thread_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=-1,
            creationflags=CREATE_NO_WINDOW,
        )
        reader = threading.Thread(
            target=_stderr_worker,
            args=(process.stderr, stderr_path, stderr_tail, args.quiet),
            daemon=True)
        reader.start()
        assert process.stdin is not None
        process.stdin.write(build_config(endpoint, password))
        process.stdin.flush()
        password = ""  # noqa: F841 - 只保留到写入 stdin 为止
        print(f"观察器已启动，事件写入 {events_path}", file=sys.stderr)

        with events_path.open("w", encoding="utf-8") as out:
            while True:
                if args.seconds and time.time() - started >= int(args.seconds) + 10:
                    report["stop_reason"] = "duration"
                    break
                line = process.stdout.readline(MAX_STDOUT_LINE + 1)  # type: ignore[union-attr]
                if not line:
                    report["stop_reason"] = "eof"
                    break
                if len(line) > MAX_STDOUT_LINE or not line.endswith(b"\n"):
                    report["warnings"].append("记录超过 64KiB 或缺少换行，已停止记录")
                    report["stop_reason"] = "invalid-record"
                    break
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    report["warnings"].append("收到非 JSON 行，已跳过")
                    continue
                if not isinstance(record, dict):
                    report["warnings"].append("收到非对象记录，已跳过")
                    continue
                sequence = record.get("sequence")
                if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                    report["warnings"].append("记录缺少有效 sequence，已跳过")
                    continue
                if sequence <= last_sequence:
                    report["invalid_sequences"] += 1
                last_sequence = sequence
                recv = time.time()
                out.write(json.dumps(
                    {"recv": round(recv, 6), "recv_iso": _iso(recv),
                     "record": record}, ensure_ascii=False) + "\n")
                report["records"] += 1
                if record.get("kind"):
                    counts[str(record["kind"])] += 1
                    if record.get("kind") == "discontinuity":
                        report["discontinuities"] += 1
                if record.get("type") == "raw-event":
                    names[str(record.get("name", "?"))] += 1
                if report["records"] % 100 == 0:
                    print(f"已记录 {report['records']} 条（seq={sequence}）",
                          file=sys.stderr)
    except KeyboardInterrupt:
        report["stop_reason"] = "interrupted"
    except BridgeError:
        raise
    except Exception as e:  # noqa: BLE001
        report["stop_reason"] = "error"
        report["warnings"].append(f"{type(e).__name__}: {e}")
    finally:
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
            try:
                process.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            report["exit_code"] = process.returncode
            try:
                if process.stdout:
                    process.stdout.close()
            except Exception:
                pass
        if reader is not None:
            reader.join(timeout=2)
        report["ended_at"] = _iso()
        report["duration"] = round(time.time() - started_at, 2)
        report["counts"] = dict(counts)
        report["event_names"] = dict(names)
        report["first_sequence"] = 1 if report["records"] else None
        report["last_sequence"] = last_sequence or None
        if stderr_tail:
            report["stderr_tail"] = list(stderr_tail)
        _write_report(report_path, report)
        print(f"报告：{report_path}", file=sys.stderr)
        print(f"事件：{events_path}", file=sys.stderr)

    if report["stop_reason"] in ("error", "invalid-record"):
        return 1
    if report["stop_reason"] == "interrupted":
        return 2
    if report["exit_code"] not in (0, None):
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="影子录制 cs2-headless-observer 的 JSONL（不改变导播）")
    parser.add_argument("--package", required=True,
                        help="cs2-headless-observer 发行包目录（含 manifest.json）")
    parser.add_argument("--steam-api", required=True,
                        help="CS2 的 game/bin/win64/steam_api64.dll")
    parser.add_argument("--endpoint", required=True,
                        help="获授权的早源 GOTV 数字 IP:port")
    parser.add_argument("--host-version", type=int, required=True,
                        help="来源 host 版本，例如 14192")
    parser.add_argument("--out", default="",
                        help="输出目录；默认 ./lookahead_runs/<时间>")
    parser.add_argument("--source-role", choices=("ahead", "delayed"),
                        default="ahead")
    parser.add_argument("--source-id", default="",
                        help="可选稳定 UUID；默认每次随机")
    parser.add_argument("--password-env", default="GOTV_PASSWORD",
                        help="从该环境变量读取 GOTV 密码")
    parser.add_argument("--node", default="", help="覆盖 Node 路径")
    parser.add_argument("--python", default="", help="覆盖 Python 路径（调试用）")
    parser.add_argument("--driver", default="", help="覆盖 driver.py（调试用）")
    parser.add_argument("--native", default="", help="覆盖 native DLL（调试用）")
    parser.add_argument("--worker", default="", help="覆盖 parser worker（调试用）")
    parser.add_argument("--seconds", type=int, default=0,
                        help="诊断时限；0 表示持续到来源结束")
    parser.add_argument("--check", action="store_true",
                        help="只校验包/路径/参数，不启动")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有输出")
    parser.add_argument("--quiet", action="store_true", help="不转发 observer stderr")
    args = parser.parse_args(argv)
    try:
        return run_bridge(args)
    except BridgeError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
