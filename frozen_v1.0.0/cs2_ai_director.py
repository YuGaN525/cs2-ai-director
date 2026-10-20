#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 AI 导播（CS2 AI Director）
================================

一个只依赖 GSI（Game State Integration）数据的 CS2 观战导播：
  1) 自动把观战视角切到“未来几秒最可能造成击杀”的选手第一视角；
  2) 回合进入 freezetime 时触发 HLAE 出生点运镜宏（每回合一次）；
  3) 炸弹即将爆炸（countdown <= 8s）时触发 HLAE 包点运镜宏（每次安放一次）；
  4) 提供 tkinter GUI：检测/选择 cs2.exe、一键安装 GSI 配置、录入快捷键与 API Key。

硬性边界（代码里也反复强调）：
  * 只读取 GSI 的 HTTP 推送，不读内存、不注入、不发送游戏控制台命令；
  * 对游戏的全部“动作”只有两类：模拟 0-9 数字键、模拟两个用户录入的 HLAE 宏快捷键；
  * 不直接与 HLAE 通信。

运行：
    python cs2_ai_director.py

作者备注：本文件是单文件程序，所有可调参数集中在 DEFAULT_CONFIG / CONFIG 中。
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import json
import logging
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------
# 0. 基本常量 / 可选依赖
# --------------------------------------------------------------------------

APP_NAME = "CS2AIDirector"
APP_TITLE = "CS2 AI 导播"
VERSION = "1.0.0"
IS_WINDOWS = os.name == "nt"

GSI_CFG_NAME = "gamestate_integration_ai_director.cfg"
DIGITS = "0123456789"

# 可选依赖全部用 try/except 包起来：缺库时程序仍能启动并在 GUI 里提示，
# 而不是 import 阶段直接崩溃。
try:
    import pydirectinput  # 向 DirectX 游戏注入按键（SendInput 扫描码）
    HAS_PYDIRECTINPUT = True
except Exception:  # pragma: no cover - 取决于运行环境
    pydirectinput = None
    HAS_PYDIRECTINPUT = False

try:
    import keyboard  # 全局低级键盘钩子 + 组合键发送
    HAS_KEYBOARD = True
except Exception:  # pragma: no cover
    keyboard = None
    HAS_KEYBOARD = False

try:
    import pygetwindow  # 窗口枚举/标题匹配
    HAS_PYGETWINDOW = True
except Exception:  # pragma: no cover
    pygetwindow = None
    HAS_PYGETWINDOW = False

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
    HAS_TK = True
except Exception:  # pragma: no cover
    tk = None
    HAS_TK = False

# --------------------------------------------------------------------------
# 1. 集中配置
# --------------------------------------------------------------------------

# 所有可配置项都集中在这里。业务逻辑只通过 cfg[...] 读取，不写死常量。
# GUI 会修改其中一部分并持久化到 %APPDATA%\CS2AIDirector\config.json。
DEFAULT_CONFIG: dict[str, Any] = {
    # ---- 路径与网络 ----
    "cs2_exe": "",
    "gsi_port": 3000,
    "window_title": "Counter-Strike 2",
    # ---- HLAE 两个宏快捷键（必须由用户在 GUI 录入，留空则禁止启动，避免误按）----
    "hlae_round_key": "",
    "hlae_bomb_key": "",
    # ---- DeepSeek 仲裁（可选）----
    "ai_enabled": True,
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    "ai_timeout": 2.5,          # 单次请求超时（秒），超时立即降级到规则引擎
    "ai_min_interval": 3.0,     # 两次 API 调用最小间隔，控制成本
    "ai_max_per_minute": 12,
    "ai_result_ttl": 2.5,       # AI 建议的有效期（秒）
    "ai_ambiguity_margin": 0.08,  # 规则前两名分差小于该值才请求 AI
    # ---- 视角切换 ----
    "manual_pause_seconds": 10.0,
    "loop_hz": 20,
    "engage_range": 1500.0,     # 基础交火距离（CS 单位，约 28m）
    "aim_fov_deg": 70.0,        # 认为“正在瞄准”的最大偏角
    "switch_min_score": 0.30,   # 低于该分数宁可不切
    "switch_margin": 0.10,      # 新目标必须比当前目标高出的分数，防抖
    "emergency_score": 0.72,    # 极高威胁时允许绕过 margin
    "min_dwell_seconds": 1.0,   # 两次切换的最小间隔
    "repeat_guard_seconds": 4.0,  # 同一 slot 的重复按键保护
    "gsi_stale_seconds": 3.0,   # 超过该时间没有 GSI 数据视为断流
    "self_press_guard": 0.30,   # 模拟按键后多久内忽略同键，防自捕获
    # ---- 运镜 ----
    "bomb_cam_countdown": 8.0,  # 炸弹剩余多少秒触发包点运镜
    "bomb_cam_grace": 1.5,      # 触发包点运镜后暂停自动切换的秒数（0=不暂停）
    "trigger_retry_seconds": 1.0,
    # ---- 其它 ----
    "auto_focus": True,         # 按键前自动把 CS2 拉到前台
    "dry_run": False,           # 演练模式：只算不按
}

# 兼容别名：需求里要求“一个 CONFIG 结构”
CONFIG = DEFAULT_CONFIG

# GSI 配置中必须开启的 data 字段（尤其 allplayers_position）
GSI_REQUIRED_DATA = (
    "provider",
    "map",
    "round",
    "player_id",
    "player_state",
    "player_weapons",
    "allplayers_id",
    "allplayers_state",
    "allplayers_weapons",
    "allplayers_position",
    "bomb",
    "phase_countdowns",
)


# --------------------------------------------------------------------------
# 2. 配置持久化 / 应用数据目录
# --------------------------------------------------------------------------

def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return app_data_dir() / "config.json"


def log_path() -> Path:
    return app_data_dir() / "director.log"


def load_config() -> dict[str, Any]:
    """读取本地 JSON，与默认配置合并（旧版本缺的键自动补默认值）。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = config_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in cfg:
                        cfg[k] = v
        except Exception:
            # 配置损坏不应阻止启动
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """只保存已知的键，保持 JSON 可读；API Key 为明文，见 README 说明。"""
    data = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    tmp = config_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config_path())


# --------------------------------------------------------------------------
# 3. 日志（文件 + GUI 队列）
# --------------------------------------------------------------------------

class LogBus:
    """线程安全的日志总线：写文件，同时把一行文本塞进队列给 tkinter 轮询。"""

    def __init__(self) -> None:
        self.queue: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=4000)
        self.logger = logging.getLogger("cs2director")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            try:
                fh = RotatingFileHandler(
                    log_path(), maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
                )
                fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                self.logger.addHandler(fh)
            except Exception:
                pass

    def _put(self, level: str, msg: str) -> None:
        try:
            self.queue.put_nowait((level, msg))
        except queue.Full:
            pass

    def info(self, msg: str) -> None:
        self.logger.info(msg)
        self._put("INFO", msg)

    def warning(self, msg: str) -> None:
        self.logger.warning(msg)
        self._put("WARN", msg)

    def error(self, msg: str) -> None:
        self.logger.error(msg)
        self._put("ERROR", msg)

    def exception(self, msg: str) -> None:
        self.logger.exception(msg)
        self._put("ERROR", f"{msg}（详情见日志文件）")


# --------------------------------------------------------------------------
# 4. 依赖检查与安装
# --------------------------------------------------------------------------

DEPENDENCY_SPECS = (
    # (import 名, pip 包名, 用途, 是否必需)
    ("pydirectinput", "pydirectinput", "向 DirectX 游戏模拟数字键", True),
    ("keyboard", "keyboard", "全局热键监听 / HLAE 组合键", True),
    ("pygetwindow", "pygetwindow", "查找并激活 CS2 窗口", True),
    ("openai", "openai", "DeepSeek 仲裁（可选）", False),
)


def _pkg_version(dist_name: str) -> Optional[str]:
    try:
        import importlib.metadata as md
        return md.version(dist_name)
    except Exception:
        return None


def check_dependencies() -> list[dict[str, Any]]:
    """返回依赖状态列表，供 GUI/CLI 展示。"""
    result: list[dict[str, Any]] = []
    for mod, pkg, purpose, required in DEPENDENCY_SPECS:
        installed = False
        try:
            __import__(mod)
            installed = True
        except Exception:
            installed = False
        result.append(
            {
                "module": mod,
                "package": pkg,
                "purpose": purpose,
                "required": required,
                "installed": installed,
                "version": _pkg_version(pkg),
            }
        )
    result.append(
        {
            "module": "tkinter",
            "package": "（Python 自带）",
            "purpose": "GUI",
            "required": True,
            "installed": HAS_TK,
            "version": f"Tk {getattr(tk, 'TkVersion', '?')}" if HAS_TK else None,
        }
    )
    return result


def install_dependencies(log_cb: Optional[Callable[[str], None]] = None) -> bool:
    """用当前解释器的 pip 安装缺失的包。返回是否全部成功。"""
    missing = [d["package"] for d in check_dependencies()
               if not d["installed"] and d["package"] != "（Python 自带）"]
    if not missing:
        if log_cb:
            log_cb("所有依赖已安装，无需操作。")
        return True
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *missing]
    if log_cb:
        log_cb("执行：" + " ".join(cmd))
    flags = 0
    if IS_WINDOWS:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if log_cb:
                log_cb(line.rstrip())
        code = proc.wait(timeout=600)
    except Exception as e:
        if log_cb:
            log_cb(f"安装失败：{type(e).__name__}: {e}")
        return False
    ok = code == 0
    if log_cb:
        log_cb("依赖安装成功。" if ok else f"pip 退出码 {code}，请检查网络或手动安装。")
    return ok


# --------------------------------------------------------------------------
# 5. GSI 数据模型与解析
# --------------------------------------------------------------------------

@dataclass
class Player:
    steamid: str
    name: str
    slot: Optional[int]
    team: str
    health: int
    armor: int
    helmet: bool
    flashed: float          # 归一化到 0..1
    burning: bool
    defusing: bool
    pos: Optional[tuple[float, float, float]]
    forward: Optional[tuple[float, float, float]]
    weapons: list[dict[str, Any]]
    active_weapon: Optional[dict[str, Any]]
    money: int


@dataclass
class Bomb:
    state: str
    countdown: Optional[float]
    position: Optional[tuple[float, float, float]]


@dataclass
class Snapshot:
    seq: int
    received_at: float          # time.monotonic()
    provider_steamid: str
    map_name: str
    phase: str                  # freezetime / live / over / warmup ...
    round_num: Optional[int]
    players: dict[str, Player]
    observer_slot: Optional[int]
    bomb: Optional[Bomb]
    score_ct: int
    score_t: int
    phase_ends_in: Optional[float]
    is_spectating: bool


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _b(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


def _vec(v: Any) -> Optional[tuple[float, float, float]]:
    """GSI 的位置/朝向既可能是 "x, y, z" 字符串，也可能是数组。"""
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.startswith("["):
            try:
                v = json.loads(s)
            except Exception:
                return None
        else:
            parts = [p.strip() for p in s.split(",")]
            if len(parts) < 2:
                return None
            v = parts
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return (
            _f(v[0]),
            _f(v[1]),
            _f(v[2]) if len(v) > 2 else 0.0,
        )
    return None


def _weapons(raw: Any) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """把 weapons 字典/数组统一成 list，并找出 state == active 的武器。"""
    items: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, val in raw.items():
            if isinstance(val, dict):
                w = dict(val)
                w.setdefault("_key", key)
                items.append(w)
    elif isinstance(raw, list):
        for val in raw:
            if isinstance(val, dict):
                items.append(dict(val))
    active = None
    for w in items:
        if str(w.get("state", "")).lower() == "active":
            active = w
            break
    return items, active


def parse_snapshot(payload: Any, received_at: float) -> Optional[Snapshot]:
    """把一次 GSI POST 解析成 Snapshot；字段缺失时尽量降级，不抛异常。"""
    if not isinstance(payload, dict):
        return None
    provider = payload.get("provider") or {}
    mapinfo = payload.get("map") or {}
    roundinfo = payload.get("round") or {}
    countdowns = payload.get("phase_countdowns") or {}
    allplayers = payload.get("allplayers") or {}
    player_sec = payload.get("player") or {}

    players: dict[str, Player] = {}
    if isinstance(allplayers, dict):
        for sid, pd in allplayers.items():
            if not isinstance(pd, dict):
                continue
            state = pd.get("state") or {}
            weapons, active = _weapons(pd.get("weapons"))
            flashed_raw = _f(state.get("flashed"), 0.0)
            # CS:GO/CS2 的 flashed 常见是 0-255，也有 0-1 的写法，统一归一化
            flashed = flashed_raw / 255.0 if flashed_raw > 1.0 else max(0.0, flashed_raw)
            slot = _i(pd.get("observer_slot"))
            players[str(sid)] = Player(
                steamid=str(sid),
                name=str(pd.get("name") or ""),
                slot=slot,
                team=str(pd.get("team") or "").upper(),
                health=_i(state.get("health"), 0) or 0,
                armor=_i(state.get("armor"), 0) or 0,
                helmet=_b(state.get("helmet")),
                flashed=min(1.0, max(0.0, flashed)),
                burning=_b(state.get("burning")),
                defusing=_b(state.get("defusing")),
                pos=_vec(pd.get("position")),
                forward=_vec(pd.get("forward")),
                weapons=weapons,
                active_weapon=active,
                money=_i(state.get("money"), 0) or 0,
            )

    bomb_raw = payload.get("bomb")
    bomb = None
    if isinstance(bomb_raw, dict) and bomb_raw.get("state"):
        bomb = Bomb(
            state=str(bomb_raw.get("state", "")).lower(),
            countdown=(
                _f(bomb_raw.get("countdown"))
                if bomb_raw.get("countdown") is not None
                else None
            ),
            position=_vec(bomb_raw.get("position")),
        )

    obs_slot = _i(player_sec.get("observer_slot"))
    player_sid = str(player_sec.get("steamid") or "")
    if obs_slot is None and player_sid in players:
        obs_slot = players[player_sid].slot

    provider_sid = str(provider.get("steamid") or "")
    local = players.get(provider_sid)
    # 关键安全判断：只有“确实在观战”时才允许发数字键。若本地玩家还活着，
    # 数字键在游戏里是切武器，绝不能发。
    local_alive = bool(local and local.health > 0)
    is_spectating = obs_slot is not None and not local_alive

    phase = str(
        countdowns.get("phase")
        or roundinfo.get("phase")
        or mapinfo.get("phase")
        or ""
    ).lower()
    if phase == "freezetime" or phase == "freeze_time":
        phase = "freezetime"

    team_ct = mapinfo.get("team_ct") or {}
    team_t = mapinfo.get("team_t") or {}

    return Snapshot(
        seq=0,
        received_at=received_at,
        provider_steamid=provider_sid,
        map_name=str(mapinfo.get("name") or "unknown"),
        phase=phase,
        round_num=_i(mapinfo.get("round")),
        players=players,
        observer_slot=obs_slot,
        bomb=bomb,
        score_ct=_i(team_ct.get("score"), 0) or 0,
        score_t=_i(team_t.get("score"), 0) or 0,
        phase_ends_in=(
            _f(countdowns.get("phase_ends_in"))
            if countdowns.get("phase_ends_in") is not None
            else None
        ),
        is_spectating=is_spectating,
    )


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


class _GSIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CS2AIDirectorGSI"

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            payload = json.loads(body.decode("utf-8", "replace"))
            snap = parse_snapshot(payload, time.monotonic())
            if snap is None:
                self._reply(400, b"bad payload")
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

    def start(self) -> None:
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _GSIHandler)
        except OSError as e:
            raise RuntimeError(
                f"GSI 端口 {self.port} 无法监听：{e}。"
                f"请换一个端口并重新“一键安装 GSI”，或关闭占用该端口的程序。"
            ) from e
        httpd.daemon_threads = True
        httpd.store = self.store  # type: ignore[attr-defined]
        httpd.log_fn = self.log_fn  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="gsi-server", daemon=True)
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


# --------------------------------------------------------------------------
# 6. 评分：纯规则引擎
# --------------------------------------------------------------------------
#
# 设计思路（重要）：
#   我们不预测“谁将会死”，而是估计“谁最可能在未来 1-3 秒内开火并拿到击杀”。
#   对每个候选选手 A，取他所有存活敌人 B 中威胁最高的一对，计算：
#
#       score(A, B) = 接近度 × 交战意识 × 紧迫度 × 胜算系数 × 持枪就绪度
#
#   - 接近度：以 A 当前武器的有效射程为包络，越近越高（幂函数让中距离也敏感）；
#   - 交战意识：双方朝向夹角（forward 与 A->B 的夹角），双方都瞄着对方最高；
#   - 紧迫度：任一方残血、B 刚掉血（正在交火）会提高；
#   - 胜算系数：由血量/护甲/武器/瞄准差/闪光算出的 1v1 胜率，避免把“必死的人”当主角；
#   - 持枪就绪度：手里是刀/雷/空仓时大幅降低。
#
#   这样“残血 + 对枪”会得到高分，“满血架远点”分数较低，符合验收标准的取向。
# --------------------------------------------------------------------------

TYPE_POWER = {
    "SniperRifle": 12.0,
    "Rifle": 10.0,
    "Machine Gun": 9.0,
    "Shotgun": 8.0,
    "SMG": 7.0,
    "Pistol": 4.5,
    "Knife": 1.2,
    "Grenade": 0.8,
    "C4": 0.3,
    "StackableItem": 0.3,
    "Tablet": 0.2,
}

NAME_POWER = {
    "weapon_awp": 14.0,
    "weapon_ssg08": 9.5,
    "weapon_scar20": 12.5,
    "weapon_g3sg1": 12.5,
    "weapon_deagle": 7.5,
    "weapon_revolver": 7.0,
    "weapon_ak47": 10.5,
    "weapon_m4a1": 10.0,
    "weapon_m4a1_silencer": 10.0,
    "weapon_aug": 10.5,
    "weapon_sg556": 10.5,
    "weapon_taser": 6.0,
}

TYPE_RANGE = {
    "SniperRifle": 2600.0,
    "Rifle": 1500.0,
    "Machine Gun": 1400.0,
    "SMG": 900.0,
    "Shotgun": 550.0,
    "Pistol": 800.0,
}

NAME_RANGE = {
    "weapon_awp": 2600.0,
    "weapon_ssg08": 1900.0,
    "weapon_scar20": 2400.0,
    "weapon_g3sg1": 2400.0,
    "weapon_deagle": 1100.0,
    "weapon_revolver": 900.0,
}

UTILITY_TYPES = {"Knife", "Grenade", "C4", "StackableItem", "Tablet"}


@dataclass
class WeaponProfile:
    power: float
    envelope: float
    ready: bool
    name: str


def weapon_profile(p: Player) -> WeaponProfile:
    w = p.active_weapon
    if w is None and p.weapons:
        # 没有 active 标记时降级：选威力最大的武器
        w = max(p.weapons, key=lambda x: TYPE_POWER.get(str(x.get("type", "")), 1.0))
    if w is None:
        return WeaponProfile(0.5, 300.0, False, "unknown")

    name = str(w.get("name") or "")
    typ = str(w.get("type") or "")
    power = NAME_POWER.get(name, TYPE_POWER.get(typ, 5.0))
    envelope = NAME_RANGE.get(name, TYPE_RANGE.get(typ, 900.0))
    ready = True

    if typ in UTILITY_TYPES or name.startswith(("weapon_knife", "weapon_c4", "weapon_taser")):
        ready = False
        envelope = min(envelope, 400.0)

    ammo = _i(w.get("ammo_clip"))
    if ammo is not None and ammo <= 0 and typ not in ("Knife", "Grenade", "C4", "StackableItem"):
        ready = False
        envelope = min(envelope, 400.0)

    if p.defusing:
        ready = False  # 拆包时无法开枪
    return WeaponProfile(power=power, envelope=max(250.0, envelope), ready=ready, name=name)


def aim_factor(src: Player, dst: Player, fov_deg: float) -> float:
    """src 的 forward 与 src->dst 方向的夹角，0=完全没瞄，1=正对。"""
    if not src.pos or not src.forward or not dst.pos:
        return 0.0
    fx, fy, fz = src.forward
    dx = dst.pos[0] - src.pos[0]
    dy = dst.pos[1] - src.pos[1]
    dz = dst.pos[2] - src.pos[2]
    fl = math.sqrt(fx * fx + fy * fy + fz * fz)
    dl = math.sqrt(dx * dx + dy * dy + dz * dz)
    if fl < 1e-6 or dl < 1e-6:
        return 0.0
    dot = (fx * dx + fy * dy + fz * dz) / (fl * dl)
    dot = max(-1.0, min(1.0, dot))
    cos_fov = math.cos(math.radians(fov_deg))
    if dot <= cos_fov:
        return 0.0
    return max(0.0, min(1.0, (dot - cos_fov) / (1.0 - cos_fov)))


def _dist(a: Optional[tuple[float, float, float]],
          b: Optional[tuple[float, float, float]]) -> Optional[float]:
    if not a or not b:
        return None
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _win_probability(a: Player, b: Player, aim_a: float, aim_b: float,
                     pa: WeaponProfile, pb: WeaponProfile) -> float:
    """1v1 胜率的 logistic 近似；只用于排序，不需要很精确。"""
    adv = 0.030 * (a.health - b.health)          # 30 血 ≈ 0.9
    adv += 0.006 * (a.armor - b.armor)
    denom = max(6.0, (pa.power + pb.power) / 2.0)
    adv += 0.80 * ((pa.power - pb.power) / denom)
    adv += 1.20 * (aim_a - aim_b)
    adv += -0.90 * max(0.0, a.flashed - 0.15) + 0.90 * max(0.0, b.flashed - 0.15)
    if a.helmet and not b.helmet:
        adv += 0.25
    if pa.ready != pb.ready:
        adv += 0.25 if pa.ready else -0.35
    if b.defusing:
        adv += 0.35
    if a.defusing:
        adv -= 1.20
    if a.burning:
        adv -= 0.25
    return max(0.02, min(0.98, 1.0 / (1.0 + math.exp(-adv))))


def readiness(p: Player, prof: WeaponProfile) -> float:
    if not prof.ready:
        if p.defusing:
            return 0.30
        return 0.55
    return 1.0


@dataclass
class Candidate:
    slot: int
    steamid: str
    name: str
    team: str
    score: float
    win: float
    enemy_slot: Optional[int]
    enemy_name: str
    distance: float
    aim_self: float
    aim_enemy: float
    hp: int
    armor: int
    enemy_hp: int
    weapon: str
    enemy_weapon: str
    enemy_damaged: bool


def score_candidates(snap: Snapshot, cfg: dict[str, Any],
                     damage_recent: Optional[Callable[[str], bool]] = None,
                     now: Optional[float] = None) -> list[Candidate]:
    """给每个可切换的选手打分，按分数降序返回。"""
    fov = float(cfg["aim_fov_deg"])
    alive_all = [p for p in snap.players.values()
                 if p.health > 0 and p.team in ("CT", "T")]
    attackers = [p for p in alive_all
                 if p.slot is not None and p.steamid != snap.provider_steamid]
    results: list[Candidate] = []

    bomb = snap.bomb
    bomb_planted = bool(bomb and bomb.state in ("planted", "defusing"))
    # engage_range 作为“交战包络”的整体缩放（默认 1500 = 1.0 倍），
    # 这样既能按武器区分射程，又保留一个全局可调的总开关。
    range_scale = max(0.4, min(2.0, float(cfg.get("engage_range", 1500.0)) / 1500.0))

    for a in attackers:
        if not a.pos:
            continue
        prof_a = weapon_profile(a)
        ready_a = readiness(a, prof_a)
        best: Optional[Candidate] = None

        for b in alive_all:
            if b.team == a.team or b.steamid == a.steamid:
                continue
            d = _dist(a.pos, b.pos)
            if d is None:
                continue
            envelope = prof_a.envelope * range_scale
            if d > envelope:
                continue
            prof_b = weapon_profile(b)

            prox = (max(0.0, 1.0 - d / envelope)) ** 0.55
            aim_a = aim_factor(a, b, fov)
            aim_b = aim_factor(b, a, fov)
            if d < 260.0:
                # 贴脸时朝向数据抖动大，给一个下限
                aim_a = max(aim_a, 0.55)
                aim_b = max(aim_b, 0.55)
            aim_pair = 0.6 * max(aim_a, aim_b) + 0.4 * min(aim_a, aim_b)
            awareness = 0.55 + 0.45 * aim_pair

            low_hp = 1.0 - min(a.health, b.health) / 100.0
            urgency = 1.0 + 0.5 * low_hp
            recent = bool(damage_recent and damage_recent(b.steamid))
            if recent:
                urgency += 0.12       # B 刚掉血：交火已经在发生
            if bomb_planted and b.defusing:
                urgency += 0.25       # 拆包者是最可能被击杀的目标

            win = _win_probability(a, b, aim_a, aim_b, prof_a, prof_b)
            score = prox * awareness * urgency * (0.30 + 0.70 * win) * ready_a

            cand = Candidate(
                slot=int(a.slot), steamid=a.steamid, name=a.name or f"slot{a.slot}",
                team=a.team, score=score, win=win,
                enemy_slot=b.slot, enemy_name=b.name or (f"slot{b.slot}" if b.slot is not None else "?"),
                distance=d, aim_self=aim_a, aim_enemy=aim_b,
                hp=a.health, armor=a.armor, enemy_hp=b.health,
                weapon=_short_weapon(prof_a.name), enemy_weapon=_short_weapon(prof_b.name),
                enemy_damaged=recent,
            )
            if best is None or cand.score > best.score:
                best = cand

        if best is not None and best.score > 0:
            results.append(best)

    results.sort(key=lambda c: c.score, reverse=True)
    return results


def _short_weapon(name: str) -> str:
    if not name:
        return "?"
    return name.replace("weapon_", "", 1)


def format_candidate(c: Candidate) -> str:
    return (
        f"slot={c.slot} {c.name} 分={c.score:.2f} 距离={c.distance:.0f} "
        f"瞄准={c.aim_self:.2f}/{c.aim_enemy:.2f} HP={c.hp}/{c.enemy_hp} "
        f"胜率={c.win:.2f} 武器={c.weapon}"
    )


# --------------------------------------------------------------------------
# 7. 切换决策（含防抖/迟滞/重复按键保护）
# --------------------------------------------------------------------------

@dataclass
class Decision:
    slot: Optional[int]
    reason: str
    candidate: Optional[Candidate] = None
    source: str = "rule"


def choose_slot(candidates: list[Candidate], current_slot: Optional[int],
                now: float, last_switch_at: Optional[float],
                last_switch_slot: Optional[int],
                cfg: dict[str, Any]) -> Decision:
    """纯函数决策，方便测试。返回 slot=None 表示保持当前视角。"""
    if not candidates:
        return Decision(None, "无交火迹象")

    best = candidates[0]
    if best.score < float(cfg["switch_min_score"]):
        return Decision(None, f"最高分 {best.score:.2f} 低于阈值")

    if current_slot is not None and best.slot == current_slot:
        return Decision(None, f"已经在 slot={best.slot}")

    if last_switch_at is not None and (now - last_switch_at) < float(cfg["min_dwell_seconds"]):
        return Decision(None, "切换冷却中")

    # 防止 GSI 还没刷新 observer_slot 时对同一目标重复按键
    if (last_switch_slot == best.slot and last_switch_at is not None
            and (now - last_switch_at) < float(cfg["repeat_guard_seconds"])):
        return Decision(None, f"slot={best.slot} 刚刚按过")

    current = next((c for c in candidates if c.slot == current_slot), None)
    if current is not None:
        gap = best.score - current.score
        if gap < float(cfg["switch_margin"]) and best.score < float(cfg["emergency_score"]):
            return Decision(None, f"分数优势不足（+{gap:.2f}）")

    return Decision(best.slot, f"切到 {best.name}", best, "rule")


# --------------------------------------------------------------------------
# 8. 运镜触发器（每回合 / 每次安放各一次）
# --------------------------------------------------------------------------

@dataclass
class TriggerEvent:
    kind: str   # "round" | "bomb"
    key: str


class TriggerTracker:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._fired_round: set[str] = set()
        self._fired_bomb: set[str] = set()
        self._attempt: dict[str, float] = {}
        self._last_phase = ""
        self._auto_round = 0
        self.counts = {"round": 0, "bomb": 0}

    def round_key(self, snap: Snapshot) -> str:
        if snap.round_num is not None:
            return f"{snap.map_name}#{snap.round_num}"
        # 没有 map.round 时，用 freezetime 的上升沿自增一个序号
        if snap.phase == "freezetime" and self._last_phase != "freezetime":
            self._auto_round += 1
        return f"{snap.map_name}#auto{self._auto_round}"

    def poll(self, snap: Snapshot, now: float) -> list[TriggerEvent]:
        events: list[TriggerEvent] = []
        key = self.round_key(snap)
        retry = float(self.cfg["trigger_retry_seconds"])

        # 出生点运镜：freezetime 触发一次
        if snap.phase == "freezetime" and key not in self._fired_round:
            last = self._attempt.get("r:" + key)
            if last is None or now - last >= retry:
                self._attempt["r:" + key] = now
                events.append(TriggerEvent("round", key))

        # 包点运镜：已安放且剩余时间 <= 阈值，一次安放只触发一次
        bomb = snap.bomb
        if bomb and bomb.state in ("planted", "defusing") and bomb.countdown is not None:
            cd = bomb.countdown
            if 0 < cd <= float(self.cfg["bomb_cam_countdown"]) and key not in self._fired_bomb:
                last = self._attempt.get("b:" + key)
                if last is None or now - last >= retry:
                    self._attempt["b:" + key] = now
                    events.append(TriggerEvent("bomb", key))

        self._last_phase = snap.phase
        return events

    def confirm(self, ev: TriggerEvent) -> None:
        if ev.kind == "round":
            self._fired_round.add(ev.key)
            self.counts["round"] += 1
        else:
            self._fired_bomb.add(ev.key)
            self.counts["bomb"] += 1
        self._prune()

    def _prune(self) -> None:
        if len(self._fired_round) > 200:
            self._fired_round.clear()
            self._fired_bomb.clear()
            self._attempt.clear()


# --------------------------------------------------------------------------
# 9. Windows 窗口 / 进程 / 按键工具
# --------------------------------------------------------------------------

if IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetForegroundWindow.argtypes = []
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.SetFocus.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SW_RESTORE = 9


def get_foreground_hwnd() -> int:
    if not IS_WINDOWS:
        return 0
    return int(_user32.GetForegroundWindow() or 0)


def get_window_title(hwnd: int) -> str:
    if not IS_WINDOWS or not hwnd:
        return ""
    length = int(_user32.GetWindowTextLengthW(hwnd))
    buf = ctypes.create_unicode_buffer(max(1, length + 1))
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def is_window(hwnd: int) -> bool:
    return bool(IS_WINDOWS and hwnd and _user32.IsWindow(hwnd))


def get_window_pid(hwnd: int) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_process_exe(pid: int) -> str:
    if not IS_WINDOWS or not pid:
        return ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        _kernel32.CloseHandle(handle)


def is_game_process(hwnd: int) -> bool:
    """前台窗口是否真的属于 cs2.exe（防止把按键发到同名标题的窗口）。"""
    exe = get_process_exe(get_window_pid(hwnd))
    return Path(exe).name.lower() == "cs2.exe"


def enumerate_windows() -> list[tuple[int, str]]:
    if not IS_WINDOWS:
        return []
    found: list[tuple[int, str]] = []

    @_WNDENUMPROC
    def _cb(hwnd: int, _lparam: int) -> bool:
        try:
            if _user32.IsWindowVisible(hwnd):
                title = get_window_title(hwnd)
                if title:
                    found.append((int(hwnd), title))
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return found


def activate_window(hwnd: int) -> bool:
    """
    尽力把窗口拉到前台。Windows 对后台进程的 SetForegroundWindow 有限制，
    这里用 AttachThreadInput 组合 SetForegroundWindow/SetFocus，失败就返回 False
    （宁可跳过按键，也不要把数字键发到别的窗口）。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)
        fg = int(_user32.GetForegroundWindow() or 0)
        if fg == hwnd:
            return True
        cur_tid = int(_kernel32.GetCurrentThreadId())
        fg_tid = int(_user32.GetWindowThreadProcessId(fg, None)) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(_user32.AttachThreadInput(fg_tid, cur_tid, True))
        try:
            # 只用窗口 API，不额外模拟 ALT 等按键（对游戏的动作必须严格
            # 限制在 0-9 与两个 HLAE 快捷键内）。若系统前台锁拦截，失败即跳过。
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetFocus(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(fg_tid, cur_tid, False)
    except Exception:
        return False
    return int(_user32.GetForegroundWindow() or 0) == hwnd


def get_async_key_state(vk: int) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


@dataclass
class WindowInfo:
    hwnd: int
    title: str


def find_cs2_window(cfg: dict[str, Any]) -> Optional[WindowInfo]:
    key = str(cfg.get("window_title") or "Counter-Strike 2").lower()
    windows: list[WindowInfo] = []

    if HAS_PYGETWINDOW:
        try:
            for w in pygetwindow.getAllWindows():
                try:
                    title = w.title or ""
                    if w.visible and title and key in title.lower():
                        windows.append(WindowInfo(int(w._hWnd), title))
                except Exception:
                    continue
        except Exception:
            pass

    if not windows:
        for hwnd, title in enumerate_windows():
            if key in title.lower():
                windows.append(WindowInfo(hwnd, title))

    if not windows:
        return None

    # 只保留真正属于 cs2.exe 的窗口
    game_windows = [w for w in windows if is_game_process(w.hwnd)]
    if not game_windows:
        return None
    exact = [w for w in game_windows if w.title.strip().lower() == key]
    return (exact or game_windows)[0]


# --------------------------------------------------------------------------
# 10. 按键注入 + 焦点保证 + 自捕获抑制
# --------------------------------------------------------------------------

# 顶排数字键扫描码（与键盘布局无关），用于识别“是不是我自己按的”
SCAN_TO_DIGIT = {
    0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5",
    0x07: "6", 0x08: "7", 0x09: "8", 0x0A: "9", 0x0B: "0",
    # 小键盘也一并识别（若用户用默认绑定）
    0x4F: "1", 0x50: "2", 0x51: "3", 0x4B: "4", 0x4C: "5",
    0x4D: "6", 0x47: "7", 0x48: "8", 0x49: "9", 0x52: "0",
}

VK_DIGITS = {
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35,
    "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39, "0": 0x30,
    "n1": 0x61, "n2": 0x62, "n3": 0x63, "n4": 0x64, "n5": 0x65,
    "n6": 0x66, "n7": 0x67, "n8": 0x68, "n9": 0x69, "n0": 0x60,
}


class InputController:
    def __init__(self, cfg: dict[str, Any], log: LogBus) -> None:
        self.cfg = cfg
        self.log = log
        # 两个锁分开：钩子回调只碰 _suppress_lock，绝不能被“正在发送按键”
        # 的 _send_lock 阻塞，否则低级钩子会拖慢/卡住系统输入。
        self._send_lock = threading.Lock()
        self._suppress_lock = threading.Lock()
        self._suppress_until: dict[str, float] = {}
        self._window_cache: Optional[WindowInfo] = None
        self._window_cache_at = 0.0
        self._last_focus_fail_log = 0.0
        self._proc_cache_at = 0.0
        self._proc_cache_ok = False
        if HAS_PYDIRECTINPUT and pydirectinput is not None:
            try:
                pydirectinput.FAILSAFE = False  # 鼠标在屏幕角落时不要抛异常
                pydirectinput.PAUSE = 0.0
            except Exception:
                pass

    # ---- 自捕获抑制 ----
    def mark_self_press(self, digit: str) -> None:
        with self._suppress_lock:
            self._suppress_until[digit] = time.monotonic() + float(self.cfg["self_press_guard"])

    def is_self_press(self, digit: str) -> bool:
        with self._suppress_lock:
            return time.monotonic() < self._suppress_until.get(digit, 0.0)

    # ---- 窗口 ----
    def _find_window(self, force: bool = False) -> Optional[WindowInfo]:
        now = time.monotonic()
        if (not force and self._window_cache is not None
                and now - self._window_cache_at < 2.0
                and is_window(self._window_cache.hwnd)):
            return self._window_cache
        win = find_cs2_window(self.cfg)
        self._window_cache = win
        self._window_cache_at = now
        return win

    def foreground_is_game(self, force: bool = False) -> bool:
        # 注意：激活窗口后必须 force=True 重新查询，否则 0.4s 缓存会把
        # “刚激活成功”误判成失败，导致第一次切换被跳过。
        now = time.monotonic()
        if not force and now - self._proc_cache_at < 0.4:
            return self._proc_cache_ok
        hwnd = get_foreground_hwnd()
        ok = bool(hwnd) and is_game_process(hwnd)
        self._proc_cache_at = now
        self._proc_cache_ok = ok
        return ok

    def ensure_foreground(self) -> bool:
        """
        按键前必须保证 CS2 在前台。找不到窗口或激活失败就跳过并记录，
        绝不盲目发送按键（否则数字键会落到浏览器/聊天框里）。
        """
        if self.foreground_is_game():
            return True
        win = self._find_window(force=True)
        if win is None:
            self._log_focus_fail("未找到 CS2 窗口（cs2.exe），跳过本次按键")
            return False
        if not bool(self.cfg.get("auto_focus", True)):
            self._log_focus_fail("CS2 不在前台且已关闭自动聚焦，跳过本次按键")
            return False
        activate_window(win.hwnd)
        for _ in range(8):
            if self.foreground_is_game(force=True):
                return True
            time.sleep(0.04)
        self._log_focus_fail("CS2 窗口激活失败（可能被系统前台锁拦截），跳过本次按键")
        return False

    def _log_focus_fail(self, msg: str) -> None:
        now = time.monotonic()
        if now - self._last_focus_fail_log > 3.0:
            self._last_focus_fail_log = now
            self.log.warning(msg)

    # ---- 按键 ----
    def press_slot(self, slot: int) -> bool:
        key = str(int(slot))
        if key not in DIGITS:
            return False
        if bool(self.cfg.get("dry_run")):
            self.log.info(f"[演练] 模拟按下观察者切换键 {key}")
            return True
        if not HAS_PYDIRECTINPUT or pydirectinput is None:
            self.log.warning("pydirectinput 未安装，无法模拟按键（请先安装依赖）")
            return False
        with self._send_lock:
            if not self.ensure_foreground():
                return False
            self.mark_self_press(key)
            try:
                pydirectinput.keyDown(key)
                time.sleep(0.03)  # 至少跨 1-2 帧，避免游戏轮询错过按键
                pydirectinput.keyUp(key)
            except Exception as e:
                self.log.warning(f"模拟数字键 {key} 失败：{type(e).__name__}: {e}")
                return False
        return True

    def send_hotkey(self, hotkey: str) -> bool:
        hotkey = (hotkey or "").strip()
        if not hotkey:
            return False
        if bool(self.cfg.get("dry_run")):
            self.log.info(f"[演练] 模拟 HLAE 快捷键 {hotkey}")
            return True
        with self._send_lock:
            if not self.ensure_foreground():
                return False
            if HAS_KEYBOARD and keyboard is not None:
                try:
                    keyboard.press_and_release(hotkey)
                    return True
                except Exception as e:
                    self.log.warning(f"发送快捷键 {hotkey} 失败：{type(e).__name__}: {e}")
                    return False
        # 没有 keyboard 时只支持单键
        if len(hotkey) == 1 and HAS_PYDIRECTINPUT and pydirectinput is not None:
            try:
                pydirectinput.press(hotkey)
                return True
            except Exception:
                return False
        self.log.warning("未安装 keyboard，无法发送组合快捷键")
        return False


class ManualKeyWatcher:
    """
    监听用户手动按 0-9：低级键盘钩子优先，装不上就退化为 GetAsyncKeyState 轮询。
    注意：钩子回调绝对不能 return True（那会把按键吞掉，游戏就收不到了）。
    """

    def __init__(self, on_manual: Callable[[str], None],
                 is_self_press: Callable[[str], bool], log: LogBus) -> None:
        self._on_manual = on_manual
        self._is_self = is_self_press
        self.log = log
        self._hook: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_hit: dict[str, float] = {}
        self.mode = "off"

    def start(self) -> None:
        if HAS_KEYBOARD and keyboard is not None:
            try:
                self._hook = keyboard.hook(self._on_event, suppress=False)
                self.mode = "hook"
                return
            except Exception as e:
                self.log.warning(f"键盘钩子安装失败：{type(e).__name__}: {e}，改用轮询检测")
        self.mode = "polling"
        self._thread = threading.Thread(target=self._poll_loop, name="manual-keys", daemon=True)
        self._thread.start()
        if not IS_WINDOWS:
            self.mode = "off"

    def stop(self) -> None:
        self._stop.set()
        if self._hook is not None and keyboard is not None:
            try:
                keyboard.unhook(self._hook)
            except Exception:
                pass
            self._hook = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _on_event(self, event: Any) -> None:
        try:
            if getattr(event, "event_type", "") != "down":
                return
            digit = SCAN_TO_DIGIT.get(int(getattr(event, "scan_code", 0) or 0))
            if digit is None:
                name = str(getattr(event, "name", "") or "").lower()
                if name in DIGITS:
                    digit = name
                elif len(name) == 2 and name.startswith("n") and name[1] in DIGITS:
                    digit = name[1]
            if digit is not None:
                self._handle(digit)
        except Exception:
            pass

    def _poll_loop(self) -> None:
        prev = {k: False for k in VK_DIGITS}
        while not self._stop.is_set():
            for name, vk in VK_DIGITS.items():
                down = get_async_key_state(vk)
                if down and not prev.get(name, False):
                    digit = name[1] if name.startswith("n") else name
                    self._handle(digit)
                prev[name] = down
            time.sleep(0.02)

    def _handle(self, digit: str) -> None:
        now = time.monotonic()
        if self._is_self(digit):
            return  # 这是导播自己按的，不能当成手动接管
        if now - self._last_hit.get(digit, 0.0) < 0.35:
            return  # 长按自动重复去抖
        self._last_hit[digit] = now
        try:
            self._on_manual(digit)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 11. DeepSeek 仲裁（可选，规则粗筛 + AI 仲裁）
# --------------------------------------------------------------------------

def build_ai_prompt(snap: Snapshot, cands: list[Candidate]) -> str:
    bomb_txt = ""
    if snap.bomb and snap.bomb.state in ("planted", "defusing") and snap.bomb.countdown is not None:
        bomb_txt = f"，炸弹已安放，剩余 {snap.bomb.countdown:.1f} 秒"
    lines = [
        f"地图 {snap.map_name}，回合 {snap.round_num if snap.round_num is not None else '?'}，"
        f"比分 CT {snap.score_ct} : {snap.score_t}{bomb_txt}。",
        "候选选手（已按规则分排序）：",
    ]
    for c in cands[:3]:
        lines.append(
            f"slot={c.slot} 昵称={c.name} HP={c.hp} 护甲={c.armor} "
            f"武器={c.weapon} 距最近敌人={c.distance:.0f} 我瞄准敌人={c.aim_self:.2f} "
            f"敌人瞄准我={c.aim_enemy:.2f} 敌人HP={c.enemy_hp} 敌人武器={c.enemy_weapon} "
            f"敌人刚受伤={'是' if c.enemy_damaged else '否'} 规则分={c.score:.2f}"
        )
    lines.append(
        "请判断未来 1-3 秒内最可能拿到击杀的选手，只从上面的 slot 中选一个；"
        "如果都不像即将击杀，slot=-1。"
    )
    lines.append('只输出 JSON，不要解释，格式：{"slot": 数字, "reason": "不超过20字"}')
    return "\n".join(lines)


def parse_ai_response(text: Optional[str]) -> tuple[Optional[int], str]:
    if not text:
        return None, "空响应"
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    obj: Any = None
    try:
        obj = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return None, "响应不是 JSON"
    slot = obj.get("slot", obj.get("observer_slot"))
    try:
        slot_i = int(slot)
    except (TypeError, ValueError):
        return None, "slot 无效"
    reason = str(obj.get("reason", ""))[:60]
    if slot_i < 0:
        return None, reason or "AI 认为无目标"
    return slot_i, reason


class AIArbiter:
    """后台线程调用 DeepSeek；任何异常都只记录并回退规则，绝不阻塞导播循环。"""

    def __init__(self, cfg: dict[str, Any], log: LogBus) -> None:
        self.cfg = cfg
        self.log = log
        self._q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._result: Optional[tuple[int, float, str]] = None
        self._last_call = 0.0
        self._call_times: deque[float] = deque()
        self.calls = 0
        self.errors = 0
        self.last_error = ""
        self.last_latency = 0.0
        self._client: Any = None
        self._client_sig: tuple[str, str] = ("", "")
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("ai_enabled")) and bool(str(self.cfg.get("deepseek_api_key") or "").strip())

    def start(self) -> None:
        if not self.enabled:
            self.log.info("未启用 DeepSeek 仲裁（无 API Key 或已关闭），仅使用规则引擎。")
            return
        self._thread = threading.Thread(target=self._worker, name="ai-arbiter", daemon=True)
        self._thread.start()
        self.log.info(f"DeepSeek 仲裁已启用，模型 {self.cfg.get('deepseek_model')}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def request(self, snap: Snapshot, cands: list[Candidate], now: float) -> None:
        if not self.enabled or not cands:
            return
        if now - self._last_call < float(self.cfg["ai_min_interval"]):
            return
        self._call_times = deque(t for t in self._call_times if now - t < 60.0)
        if len(self._call_times) >= int(self.cfg["ai_max_per_minute"]):
            return
        req = {
            "ts": now,
            "prompt": build_ai_prompt(snap, cands),
            "slots": {c.slot for c in cands},
        }
        try:
            self._q.put_nowait(req)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(req)
            except Exception:
                pass

    def fresh(self, now: float) -> Optional[tuple[int, float, str]]:
        with self._lock:
            if self._result is None:
                return None
            slot, ts, reason = self._result
            if now - ts > float(self.cfg["ai_result_ttl"]):
                return None
            return slot, ts, reason

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                req = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop.is_set():
                break
            now = time.monotonic()
            if now - float(req["ts"]) > 2.0:
                continue  # 过期请求直接丢弃
            slot, reason = self._ask(str(req["prompt"]), set(req["slots"]))
            if slot is not None:
                with self._lock:
                    self._result = (slot, time.monotonic(), reason)

    def _ask(self, prompt: str, allowed: set[int]) -> tuple[Optional[int], str]:
        key = str(self.cfg.get("deepseek_api_key") or "").strip()
        base = str(self.cfg.get("deepseek_base_url") or "").strip()
        model = str(self.cfg.get("deepseek_model") or "deepseek-chat").strip()
        t0 = time.monotonic()
        self._call_times.append(t0)
        self._last_call = t0
        self.calls += 1
        try:
            from openai import OpenAI
        except Exception as e:
            self._fail(f"openai 未安装：{e}")
            return None, ""
        try:
            sig = (key, base)
            if self._client is None or self._client_sig != sig:
                self._client = OpenAI(
                    api_key=key,
                    base_url=base or None,
                    timeout=float(self.cfg["ai_timeout"]),
                    max_retries=0,
                )
                self._client_sig = sig
            content = self._request_chat(model, prompt, json_mode=True)
        except Exception:
            # 有些兼容端点不支持 response_format，去掉再试一次
            try:
                content = self._request_chat(model, prompt, json_mode=False)
            except Exception as e2:
                self._fail(f"{type(e2).__name__}: {e2}")
                return None, ""
        self.last_latency = time.monotonic() - t0
        if not content:
            self._fail("API 返回空 content")
            return None, ""
        slot, reason = parse_ai_response(content)
        if slot is None:
            self.log.info(f"AI 未给出有效目标（{reason}），继续使用规则引擎")
            return None, ""
        if slot not in allowed:
            self.log.warning(f"AI 返回了候选之外的 slot={slot}，忽略")
            return None, ""
        self.log.info(f"AI 仲裁建议 slot={slot}（{reason}，{self.last_latency:.2f}s）")
        return slot, reason

    def _request_chat(self, model: str, prompt: str, json_mode: bool) -> Optional[str]:
        assert self._client is not None
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是 CS2 电竞导播助手，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 100,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        if not getattr(resp, "choices", None):
            return None
        msg = resp.choices[0].message
        return getattr(msg, "content", None)

    def _fail(self, msg: str) -> None:
        self.errors += 1
        self.last_error = msg[:200]
        self.log.warning(f"DeepSeek 仲裁失败，已回退规则引擎：{msg}")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "calls": self.calls,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_latency": self.last_latency,
        }


# --------------------------------------------------------------------------
# 12. 导播引擎（线程编排）
# --------------------------------------------------------------------------

class DirectorEngine:
    """
    线程模型：
      - GSI HTTP 服务线程：解析推送，写入 SnapshotStore；
      - 导播循环线程：20Hz，读快照 → 运镜触发 → 评分 → 切换；
      - 手动按键监听线程：keyboard 钩子（或 GetAsyncKeyState 轮询）；
      - DeepSeek 仲裁线程：只在规则前两名难分时调用，失败自动回退。
    任何一条链路挂掉都只降级，不会让进程崩溃。
    """

    def __init__(self, cfg: dict[str, Any], bus: LogBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.store = SnapshotStore()
        self.server: Optional[GSIServer] = None
        self.input = InputController(cfg, bus)
        self.arbiter = AIArbiter(cfg, bus)
        self.triggers = TriggerTracker(cfg)
        self.watcher: Optional[ManualKeyWatcher] = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {"running": False}

        self._last_seq = -1
        self._hp: dict[str, int] = {}
        self._damage_at: dict[str, float] = {}
        self._manual_until = 0.0
        self._last_switch_at: Optional[float] = None
        self._last_switch_slot: Optional[int] = None
        self._bomb_cam_until = 0.0
        self._stale_active = False
        self._last_spectate_log = 0.0
        self._last_trigger_fail = 0.0
        self._last_reason = ""
        self._press_retry_after = 0.0

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._running:
            return
        port = int(self.cfg["gsi_port"])
        server = GSIServer("127.0.0.1", port, self.store, self.bus.warning)
        server.start()  # 绑定失败会抛 RuntimeError，由 GUI 提示
        self.server = server
        try:
            self.arbiter.start()
            self.watcher = ManualKeyWatcher(self._on_manual_key, self.input.is_self_press, self.bus)
            self.watcher.start()
        except Exception:
            server.stop()
            raise
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="director", daemon=True)
        self._thread.start()
        self.bus.info(
            f"导播已启动：GSI 127.0.0.1:{port}，循环 {self.cfg['loop_hz']}Hz，"
            f"手动接管检测={self.watcher.mode if self.watcher else 'off'}，"
            f"演练={'开' if self.cfg.get('dry_run') else '关'}"
        )

    def stop(self) -> None:
        if not self._running and self.server is None:
            return
        self._stop.set()
        self._running = False
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self.arbiter.stop()
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._status_lock:
            self._status = {"running": False}
        self.bus.info("导播已停止。")

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    # ---- 手动接管 ----
    def _on_manual_key(self, digit: str) -> None:
        now = time.monotonic()
        pause = float(self.cfg["manual_pause_seconds"])
        self._manual_until = max(self._manual_until, now + pause)
        self.bus.info(f"检测到手动切换（按 {digit}），AI 自动切换暂停 {pause:.0f} 秒。")

    # ---- 主循环 ----
    def _loop(self) -> None:
        hz = max(5.0, min(60.0, float(self.cfg["loop_hz"])))
        interval = 1.0 / hz
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception:
                self.bus.exception("导播循环出现异常（已继续运行）")
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))

    def _tick(self) -> None:
        now = time.monotonic()
        snap = self.store.get()
        packets, _ = self.store.stats()
        status: dict[str, Any] = {
            "running": True,
            "gsi": "waiting",
            "gsi_age": None,
            "packets": packets,
            "phase": "-",
            "observed": None,
            "target": None,
            "target_name": "",
            "score": 0.0,
            "reason": "等待 GSI 数据…",
            "paused_for": max(0.0, self._manual_until - now),
            "bomb_cam_for": max(0.0, self._bomb_cam_until - now),
            "ai": self.arbiter.status(),
            "triggers": dict(self.triggers.counts),
            "spectating": False,
            "candidates": 0,
        }

        if snap is None:
            self._set_status(status)
            return

        age = max(0.0, now - snap.received_at)
        status["gsi_age"] = age
        status["phase"] = snap.phase or "?"
        status["observed"] = snap.observer_slot

        if age > float(self.cfg["gsi_stale_seconds"]):
            status["gsi"] = "stale"
            status["reason"] = f"GSI 数据中断 {age:.1f}s"
            if not self._stale_active:
                self._stale_active = True
                self.bus.warning(f"GSI 数据流中断（最后一条 {age:.1f}s 前），暂停决策等待恢复。")
            self._set_status(status)
            return

        if self._stale_active:
            self._stale_active = False
            self.bus.info("GSI 数据流已恢复。")
        status["gsi"] = "ok"

        if snap.seq != self._last_seq:
            self._update_damage(snap, now)
            self._last_seq = snap.seq

        self._handle_triggers(snap, now)
        status["triggers"] = dict(self.triggers.counts)
        status["bomb_cam_for"] = max(0.0, self._bomb_cam_until - now)
        status["paused_for"] = max(0.0, self._manual_until - now)
        status["spectating"] = snap.is_spectating

        try:
            self._decide_and_switch(snap, now, status)
        finally:
            status["ai"] = self.arbiter.status()
            self._set_status(status)

    def _set_status(self, status: dict[str, Any]) -> None:
        with self._status_lock:
            self._status = status

    # ---- 伤害追踪：用于判断“正在交火” ----
    def _update_damage(self, snap: Snapshot, now: float) -> None:
        for sid, p in snap.players.items():
            prev = self._hp.get(sid)
            if prev is not None and p.health < prev - 2:
                self._damage_at[sid] = now
            self._hp[sid] = p.health
        if len(self._damage_at) > 40:
            self._damage_at = {k: v for k, v in self._damage_at.items() if now - v < 15.0}

    def _damage_recent(self, sid: str) -> bool:
        return (time.monotonic() - self._damage_at.get(sid, -999.0)) <= 1.6

    # ---- 运镜 ----
    def _handle_triggers(self, snap: Snapshot, now: float) -> None:
        if not snap.is_spectating:
            # 本地玩家还活着时触发 HLAE 相机宏会干扰正常游戏，直接跳过
            return
        for ev in self.triggers.poll(snap, now):
            hotkey = (self.cfg["hlae_round_key"] if ev.kind == "round"
                      else self.cfg["hlae_bomb_key"])
            if not hotkey:
                continue
            ok = self.input.send_hotkey(str(hotkey))
            if ok:
                self.triggers.confirm(ev)
                if ev.kind == "round":
                    self.bus.info(f"回合开始（{snap.phase}），触发 HLAE 出生点运镜：{hotkey}")
                else:
                    grace = float(self.cfg["bomb_cam_grace"])
                    if grace > 0:
                        self._bomb_cam_until = now + grace
                    cd = snap.bomb.countdown if snap.bomb else None
                    self.bus.info(
                        f"炸弹剩余 {cd:.1f}s，触发 HLAE 包点运镜：{hotkey}"
                        + (f"（AI 切换让位 {grace:.1f}s）" if grace > 0 else "")
                    )
            else:
                if now - self._last_trigger_fail > 3.0:
                    self._last_trigger_fail = now
                    self.bus.warning(f"{ev.kind} 运镜快捷键发送失败，稍后重试（窗口未聚焦？）")

    # ---- 核心：评分 + 决策 + 按键 ----
    def _decide_and_switch(self, snap: Snapshot, now: float,
                           status: dict[str, Any]) -> None:
        if not snap.players:
            status["reason"] = "GSI 中缺少 allplayers 数据"
            return
        if not snap.is_spectating:
            # 本地玩家存活时数字键是换武器，绝对不能按
            status["reason"] = "未处于观战者第一视角（本地玩家可能存活），为安全不按键"
            if now - self._last_spectate_log > 10.0:
                self._last_spectate_log = now
                self.bus.warning("当前不是观战视角，已跳过自动切换（避免误切武器）。")
            return
        if snap.phase not in ("", "live"):
            status["reason"] = f"阶段 {snap.phase}，不自动切换"
            return
        if now < self._manual_until:
            status["reason"] = f"手动接管中，剩余 {self._manual_until - now:.1f}s"
            return
        if now < self._bomb_cam_until:
            status["reason"] = f"包点运镜保护中，剩余 {self._bomb_cam_until - now:.1f}s"
            return
        if not any(p.pos for p in snap.players.values() if p.health > 0):
            status["reason"] = "缺少 allplayers_position，无法判断距离（请重装 GSI 配置）"
            return

        cands = score_candidates(snap, self.cfg, self._damage_recent, now)
        status["candidates"] = len(cands)
        if not cands:
            status["reason"] = "无交火迹象"
            self._last_reason = "无交火迹象"
            return
        status["score"] = cands[0].score

        # AI 建议只用于“规则前两名难分”的场景；这里先按建议重排，
        # 最终仍由 choose_slot 做阈值/迟滞/冷却检查，AI 不能绕过安全约束。
        hint = self.arbiter.fresh(now)
        ordered = cands
        if hint:
            slot = hint[0]
            idx = next((i for i, c in enumerate(cands) if c.slot == slot), None)
            # 只有当 AI 推荐的目标本身也过阈值时才采信，否则会把一个
            # 低分目标顶到队首，反而压掉规则引擎本来正确的选择。
            if (idx is not None and idx > 0
                    and cands[idx].score >= float(self.cfg["switch_min_score"]) * 0.9):
                ordered = [cands[idx]] + cands[:idx] + cands[idx + 1:]

        decision = choose_slot(
            ordered, snap.observer_slot, now,
            self._last_switch_at, self._last_switch_slot, self.cfg,
        )
        status["reason"] = decision.reason

        if decision.slot is not None:
            if now < self._press_retry_after:
                # 按键失败后 1 秒内不再重试，避免每帧都去抢焦点
                status["reason"] = "按键失败，等待重试"
                return
            cand = decision.candidate
            ok = self.input.press_slot(decision.slot)
            if ok:
                self._last_switch_at = now
                self._last_switch_slot = decision.slot
                status["target"] = decision.slot
                status["target_name"] = cand.name if cand else ""
                source = "AI" if (hint and hint[0] == decision.slot) else "规则"
                self.bus.info(
                    f"切换视角 → slot={decision.slot}（{cand.name if cand else '?'}）"
                    f"[{source}] " + (format_candidate(cand) if cand else "")
                )
                self._last_reason = decision.reason
            else:
                self._press_retry_after = now + 1.0
                status["reason"] = "按键未发送：窗口未聚焦或依赖缺失（详见日志）"
        else:
            if decision.reason != self._last_reason:
                self._last_reason = decision.reason
            # 显示 AI 当前认可的目标：刚切过的目标优先（GSI observer_slot 有延迟）
            shown = None
            if self._last_switch_at is not None and now - self._last_switch_at < 5.0:
                shown = self._last_switch_slot
            if shown is None:
                shown = snap.observer_slot
            if shown is not None:
                status["target"] = shown
                status["target_name"] = next(
                    (c.name for c in cands if c.slot == shown), "")

        # 规则前两名接近时才请求 AI，控制成本与延迟
        if self.arbiter.enabled and len(cands) >= 2:
            gap = cands[0].score - cands[1].score
            if (cands[0].score >= float(self.cfg["switch_min_score"])
                    and gap <= float(self.cfg["ai_ambiguity_margin"])):
                self.arbiter.request(snap, cands[:3], now)


# --------------------------------------------------------------------------
# 13. GSI 配置安装 / CS2 路径探测
# --------------------------------------------------------------------------

def gsi_config_dict(port: int) -> dict[str, Any]:
    """生成 GSI 配置文件内容；data 字段必须覆盖需求列出的所有项。"""
    return {
        "uri": f"http://127.0.0.1:{int(port)}",
        "timeout": 5.0,
        "buffer": 0.1,
        "throttle": 0.1,
        "heartbeat": 10.0,
        "data": {name: 1 for name in GSI_REQUIRED_DATA},
    }


def derive_cfg_dir(exe_path: str) -> Optional[Path]:
    """
    从 cs2.exe 推导 game/csgo/cfg。兼容两种安装布局：
      ...\\game\\cs2.exe
      ...\\game\\bin\\win64\\cs2.exe
    """
    if not exe_path:
        return None
    exe = Path(exe_path)
    if exe.name.lower() != "cs2.exe":
        return None
    cur = exe.parent
    for _ in range(5):
        cand = cur / "csgo" / "cfg"
        if cand.is_dir():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def install_gsi_config(cfg_dir: Path, port: int, overwrite: bool) -> Path:
    """写入 GSI cfg；已存在时按 overwrite 决定是否覆盖，并保留时间戳备份。"""
    target = Path(cfg_dir) / GSI_CFG_NAME
    if target.exists():
        if not overwrite:
            raise FileExistsError(str(target))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = target.with_name(target.name + f".bak_{stamp}")
        try:
            backup.write_bytes(target.read_bytes())
        except Exception:
            pass
    text = json.dumps(gsi_config_dict(port), ensure_ascii=False, indent=4) + "\n"
    target.write_text(text, encoding="utf-8")
    # 立刻回读校验，防止写入半截/编码问题
    check = json.loads(target.read_text(encoding="utf-8"))
    if "allplayers_position" not in (check.get("data") or {}):
        raise RuntimeError("GSI 配置校验失败：缺少 allplayers_position")
    return target


def _steam_roots_from_registry() -> list[Path]:
    if not IS_WINDOWS:
        return []
    import winreg

    specs = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    roots: list[Path] = []
    for hive, key, value in specs:
        try:
            with winreg.OpenKey(hive, key) as k:
                v, _ = winreg.QueryValueEx(k, value)
                if v:
                    roots.append(Path(str(v).replace("\\", "/")))
        except Exception:
            continue
    return roots


def _steam_libraries(root: Path) -> list[Path]:
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'"path"\s*"([^"]+)"', text):
                p = Path(m.group(1).replace("\\\\", "\\"))
                if p.is_dir():
                    libs.append(p)
        except Exception:
            pass
    return libs


def detect_cs2_executables() -> list[Path]:
    """注册表 Steam 路径 + libraryfolders.vdf + 常见盘符 SteamLibrary 扫描。"""
    roots = _steam_roots_from_registry()
    roots += [Path(r"C:/Program Files (x86)/Steam"), Path(r"C:/Steam")]
    libs: list[Path] = []
    for r in roots:
        try:
            if r.is_dir():
                libs.extend(_steam_libraries(r))
        except Exception:
            continue

    found: list[Path] = []

    def _probe(lib: Path) -> None:
        base = lib / "steamapps" / "common" / "Counter-Strike Global Offensive"
        for rel in ("game/bin/win64/cs2.exe", "game/cs2.exe", "cs2.exe"):
            p = base / rel
            try:
                if p.is_file() and p not in found:
                    found.append(p)
            except Exception:
                continue

    for lib in libs:
        _probe(lib)

    # 常见盘符直扫（只查固定目录，不做全盘遍历）
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:/")
        try:
            if not drive.exists():
                continue
        except Exception:
            continue
        for rel in ("SteamLibrary", "Steam", "Program Files (x86)/Steam",
                    "Program Files/Steam", "Games/SteamLibrary"):
            _probe(drive / rel)

    return found


# --------------------------------------------------------------------------
# 14. GUI
# --------------------------------------------------------------------------

class DirectorApp:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.bus = LogBus()
        self.engine: Optional[DirectorEngine] = None
        self._closing = False
        self._detecting = False

        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} v{VERSION}")
        self.root.geometry("1000x780")
        self.root.minsize(900, 740)
        self._init_style()

        self.var_exe = tk.StringVar(value=str(cfg.get("cs2_exe", "")))
        self.var_round = tk.StringVar(value=str(cfg.get("hlae_round_key", "")))
        self.var_bomb = tk.StringVar(value=str(cfg.get("hlae_bomb_key", "")))
        self.var_api = tk.StringVar(value=str(cfg.get("deepseek_api_key", "")))
        self.var_model = tk.StringVar(value=str(cfg.get("deepseek_model", "deepseek-chat")))
        self.var_ai = tk.BooleanVar(value=bool(cfg.get("ai_enabled", True)))
        self.var_dry = tk.BooleanVar(value=bool(cfg.get("dry_run", False)))
        self.var_port = tk.StringVar(value=str(cfg.get("gsi_port", 3000)))
        self.var_score = tk.StringVar(value=str(cfg.get("switch_min_score", 0.30)))
        self.var_pause = tk.StringVar(value=str(cfg.get("manual_pause_seconds", 10.0)))

        self._build()
        self._refresh_deps()
        self._refresh_gsi_status()
        self.bus.info(f"配置目录：{app_data_dir()}")
        self.bus.info("使用步骤：选择 cs2.exe → 一键安装 GSI → 重启 CS2 → 填写 HLAE 快捷键 → 启动导播。")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._poll)

    def _init_style(self) -> None:
        try:
            self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
            style = ttk.Style()
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass

    # ---- 界面 ----
    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        # 设置 / 日志分页：设置页保持紧凑，日志页占满剩余高度。
        notebook = ttk.Notebook(outer)
        notebook.grid(row=0, column=0, sticky="nsew")
        settings = ttk.Frame(notebook, padding=8)
        logs = ttk.Frame(notebook, padding=6)
        notebook.add(settings, text=" 设置 ")
        notebook.add(logs, text=" 日志 ")
        settings.columnconfigure(0, weight=1)

        # 1) 游戏与 GSI
        lf = ttk.LabelFrame(settings, text=" 1. 游戏与 GSI 配置 ", padding=8)
        lf.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(1, weight=1)
        ttk.Label(lf, text="cs2.exe").grid(row=0, column=0, sticky="w")
        ttk.Entry(lf, textvariable=self.var_exe).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(lf, text="自动检测", width=9, command=self._auto_detect).grid(row=0, column=2)
        ttk.Button(lf, text="浏览…", width=8, command=self._browse_exe).grid(row=0, column=3, padx=(4, 0))

        self.lbl_gsi = ttk.Label(lf, text="GSI 配置：检查中…", foreground="#555555")
        self.lbl_gsi.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        row = ttk.Frame(lf)
        row.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(row, text="一键安装 GSI", command=self._install_gsi).pack(side="left")
        ttk.Button(row, text="打开 cfg 目录", command=self._open_cfg_dir).pack(side="left", padx=6)
        ttk.Label(row, text="安装/覆盖后需要完全重启 CS2 才会生效", foreground="#a15c00").pack(
            side="left", padx=6)

        # 2) HLAE
        lf2 = ttk.LabelFrame(settings, text=" 2. HLAE 运镜快捷键（与 HLAE 内绑定一致） ", padding=8)
        lf2.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        lf2.columnconfigure(1, weight=1)
        lf2.columnconfigure(3, weight=1)
        ttk.Label(lf2, text="出生点运镜").grid(row=0, column=0, sticky="w")
        ttk.Entry(lf2, textvariable=self.var_round).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(lf2, text="包点运镜").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Entry(lf2, textvariable=self.var_bomb).grid(row=0, column=3, sticky="ew", padx=6)
        ttk.Label(lf2, text="示例：f6、f7、ctrl+shift+f9；不要使用 0-9 单个数字键。",
                  foreground="#555555").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 3) AI
        lf3 = ttk.LabelFrame(settings, text=" 3. DeepSeek 仲裁（可选） ", padding=8)
        lf3.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        lf3.columnconfigure(1, weight=1)
        ttk.Label(lf3, text="API Key").grid(row=0, column=0, sticky="w")
        ttk.Entry(lf3, textvariable=self.var_api, show="*").grid(
            row=0, column=1, sticky="ew", padx=6, columnspan=3)
        ttk.Label(lf3, text="模型").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(lf3, textvariable=self.var_model, width=24).grid(
            row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Checkbutton(lf3, text="启用 AI 仲裁（留空 Key 则自动只用规则引擎）",
                        variable=self.var_ai).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(lf3, text="演练模式（不实际发送按键）",
                        variable=self.var_dry).grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(lf3, text="成本控制：仅在规则前两名分差很小时调用，默认最多 12 次/分钟，超时 2.5s 自动回退。",
                  foreground="#555555").grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 4) 高级
        lf4 = ttk.LabelFrame(settings, text=" 4. 高级参数 ", padding=8)
        lf4.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(lf4, text="GSI 端口").grid(row=0, column=0, sticky="w")
        ttk.Entry(lf4, textvariable=self.var_port, width=8).grid(row=0, column=1, sticky="w", padx=(4, 16))
        ttk.Label(lf4, text="切换阈值").grid(row=0, column=2, sticky="w")
        ttk.Entry(lf4, textvariable=self.var_score, width=8).grid(row=0, column=3, sticky="w", padx=(4, 16))
        ttk.Label(lf4, text="接管暂停(秒)").grid(row=0, column=4, sticky="w")
        ttk.Entry(lf4, textvariable=self.var_pause, width=8).grid(row=0, column=5, sticky="w", padx=(4, 0))
        ttk.Label(lf4, text="阈值越低越容易切；0.30 适合大多数情况。其余参数见 config.json。",
                  foreground="#555555").grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # 5) 依赖
        lf5 = ttk.LabelFrame(settings, text=" 5. 依赖状态 ", padding=8)
        lf5.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        lf5.columnconfigure(0, weight=1)
        self.lbl_deps = ttk.Label(lf5, text="检查中…", wraplength=760, justify="left")
        self.lbl_deps.grid(row=0, column=0, sticky="w")
        row = ttk.Frame(lf5)
        row.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(row, text="重新检查", command=self._refresh_deps).pack(side="left")
        ttk.Button(row, text="安装缺失依赖", command=self._install_deps_async).pack(side="left", padx=6)

        # 6) 控制条（始终可见）
        ctrl = ttk.Frame(outer)
        ctrl.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        self.btn_start = ttk.Button(ctrl, text="▶ 启动导播", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(ctrl, text="■ 停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(ctrl, text="保存配置", command=self._save_only).pack(side="left")
        self.lbl_conn = ttk.Label(ctrl, text="GSI：未启动", foreground="#1a4f8a")
        self.lbl_conn.pack(side="right")

        # 7) 运行状态（始终可见）
        status_box = ttk.LabelFrame(outer, text=" 运行状态 ", padding=8)
        status_box.grid(row=2, column=0, sticky="ew")
        status_box.columnconfigure(0, weight=1)
        self.lbl_play = ttk.Label(status_box, text="阶段：-    当前视角：-    AI 目标：-",
                                  justify="left", wraplength=940)
        self.lbl_play.grid(row=0, column=0, sticky="w")
        self.lbl_ai = ttk.Label(status_box, text="AI：未启动    运镜：-", foreground="#444444",
                                justify="left", wraplength=940)
        self.lbl_ai.grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.lbl_reason = ttk.Label(status_box, text="决策：-", foreground="#666666",
                                    justify="left", wraplength=940)
        self.lbl_reason.grid(row=2, column=0, sticky="w", pady=(3, 0))

        # 8) 日志页
        logs.rowconfigure(0, weight=1)
        logs.columnconfigure(0, weight=1)
        self.txt = scrolledtext.ScrolledText(
            logs, height=10, state="disabled", wrap="word",
            font=("Consolas", 9), background="#111418", foreground="#d6d6d6",
            insertbackground="#d6d6d6",
        )
        self.txt.grid(row=0, column=0, sticky="nsew")
        self.txt.tag_configure("WARN", foreground="#ffcc66")
        self.txt.tag_configure("ERROR", foreground="#ff6b6b")
        self.txt.tag_configure("INFO", foreground="#d6d6d6")

    # ---- 依赖 ----
    def _refresh_deps(self) -> None:
        try:
            parts = []
            for d in check_dependencies():
                mark = "✓" if d["installed"] else "✗ 缺失"
                ver = f" {d['version']}" if d.get("version") else ""
                parts.append(f"{d['module']}{ver} {mark}")
            self.lbl_deps.configure(text="依赖：" + "    ".join(parts))
        except Exception:
            self.lbl_deps.configure(text="依赖检查失败，请查看日志。")

    def _install_deps_async(self) -> None:
        if self._detecting:
            return
        self._detecting = True
        self.bus.info("开始安装缺失依赖…")

        def worker() -> None:
            ok = install_dependencies(lambda line: self.bus.info("pip | " + line))
            self.root.after(0, lambda: self._deps_done(ok))

        threading.Thread(target=worker, name="pip-install", daemon=True).start()

    def _deps_done(self, ok: bool) -> None:
        self._detecting = False
        self._refresh_deps()
        if ok:
            messagebox.showinfo("依赖安装", "依赖安装完成。\n如果刚安装了 pydirectinput/keyboard，建议重启本程序。")
        else:
            messagebox.showerror("依赖安装", "安装失败，请查看日志或手动执行：\n"
                                 "python -m pip install pydirectinput keyboard pygetwindow openai")

    # ---- 路径 ----
    def _auto_detect(self) -> None:
        if self._detecting:
            return
        self._detecting = True
        self.bus.info("正在检测 cs2.exe…")

        def worker() -> None:
            try:
                found = detect_cs2_executables()
            except Exception:
                found = []
            self.root.after(0, lambda: self._detect_done(found))

        threading.Thread(target=worker, name="detect-cs2", daemon=True).start()

    def _detect_done(self, found: list[Path]) -> None:
        self._detecting = False
        if found:
            self.var_exe.set(str(found[0]))
            self.bus.info(f"检测到 cs2.exe：{found[0]}")
            self._refresh_gsi_status()
            if len(found) > 1:
                self.bus.info(f"另有 {len(found) - 1} 个候选路径，可在“浏览…”中手动选择。")
        else:
            self.bus.warning("未自动检测到 cs2.exe，请用“浏览…”手动选择。")
            messagebox.showwarning("自动检测", "未找到 cs2.exe。\n请点击“浏览…”手动选择。")

    def _browse_exe(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 cs2.exe",
            filetypes=[("cs2.exe", "cs2.exe"), ("可执行文件", "*.exe")],
        )
        if path:
            self.var_exe.set(path)
            self._refresh_gsi_status()

    def _refresh_gsi_status(self) -> None:
        exe = self.var_exe.get().strip()
        if not exe:
            self.lbl_gsi.configure(text="GSI 配置：未选择 cs2.exe", foreground="#555555")
            return
        cfg_dir = derive_cfg_dir(exe)
        if cfg_dir is None:
            self.lbl_gsi.configure(
                text="GSI 配置：路径无法推导 game/csgo/cfg，请确认选择的是 cs2.exe",
                foreground="#b00020",
            )
            return
        target = cfg_dir / GSI_CFG_NAME
        if target.is_file():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                uri = str(data.get("uri", ""))
                port = data.get("data", {})
                ok_pos = "allplayers_position" in port
                self.lbl_gsi.configure(
                    text=f"GSI 配置：已安装 → {target}   ({uri}{'' if ok_pos else '，缺少 allplayers_position'})",
                    foreground="#1b7f3b" if ok_pos else "#b00020",
                )
            except Exception:
                self.lbl_gsi.configure(text=f"GSI 配置：已存在但无法解析 → {target}", foreground="#b00020")
        else:
            self.lbl_gsi.configure(text=f"GSI 配置：未安装（目标 {target}）", foreground="#a15c00")

    def _install_gsi(self) -> None:
        exe = self.var_exe.get().strip()
        if not exe:
            messagebox.showwarning("安装 GSI", "请先选择 cs2.exe。")
            return
        cfg_dir = derive_cfg_dir(exe)
        if cfg_dir is None:
            messagebox.showerror(
                "安装 GSI",
                "无法从该路径推导出 game/csgo/cfg。\n"
                "请确认选择的是 CS2 安装目录下的 cs2.exe（通常在 game 或 game/bin/win64 下）。",
            )
            return
        try:
            port = int(self.var_port.get().strip())
            if not (1024 <= port <= 65535):
                raise ValueError
        except Exception:
            messagebox.showerror("安装 GSI", "端口必须是 1024-65535 之间的整数。")
            return
        target = cfg_dir / GSI_CFG_NAME
        if target.exists():
            if not messagebox.askyesno(
                "确认覆盖",
                f"已存在同名配置：\n{target}\n\n是否覆盖？（旧文件会备份为 .bak_时间戳）",
            ):
                return
        try:
            path = install_gsi_config(cfg_dir, port, overwrite=True)
        except Exception as e:
            self.bus.error(f"GSI 配置安装失败：{type(e).__name__}: {e}")
            messagebox.showerror("安装 GSI", f"安装失败：{e}")
            return
        self.bus.info(f"GSI 配置已写入：{path}（端口 {port}）")
        self._refresh_gsi_status()
        messagebox.showinfo(
            "安装成功",
            f"已写入：\n{path}\n\n请完全退出并重启 CS2，然后启动导播。\n"
            f"若仍无数据，请检查 CS2 是否带 -gamestateintegration 启动，以及防火墙是否拦截本机 127.0.0.1:{port}。",
        )

    def _open_cfg_dir(self) -> None:
        cfg_dir = derive_cfg_dir(self.var_exe.get().strip())
        if cfg_dir is None:
            messagebox.showwarning("打开目录", "请先选择有效的 cs2.exe。")
            return
        try:
            os.startfile(str(cfg_dir))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("打开目录", str(e))

    # ---- 启动 / 停止 ----
    def _collect_cfg(self) -> dict[str, Any]:
        cfg = copy.deepcopy(self.cfg)
        cfg["cs2_exe"] = self.var_exe.get().strip()
        cfg["hlae_round_key"] = self.var_round.get().strip()
        cfg["hlae_bomb_key"] = self.var_bomb.get().strip()
        cfg["deepseek_api_key"] = self.var_api.get().strip()
        cfg["deepseek_model"] = self.var_model.get().strip() or "deepseek-chat"
        cfg["ai_enabled"] = bool(self.var_ai.get())
        cfg["dry_run"] = bool(self.var_dry.get())
        try:
            cfg["gsi_port"] = int(self.var_port.get().strip())
        except Exception:
            cfg["gsi_port"] = int(DEFAULT_CONFIG["gsi_port"])
        try:
            cfg["switch_min_score"] = max(0.05, min(0.95, float(self.var_score.get().strip())))
        except Exception:
            cfg["switch_min_score"] = DEFAULT_CONFIG["switch_min_score"]
        try:
            cfg["manual_pause_seconds"] = max(0.0, min(120.0, float(self.var_pause.get().strip())))
        except Exception:
            cfg["manual_pause_seconds"] = DEFAULT_CONFIG["manual_pause_seconds"]
        return cfg

    def _validate_cfg(self, cfg: dict[str, Any]) -> bool:
        if not (1024 <= int(cfg["gsi_port"]) <= 65535):
            messagebox.showerror("配置错误", "GSI 端口必须是 1024-65535 之间的整数。")
            return False
        for label, key in (("出生点运镜", "hlae_round_key"), ("包点运镜", "hlae_bomb_key")):
            value = str(cfg[key]).strip()
            if not value:
                messagebox.showerror("配置错误", f"{label}快捷键不能为空。")
                return False
            if value in DIGITS:
                messagebox.showerror("配置错误", f"{label}快捷键不能是单个数字键（会与观察者切换冲突）。")
                return False
            if not validate_hotkey(value):
                messagebox.showerror("配置错误", f"{label}快捷键格式无法识别：{value}")
                return False
        if cfg["hlae_round_key"].lower() == cfg["hlae_bomb_key"].lower():
            if not messagebox.askyesno("确认", "两个 HLAE 快捷键相同，确定继续？"):
                return False
        if cfg["ai_enabled"] and not cfg["deepseek_api_key"]:
            if not messagebox.askyesno(
                "未填写 API Key",
                "未填写 DeepSeek API Key，将只使用规则引擎。\n是否继续启动？",
            ):
                return False
            cfg["ai_enabled"] = False
        if not cfg["cs2_exe"]:
            if not messagebox.askyesno("未选择 cs2.exe", "未选择 cs2.exe，仍要启动吗？（将找不到游戏窗口）"):
                return False
        elif not Path(cfg["cs2_exe"]).is_file():
            if not messagebox.askyesno("路径不存在", "cs2.exe 路径不存在，仍要启动吗？"):
                return False
        if not HAS_PYDIRECTINPUT and not cfg["dry_run"]:
            messagebox.showwarning("缺少依赖", "pydirectinput 未安装，无法模拟按键。\n请先安装依赖，或勾选“演练模式”。")
            return False
        if not HAS_KEYBOARD:
            messagebox.showwarning(
                "缺少 keyboard",
                "keyboard 未安装：手动接管检测会退化为轮询（仍可用），"
                "但 HLAE 组合快捷键可能发送失败。建议先安装依赖。",
            )
        return True

    def _start(self) -> None:
        if self.engine is not None:
            return
        cfg = self._collect_cfg()
        if not self._validate_cfg(cfg):
            return
        try:
            save_config(cfg)
        except Exception as e:
            # 配置写不进去（只读目录/杀软拦截）不应该阻止本次运行
            self.bus.warning(f"配置保存失败（将继续启动）：{type(e).__name__}: {e}")
        self.cfg = cfg
        try:
            engine = DirectorEngine(cfg, self.bus)
            engine.start()
        except Exception as e:
            self.bus.error(f"启动失败：{type(e).__name__}: {e}")
            messagebox.showerror("启动失败", str(e))
            return
        self.engine = engine
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_conn.configure(text="GSI：等待数据…", foreground="#1a4f8a")

    def _stop(self) -> None:
        if self.engine is None:
            return
        try:
            self.engine.stop()
        except Exception:
            self.bus.exception("停止导播时出现异常")
        self.engine = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.lbl_conn.configure(text="GSI：未启动", foreground="#555555")

    def _save_only(self) -> None:
        cfg = self._collect_cfg()
        try:
            save_config(cfg)
            self.cfg = cfg
            self.bus.info(f"配置已保存：{config_path()}")
            self._refresh_gsi_status()
            messagebox.showinfo("保存配置", f"已保存到：\n{config_path()}")
        except Exception as e:
            messagebox.showerror("保存配置", f"保存失败：{e}")

    # ---- 状态轮询 ----
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            self._drain_logs()
            status = self.engine.status() if self.engine else None
            if status and status.get("running"):
                self._render_status(status)
            elif self.engine is None:
                self.lbl_conn.configure(text="GSI：未启动", foreground="#555555")
                self.lbl_play.configure(text="阶段：-    当前视角：-    AI 目标：-")
                self.lbl_ai.configure(text="AI：未启动    运镜：-")
                self.lbl_reason.configure(text="决策：-")
        except Exception:
            pass
        self.root.after(200, self._poll)

    def _render_status(self, st: dict[str, Any]) -> None:
        gsi = st.get("gsi")
        age = st.get("gsi_age")
        packets = st.get("packets", 0)
        if gsi == "ok" and age is not None:
            text = f"GSI：已连接（{age:.1f}s 前，收包 {packets}）"
            color = "#1b7f3b"
        elif gsi == "stale" and age is not None:
            text = f"GSI：断流 {age:.1f}s"
            color = "#b00020"
        else:
            text = "GSI：等待数据…（检查是否重启 CS2）"
            color = "#a15c00"
        self.lbl_conn.configure(text=text, foreground=color)

        observed = st.get("observed")
        target = st.get("target")
        tname = st.get("target_name") or ""
        paused = float(st.get("paused_for") or 0)
        target_txt = f"slot={target} {tname}" if target is not None else "-"
        self.lbl_play.configure(
            text=f"阶段：{st.get('phase', '-')}    当前视角：{('slot=' + str(observed)) if observed is not None else '-'}"
                 f"    AI 目标：{target_txt}" + (f"    手动接管剩余：{paused:.1f}s" if paused > 0 else "")
        )

        ai = st.get("ai") or {}
        ai_txt = "关闭" if not ai.get("enabled") else f"开启 调用{ai.get('calls', 0)}/失败{ai.get('errors', 0)}"
        trig = st.get("triggers") or {}
        self.lbl_ai.configure(
            text=f"AI：{ai_txt}    运镜：出生点 {trig.get('round', 0)} 次 / 包点 {trig.get('bomb', 0)} 次"
                 + (f"    运镜保护 {st.get('bomb_cam_for', 0):.1f}s" if st.get("bomb_cam_for", 0) > 0 else "")
        )
        self.lbl_reason.configure(
            text=f"决策：{st.get('reason', '-')}    最高分：{float(st.get('score') or 0):.2f}    候选：{st.get('candidates', 0)}"
        )

    def _drain_logs(self) -> None:
        for _ in range(80):
            try:
                level, msg = self.bus.queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(level, msg)

    def _append_log(self, level: str, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.txt.configure(state="normal")
        self.txt.insert("end", f"[{stamp}] {msg}\n", level if level in ("WARN", "ERROR") else "INFO")
        # 控制日志长度，避免长时间运行后卡顿
        if int(self.txt.index("end-1c").split(".")[0]) > 800:
            self.txt.delete("1.0", "200.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    # ---- 关闭 ----
    def _on_close(self) -> None:
        self._closing = True
        try:
            if self.engine is not None:
                self.engine.stop()
                self.engine = None
        except Exception:
            pass
        try:
            save_config(self._collect_cfg())
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def validate_hotkey(hotkey: str) -> bool:
    """优先用 keyboard 自己的解析器校验组合键。"""
    hotkey = (hotkey or "").strip()
    if not hotkey:
        return False
    if HAS_KEYBOARD and keyboard is not None:
        try:
            keyboard.parse_hotkey(hotkey)
            return True
        except Exception:
            return False
    return bool(re.fullmatch(r"[A-Za-z0-9_+\-]+", hotkey))


# --------------------------------------------------------------------------
# 15. 命令行入口
# --------------------------------------------------------------------------

def print_env_report() -> None:
    import platform
    print("=" * 66)
    print(f"{APP_TITLE} v{VERSION} 环境自检")
    print("=" * 66)
    print(f"操作系统 : {platform.platform()}")
    print(f"Python   : {sys.version.split()[0]}  ({sys.executable})")
    print(f"配置目录 : {app_data_dir()}")
    print(f"日志文件 : {log_path()}")
    print("-" * 66)
    for d in check_dependencies():
        state = "OK" if d["installed"] else "MISSING"
        print(f"{d['module']:<14} {str(d.get('version') or '-'):<12} {state:<8} {d['purpose']}")
    print("=" * 66)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} v{VERSION}")
    parser.add_argument("--check", action="store_true", help="打印环境自检表后退出")
    parser.add_argument("--install-deps", action="store_true", help="安装缺失依赖后退出")
    args = parser.parse_args(argv)

    if args.check:
        print_env_report()
        return 0
    if args.install_deps:
        print_env_report()
        return 0 if install_dependencies(print) else 1

    if not HAS_TK:
        print("tkinter 不可用，无法启动 GUI。请安装带 Tk 的 Python。", file=sys.stderr)
        return 1

    if IS_WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高 DPI 下界面不糊
        except Exception:
            pass

    cfg = load_config()
    app = DirectorApp(cfg)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

