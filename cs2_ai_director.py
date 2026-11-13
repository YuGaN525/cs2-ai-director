#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 AI 导播（CS2 AI Director）
================================

一个只依赖 GSI（Game State Integration）数据的 CS2 观战导播：
  1) 自动把观战视角切到“未来几秒最可能造成击杀”的选手第一视角；
  2) 回合进入 freezetime 时触发 MulNX 出生点运镜宏（每回合一次）；
  3) 炸弹即将爆炸（countdown <= 8s）时触发 MulNX 包点运镜宏（每次安放一次）；
  4) 提供 tkinter GUI：检测/选择 cs2.exe、一键安装 GSI 配置、录入快捷键与 API Key。

硬性边界（代码里也反复强调）：
  * 只读取 GSI 的 HTTP 推送，不读内存、不注入、不发送游戏控制台命令；
  * 对游戏的全部“动作”只有两类：模拟 0-9 数字键、模拟两个用户录入的 MulNX 宏快捷键；
  * 不直接与 MulNX 通信。

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
from desktop_ui import schedule
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------
# 0. 基本常量 / 可选依赖
# --------------------------------------------------------------------------

# APP_NAME is the data/config folder name. Keep it unchanged so existing
# %APPDATA% data and the trainer keep working.
APP_NAME = "CS2AIDirector"
APP_TITLE = "Yuzi_CS2_AIDirector"
from client_version import VERSION, SLOGAN
APP_SUBTITLE = SLOGAN

UI_BG = "#f2ead9"
UI_CARD = "#fffdf8"
UI_INK = "#20242a"
UI_MUTED = "#6f6a60"
UI_GOLD = "#b8862f"
UI_GOLD_DARK = "#7a561c"
UI_HEADER = "#171a20"
UI_BORDER = "#e2d6bf"
IS_WINDOWS = os.name == "nt"

GSI_CFG_NAME = "gamestate_integration_ai_director.cfg"
DIGITS = "0123456789"


def is_admin() -> bool:
    """当前进程是否管理员；UIPI 会拦截对提升权限 CS2/MulNX 的模拟按键。"""
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

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

import backup_bundle as bundle_mod
import decision_calibration as cal_mod
import decision_model as dm
import director_narrative as narrative_mod
import director_pro as pro_mod
import director_story as story_mod

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
from director_core.config import DEFAULT_CONFIG

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


def asset_path(name: str) -> Path:
    """Resolve bundled assets in source and PyInstaller onefile modes."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / name


def config_path() -> Path:
    return app_data_dir() / "config.json"


def log_path() -> Path:
    return app_data_dir() / "director.log"


def load_config() -> dict[str, Any]:
    """读取本地 JSON，与默认配置合并（旧版本缺的键自动补默认值）。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = config_path()
    from arbitration_runtime import PersonalKeyStore, migrate_config
    try:
        migrate_config(path, PersonalKeyStore(app_data_dir()))
    except Exception:
        # Keep the source recoverable; never enable a key whose migration failed.
        pass
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k not in cfg:
                        continue
                    if k == "bomb_site_refs" and isinstance(v, dict):
                        # 逐地图合并，避免旧配置整体覆盖掉内置的 7 张图
                        for map_name, ref in v.items():
                            if isinstance(ref, dict):
                                cfg[k][map_name] = ref
                    else:
                        cfg[k] = v
                # v1.9：旧的在线学习配置迁移为静态 demo 模型开关
                if "learning_enabled" in raw and "demo_model_enabled" not in raw:
                    cfg["demo_model_enabled"] = bool(raw["learning_enabled"])
                if "learning_min_samples" in raw and "demo_model_min_samples" not in raw:
                    cfg["demo_model_min_samples"] = raw["learning_min_samples"]
                if "learning_blend" in raw and "demo_model_blend" not in raw:
                    cfg["demo_model_blend"] = raw["learning_blend"]
        except Exception:
            # 配置损坏不应阻止启动
            pass
    # v1.4 旧配置迁移：原来只有一个出生点快捷键，先同时填到 CT/T。
    legacy_spawn = str(cfg.get("hlae_round_key") or "").strip()
    if legacy_spawn:
        if not str(cfg.get("hlae_spawn_ct_key") or "").strip():
            cfg["hlae_spawn_ct_key"] = legacy_spawn
            cfg["hlae_spawn_ct_mode"] = str(cfg.get("hlae_round_mode") or "double")
        if not str(cfg.get("hlae_spawn_t_key") or "").strip():
            cfg["hlae_spawn_t_key"] = legacy_spawn
            cfg["hlae_spawn_t_mode"] = str(cfg.get("hlae_round_mode") or "double")
    # v1.1 旧配置迁移：原来只有一个包点快捷键，先同时填到 A/B，
    # 用户在 GUI 里再改成两个不同的键。
    legacy_key = str(cfg.get("hlae_bomb_key") or "").strip()
    if legacy_key:
        if not str(cfg.get("hlae_bomb_a_key") or "").strip():
            cfg["hlae_bomb_a_key"] = legacy_key
            cfg["hlae_bomb_a_mode"] = str(cfg.get("hlae_bomb_mode") or "double")
        if not str(cfg.get("hlae_bomb_b_key") or "").strip():
            cfg["hlae_bomb_b_key"] = legacy_key
            cfg["hlae_bomb_b_mode"] = str(cfg.get("hlae_bomb_mode") or "double")
    cfg['deepseek_api_key'] = ''
    cfg['ai_enabled'] = False
    cfg['arbitration_mode'] = 'off'
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """Persist ordinary preferences; paid arbitration always needs explicit re-enabling."""
    from arbitration_runtime import sanitize_config, migrate_config, PersonalKeyStore
    migrate_config(config_path(), PersonalKeyStore(app_data_dir()))
    data = sanitize_config({k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG})
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
        if not any(isinstance(handler, RotatingFileHandler) for handler in self.logger.handlers):
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
    ("keyboard", "keyboard", "全局热键监听 / MulNX 组合键", True),
    ("pygetwindow", "pygetwindow", "查找并激活 CS2 窗口", True),
    ("obsws_python", "obsws-python", "OBS 场景与媒体控制（可选）", False),
    ("cryptography", "cryptography", "设备许可与签名模型验证", True),
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
    if getattr(sys, 'frozen', False):
        if log_cb:
            log_cb('发行包组件缺失，请从设置中的官网入口重新下载完整程序。')
        return False
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

from director_core.legacy import (
    Player,
    Bomb,
    Snapshot,
    _f,
    _i,
    _b,
    slot_to_key,
    key_to_slot,
    format_slot,
    _vec,
    _weapons,
    parse_snapshot,
    TYPE_POWER,
    NAME_POWER,
    TYPE_RANGE,
    NAME_RANGE,
    UTILITY_TYPES,
    WeaponProfile,
    weapon_profile,
    aim_factor,
    _dist,
    _vertical_limit,
    has_possible_engagement,
    _win_probability,
    readiness,
    Candidate,
    score_candidates,
    _short_weapon,
    detect_bomb_site,
    format_candidate,
    Decision,
    choose_slot,
    _candidate_features
)


from director_adapters.gsi import SnapshotStore, GSIServer, _GSIHandler


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

    def poll(self, snap: Snapshot, now: float, *, ignore_retry=False) -> list[TriggerEvent]:
        events: list[TriggerEvent] = []
        key = self.round_key(snap)
        retry = float(self.cfg["trigger_retry_seconds"])

        # 出生点运镜：freezetime 触发一次
        if snap.phase == "freezetime" and key not in self._fired_round:
            last = self._attempt.get("r:" + key)
            if ignore_retry or last is None or now - last >= retry:
                self._attempt["r:" + key] = now
                events.append(TriggerEvent("round", key))

        # 包点运镜：已安放且剩余时间 <= 阈值，一次安放只触发一次
        bomb = snap.bomb
        if bomb and bomb.state in ("planted", "defusing") and bomb.countdown is not None:
            cd = bomb.countdown
            if 0 < cd <= float(self.cfg["bomb_cam_countdown"]) and key not in self._fired_bomb:
                last = self._attempt.get("b:" + key)
                if ignore_retry or last is None or now - last >= retry:
                    self._attempt["b:" + key] = now
                    events.append(TriggerEvent("bomb", key))

        self._last_phase = snap.phase
        return events

    def confirm(self, ev: TriggerEvent, count: bool = True) -> None:
        if ev.kind == "round":
            self._fired_round.add(ev.key)
            if count:
                self.counts["round"] += 1
        else:
            self._fired_bomb.add(ev.key)
            if count:
                self.counts["bomb"] += 1
        self._prune()

    def _prune(self) -> None:
        if len(self._fired_round) > 200:
            self._fired_round.clear()
            self._fired_bomb.clear()
            self._attempt.clear()


# --------------------------------------------------------------------------
# 8.5 开局控图：投掷道具识别
# --------------------------------------------------------------------------

UTILITY_LABELS = {
    "weapon_smokegrenade": "烟雾弹",
    "weapon_flashbang": "闪光弹",
    "weapon_molotov": "燃烧弹",
    "weapon_incgrenade": "燃烧弹",
    "weapon_hegrenade": "高爆手雷",
    "weapon_decoy": "诱饵弹",
}

UTILITY_PRIORITY = {
    "weapon_smokegrenade": 5,
    "weapon_flashbang": 4,
    "weapon_molotov": 3,
    "weapon_incgrenade": 3,
    "weapon_hegrenade": 2,
    "weapon_decoy": 1,
}


@dataclass
class UtilityThrow:
    slot: int
    steamid: str
    name: str
    team: str
    grenade: str
    label: str
    at: float
    kind: str = "throw"       # hold = 手持道具，throw = 刚投掷
    held_for: float = 0.0


def _grenade_stock(weapons: Any) -> dict[str, tuple[int, int]]:
    """统计每种手雷的条目数和弹药数，兼容不同 GSI 表示方式。"""
    stock: dict[str, tuple[int, int]] = {}
    for w in weapons or []:
        if not isinstance(w, dict):
            continue
        name = str(w.get("name") or "").lower()
        if name not in UTILITY_LABELS:
            continue
        count, ammo = stock.get(name, (0, 0))
        count += 1
        for key in ("ammo_reserve", "ammo_clip"):
            try:
                value = int(float(w.get(key)))
            except (TypeError, ValueError):
                continue
            if value > 0:
                ammo += value
        stock[name] = (count, ammo)
    return stock


class UtilityThrowTracker:
    """跟踪手持手雷（active_weapon）与库存减少推断的投掷。

    GSI 没有“投掷”事件，只能在两次快照之间比较手雷条目/弹药数。
    只在 live 阶段记录；freeze 买枪、回合切换、阵亡都不触发。
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._stock: dict[str, dict[str, tuple[int, int]]] = {}
        self._held: dict[str, dict[str, Any]] = {}
        self._used_holds: set[str] = set()
        self._throws: deque[UtilityThrow] = deque()
        self._round_key = ""
        self._last_phase = ""
        self._types = self._configured_types()

    def _configured_types(self) -> set[str]:
        raw = self.cfg.get("utility_cam_types")
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        if not isinstance(raw, (list, tuple, set)):
            raw = DEFAULT_CONFIG["utility_cam_types"]
        names = {str(x).strip().lower() for x in raw if str(x).strip()}
        return names or set(DEFAULT_CONFIG["utility_cam_types"])

    def _round_key_of(self, snap: Snapshot) -> str:
        rnd = getattr(snap, "round_num", None)
        return f"{getattr(snap, 'map_name', '')}|{rnd}" if rnd is not None else ""

    def _active_grenade(self, p: Any) -> str:
        active = getattr(p, "active_weapon", None)
        if isinstance(active, dict):
            name = str(active.get("name") or "").lower()
            if name in self._types:
                return name
        return ""

    def reset(self) -> None:
        self._stock.clear()
        self._held.clear()
        self._used_holds.clear()
        self._throws.clear()
        self._round_key = ""
        self._last_phase = ""

    def update(self, snap: Snapshot, now: float) -> list[UtilityThrow]:
        key = self._round_key_of(snap)
        phase = str(getattr(snap, "phase", "") or "").lower()
        if key and key != self._round_key:
            self._stock.clear()
            self._held.clear()
            self._used_holds.clear()
            self._throws.clear()
            self._round_key = key
        elif not key and phase == "freezetime" and self._last_phase != "freezetime":
            self._stock.clear()
            self._held.clear()
            self._used_holds.clear()
            self._throws.clear()
        # freeze 结束后的第一包只建立基线，避免买枪/丢弃造成误判。
        if self._last_phase == "freezetime" and phase != "freezetime":
            self._stock.clear()
            self._held.clear()
            self._used_holds.clear()

        current: dict[str, dict[str, tuple[int, int]]] = {}
        held: dict[str, dict[str, Any]] = {}
        found: list[UtilityThrow] = []
        for p in getattr(snap, "players", {}).values():
            if int(getattr(p, "health", 0) or 0) <= 0:
                continue
            slot = getattr(p, "slot", None)
            if slot is None:
                continue
            weapons = getattr(p, "weapons", [])
            stock = {
                name: value for name, value in _grenade_stock(weapons).items()
                if name in self._types
            }
            if not stock and not weapons:
                continue  # 精简/缺字段的包，不拿它当“手雷用完”
            sid = str(getattr(p, "steamid", ""))
            active = self._active_grenade(p)
            if active:
                prev_hold = self._held.get(sid)
                since = (float(prev_hold["since"])
                         if prev_hold and prev_hold.get("grenade") == active
                         else now)
                held[sid] = {
                    "slot": int(slot),
                    "name": str(getattr(p, "name", "") or ""),
                    "team": str(getattr(p, "team", "") or ""),
                    "grenade": active,
                    "label": UTILITY_LABELS.get(active, active),
                    "since": since,
                }
            prev = self._stock.get(sid)
            if prev:
                for name in set(prev) | set(stock):
                    old_count, old_ammo = prev.get(name, (0, 0))
                    new_count, new_ammo = stock.get(name, (0, 0))
                    if new_count < old_count or new_ammo < old_ammo:
                        found.append(UtilityThrow(
                            slot=int(slot),
                            steamid=sid,
                            name=str(getattr(p, "name", "") or ""),
                            team=str(getattr(p, "team", "") or ""),
                            grenade=name,
                            label=UTILITY_LABELS.get(name, name),
                            at=now,
                        ))
            current[sid] = stock

        self._stock = current
        self._held = held if phase == "live" else {}
        self._last_phase = phase
        if phase != "live":
            return []
        for throw in found:
            self._throws.append(throw)
        cutoff = now - 10.0
        while self._throws and self._throws[0].at < cutoff:
            self._throws.popleft()
        return found

    def recent(self, now: float, window: float = 2.0) -> Optional[UtilityThrow]:
        try:
            window = max(0.1, min(10.0, float(window)))
        except (TypeError, ValueError):
            window = 2.0
        cutoff = now - window
        recent = [t for t in self._throws if t.at >= cutoff]
        if not recent:
            return None
        # 最新投掷优先；同一包内按道具价值排序。
        return max(recent, key=lambda t: (t.at, UTILITY_PRIORITY.get(t.grenade, 0)))

    def recent_hold(self, now: float,
                    min_hold: float = 0.0) -> Optional[UtilityThrow]:
        try:
            min_hold = max(0.0, min(10.0, float(min_hold)))
        except (TypeError, ValueError):
            min_hold = 0.0
        eligible = [
            (sid, info) for sid, info in self._held.items()
            if now - float(info["since"]) >= min_hold
            and self._hold_key(sid, info) not in self._used_holds
        ]
        if not eligible:
            return None
        sid, info = max(
            eligible,
            key=lambda item: (float(item[1]["since"]),
                              UTILITY_PRIORITY.get(str(item[1]["grenade"]), 0)),
        )
        return UtilityThrow(
            slot=int(info["slot"]), steamid=sid, name=str(info["name"]),
            team=str(info["team"]), grenade=str(info["grenade"]),
            label=str(info["label"]), at=float(info["since"]),
            kind="hold", held_for=now - float(info["since"]),
        )

    @staticmethod
    def _hold_key(sid: str, info: dict[str, Any]) -> str:
        return f"{sid}|{info.get('grenade')}|{float(info.get('since', 0.0)):.3f}"

    def mark_hold_used(self, action: UtilityThrow) -> None:
        if action.kind != "hold":
            return
        self._used_holds.add(
            f"{action.steamid}|{action.grenade}|{action.at:.3f}")

    def recent_action(self, now: float, pre_buffer: float,
                      post_hold: float) -> Optional[UtilityThrow]:
        """投掷后的 post_hold 内优先保留投掷者，否则返回正在手持道具的选手。

        手持只做约 100ms 的确认，避免武器切换抖动；pre_buffer 由调用方
        用作切过去后的最短保持时间。
        """
        throw = self.recent(now, post_hold)
        if throw is not None:
            return throw
        confirm = min(0.10, max(0.0, float(pre_buffer)))
        return self.recent_hold(now, confirm)


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


def game_window_identity(hwnd: int, cfg: dict) -> Optional[tuple]:
    """Fresh HWND/PID/full image path; errors deny input, never inspect memory."""
    if not hwnd or not is_window(hwnd):
        return None
    pid = get_window_pid(hwnd)
    exe = get_process_exe(pid)
    if not exe or Path(exe).name.casefold() != "cs2.exe":
        return None
    normalized = os.path.normcase(os.path.abspath(exe))
    configured = str(cfg.get("cs2_exe") or "").strip()
    if configured and normalized != os.path.normcase(os.path.abspath(configured)):
        return None
    if get_window_pid(hwnd) != pid:
        return None
    return hwnd, pid, normalized


def modifier_conflict(owned: set[str]) -> bool:
    # AsyncKeyState includes our synthetic modifiers. Ignore only modifiers
    # owned by this transaction; never release unrelated/physical user keys.
    groups = ((0x12, {"alt"}), (0x11, {"ctrl", "control"}),
              (0x10, {"shift"}), (0x5B, {"win", "windows", "cmd"}),
              (0x5C, {"win", "windows", "cmd"}))
    return any(get_async_key_state(vk) and not (aliases & owned) for vk, aliases in groups)


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
            # 限制在 0-9 与两个 MulNX 快捷键内）。若系统前台锁拦截，失败即跳过。
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

# Alt / Ctrl / Shift / Win。指南里的运镜交互是 alt+按键，所以带修饰键的
# 数字键是相机宏，不是“手动接管 0-9”，必须排除。
MODIFIER_VKS = (0x12, 0x11, 0x10, 0x5B, 0x5C)

HOTKEY_MODIFIERS = {"alt", "ctrl", "control", "shift", "windows", "win", "cmd"}

# GUI 只允许这三类修饰键 + A-Z/0-9，避免自由输入写出 MulNX 不认的组合。
SELECTABLE_MODIFIERS = ("ALT", "SHIFT", "CTRL")
SELECTABLE_KEYS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + tuple("0123456789")


def parse_selectable_hotkey(hotkey: str) -> tuple[str, str]:
    """把配置里的 "alt+N" 解析成 ("ALT", "N")；不合法返回空串。"""
    mods, keys = split_hotkey(hotkey)
    mod = next((m.upper() for m in mods
                if m.lower() in ("alt", "ctrl", "shift")), "")
    key = keys[0].upper() if len(keys) == 1 else ""
    if key not in SELECTABLE_KEYS:
        key = ""
    return mod, key


def split_hotkey(hotkey: str) -> tuple[list[str], list[str]]:
    """把 "alt+1" 拆成 (["alt"], ["1"])；无法识别时返回空 keys。"""
    # keyboard 的 Windows 别名表把大写 M 优先映射为浏览器主页键。
    # 配置/界面仍可显示大写；传给输入后端的物理键名统一小写。
    parts = [p.strip().lower() for p in str(hotkey or "").split("+") if p.strip()]
    mods = [p for p in parts if p.lower() in HOTKEY_MODIFIERS]
    keys = [p for p in parts if p.lower() not in HOTKEY_MODIFIERS]
    return mods, keys


from director_adapters.input import InputControllerAdapter


class InputController(InputControllerAdapter):
    """Compatibility facade; preserves existing imports and test patches."""
    def __init__(self, cfg, log, authorization_gate=None):
        super().__init__(cfg, log, authorization_gate, runtime=sys.modules[__name__])


class ManualKeyWatcher:
    """
    监听用户手动按 0-9：低级键盘钩子优先，装不上就退化为 GetAsyncKeyState 轮询。
    注意：钩子回调绝对不能 return True（那会把按键吞掉，游戏就收不到了）。
    """

    def __init__(self, on_manual: Callable[[str], None],
                 is_self_press: Callable[[str], bool], log: LogBus, foreground_check=None) -> None:
        self._on_manual = on_manual
        self._foreground_check = foreground_check or (lambda: bool(game_window_identity(get_foreground_hwnd(), {})))
        self._is_self = is_self_press
        self.log = log
        self._hook: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_hit: dict[str, float] = {}
        self._last_warn = 0.0
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

    def _warn(self, msg: str) -> None:
        """钩子线程里的异常不能静默，也不能刷屏；5 秒最多一条。"""
        now = time.monotonic()
        if now - self._last_warn > 5.0:
            self._last_warn = now
            self.log.warning(msg)

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
        except Exception as e:
            self._warn(f"键盘钩子事件处理异常：{type(e).__name__}: {e}")

    def _poll_loop(self) -> None:
        prev = {k: False for k in VK_DIGITS}
        while not self._stop.is_set():
            try:
                for name, vk in VK_DIGITS.items():
                    down = get_async_key_state(vk)
                    if down and not prev.get(name, False):
                        digit = name[1] if name.startswith("n") else name
                        self._handle(digit)
                    prev[name] = down
            except Exception as e:
                self._warn(f"按键轮询异常（继续运行）：{type(e).__name__}: {e}")
                time.sleep(0.1)
                continue
            time.sleep(0.02)

    def _modifier_down(self) -> bool:
        try:
            return any(get_async_key_state(vk) for vk in MODIFIER_VKS)
        except Exception:
            return False

    def _handle(self, digit: str) -> None:
        if not self._foreground_check():
            return
        now = time.monotonic()
        # alt+1 / ctrl+2 等是 MulNX 运镜宏（指南交互格式 alt+按键），
        # 不能算手动接管，否则每次出生点/包点运镜都会误暂停 AI 10 秒。
        if self._modifier_down():
            return
        if self._is_self(digit):
            return  # 这是导播自己按的，不能当成手动接管
        if now - self._last_hit.get(digit, 0.0) < 0.35:
            return  # 长按自动重复去抖
        self._last_hit[digit] = now
        try:
            self._on_manual(digit)
        except Exception as e:
            self._warn(f"手动接管处理异常（继续监听）：{type(e).__name__}: {e}")


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
        self.cloud = None
        self._result_generation = None
        self._result_request = None
        self._reported_terminal = set()
        self._observations = queue.Queue(maxsize=16)
        self.received = self.applied = self.discarded = 0

    def observe(self, request, state, captured_at, now):
        if request is None:
            return
        key = request['requestId']
        if state != 'received':
            if key in self._reported_terminal:
                return
            if len(self._reported_terminal) > 1000:
                self._reported_terminal.clear()
            self._reported_terminal.add(key)
        if state == 'received': self.received += 1
        elif state == 'applied': self.applied += 1
        else: self.discarded += 1
        if getattr(self.cloud, 'mode', '') == 'platform':
            try:self._observations.put_nowait((key,state,(now-captured_at)*1000))
            except queue.Full:pass  # Best-effort telemetry never blocks or changes billing.

    def applied_hint(self, slot, now):
        with self._lock:
            if self._result and self._result[0] == slot:
                self.observe(self._result_request,'applied',self._result[1],now)

    def configure(self, account, store):
        from arbitration_runtime import CloudSession
        self.cloud = CloudSession(account, store, self.cfg.get('arbitration_mode', 'off'),
                                  self.cfg.get('deepseek_model', 'deepseek-flash'))

    def invalidate(self):
        if self.cloud:
            self.cloud.invalidate()
        with self._lock:
            self._result = None

    def sync_context(self, snap):
        if self.cloud:
            self.cloud.sync((snap.map_name, snap.round_num, snap.phase,
                             tuple(sorted((str(key),player.slot) for key,player in snap.players.items()))))

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("ai_enabled")) and self.cloud is not None and self.cloud.allowed()

    def start(self) -> None:
        if not self.enabled:
            self.log.info("云端仲裁未启用，继续使用本地模型与规则引擎。")
            return
        self._thread = threading.Thread(target=self._worker, name="ai-arbiter", daemon=True)
        self._thread.start()
        self.log.info("云端仲裁已启用：" + self.cloud.mode)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._result = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def request(self, snap: Snapshot, cands: list[Candidate], now: float) -> None:
        if self._stop.is_set() or not self.enabled or not cands:
            return
        if now - self._last_call < float(self.cfg["ai_min_interval"]):
            return
        try:
            cloud_request = self.cloud.request(snap, cands)
        except Exception:
            self._fail('仲裁上下文不可用，继续本地规则')
            return
        req = {
            "ts": now,
            "slots": {c.slot for c in cands},
            "cloud": cloud_request,
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
            if self._stop.is_set() or not self.enabled or self._result is None:
                return None
            if self.cloud and self._result_generation != self.cloud.generation:
                self.observe(self._result_request,'discarded',self._result[1],now)
                return None
            slot, ts, reason = self._result
            if not 0 <= now - ts <= float(self.cfg["ai_result_ttl"]):
                self.observe(self._result_request,'discarded',ts,now)
                return None
            return slot, ts, reason

    def _worker(self) -> None:
        if getattr(self.cloud,'mode','') == 'platform':
            try:self.cloud.provider.prepare()
            except Exception:
                self.cloud.disabled=True
                self._fail('平台配置或额度不可用，请在网站核对后重新主动启用')
                return
        while not self._stop.is_set():
            try:
                observation = self._observations.get_nowait()
                try:self.cloud.provider.observe(*observation)
                except Exception:pass  # Telemetry is not a paid request and never controls balances.
            except queue.Empty:pass
            try:
                req = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop.is_set():
                break
            now = time.monotonic()
            if now - float(req["ts"]) > 2.0:
                continue  # 过期请求直接丢弃
            try:
                if not self.enabled or req["cloud"]["generation"] != self.cloud.generation:
                    continue
                # Authoritative rate check is on the single worker, including work queued
                # before a previous request started. UI sampling must not bypass cooldown.
                self._call_times = deque(t for t in self._call_times if now - t < 60.0)
                if (now-self._last_call < max(3.0,float(self.cfg['ai_min_interval']))
                        or len(self._call_times) >= min(12,int(self.cfg['ai_max_per_minute']))):
                    continue
                self._call_times.append(now)
                self._last_call = now
                self.calls += 1
                slot = self.cloud.ask(req["cloud"])
                reason = "云端候选建议"
                self.last_latency = time.monotonic() - now
            except Exception as e:
                # _ask 内部已处理预期异常；这里兜底，保证仲裁线程不会因
                # 未预料错误退出（规则引擎继续工作）。
                from arbitration_runtime import ArbitrationError
                self._fail(str(e) if isinstance(e, ArbitrationError) else "仲裁请求失败，继续本地规则")
                continue
            if slot is not None:
                with self._lock:
                    # TTL belongs to the captured scene, never to response arrival.
                    captured_at = float(req["ts"])
                    age = time.monotonic() - captured_at
                    if (not self._stop.is_set() and self.enabled
                            and req["cloud"]["generation"] == self.cloud.generation
                            and 0 <= age <= float(self.cfg["ai_result_ttl"])):
                        self._result = (slot, captured_at, reason)
                        self._result_generation = req["cloud"]["generation"]
                        self._result_request = req["cloud"]
                        self.observe(req["cloud"],'received',captured_at,captured_at+age)
                    else:
                        self.observe(req["cloud"],'discarded',captured_at,captured_at+age)

    def _ask(self, prompt, allowed):
        self._fail('旧提示词仲裁入口已停用，请使用登录后的结构化提供器')
        return None, ''

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
            "mode": getattr(self.cloud,'mode','off'),
            "received": self.received, "applied_input_reports": self.applied,
            "discarded": self.discarded,
        }


# --------------------------------------------------------------------------
# 11.5 离线 demo 模型：静态权重，不做在线更新
# --------------------------------------------------------------------------

LEARN_FEATURE_NAMES = (
    "aim_self", "aim_enemy", "win", "enemy_low_hp",
    "self_low_hp", "proximity", "enemy_damaged", "hp_advantage",
)


def learner_path() -> Path:
    return app_data_dir() / "learner.json"




class DemoModel:
    """加载 train_from_demos.py 生成的静态 demo 权重；不再做任何在线更新。"""

    def __init__(self, cfg: dict[str, Any], log: LogBus) -> None:
        self.cfg = cfg
        self.log = log
        self.weights = [0.0] * len(LEARN_FEATURE_NAMES)
        self.bias = 0.0
        self.samples = 0
        self.attempts = 0
        self.hits = 0
        self.load()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("demo_model_enabled", True))

    def _min_samples(self) -> int:
        try:
            return max(1, int(self.cfg.get("demo_model_min_samples", 20)))
        except (TypeError, ValueError):
            return 20

    def load(self) -> None:
        path = learner_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            w = raw.get("weights")
            if not isinstance(w, list) or len(w) != len(self.weights):
                return
            self.weights = [max(-3.0, min(3.0, float(x))) for x in w]
            self.bias = max(-5.0, min(5.0, float(raw.get("bias", 0.0))))
            self.samples = max(0, int(raw.get("samples", 0)))
            self.attempts = max(0, int(raw.get("attempts", 0)))
            self.hits = max(0, int(raw.get("hits", 0)))
        except Exception:
            self.log.warning("demo 模型数据损坏，已忽略。")

    def _predict_vec(self, x: list[float]) -> float:
        z = self.bias + sum(w * v for w, v in zip(self.weights, x))
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def adjust(self, cands: list[Candidate]) -> None:
        """按离线 demo 模型的概率小幅调整候选分数；不更新权重。"""
        if not self.enabled or not cands or self.samples < self._min_samples():
            return
        try:
            blend = max(0.0, min(0.6, float(
                self.cfg.get("demo_model_blend", 0.25))))
        except (TypeError, ValueError):
            blend = 0.25
        for c in cands:
            p = self._predict_vec(_candidate_features(c))
            c.score *= 1.0 + blend * (p - 0.5)
        cands.sort(key=lambda c: c.score, reverse=True)

    def status(self) -> dict[str, Any]:
        rate = (self.hits / self.attempts * 100.0) if self.attempts else 0.0
        return {
            "enabled": self.enabled,
            "samples": self.samples,
            "attempts": self.attempts,
            "hits": self.hits,
            "hit_rate": round(rate, 1),
            "online": False,
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

    def __init__(self, cfg: dict[str, Any], bus: LogBus,
                 pro: Optional[pro_mod.ProDirector] = None,
                 decision_logger: Optional[dm.DecisionLogger] = None,
                 model_arbiter: Optional[dm.ModelArbiter] = None,
                 timeline: Optional[story_mod.ShotTimeline] = None,
                 authorization_gate=None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.pro = pro
        self.decision_logger = decision_logger
        self.model_arbiter = model_arbiter
        self.timeline = timeline
        self.story = story_mod.StoryTracker(cfg)
        self.store = SnapshotStore()
        self.server: Optional[GSIServer] = None
        self.authorization_gate = authorization_gate
        self.input = InputController(cfg, bus, authorization_gate)
        if self.pro is not None and authorization_gate is not None:
            self.pro.obs.authorization_gate = authorization_gate
        self.arbiter = AIArbiter(cfg, bus)
        self.learner = DemoModel(cfg, bus)
        self.triggers = TriggerTracker(cfg)
        self.utility_tracker = UtilityThrowTracker(cfg)
        self.watcher: Optional[ManualKeyWatcher] = None

        self._stop = threading.Event()
        self.input.cancel_event = self._stop
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {"running": False}

        self._last_seq = -1
        self._hp: dict[str, int] = {}
        self._damage_at: dict[str, float] = {}
        self._damage_generation = 0
        self._candidate_cache: list[Candidate] = []
        self._candidate_cache_key: Optional[tuple[int, int, int]] = None
        self._candidate_cache_at = 0.0
        self._manual_until = 0.0
        self._last_switch_at: Optional[float] = None
        self._last_switch_slot: Optional[int] = None
        self._bomb_cam_until = 0.0
        self._cinematic_request: Optional[dict[str, Any]] = None
        self._cinematic_retry_at = 0.0
        self._cinematic_control_lock = threading.Lock()
        self._cinematic_cancel = threading.Event()
        if not cfg.get('cinematics_enabled', True): self._cinematic_cancel.set()
        self._stale_active = False
        self._last_spectate_log = 0.0
        self._last_trigger_fail = 0.0
        self._last_bomb_site_log = 0.0
        self._last_bomb_pov_log = 0.0
        self._last_reason = ""
        self._press_retry_after = 0.0
        self._last_observed_slot: Optional[int] = None
        self._dmg_dealt: dict[str, int] = {}
        self._round_kills: dict[str, int] = {}
        self._cut_times: deque[float] = deque()
        self._last_fight_lock_log = 0.0
        self._last_utility_cut_at = 0.0
        self._utility_cuts = 0
        self._utility_round_key = ""
        self._utility_hold_until = 0.0
        self._utility_hold_slot: Optional[int] = None
        self._last_round_winner: Optional[str] = None
        self._last_scores: Optional[tuple[int, int]] = None
        self._last_map_name = ""
        self._last_observed_info: Optional[
            tuple[int, str, Optional[tuple[float, float, float]]]
        ] = None
        self._calibration_due_at: Optional[float] = None
        self._calibration_running = False
        self._calibration_gameover_seen = False
        self._last_calibration_result = ""
        self._narrative_text = ""
        self._narrative_round_key = ""
        self._narrative_over_key = ""

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._running:
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError('上一导播线程尚未退出')
        if self.pro is not None:
            self.pro.reset()
        port = int(self.cfg["gsi_port"])
        exe = str(self.cfg.get("cs2_exe") or "").strip()
        cfg_dir = derive_cfg_dir(exe) if exe else None
        if cfg_dir is not None:
            cfg_file = cfg_dir / GSI_CFG_NAME
            try:
                if cfg_file.is_file() and cfg_file.read_text(
                        encoding="utf-8", errors="ignore").lstrip().startswith("{"):
                    self.bus.warning(
                        "已安装的 GSI 配置是旧 JSON 格式，CS2 会忽略它；"
                        "请用当前版本重新“一键安装 GSI”并完全重启 CS2。"
                    )
            except Exception:
                pass
            conflicts = gsi_port_conflicts(cfg_dir, port)
            if conflicts:
                self.bus.warning(
                    f"端口 {port} 同时被其他 GSI 配置使用：{', '.join(conflicts)}。"
                    f"对方的精简数据包可能缺少 allplayers_position；"
                    f"建议给本程序换一个独立端口并重新安装 GSI。"
                )
        server = GSIServer("127.0.0.1", port, self.store, self.bus.warning)
        server.start()  # 绑定失败会抛 RuntimeError，由 GUI 提示
        self.server = server
        try:
            if IS_WINDOWS and not self.cfg.get("dry_run"):
                from physical_modifiers import PhysicalModifiers
                self.input.physical_modifiers = PhysicalModifiers()
                self.input.physical_modifiers.start()
            self.arbiter.start()
            self.watcher = ManualKeyWatcher(self._on_manual_key, self.input.is_self_press, self.bus, self.input.foreground_is_game)
            self.watcher.start()
        except Exception:
            if self.watcher is not None:
                self.watcher.stop()
                self.watcher = None
            self.arbiter.stop()
            server.stop()
            self.server = None
            self.input.release_held_keys()
            if self.input.physical_modifiers is not None:
                self.input.physical_modifiers.close()
                self.input.physical_modifiers = None
            raise
        self._stop.clear()
        if self.timeline is not None:
            self.timeline.start(time.monotonic())
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="director", daemon=True)
        self._thread.start()
        admin = is_admin()
        self.bus.info(
            f"导播已启动：GSI 127.0.0.1:{port}，循环 {self.cfg['loop_hz']}Hz，"
            f"手动接管检测={self.watcher.mode if self.watcher else 'off'}，"
            f"演练={'开' if self.cfg.get('dry_run') else '关'}，"
            f"权限={'管理员' if admin else '普通用户'}"
        )
        if IS_WINDOWS and not admin and not self.cfg.get("dry_run"):
            self.bus.warning(
                "当前不是管理员：如果 CS2/MulNX 是以管理员运行，Windows UIPI "
                "会拦截模拟按键，日志会显示“已发送”但运镜/切人可能不触发。"
                "请右键以管理员身份重新启动本程序。"
            )

    def stop(self) -> None:
        self._stop.set()
        self.input.release_held_keys()
        if not self._running and self.server is None and self._thread is None and not getattr(self, '_needs_flush', False):
            return
        self._stop.set()
        self._running = False
        if self.pro is not None:
            self.pro.actions.cancel_pending()
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self.arbiter.stop()
        if self.input.physical_modifiers is not None:
            self.input.physical_modifiers.close()
            self.input.physical_modifiers = None
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                self.input.release_held_keys()
                raise RuntimeError('导播线程仍在停止，暂不能启动新会话，请稍后重试停止')
            self._thread = None
        self.input.release_held_keys()
        if self._cinematic_request is not None:
            self.bus.warning("已停止发送按键；若 MulNX 运镜仍在播放，请按 Alt+P 单次停止预览。")
            self._cinematic_request = None
        if self.timeline is not None:
            self._needs_flush = True
            if self.timeline.close(time.monotonic(), '导播停止') is False:
                raise RuntimeError('镜头时间线尚未完全落盘，请检查磁盘后重试停止')
            self._needs_flush = False
        with self._status_lock:
            self._status = {"running": False}
        self.bus.info("导播已停止。")

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            status = dict(self._status)
        status['cinematics_enabled'] = self.cinematics_enabled()
        request = self._cinematic_request
        status['cinematic_pending_stop'] = bool(request and request['cancel'].is_set())
        return status

    def cinematics_enabled(self) -> bool:
        return not self._cinematic_cancel.is_set()

    def set_cinematics_enabled(self, enabled: bool) -> None:
        # UI never waits on the send lock. A cancelled generation stays cancelled
        # even if the user quickly enables the next generation again.
        with self._cinematic_control_lock:
            if enabled:
                if self._cinematic_cancel.is_set(): self._cinematic_cancel = threading.Event()
            else:
                self._cinematic_cancel.set()

    # ---- 手动接管 ----
    def _on_manual_key(self, digit: str) -> None:
        now = time.monotonic()
        pause = float(self.cfg["manual_pause_seconds"])
        self._manual_until = max(self._manual_until, now + pause)
        self.arbiter.invalidate()
        slot = key_to_slot(digit)
        if self.timeline is not None and slot is not None:
            try:
                self.timeline.note_manual(slot, self._last_observed_slot,
                                          now, pause)
            except Exception:
                self.bus.exception("记录人工接管时出现异常")
        self.bus.info(
            f"检测到手动切换（按 {digit}，对应 GSI slot={slot}），"
            f"AI 自动切换暂停 {pause:.0f} 秒。")

    # ---- 在线校准 ----
    def _maybe_start_calibration(self, snap: Snapshot, now: float) -> None:
        if not bool(self.cfg.get("decision_calibration_enabled", True)):
            return
        if self.model_arbiter is None or not self.model_arbiter.loaded:
            return
        phase = str(getattr(snap, "phase", "") or "").lower()
        if phase == "gameover":
            if not self._calibration_gameover_seen:
                self._calibration_gameover_seen = True
                self._calibration_due_at = now + 8.0
        elif phase not in ("gameover", "intermission"):
            self._calibration_gameover_seen = False
        if self._calibration_due_at is None or now < self._calibration_due_at:
            return
        if self._calibration_running:
            return
        self._calibration_due_at = None
        try:
            min_samples = max(100, int(self.cfg.get("decision_calibration_min_samples", 500)))
        except (TypeError, ValueError):
            min_samples = 500
        rows = int(getattr(self.decision_logger, "rows", 0) or 0) if self.decision_logger else 0
        if rows < min_samples:
            self.bus.info(f"在线校准跳过：标注样本 {rows} < {min_samples}")
            return
        self._calibration_running = True
        self.bus.info(f"比赛结束，开始在线校准（{rows} 条已标注样本）…")
        threading.Thread(target=self._run_calibration, daemon=True,
                         name="decision-calibration").start()

    def _run_calibration(self) -> None:
        try:
            out = dm.decision_model_path().with_name("decision_calibration.json")
            result = cal_mod.calibrate_csv(
                dm.decision_csv_path(), dm.decision_model_path(), out,
                min_samples=max(100, int(self.cfg.get("decision_calibration_min_samples", 500))))
            if result.get("ok"):
                self._last_calibration_result = (
                    f"Brier {result.get('brier_before'):.3f}→{result.get('brier_after'):.3f}")
                self.bus.info(
                    f"在线校准已应用：{self._last_calibration_result}；"
                    f"AUC {result.get('auc', 0):.3f}")
                if self.model_arbiter is not None:
                    self.model_arbiter.reload_if_changed()
            else:
                self._last_calibration_result = f"未应用：{result.get('reason', '')}"
                self.bus.warning(f"在线校准未应用：{result.get('reason', '')}")
        except Exception as e:
            self._last_calibration_result = f"异常：{type(e).__name__}: {e}"
            self.bus.warning(f"在线校准异常：{type(e).__name__}: {e}")
        finally:
            self._calibration_running = False

    # ---- 回合叙事 ----
    def _update_narrative(self, snap: Snapshot, story: Any, now: float) -> None:
        if not bool(self.cfg.get("narrative_enabled", True)):
            return
        phase = str(getattr(snap, "phase", "") or "").lower()
        key = str(getattr(story, "round_key", "") or "")
        if phase == "freezetime" and key and key != self._narrative_round_key:
            self._narrative_round_key = key
            self._narrative_text = narrative_mod.freeze_text(
                snap, story, self._last_round_winner or "")
            self._emit_narrative(self._narrative_text)
        elif phase in ("over", "gameover") and key and key != self._narrative_over_key:
            self._narrative_over_key = key
            self._narrative_text = narrative_mod.round_end_text(
                snap, story, self._last_round_winner or "")
            self._emit_narrative(self._narrative_text)

    def _emit_narrative(self, text: str) -> None:
        if not text:
            return
        self.bus.info(f"叙事：{text}")
        if not bool(self.cfg.get("obs_narrative_enabled", False)):
            return
        source = str(self.cfg.get("obs_narrative_source", "") or "").strip()
        if not source or self.pro is None:
            return
        self.pro.actions.enqueue({'name': '叙事文字', 'actions': [
            {'type': 'set_text', 'target': source, 'text': text}]})

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
            self._stop.wait(max(0.0, interval - elapsed))

    def _tick(self) -> None:
        if self.authorization_gate is not None and not self.authorization_gate.allowed():
            self.arbiter.invalidate()
            self.input.release_held_keys()
            self._set_status({"running": True, "gsi": "authorization_required", "phase": "-",
                              "reason": "离线授权不可用，请联网校验", "authorization": self.authorization_gate.last_state.reason})
            return
        now = time.monotonic()
        snap = self.store.get()
        request = self._cinematic_request
        if request and request['cancel'].is_set(): self._handoff_cinematic(snap, now)
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
            "demo_model": self.learner.status(),
            "decision": (self.decision_logger.stats()
                         if self.decision_logger else {}),
            "model": (self.model_arbiter.status()
                      if self.model_arbiter else {}),
            "triggers": dict(self.triggers.counts),
            "spectating": False,
            "candidates": 0,
            "admin": is_admin(),
            "cuts_10s": self._recent_cut_count(now),
            "story": self.story.state.to_dict(),
            "story_text": self.story.state.story,
            "narrative": self._narrative_text,
            "metrics": (self.timeline.session_metrics()
                        if self.timeline is not None else {}),
            "current_shot": (
                self.timeline.current.to_dict(now)
                if (self.timeline is not None and self.timeline.current)
                else None),
        }

        if snap is None:
            self._set_status(status)
            return

        self.arbiter.sync_context(snap)
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
        self._maybe_start_calibration(snap, now)

        # ---- 回合故事 + 镜头时间线（Phase 1，不影响评分/模型）----
        self._last_observed_slot = snap.observer_slot
        bomb_site: Optional[str] = None
        if snap.bomb is not None and snap.bomb.state in ("planted", "defusing"):
            try:
                bomb_site = detect_bomb_site(snap.map_name, snap.bomb.position,
                                             self.cfg)
            except Exception:
                bomb_site = None
        story = self.story.update(snap, now, bomb_site)
        self._update_narrative(snap, story, now)
        status["narrative"] = self._narrative_text
        status["story"] = story.to_dict()
        status["story_text"] = story.story
        for ev in story.events:
            self.bus.info(f"故事：{ev}")
            if self.timeline is not None:
                if ev.startswith("下包"):
                    self.timeline.note_bomb("bomb_planted", story.bomb_site, now)
                elif ev.startswith("开始拆包"):
                    self.timeline.note_bomb("bomb_defusing", story.bomb_site, now)

        # 先让 timeline 识别新回合（否则新回合的伤害会被算进上一回合），
        # 再采集伤害/击杀，最后 tick 一次处理 POV 变化与回合结束。
        if self.timeline is not None:
            try:
                if story.round_key and story.round_key != self.timeline.round_key:
                    self.timeline.tick(snap, story, now)
            except Exception:
                self.bus.exception("镜头时间线更新异常（已继续运行）")

        if snap.seq != self._last_seq:
            self._update_damage(snap, now)
            for throw in self.utility_tracker.update(snap, now):
                if bool(self.cfg.get("utility_cam_enabled", True)):
                    try:
                        phase_ok = str(self.story.state.phase) in (
                            "opening", "control")
                    except Exception:
                        phase_ok = False
                    if phase_ok:
                        post_hold = self._utility_buffers(
                            self._utility_is_hot(now, []))[1]
                        self._utility_hold_slot = throw.slot
                        self._utility_hold_until = max(
                            self._utility_hold_until, now + post_hold)
                    self.bus.info(
                        f"检测到投掷道具：{format_slot(throw.slot)} "
                        f"{throw.name or '?'} {throw.label}"
                    )
            if self.decision_logger is not None:
                self.decision_logger.poll(snap, now)
            if self.pro is not None:
                self.pro.tick(snap, now)
            self._last_seq = snap.seq

        if self.timeline is not None:
            try:
                self.timeline.tick(snap, story, now)
            except Exception:
                self.bus.exception("镜头时间线更新异常（已继续运行）")

        self._update_round_winner(snap)
        self._handle_triggers(snap, now)
        status["triggers"] = dict(self.triggers.counts)
        status["bomb_cam_for"] = max(0.0, self._bomb_cam_until - now)
        status["paused_for"] = max(0.0, self._manual_until - now)
        status["spectating"] = snap.is_spectating

        try:
            self._decide_and_switch(snap, now, status)
        finally:
            status["ai"] = self.arbiter.status()
            if self.timeline is not None:
                try:
                    status["metrics"] = self.timeline.session_metrics()
                    cur = self.timeline.current
                    status["current_shot"] = cur.to_dict(now) if cur else None
                except Exception:
                    pass
            self._set_status(status)

    def _set_status(self, status: dict[str, Any]) -> None:
        with self._status_lock:
            self._status = status

    # ---- 伤害/击杀/死亡追踪：评分 + 专业导播指标共用 ----
    def _update_damage(self, snap: Snapshot, now: float) -> None:
        attackers: list[int] = []
        victims: list[int] = []
        deaths: list[int] = []
        killers: list[int] = []
        damage_amount = 0

        for sid, p in snap.players.items():
            slot = p.slot
            prev_hp = self._hp.get(sid)
            if prev_hp is not None:
                if p.health < prev_hp - 2:
                    self._damage_at[sid] = now
                if p.health < prev_hp:
                    self._damage_generation += 1
                if p.health < prev_hp and slot is not None:
                    victims.append(int(slot))
                if prev_hp > 0 and p.health <= 0 and slot is not None:
                    deaths.append(int(slot))
            self._hp[sid] = p.health

            # round_totaldmg 增量 = 本 tick 造成伤害的人；不需要读内存。
            prev_dmg = self._dmg_dealt.get(sid)
            if prev_dmg is not None and p.round_totaldmg > prev_dmg:
                delta = p.round_totaldmg - prev_dmg
                damage_amount += delta
                if slot is not None:
                    attackers.append(int(slot))
            self._dmg_dealt[sid] = p.round_totaldmg

            # round_kills 增量 = 击杀者；可能比死亡晚一个 GSI 包。
            prev_kills = self._round_kills.get(sid)
            if prev_kills is not None and p.round_kills > prev_kills:
                if slot is not None:
                    killers.extend([int(slot)] * (p.round_kills - prev_kills))
            self._round_kills[sid] = p.round_kills

        if attackers or victims:
            self.story.note_damage(now)
            if self.timeline is not None:
                try:
                    self.timeline.note_damage(attackers, victims, now,
                                              damage_amount)
                except Exception:
                    self.bus.exception("记录伤害事件时出现异常")
        if killers and self.timeline is not None:
            try:
                self.timeline.note_kill(killers, deaths, now)
            except Exception:
                self.bus.exception("记录击杀事件时出现异常")
        if deaths and self.timeline is not None:
            try:
                for slot in deaths:
                    self.timeline.note_death(slot, now)
            except Exception:
                self.bus.exception("记录死亡事件时出现异常")

        if len(self._damage_at) > 40:
            self._damage_at = {k: v for k, v in self._damage_at.items() if now - v < 15.0}
        if len(self._hp) > 80:
            # 换图/重连后的旧 SteamID 清理，避免长局内存增长
            keep = set(snap.players)
            self._hp = {k: v for k, v in self._hp.items() if k in keep}
            self._dmg_dealt = {k: v for k, v in self._dmg_dealt.items() if k in keep}
            self._round_kills = {k: v for k, v in self._round_kills.items() if k in keep}

    def _score_candidates(self, snap: Snapshot, now: float) -> list[Candidate]:
        """按快照缓存候选；不改变评分/调整结果，只避免同帧重复计算。"""
        key = (int(getattr(snap, "seq", 0) or 0),
               self._damage_generation,
               int(getattr(self.learner, "samples", 0) or 0))
        if (self._candidate_cache_key == key
                and now - self._candidate_cache_at <= 0.25):
            return list(self._candidate_cache)
        cands = score_candidates(snap, self.cfg, self._damage_recent, now)
        self.learner.adjust(cands)
        self._candidate_cache = list(cands)
        self._candidate_cache_key = key
        self._candidate_cache_at = now
        return list(cands)

    def _damage_recent(self, sid: str) -> bool:
        return (time.monotonic() - self._damage_at.get(sid, -999.0)) <= 1.6

    def _update_round_winner(self, snap: Snapshot) -> None:
        """记录上一回合胜方；优先用 round.win_team，缺失时用比分增量兜底。"""
        if self._last_map_name != snap.map_name:
            self._last_map_name = snap.map_name
            self._last_round_winner = None
            self._last_scores = None
            self._last_observed_info = None
        if snap.round_win_team in ("CT", "T"):
            self._last_round_winner = snap.round_win_team
        if self._last_scores is not None:
            dct = snap.score_ct - self._last_scores[0]
            dt = snap.score_t - self._last_scores[1]
            if dct > 0 and dt <= 0:
                self._last_round_winner = "CT"
            elif dt > 0 and dct <= 0:
                self._last_round_winner = "T"
        self._last_scores = (snap.score_ct, snap.score_t)

    # ---- 运镜 ----
    def _handoff_cinematic(self, snap: Optional[Snapshot], now: float) -> bool:
        """结束本程序请求的宏，再允许 POV；不干预未由本程序触发的预览。"""
        request = self._cinematic_request
        if request is None:
            return True
        reason = ""
        if request['cancel'].is_set():
            reason = "用户关闭自动运镜"
        elif request.get('interrupted'):
            reason = "运镜输入中断"
        elif now < self._manual_until:
            reason = "人工接管"
        elif snap is not None and (snap.map_name, snap.round_num) != request["round"]:
            reason = "回合或地图变化"
        elif snap is not None and request["kind"] == "round" and snap.phase != "freezetime":
            reason = "出生点阶段结束"
        elif snap is not None and request["kind"] == "bomb" and snap.phase != "live":
            reason = "包点阶段结束"
        elif now >= request["until"]:
            reason = "运镜保护时间结束"
        if not reason or now < self._cinematic_retry_at:
            return False
        self._cinematic_retry_at = now + 1.0
        # MulNX 内置 Alt+P 单次停止所有预览。双击会清理运镜，绝不能使用。
        # 沿用授权门、停止信号、前台检查和 200ms 连击缓冲后的 Alt 保持。
        if not self.input.send_hotkey("alt+p", double=False):
            return False
        self._cinematic_request = None
        self._bomb_cam_until = 0.0
        if self.timeline is not None:
            self.timeline.end_cinematic(now, f"{reason}，已请求 MulNX 交还 POV")
        self.bus.info(f"{reason}，已发送 MulNX 停止预览：alt+p（单次），恢复 POV。")
        return True

    def _handle_triggers(self, snap: Snapshot, now: float) -> None:
        cancel = self._cinematic_cancel
        if cancel.is_set():
            # Observe disabled opportunities so re-enabling does not replay them.
            for event in self.triggers.poll(snap, now, ignore_retry=True):
                self.triggers.confirm(event, count=False)
            self._handoff_cinematic(snap, now)
            return
        if not self._handoff_cinematic(snap, now) or now < self._manual_until:
            return
        # 按用户要求，不再用“是否观战视角”阻断运镜/切换。
        for ev in self.triggers.poll(snap, now):
            site: Optional[str] = None
            side = ""
            spawn_default = False
            if ev.kind == "round":
                side = self._last_round_winner or str(
                    self.cfg.get("spawn_cam_default_side", "CT")
                ).upper()
                if side not in ("CT", "T"):
                    side = "CT"
                spawn_default = self._last_round_winner is None
                hotkey = self.cfg.get(f"hlae_spawn_{side.lower()}_key")
                mode_key = f"hlae_spawn_{side.lower()}_mode"
            else:
                # POV 优先：只要还有“可能交火”的存活敌我对，就不让包点运镜
                # 抢画面；若玩家只是隔楼层/距离过远，则允许包点运镜。
                policy = str(self.cfg.get("bomb_cam_policy", "pov_first")).lower()
                if policy != "always" and has_possible_engagement(snap, self.cfg):
                    if now - self._last_bomb_pov_log > 10.0:
                        self._last_bomb_pov_log = now
                        cd_pov = snap.bomb.countdown if snap.bomb else None
                        self.bus.info(
                            f"炸弹剩余 {cd_pov:.1f}s，场上仍有可交火目标："
                            f"优先玩家视角，跳过包点运镜。"
                        )
                    self.triggers.confirm(ev, count=False)
                    continue
                map_key = str(snap.map_name or "").split("/")[-1].lower()
                refs = self.cfg.get("bomb_site_refs") or {}
                site = detect_bomb_site(
                    map_key, snap.bomb.position if snap.bomb else None, self.cfg
                )
                if site is None:
                    if map_key not in refs:
                        # 未收录地图：本轮只提示一次，避免每秒重试刷屏
                        if now - self._last_bomb_site_log > 30.0:
                            self._last_bomb_site_log = now
                            self.bus.warning(
                                f"地图 {map_key} 没有 A/B 包点参考点，跳过包点运镜；"
                                f"可在 config.json 的 bomb_site_refs 补充。"
                            )
                        self.triggers.confirm(ev, count=False)
                    else:
                        # 地图已收录但炸弹位置缺失：下个 tick 再试
                        if now - self._last_bomb_site_log > 3.0:
                            self._last_bomb_site_log = now
                            self.bus.warning("炸弹位置缺失，暂时无法判定 A/B 包点，稍后重试。")
                    continue
                hotkey = self.cfg.get(f"hlae_bomb_{site.lower()}_key")
                mode_key = f"hlae_bomb_{site.lower()}_mode"
            if not hotkey:
                if now - self._last_trigger_fail > 3.0:
                    self._last_trigger_fail = now
                    self.bus.warning(f"{ev.kind} 运镜快捷键未配置，跳过本次触发。")
                continue
            double = str(self.cfg.get(mode_key, "double")).lower() == "double"
            mode_txt = "双击" if double else "单次"
            request = {
                    "kind": ev.kind, "round": (snap.map_name, snap.round_num),
                    "cancel": cancel,
                    "until": now + (30.0 if ev.kind == "round" else
                                    max(0.0, float(self.cfg["bomb_cam_grace"]))),
                }
            ok = self.input.send_hotkey(str(hotkey), double=double, cancel_event=cancel)
            if ok:
                self._cinematic_request = request
                self._cinematic_retry_at = 0.0
                self.triggers.confirm(ev)
                if ev.kind == "round":
                    why = f"默认 {side}" if spawn_default else f"上回合 {side} 胜"
                    self.bus.info(
                        f"回合开始（{snap.phase}），{why}，"
                        f"已发送 MulNX {side} 出生点运镜：{hotkey}（{mode_txt}）；"
                        f"若画面未变化：确认以管理员运行，或把该项改为单次再试"
                    )
                    if self.timeline is not None:
                        self.timeline.begin_cinematic(
                            story_mod.SHOT_SPAWN, f"side={side}", why,
                            "spawn_cam", now, expires_at=now + 30.0)
                else:
                    grace = float(self.cfg["bomb_cam_grace"])
                    if grace > 0:
                        self._bomb_cam_until = now + grace
                    cd = snap.bomb.countdown if snap.bomb else None
                    self.bus.info(
                        f"炸弹剩余 {cd:.1f}s，{site} 包点，已发送 MulNX 包点运镜：{hotkey}（{mode_txt}）"
                        + (f"（AI 切换让位 {grace:.1f}s）" if grace > 0 else "")
                        + "；若画面未变化：确认管理员权限/单次双击"
                    )
                    if self.timeline is not None:
                        self.timeline.begin_cinematic(
                            story_mod.SHOT_BOMB, f"site={site}",
                            f"炸弹剩余 {cd:.1f}s，{site} 包点" if cd is not None
                            else f"{site} 包点",
                            "bomb_cam", now,
                            expires_at=now + max(1.5, grace) + 1.0)
            else:
                if self.input.hotkey_started:
                    request['interrupted'] = True
                    self._cinematic_request = request
                    self._cinematic_retry_at = 0.0
                    self.triggers.confirm(ev, count=False)
                elif cancel.is_set():
                    self.triggers.confirm(ev, count=False)
                if not self.input.hotkey_started and not cancel.is_set() and now - self._last_trigger_fail > 3.0:
                    self._last_trigger_fail = now
                    self.bus.warning(f"{ev.kind} 运镜快捷键发送失败，稍后重试（窗口未聚焦？）")

    def _effective_min_shot(self) -> float:
        try:
            dwell = max(0.2, float(self.cfg.get("min_dwell_seconds", 1.0)))
            shot = max(0.0, float(self.cfg.get("min_shot_duration", 2.0)))
            return max(dwell, shot)
        except (TypeError, ValueError):
            return 2.0

    def _max_cuts(self) -> int:
        try:
            return max(0, int(float(self.cfg.get("max_cuts_per_10s", 3))))
        except (TypeError, ValueError):
            return 3

    def _recent_cut_count(self, now: float) -> int:
        cutoff = now - 10.0
        while self._cut_times and self._cut_times[0] < cutoff:
            self._cut_times.popleft()
        return len(self._cut_times)

    def _note_cut(self, now: float) -> None:
        self._cut_times.append(now)
        self._recent_cut_count(now)
        if len(self._cut_times) > 100:
            self._cut_times = deque(list(self._cut_times)[-100:])

    def _current_fight_lock(self, snap: Snapshot, now: float) -> bool:
        """当前 POV 正在交火/拆包时，不允许切走。

        交火判定不依赖“是否在候选名单里”，只看当前观察者：
        - 当前玩家正在拆包 → 锁定；
        - 敌人进入当前武器射程包络，且任一方近期掉血 → 锁定；
        - 双方都朝对方瞄准（aim_factor 都过阈值）→ 锁定。
        GSI 不提供攻击者，因此用“距离 + 最近掉血 + 双向瞄准”做保守近似。
        """
        if not bool(self.cfg.get("fight_lock_enabled", True)):
            return False
        slot = snap.observer_slot
        if slot is None:
            slot = self._last_switch_slot
        if slot is None:
            return False
        current = next(
            (p for p in snap.players.values() if p.slot == slot), None)
        if current is None or current.health <= 0 or not current.pos:
            return False
        if current.defusing:
            return True
        prof = weapon_profile(current)
        try:
            range_scale = max(0.4, min(
                2.0, float(self.cfg.get("engage_range", 1500.0)) / 1500.0))
            fov = float(self.cfg.get("aim_fov_deg", 70.0))
            aim_threshold = max(0.3, min(
                1.0, float(self.cfg.get("fight_lock_aim", 0.60))))
        except (TypeError, ValueError):
            range_scale, fov, aim_threshold = 1.0, 70.0, 0.60
        envelope = prof.envelope * range_scale
        vlimit = _vertical_limit(self.cfg, snap.map_name)
        for enemy in snap.players.values():
            if enemy.health <= 0 or enemy.team == current.team:
                continue
            if not enemy.pos:
                continue
            d = _dist(current.pos, enemy.pos)
            if d is None or d > envelope:
                continue
            if abs(current.pos[2] - enemy.pos[2]) > vlimit:
                continue
            if (self._damage_recent(current.steamid)
                    or self._damage_recent(enemy.steamid)):
                return True
            aim_c = aim_factor(current, enemy, fov)
            aim_e = aim_factor(enemy, current, fov)
            if min(aim_c, aim_e) >= aim_threshold:
                return True
        return False

    def _death_fallback_slot(self, snap: Snapshot,
                             cands: list[Candidate]) -> Optional[int]:
        """当前观察对象已阵亡时，切到离尸体最近的敌人（通常是击杀者）。

        CS2 有时会在目标死亡后清空 player.observer_slot，因此额外记住
        最后一次“活着被观察”的 slot/位置，用它做死亡回切。
        """
        players = list(snap.players.values())
        observed = next(
            (p for p in players if p.slot == snap.observer_slot), None
        )
        team = ""
        pos: Optional[tuple[float, float, float]] = None

        if observed is not None and observed.health > 0:
            self._last_observed_info = (observed.slot, observed.team, observed.pos)
            return None
        if observed is not None:
            team, pos = observed.team, observed.pos
            self._last_observed_info = (observed.slot, team, pos)
        else:
            # observer_slot 变空/指向已消失的玩家：用最后一次存活观察记录
            if self._last_observed_info is None:
                return None
            last_slot, team, pos = self._last_observed_info
            if snap.observer_slot is not None and snap.observer_slot != last_slot:
                return None
            current = next((p for p in players if p.slot == last_slot), None)
            if current is not None and current.health > 0:
                self._last_observed_info = (current.slot, current.team, current.pos)
                return None

        alive = [p for p in players
                 if p.health > 0 and p.slot is not None and p.team in ("CT", "T")]
        if not alive:
            return None
        enemies = [p for p in alive if team and p.team != team]
        pool = enemies or alive
        if pos:
            best = None
            best_d = 1e18
            for p in pool:
                if not p.pos:
                    continue
                d = _dist(pos, p.pos)
                if d is not None and d < best_d:
                    best_d = d
                    best = p
            if best is not None:
                return best.slot
        if cands:
            return cands[0].slot
        return pool[0].slot

    def _utility_is_hot(self, now: float,
                        cands: list[Candidate]) -> bool:
        """场上是否有近期伤害或高威胁候选，用于微调道具镜头缓冲。"""
        try:
            lookback = max(0.5, min(10.0, float(
                self.cfg.get("utility_cam_fight_lookback", 2.5))))
        except (TypeError, ValueError):
            lookback = 2.5
        for t in self._damage_at.values():
            try:
                if now - float(t) <= lookback:
                    return True
            except (TypeError, ValueError):
                continue
        if cands:
            try:
                threshold = max(0.05, float(
                    self.cfg.get("switch_min_score", 0.30))) * 0.9
            except (TypeError, ValueError):
                threshold = 0.27
            if float(getattr(cands[0], "score", 0.0)) >= threshold:
                return True
        return False

    def _utility_buffers(self, hot: bool) -> tuple[float, float]:
        """返回 (预切确认, 投掷后保持)；场上有交火时缩短，安静时放宽。"""
        try:
            pre = float(self.cfg.get("utility_cam_pre_buffer", 0.35))
            post = float(self.cfg.get("utility_cam_post_hold", 0.35))
        except (TypeError, ValueError):
            pre, post = 0.35, 0.35
        pre = max(0.25, min(0.50, pre))
        post = max(0.25, min(0.50, post))
        if hot:
            pre = max(0.25, pre - 0.10)
            post = max(0.25, post - 0.10)
        else:
            pre = min(0.50, pre + 0.15)
            post = min(0.50, post + 0.15)
        return pre, post

    def _try_utility_cut(self, snap: Snapshot, cands: list[Candidate],
                         now: float, status: dict[str, Any]) -> bool:
        """开局/控图阶段，优先拍手持或刚投掷道具的选手。

        手持道具时提前切过去；投掷后保留 post_hold 缓冲。投掷者可能不在
        score_candidates 里（例如开局在后场扔烟），所以这里走独立规则，
        但仍遵守最短停留、切率、硬间隔和重复保护。
        """
        if not bool(self.cfg.get("utility_cam_enabled", True)):
            return False
        try:
            phase = str(self.story.state.phase)
        except Exception:
            return False
        if phase not in ("opening", "control"):
            return False

        hot = self._utility_is_hot(now, cands)
        pre_buffer, post_hold = self._utility_buffers(hot)
        action = self.utility_tracker.recent_action(
            now, pre_buffer, post_hold)
        if action is None:
            return False
        player = next(
            (p for p in snap.players.values()
             if getattr(p, "slot", None) == action.slot
             and int(getattr(p, "health", 0) or 0) > 0),
            None,
        )
        if player is None:
            return False

        current_slot = snap.observer_slot
        if (current_slot is None and self._last_switch_at is not None
                and now - self._last_switch_at < 5.0):
            current_slot = self._last_switch_slot
        if current_slot == action.slot:
            self._utility_hold_slot = action.slot
            self._utility_hold_until = max(
                self._utility_hold_until,
                now + (post_hold if action.kind == "throw" else pre_buffer),
            )
            if action.kind == "hold":
                self.utility_tracker.mark_hold_used(action)
            status["reason"] = ("道具镜头：保持投掷者"
                                if action.kind == "throw"
                                else "道具镜头：已在手持道具选手")
            self._last_reason = status["reason"]
            return True
        if now < self._press_retry_after:
            return False
        try:
            hard_floor = max(0.2, float(
                self.cfg.get("hard_min_switch_interval", 0.2)))
            repeat_guard = max(0.0, float(
                self.cfg.get("repeat_guard_seconds", 4.0)))
            min_interval = max(0.0, min(30.0, float(
                self.cfg.get("utility_cam_min_interval", 3.0))))
            max_per_round = max(0, int(
                float(self.cfg.get("utility_cam_max_per_round", 4))))
        except (TypeError, ValueError):
            hard_floor, repeat_guard, min_interval, max_per_round = 0.2, 4.0, 3.0, 4
        if (self._last_switch_at is not None
                and now - self._last_switch_at < hard_floor):
            return False
        if (self._last_switch_slot == action.slot
                and self._last_switch_at is not None
                and now - self._last_switch_at < repeat_guard):
            return False

        round_key = f"{snap.map_name}|{snap.round_num}"
        if round_key != self._utility_round_key:
            self._utility_round_key = round_key
            self._utility_cuts = 0
            self._last_utility_cut_at = 0.0
            self._utility_hold_until = 0.0
            self._utility_hold_slot = None
        if max_per_round > 0 and self._utility_cuts >= max_per_round:
            return False
        if (self._last_utility_cut_at > 0
                and now - self._last_utility_cut_at < min_interval):
            return False

        verb = "手持" if action.kind == "hold" else "投掷"
        reason = (f"开局控图：{player.name or format_slot(action.slot)} "
                  f"{verb}{action.label}")
        if not self.input.press_slot(action.slot):
            self._press_retry_after = now + 1.0
            status["reason"] = "道具镜头按键未发送：窗口未聚焦或依赖缺失"
            return False
        self._last_switch_at = now
        self._last_switch_slot = action.slot
        self._last_utility_cut_at = now
        self._utility_cuts += 1
        self._note_cut(now)
        self._utility_hold_slot = action.slot
        self._utility_hold_until = max(
            self._utility_hold_until,
            now + (post_hold if action.kind == "throw" else pre_buffer),
        )
        if action.kind == "hold":
            self.utility_tracker.mark_hold_used(action)
        status["target"] = action.slot
        status["target_name"] = player.name
        status["reason"] = reason
        status["source"] = "utility"
        if self.timeline is not None:
            self.timeline.expect_switch(action.slot, reason, "utility", now)
        self.bus.info(f"切换视角 → {format_slot(action.slot)}（{reason}）")
        self._last_reason = reason
        return True

    # ---- 核心：评分 + 决策 + 按键 ----
    def _decide_and_switch(self, snap: Snapshot, now: float,
                           status: dict[str, Any]) -> None:
        if not self._handoff_cinematic(snap, now):
            status["reason"] = "MulNX 运镜播放中，或等待停止预览后恢复 POV"
            return
        if not snap.players:
            status["reason"] = "GSI 中缺少 allplayers 数据"
            return
        # 按用户要求，移除“当前不是观战视角就跳过”的判定；即使 GSI 没给出
        # observer_slot，也继续按评分切人。
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

        cands = self._score_candidates(snap, now)
        # 规则前两名分数接近时，用离线随机森林模型仲裁；强规则不交给模型。
        model_slot: Optional[int] = None
        if (self.model_arbiter is not None and len(cands) >= 2
                and cands[0].score >= float(self.cfg["switch_min_score"])):
            model_slot = self.model_arbiter.arbitrate(
                cands, cands[0].score, snap.observer_slot)
            if model_slot is not None:
                idx = next((i for i, c in enumerate(cands) if c.slot == model_slot), None)
                if idx is not None and idx > 0:
                    cands = [cands[idx]] + cands[:idx] + cands[idx + 1:]
        status["candidates"] = len(cands)
        death_slot = self._death_fallback_slot(snap, cands)

        # 专业导播约束：交火锁定 > 最短停留 > 10 秒切率上限。
        # 当前观察者阵亡时允许立即回切（death_slot 非空），避免停在尸体。
        if death_slot is None:
            if self._current_fight_lock(snap, now):
                status["reason"] = "当前镜头正在交火，保持不切"
                if now - self._last_fight_lock_log > 3.0:
                    self._last_fight_lock_log = now
                    self.bus.info(
                        "当前 POV 正在交火/拆包，保持镜头不切换。")
                self._last_reason = status["reason"]
                return
            min_shot = self._effective_min_shot()
            if (self._last_switch_at is not None
                    and now - self._last_switch_at < min_shot):
                status["reason"] = f"镜头最短停留 {min_shot:.1f}s"
                return
            max_cuts = self._max_cuts()
            if max_cuts > 0 and self._recent_cut_count(now) >= max_cuts:
                status["reason"] = f"10 秒内切换已达上限（{max_cuts} 次）"
                return
            current_slot = snap.observer_slot
            if current_slot is None:
                current_slot = self._last_switch_slot
            if (now < self._utility_hold_until
                    and self._utility_hold_slot is not None
                    and current_slot == self._utility_hold_slot):
                status["reason"] = "道具镜头缓冲"
                return
            if self._try_utility_cut(snap, cands, now, status):
                return

        if not cands:
            if (death_slot is not None and death_slot != snap.observer_slot
                    and now >= self._press_retry_after):
                try:
                    hard_floor = max(0.2, float(
                        self.cfg.get("hard_min_switch_interval", 0.2)))
                except (TypeError, ValueError):
                    hard_floor = 0.2
                recent_enough = (self._last_switch_at is None
                                 or now - self._last_switch_at >= hard_floor)
                not_just_pressed = not (
                    self._last_switch_slot == death_slot
                    and self._last_switch_at is not None
                    and now - self._last_switch_at < 2.0
                )
                if recent_enough and not_just_pressed:
                    if self.input.press_slot(death_slot):
                        self._last_switch_at = now
                        self._last_switch_slot = death_slot
                        self._note_cut(now)
                        status["target"] = death_slot
                        status["reason"] = "观战对象已阵亡，切到最近敌人"
                        if self.timeline is not None:
                            self.timeline.expect_switch(
                                death_slot, status["reason"], "death", now)
                        self.bus.info(
                            f"切换视角 → {format_slot(death_slot)}"
                            f"（观战对象已阵亡，切到最近敌人）"
                        )
                        return
                    self._press_retry_after = now + 1.0
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

        from director_core.scheduler import choose_compatible
        decision = choose_compatible(
            ordered, snap.observer_slot, now,
            self._last_switch_at, self._last_switch_slot, self.cfg,
        )
        # 当前观察对象阵亡：即使没有“即将交火”的高分目标，也要切到
        # 离尸体最近的敌人（通常就是击杀者），否则会一直停在尸体视角。
        if (decision.slot is None and death_slot is not None
                and death_slot != snap.observer_slot):
            try:
                hard_floor = max(0.2, float(
                    self.cfg.get("hard_min_switch_interval", 0.2)))
            except (TypeError, ValueError):
                hard_floor = 0.2
            recent_enough = (self._last_switch_at is None
                             or now - self._last_switch_at >= hard_floor)
            not_just_pressed = not (
                self._last_switch_slot == death_slot
                and self._last_switch_at is not None
                and now - self._last_switch_at < 2.0
            )
            if recent_enough and not_just_pressed:
                decision = Decision(
                    death_slot, "观战对象已阵亡，切到最近敌人", None, "death"
                )

        status["reason"] = decision.reason

        if decision.slot is not None:
            if now < self._press_retry_after:
                # 按键失败后 1 秒内不再重试，避免每帧都去抢焦点
                status["reason"] = "按键失败，等待重试"
                return
            cand = decision.candidate
            ok = self.input.press_slot(decision.slot)
            if ok and hint and hint[0] == decision.slot:
                self.arbiter.applied_hint(decision.slot,now)
            if ok:
                self._last_switch_at = now
                self._last_switch_slot = decision.slot
                self._note_cut(now)
                status["target"] = decision.slot
                status["target_name"] = cand.name if cand else ""
                if decision.source == "death":
                    if self.timeline is not None:
                        self.timeline.expect_switch(
                            decision.slot, decision.reason, "death", now)
                    self.bus.info(
                        f"切换视角 → {format_slot(decision.slot)}"
                        f"（观战对象已阵亡，切到最近敌人）"
                    )
                else:
                    source = ("AI" if (hint and hint[0] == decision.slot)
                              else ("模型" if model_slot == decision.slot else "规则"))
                    timeline_source = {"AI": "ai", "模型": "model",
                                       "规则": "rule"}.get(source, "rule")
                    if self.timeline is not None:
                        self.timeline.expect_switch(
                            decision.slot, decision.reason, timeline_source, now)
                    self.bus.info(
                        f"切换视角 → {format_slot(decision.slot)} "
                        f"{cand.name if cand else '?'}"
                        f" [{source}] " + (format_candidate(cand) if cand else "")
                    )
                    if self.decision_logger is not None:
                        self.decision_logger.record(
                            snap, cands, decision.slot, "switch",
                            decision.reason, snap.observer_slot, now,
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
            if (self.decision_logger is not None
                    and decision.source != "death"):
                target_slot = shown if shown is not None else snap.observer_slot
                self.decision_logger.record(
                    snap, cands, target_slot, "hold",
                    decision.reason, snap.observer_slot, now,
                )

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
    """生成 GSI 配置的字段字典（测试/兼容用）；实际写盘用 KeyValues。"""
    return {
        "uri": f"http://127.0.0.1:{int(port)}",
        "timeout": 5.0,
        "buffer": 0.1,
        "throttle": 0.1,
        "heartbeat": 2.0,
        "data": {name: 1 for name in GSI_REQUIRED_DATA},
    }


def gsi_config_text(port: int) -> str:
    """
    CS2 的 gamestate_integration_*.cfg 必须是 Valve KeyValues 格式，
    不能写 JSON。JSON 配置会被 CS2 静默忽略，表现为“GSI 一直等待数据”。
    """
    lines = [
        '"CS2 AI Director"',
        "{",
        f'    "uri" "http://127.0.0.1:{int(port)}"',
        '    "timeout" "5.0"',
        '    "buffer" "0.1"',
        '    "throttle" "0.1"',
        '    "heartbeat" "2.0"',
        '    "data"',
        "    {",
    ]
    for name in GSI_REQUIRED_DATA:
        lines.append(f'        "{name}" "1"')
    lines.extend(("    }", "}", ""))
    return "\n".join(lines)


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


def gsi_port_conflicts(cfg_dir: Path, port: int,
                       own_name: str = GSI_CFG_NAME) -> list[str]:
    """扫描同目录其他 GSI 配置，返回与本程序同端口的文件名。"""
    conflicts: list[str] = []
    try:
        files = list(Path(cfg_dir).glob("gamestate_integration*.cfg"))
    except Exception:
        return []
    for f in files:
        if f.name == own_name:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        m = re.search(r'"uri"\s*:?\s*"https?://[^":]+:(\d+)', text)
        if not m:
            continue
        try:
            if int(m.group(1)) == int(port):
                conflicts.append(f.name)
        except (TypeError, ValueError):
            continue
    return sorted(conflicts)


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
    text = gsi_config_text(port)
    target.write_text(text, encoding="utf-8")
    # 立刻回读校验，防止写入半截/编码问题（KeyValues 不是 JSON，不能 json.loads）
    check = target.read_text(encoding="utf-8")
    if '"allplayers_position"' not in check:
        raise RuntimeError("GSI 配置校验失败：缺少 allplayers_position")
    if f'"http://127.0.0.1:{int(port)}"' not in check:
        raise RuntimeError("GSI 配置校验失败：uri 不正确")
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

from desktop_ui import ScrollableFrame as _ScrollableFrame


class DirectorApp:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = copy.deepcopy(cfg)
        self._config_version = 0
        from account_client import default_account
        self.account = default_account()
        self.bus = LogBus()
        self.pro = pro_mod.ProDirector(cfg)
        self.decision_logger = dm.DecisionLogger(cfg)
        self.model_arbiter = dm.ModelArbiter(cfg)
        self.timeline = story_mod.ShotTimeline(cfg, log_fn=self.bus.info, async_writes=True)
        self.engine: Optional[DirectorEngine] = None
        self._closing = False
        self._detecting = False
        self._training = False
        self._last_gui_warn = 0.0
        self._last_pro_log_refresh = 0.0
        self._last_model_check = 0.0

        self.root = tk.Tk()
        from desktop_tasks import Tasks
        self.tasks = Tasks(self.root)
        self.root.title(f"{APP_TITLE} v{VERSION} · {APP_SUBTITLE}")
        self.root.geometry("1060x940")
        from desktop_ui import fit_window
        fit_window(self.root)
        from preferences import Preferences
        self.preferences = Preferences(app_data_dir())
        from diagnostics_runtime import attach
        attach(self)
        from desktop_ui import restore_window
        restore_window(self, 'director', 1060, 850)
        self.root.report_callback_exception = self._on_tk_exception
        self._init_style()
        self._set_window_icon()

        self.var_exe = tk.StringVar(value=str(cfg.get("cs2_exe", "")))

        def _hotkey_vars(value: Any) -> tuple[tk.StringVar, tk.StringVar]:
            mod, key = parse_selectable_hotkey(str(value or ""))
            return (tk.StringVar(value=mod or "请选择"),
                    tk.StringVar(value=key or "请选择"))

        self.var_spawn_ct_mod, self.var_spawn_ct_keyname = _hotkey_vars(
            cfg.get("hlae_spawn_ct_key") or cfg.get("hlae_round_key")
        )
        self.var_spawn_t_mod, self.var_spawn_t_keyname = _hotkey_vars(
            cfg.get("hlae_spawn_t_key") or cfg.get("hlae_round_key")
        )
        self.var_bomb_a_mod, self.var_bomb_a_keyname = _hotkey_vars(
            cfg.get("hlae_bomb_a_key") or cfg.get("hlae_bomb_key")
        )
        self.var_bomb_b_mod, self.var_bomb_b_keyname = _hotkey_vars(
            cfg.get("hlae_bomb_b_key") or cfg.get("hlae_bomb_key")
        )
        self.var_spawn_ct_mode = tk.StringVar(
            value="双击" if str(cfg.get("hlae_spawn_ct_mode")
                              or cfg.get("hlae_round_mode", "double")).lower() == "double" else "单次"
        )
        self.var_spawn_t_mode = tk.StringVar(
            value="双击" if str(cfg.get("hlae_spawn_t_mode")
                              or cfg.get("hlae_round_mode", "double")).lower() == "double" else "单次"
        )
        self.var_bomb_a_mode = tk.StringVar(
            value="双击" if str(cfg.get("hlae_bomb_a_mode", "double")).lower() == "double" else "单次"
        )
        self.var_bomb_b_mode = tk.StringVar(
            value="双击" if str(cfg.get("hlae_bomb_b_mode", "double")).lower() == "double" else "单次"
        )
        self.var_obs_host = tk.StringVar(value=str(cfg.get("obs_host", "127.0.0.1")))
        self.var_obs_port = tk.StringVar(value=str(cfg.get("obs_port", 4455)))
        self.var_obs_pwd = tk.StringVar(value=str(cfg.get("obs_password", "")))
        self.var_narrative_enabled = tk.BooleanVar(
            value=bool(cfg.get("narrative_enabled", True)))
        self.var_obs_narrative_enabled = tk.BooleanVar(
            value=bool(cfg.get("obs_narrative_enabled", False)))
        self.var_obs_narrative_source = tk.StringVar(
            value=str(cfg.get("obs_narrative_source", "")))
        self.var_pro_enabled = tk.BooleanVar(value=bool(cfg.get("event_actions_enabled", True)))
        self.var_pause_hotkey = tk.StringVar(value=str(cfg.get("pause_hotkey", "ctrl+alt+p")))
        self.var_director_hotkey = tk.StringVar(value=str(cfg.get('director_toggle_hotkey', 'ctrl+alt+d')))
        self.var_cinematics_hotkey = tk.StringVar(value=str(cfg.get('cinematics_toggle_hotkey', 'ctrl+alt+m')))
        self.var_cinematics = tk.BooleanVar(value=bool(cfg.get('cinematics_enabled', True)))
        from desktop_hotkeys import ControlHotkeys
        self._control_hotkeys = ControlHotkeys(keyboard if HAS_KEYBOARD else None)
        self._shortcuts_note = ''
        self.var_api = tk.StringVar(value='')
        self.var_mode = tk.StringVar(value='off')
        from arbitration_runtime import PersonalKeyStore
        self.personal_keys = PersonalKeyStore(app_data_dir())
        self.var_cloud_status = tk.StringVar(value='关闭；登录后可选平台额度或自有 API。密钥仅保存在本机。')
        self.var_model = tk.StringVar(value=str(cfg.get("deepseek_model", "deepseek-flash")))
        self.var_ai = tk.BooleanVar(value=bool(cfg.get("ai_enabled", True)))
        self.var_learn = tk.BooleanVar(value=bool(cfg.get("demo_model_enabled", True)))
        self.var_dry = tk.BooleanVar(value=bool(cfg.get("dry_run", False)))
        self.var_port = tk.StringVar(value=str(cfg.get("gsi_port", 3000)))
        self.var_score = tk.StringVar(value=str(cfg.get("switch_min_score", 0.30)))
        self.var_pause = tk.StringVar(value=str(cfg.get("manual_pause_seconds", 10.0)))
        self.var_utility_cam = tk.BooleanVar(
            value=bool(cfg.get("utility_cam_enabled", True)))

        self._build()
        self._apply_preferences()
        from training_data_runtime import TrainingDataRuntime
        self.training_data = TrainingDataRuntime(csv_path=self.decision_logger.path, identity_provider=self.account.upload_identity, binding_provider=self.account.binding)
        self.account.start()
        self.pro.obs.authorization_gate = self.account
        self.decision_logger.mature_sink = self.training_data.recorder.offer
        self._register_control_hotkeys()
        self._update_model_label()
        self._refresh_deps()
        self._refresh_gsi_status()
        self.bus.info(f"配置目录：{app_data_dir()}")
        self.bus.info("使用步骤：选择 cs2.exe → 一键安装 GSI → 重启 CS2 → 填写 MulNX 快捷键 → 启动导播。")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        schedule(self.root,200, self._poll)
        from client_update_ui import install as install_updates
        install_updates(self)

    def _apply_preferences(self):
        from desktop_ui import theme
        prefs = self.preferences.read()
        self._ui_preferences = prefs
        theme(self.root, prefs['theme'], prefs['font_size'])
        self._follow_logs = prefs['follow_logs']
        from desktop_ui import PALETTES
        self._colors = PALETTES[prefs['theme']]
        for widget in self._developer_widgets:
            widget.grid() if prefs['developer_mode'] else widget.grid_remove()
        for tab in self._developer_tabs:
            self.notebook.add(tab) if prefs['developer_mode'] else self.notebook.hide(tab)

    def _on_tk_exception(self, exc: Any, val: Any, tb: Any) -> None:
        from diagnostics_runtime import capture_gui
        capture_gui(self, exc, tb)
        """tkinter 回调里的未捕获异常不能只打到 stderr（pythonw 下看不见）。"""
        try:
            self.bus.error(f"GUI 回调异常：{getattr(exc, '__name__', exc)}: {val}")
        except Exception:
            pass

    def _init_style(self) -> None:
        from desktop_ui import theme
        self.root.option_add('*Font', ('Microsoft YaHei UI', 10))
        from desktop_ui import PALETTES
        self._colors = PALETTES[self.preferences.read()['theme']]
        theme(self.root, self.preferences.read()['theme'])

    def _checkbutton(self, parent: Any, text: str,
                     variable: tk.BooleanVar, **kwargs: Any) -> tk.Checkbutton:
        """Use the native check mark; ttk clam renders an 'x' on Windows."""
        return tk.Checkbutton(
            parent, text=text, variable=variable,
            bg=UI_CARD, fg=UI_INK,
            activebackground=UI_CARD, activeforeground=UI_INK,
            selectcolor="#ffffff", bd=0, highlightthickness=0,
            font=("Microsoft YaHei UI", 9), anchor="w", **kwargs)

    def _unregister_control_hotkeys(self) -> None:
        self._control_hotkeys.close()

    def _register_control_hotkeys(self) -> None:
        try:
            failed = self._control_hotkeys.configure(self.cfg)
            active = self._control_hotkeys.registered
            self._shortcuts_note = '松开触发 · 启停导播：'+active.get('director', '未启用').upper()+' · 运镜：'+active.get('cinematics', '未启用').upper()
            if failed:
                self.bus.warning('快捷键未注册：'+'、'.join(failed)+'；仍可使用界面按钮。')
        except ValueError as error:
            self.bus.warning('控制快捷键未更新：'+str(error))
            self._shortcuts_note = '快捷键配置有误，请在配置页修改后保存。'
        self._refresh_cinematic_controls()

    def _poll_control_hotkeys(self) -> None:
        generation = self._control_hotkeys.generation
        for action in self._control_hotkeys.drain():
            if self._closing or self._control_hotkeys.generation != generation: break
            if action == 'director': self._toggle_director()
            elif action == 'cinematics': self._toggle_cinematics()
            elif action == 'pause': self._on_pause_hotkey()

    def _toggle_director(self) -> None:
        if self._closing or getattr(self, '_starting', False) or 'stop' in self.tasks.running: return
        if self.engine is None: self._start()
        else: self._stop()

    def _toggle_cinematics(self) -> None:
        self._set_cinematics_enabled(not self.var_cinematics.get())

    def _set_cinematics_enabled(self, enabled: bool) -> None:
        if self._closing: return
        enabled = bool(enabled)
        if enabled and self.engine is not None and any(not str(self.engine.cfg.get(key) or '').strip() for key in
                ('hlae_spawn_ct_key','hlae_spawn_t_key','hlae_bomb_a_key','hlae_bomb_b_key')):
            self.var_cinematics.set(False)
            self._refresh_cinematic_controls()
            messagebox.showwarning('运镜尚未配置', '当前导播没有完整的 MulNX 运镜配置。请停止导播、补齐运镜键后重新启动；玩家视角切换可继续使用。', parent=self.root)
            return
        self.var_cinematics.set(enabled)
        self.cfg['cinematics_enabled'] = enabled
        if self.engine is not None: self.engine.set_cinematics_enabled(enabled)
        self._refresh_cinematic_controls()
        self.bus.info('自动运镜已'+('开启' if enabled else '关闭')+'；玩家视角自动切换保持。')
        try: save_config(copy.deepcopy(self.cfg))
        except Exception as error: self.bus.warning('运镜开关本次已生效，但未保存：'+type(error).__name__)

    def _refresh_cinematic_controls(self) -> None:
        enabled = bool(self.var_cinematics.get())
        pending = bool(self.engine and self.engine.status().get('cinematic_pending_stop'))
        self.btn_cinematics.configure(text='运镜：'+('开启' if enabled else '关闭'))
        note = '等待 CS2 前台停止本程序的运镜预览 · ' if pending else ''
        self.lbl_control_shortcuts.configure(text=note+self._shortcuts_note)

    def _on_pause_hotkey(self) -> None:
        try:
            paused = self.pro.toggle_pause()
            self.bus.info(f"暂停热键：{'暂停开始' if paused else '暂停结束'}")
        except Exception as e:
            self.bus.warning(f"暂停热键执行失败：{type(e).__name__}: {e}")

    def _set_window_icon(self) -> None:
        try:
            ico = asset_path("yuzi.ico")
            if ico.is_file():
                self.root.iconbitmap(str(ico))
        except Exception:
            pass
        try:
            png = asset_path("yuzi_logo_64.png")
            if png.is_file():
                self._icon_img = tk.PhotoImage(file=str(png))
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    # ---- 界面 ----
    # ---- 专业导播页 ----
    def _build_pro(self, parent: Any) -> None:
        parent.columnconfigure(0, weight=1)

        heading = ttk.Frame(parent)
        heading.grid(row=0, column=0, sticky="ew", pady=(2, 10))
        ttk.Label(heading, text="专业导播", style='H2.TLabel').pack(anchor="w")
        ttk.Label(heading, text="OBS、事件动作、回合叙事与镜头指标。",
                  style='Caption.TLabel').pack(anchor="w", pady=(2, 0))

        lf = ttk.LabelFrame(parent, text="OBS WebSocket", padding=10)
        lf.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(lf, text="地址").grid(row=0, column=0, sticky="w")
        ttk.Entry(lf, textvariable=self.var_obs_host, width=16).grid(
            row=0, column=1, sticky="w", padx=4)
        ttk.Label(lf, text="端口").grid(row=0, column=2, sticky="w")
        ttk.Entry(lf, textvariable=self.var_obs_port, width=7).grid(
            row=0, column=3, sticky="w", padx=4)
        ttk.Label(lf, text="密码").grid(row=0, column=4, sticky="w")
        ttk.Entry(lf, textvariable=self.var_obs_pwd, show="*", width=14).grid(
            row=0, column=5, sticky="w", padx=4)
        ttk.Button(lf, text="连接", width=7, command=self._pro_connect).grid(
            row=0, column=6, padx=(8, 2))
        ttk.Button(lf, text="断开", width=7, command=self._pro_disconnect).grid(
            row=0, column=7)
        self.lbl_obs = ttk.Label(lf, text="OBS：未连接", foreground="#555555")
        self.lbl_obs.grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))
        narr = ttk.Frame(lf)
        narr.grid(row=2, column=0, columnspan=8, sticky="w", pady=(4, 0))
        self._checkbutton(narr, "启用回合叙事", self.var_narrative_enabled).pack(
            side="left")
        self._checkbutton(narr, "推送 OBS", self.var_obs_narrative_enabled).pack(
            side="left", padx=(12, 4))
        ttk.Label(narr, text="文本源").pack(side="left")
        ttk.Entry(narr, textvariable=self.var_obs_narrative_source, width=18).pack(
            side="left", padx=4)
        ttk.Label(narr, text="（OBS 文本源名，可留空只显示在应用内）",
                  foreground=UI_MUTED).pack(side="left")

        lf3 = ttk.LabelFrame(parent, text="事件动作（独立于 AI 决策）", padding=10)
        lf3.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        lf3.columnconfigure(0, weight=1)
        top = ttk.Frame(lf3)
        top.grid(row=0, column=0, sticky="ew")
        self._checkbutton(top, "启用事件动作", self.var_pro_enabled).pack(
            side="left")
        ttk.Button(top, text="添加", width=6, command=self._pro_add_rule).pack(
            side="left", padx=(12, 2))
        ttk.Button(top, text="编辑", width=6, command=self._pro_edit_rule).pack(side="left")
        ttk.Button(top, text="删除", width=6, command=self._pro_delete_rule).pack(side="left")
        ttk.Button(top, text="启用/禁用", width=9, command=self._pro_toggle_rule).pack(
            side="left", padx=2)
        ttk.Button(top, text="测试选中", width=8, command=self._pro_test_rule).pack(
            side="left", padx=2)
        ttk.Button(top, text="保存预设…", command=self._pro_save_preset).pack(
            side="right", padx=2)
        ttk.Button(top, text="加载预设…", command=self._pro_load_preset).pack(side="right")

        hot = ttk.Frame(lf3)
        hot.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(hot, text="暂停热键").pack(side="left")
        ttk.Entry(hot, textvariable=self.var_pause_hotkey, width=16).pack(
            side="left", padx=4)
        ttk.Button(hot, text="暂停开始", command=lambda: self._pro_pause(True)).pack(
            side="left", padx=(8, 2))
        ttk.Button(hot, text="暂停结束", command=lambda: self._pro_pause(False)).pack(
            side="left")
        ttk.Label(hot, text="CS2 GSI 不提供暂停状态；用热键/按钮触发暂停规则",
                  foreground=UI_MUTED).pack(side="left", padx=8)

        cols = ("event", "name", "actions", "enabled")
        self.rule_tree = ttk.Treeview(lf3, columns=cols, show="headings", height=5)
        for col, text, width in (
            ("event", "事件", 110), ("name", "规则名", 220),
            ("actions", "动作", 330), ("enabled", "启用", 50),
        ):
            self.rule_tree.heading(col, text=text)
            self.rule_tree.column(col, width=width, anchor="w")
        self.rule_tree.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        lf5 = ttk.LabelFrame(parent, text="决策模型（内置训练工作台）", padding=10)
        lf5.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(lf5, text="训练数据状态", command=lambda: __import__("desktop_ui").settings(self)).pack(side="right")
        ttk.Button(lf5, text="打开训练工作台…", command=self._open_trainer).pack(side="left")
        ttk.Button(lf5, text="重新加载模型", command=self._reload_model).pack(
            side="left", padx=6)
        ttk.Button(lf5, text="校准线上模型", command=self._calibrate_online).pack(
            side="left", padx=(0, 6))
        self.lbl_model = ttk.Label(lf5, text="随机森林：检查中…", foreground="#555555")
        self.lbl_model.pack(side="left", padx=6)

        lf6 = ttk.LabelFrame(parent, text="镜头时间线 / 专业指标（Phase 1）", padding=10)
        lf6.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        lf6.columnconfigure(0, weight=1)
        self.lbl_pro_shot = ttk.Label(
            lf6, text="当前镜头：-    故事：-", foreground="#444444",
            justify="left", wraplength=900)
        self.lbl_pro_shot.grid(row=0, column=0, sticky="w")
        self.lbl_pro_metrics = ttk.Label(
            lf6, text="专业指标：击杀覆盖 -    交火前覆盖 -    尸体 -s    "
                      "切换 -/min    POV均长 -s    人工 -",
            foreground="#555555", justify="left", wraplength=900)
        self.lbl_pro_metrics.grid(row=1, column=0, sticky="w", pady=(3, 0))
        row = ttk.Frame(lf6)
        row.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(row, text="打开数据目录",
                   command=self._open_timeline_dir).pack(side="left")
        ttk.Label(row, text="shot_timeline.csv / shot_events.csv / round_metrics.csv / "
                            "director_metrics.json",
                  foreground="#777777").pack(side="left", padx=8)
        self._pro_developer_widgets = [lf5, lf6]
        self._refresh_rules()

    def _build_pro_logs(self, parent: Any) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(bar, text="类别").pack(side="left")
        self.var_log_cat = tk.StringVar(value="全部")
        ttk.Combobox(bar, textvariable=self.var_log_cat, state="readonly", width=12,
                     values=("全部",) + pro_mod.ProLog.CATEGORIES).pack(
            side="left", padx=4)
        ttk.Label(bar, text="级别").pack(side="left", padx=(8, 0))
        self.var_log_level = tk.StringVar(value="全部")
        ttk.Combobox(bar, textvariable=self.var_log_level, state="readonly", width=7,
                     values=("全部", "INFO", "WARN", "ERROR")).pack(side="left", padx=4)
        ttk.Label(bar, text="时间").pack(side="left", padx=(8, 0))
        self.var_log_window = tk.StringVar(value="全部")
        ttk.Combobox(bar, textvariable=self.var_log_window, state="readonly", width=7,
                     values=("全部", "1 分钟", "5 分钟", "30 分钟")).pack(
            side="left", padx=4)
        ttk.Label(bar, text="关键字").pack(side="left", padx=(8, 0))
        self.var_log_kw = tk.StringVar()
        ttk.Entry(bar, textvariable=self.var_log_kw, width=22).pack(side="left", padx=4)
        ttk.Button(bar, text="刷新", command=self._refresh_pro_logs).pack(side="left", padx=6)
        from desktop_ui import ThemedScrolledText
        self.pro_log_text = ThemedScrolledText(
            parent, height=20, wrap="word", font=("Consolas", 9))
        self.pro_log_text.grid(row=1, column=0, sticky="nsew")
        self.pro_log_text.configure(state="disabled")

    def _refresh_pro_logs(self) -> None:
        if not hasattr(self, "pro_log_text"):
            return
        window = {"1 分钟": 60, "5 分钟": 300, "30 分钟": 1800}.get(
            self.var_log_window.get(), 0)
        since = time.time() - window if window else 0.0
        recs = self.pro.log.filtered(
            self.var_log_cat.get(), self.var_log_kw.get(),
            self.var_log_level.get(), since)
        text = "\n".join(r.line() for r in recs[-800:])
        self.pro_log_text.configure(state="normal")
        self.pro_log_text.delete("1.0", "end")
        self.pro_log_text.insert("end", text)
        self.pro_log_text.see("end")
        self.pro_log_text.configure(state="disabled")

    def _refresh_rules(self) -> None:
        if not hasattr(self, "rule_tree"):
            return
        for item in self.rule_tree.get_children():
            self.rule_tree.delete(item)
        for i, rule in enumerate(self.cfg.get("event_rules", [])):
            if not isinstance(rule, dict):
                continue
            actions = "、".join(
                pro_mod.ACTION_LABELS.get(str(a.get("type", "")), str(a.get("type", "")))
                for a in rule.get("actions", []) if isinstance(a, dict)
            )
            self.rule_tree.insert("", "end", iid=str(i), values=(
                pro_mod.EVENT_LABELS.get(str(rule.get("event", "")), rule.get("event", "")),
                rule.get("name", ""), actions,
                "是" if rule.get("enabled", True) else "否",
            ))
        # Editing a rule only updates the draft; apply at save/start.

    # ---- 专业导播动作 ----
    def _sync_pro_cfg(self) -> None:
        if self.engine is not None:
            return  # Running sessions retain their validated configuration snapshot.
        cfg = self._collect_cfg()
        self._apply_runtime_cfg(cfg)

    def _pro_thread(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _update_model_label(self) -> None:
        st = self.model_arbiter.status()
        if st.get("loaded"):
            cal = " · 已校准" if st.get("calibrated") else ""
            self.lbl_model.configure(
                text=f"本地候选模型：已加载（{st.get('format', '?')}）{cal}",
                foreground=self._colors['success'])
        else:
            self.lbl_model.configure(
                text="本地候选模型：未加载；正式执行模型状态请查看设置中心",
                foreground=self._colors['accent'])

    def _reload_model(self) -> None:
        if self.engine is not None or self._closing or 'model-check' in self.tasks.running:
            self.bus.warning('请停止导播并等待检查结束后重新加载本地候选模型。')
            return
        def done(ok, error):
            self._update_model_label()
            if error or not ok:
                self.bus.warning('本地候选模型加载未完成，请检查模型文件。')
            else:
                self.bus.info('本地候选模型已重新加载。')
        self.tasks.submit('model-load', self.model_arbiter.load, done)

    def _calibrate_online(self) -> None:
        if not self.model_arbiter.loaded:
            messagebox.showwarning("校准", "请先加载仲裁模型。")
            return
        self.bus.info("开始用线上 decisions.csv 校准概率…")
        self._pro_thread(self._calibrate_online_worker)

    def _calibrate_online_worker(self) -> None:
        try:
            out = dm.decision_model_path().with_name("decision_calibration.json")
            result = cal_mod.calibrate_csv(
                dm.decision_csv_path(), dm.decision_model_path(), out,
                min_samples=max(100, int(self.cfg.get(
                    "decision_calibration_min_samples", 500))))
            if result.get("ok"):
                self.bus.info(
                    f"校准完成：Brier {result.get('brier_before'):.3f}→"
                    f"{result.get('brier_after'):.3f}，AUC {result.get('auc', 0):.3f}")
                self.model_arbiter.reload_if_changed()
            else:
                self.bus.warning(f"校准未应用：{result.get('reason', '')}")
        except Exception as e:
            self.bus.warning(f"校准异常：{type(e).__name__}: {e}")
        finally:
            schedule(self.root,0, self._update_model_label)

    def _open_trainer(self) -> None:
        from trainer_launcher import open_trainer
        try:
            self._trainer_process = open_trainer(getattr(self, "_trainer_process", None))
        except Exception as e:
            messagebox.showerror("训练工作台", f"无法启动内置训练工作台：{e}", parent=self.root)

    def _pro_connect(self) -> None:
        self._sync_pro_cfg()
        self._pro_thread(self._pro_connect_worker)

    def _pro_connect_worker(self) -> None:
        ok = self.pro.obs.connect()
        schedule(self.root,0, lambda: self.lbl_obs.configure(
            text=("OBS：已连接" if ok else f"OBS：连接失败 {self.pro.obs.last_error}"),
            foreground=(self._colors['success'] if ok else self._colors['error'])))

    def _pro_disconnect(self) -> None:
        def done(result, error):
            self.lbl_obs.configure(text='OBS：断开失败' if error else 'OBS：未连接')
        self.tasks.submit('obs-disconnect', self.pro.obs.disconnect, done)

    def _pro_add_rule(self) -> None:
        self._pro_rule_dialog(None)

    def _pro_edit_rule(self) -> None:
        sel = self.rule_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        rules = self.cfg.get("event_rules", [])
        if 0 <= idx < len(rules):
            self._pro_rule_dialog(rules[idx], idx)

    def _pro_delete_rule(self) -> None:
        sel = self.rule_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        rules = self.cfg.get("event_rules", [])
        if 0 <= idx < len(rules):
            del rules[idx]
            self._refresh_rules()

    def _pro_toggle_rule(self) -> None:
        sel = self.rule_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        rules = self.cfg.get("event_rules", [])
        if 0 <= idx < len(rules):
            rules[idx]["enabled"] = not bool(rules[idx].get("enabled", True))
            self._refresh_rules()

    def _pro_pause(self, paused: bool) -> None:
        try:
            self.pro.set_pause(paused)
            self.bus.info(f"手动暂停：{'暂停开始' if paused else '暂停结束'}")
        except Exception as e:
            self.bus.warning(f"暂停事件触发失败：{type(e).__name__}: {e}")

    def _pro_test_rule(self) -> None:
        """不依赖 GSI 事件，直接执行选中规则，用于排查 OBS 源/场景问题。"""
        sel = self.rule_tree.selection()
        if not sel:
            messagebox.showinfo("测试规则", "先在列表里选中一条规则。")
            return
        idx = int(sel[0])
        rules = self.cfg.get("event_rules", [])
        if not (0 <= idx < len(rules)):
            return
        if not self.pro.obs.connected:
            messagebox.showwarning("测试规则", "OBS 尚未连接，请先点“连接”。")
            return
        rule = copy.deepcopy(rules[idx])
        self.bus.info(f"手动测试规则：{rule.get('name', '')}")
        self._pro_thread(lambda: self.pro.actions.execute(rule))

    def _pro_save_preset(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存事件动作预设", defaultextension=".json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(
                self.cfg.get("event_rules", []), ensure_ascii=False, indent=2),
                encoding="utf-8")
            messagebox.showinfo("预设", "已保存")
        except Exception as e:
            messagebox.showerror("预设", str(e))

    def _pro_load_preset(self) -> None:
        path = filedialog.askopenfilename(
            title="加载事件动作预设", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("预设必须是规则数组")
            self.cfg["event_rules"] = data
            self._refresh_rules()
            messagebox.showinfo("预设", f"已加载 {len(data)} 条规则")
        except Exception as e:
            messagebox.showerror("预设", str(e))

    def _pro_rule_dialog(self, rule: Optional[dict[str, Any]], idx: Optional[int] = None) -> None:
        top = tk.Toplevel(self.root)
        top.title("编辑事件动作" if rule else "添加事件动作")
        top.transient(self.root)
        top.grab_set()
        top.columnconfigure(1, weight=1)
        name_var = tk.StringVar(value=str((rule or {}).get("name", "")))
        event_label = pro_mod.EVENT_LABELS.get(str((rule or {}).get("event", "")), "回合开始")
        event_var = tk.StringVar(value=event_label)
        enabled_var = tk.BooleanVar(value=bool((rule or {}).get("enabled", True)))
        actions: list[dict[str, Any]] = [
            dict(a) for a in (rule or {}).get("actions", []) if isinstance(a, dict)
        ]

        ttk.Label(top, text="规则名").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(top, textvariable=name_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=8, pady=6)
        ttk.Label(top, text="事件").grid(row=1, column=0, sticky="w", padx=8)
        ttk.Combobox(top, textvariable=event_var, state="readonly",
                     values=tuple(pro_mod.EVENT_LABELS.values())).grid(
            row=1, column=1, sticky="w", padx=8)
        self._checkbutton(top, "启用", enabled_var).grid(
            row=1, column=2, sticky="w", padx=8)

        listbox = tk.Listbox(top, height=7, width=62)
        listbox.grid(row=2, column=0, columnspan=4, sticky="ew", padx=8, pady=6)
        for a in actions:
            listbox.insert("end", f"{pro_mod.ACTION_LABELS.get(a.get('type'), a.get('type'))}  {a.get('target','')}")

        ttk.Label(top, text="动作").grid(row=3, column=0, sticky="w", padx=8)
        act_var = tk.StringVar(value="播放媒体源")
        ttk.Combobox(top, textvariable=act_var, state="readonly",
                     values=tuple(pro_mod.ACTION_LABELS.values())).grid(
            row=3, column=1, sticky="w", padx=8)
        target_var = tk.StringVar()
        ttk.Entry(top, textvariable=target_var, width=26).grid(
            row=3, column=2, sticky="w", padx=4)
        act_rev = {v: k for k, v in pro_mod.ACTION_LABELS.items()}

        def add_action() -> None:
            typ = act_rev.get(act_var.get(), "")
            actions.append({"type": typ, "target": target_var.get().strip()})
            listbox.insert("end", f"{act_var.get()}  {target_var.get().strip()}")
            target_var.set("")

        def del_action() -> None:
            sel = listbox.curselection()
            if sel:
                i = int(sel[0])
                listbox.delete(i)
                del actions[i]

        ttk.Button(top, text="添加动作", command=add_action).grid(
            row=3, column=3, sticky="w", padx=4)
        ttk.Button(top, text="删除动作", command=del_action).grid(
            row=4, column=0, sticky="w", padx=8, pady=(0, 6))

        def save() -> None:
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("规则", "请填写规则名", parent=top)
                return
            if not actions:
                messagebox.showwarning("规则", "至少添加一个动作", parent=top)
                return
            ev = next((k for k, v in pro_mod.EVENT_LABELS.items() if v == event_var.get()),
                      "round_started")
            new_rule = {"name": name, "event": ev, "enabled": enabled_var.get(),
                        "actions": actions}
            rules = self.cfg.setdefault("event_rules", [])
            if idx is None:
                rules.append(new_rule)
            else:
                rules[idx] = new_rule
            self._refresh_rules()
            top.destroy()

        ttk.Button(top, text="保存", command=save).grid(
            row=4, column=3, sticky="e", padx=8, pady=(0, 6))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=0)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = tk.Frame(outer, bg=self._colors['header'], height=100)
        header._theme_surface = 'header'
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header_inner = tk.Frame(header, bg=self._colors['header'])
        header_inner._theme_surface = 'header'
        header_inner.pack(fill="both", expand=True, padx=22, pady=(13, 11))
        try:
            self._logo_img = tk.PhotoImage(file=str(asset_path("yuzi_logo_76.png")))
            tk.Label(header_inner, image=self._logo_img,
                     bg=self._colors['header']).pack(side="left", padx=(0, 16))
        except Exception:
            self._logo_img = None
        title_box = tk.Frame(header_inner, bg=self._colors['header'])
        title_box._theme_surface = 'header'
        title_box.pack(side="left", fill="y")
        tk.Label(title_box, text=APP_TITLE.replace("_", " ").replace("AIDirector", "AI Director"),
                 bg=self._colors['header'], fg=self._colors['accent'],
                 font=("Microsoft YaHei UI", 21, "bold")).pack(
            side="top", anchor="w", pady=(1, 0))
        tk.Label(title_box, text=f"{APP_SUBTITLE}  ·  v{VERSION}",
                 bg=self._colors['header'], fg=self._colors['muted'],
                 font=("Microsoft YaHei UI", 9)).pack(side="top", anchor="w", pady=(2, 0))
        mark = tk.Frame(header_inner, bg=self._colors['header'])
        mark._theme_surface = 'header'
        mark.pack(side="right", anchor="e", padx=(0, 2))
        tk.Label(mark, text="YUZI CUP", bg=self._colors['header'],
                 fg=self._colors['accent'], font=("Segoe UI", 9, "bold")).pack(anchor="e")
        tk.Label(mark, text="DIRECTOR CONSOLE", bg=self._colors['header'],
                 fg=self._colors['muted'], font=("Segoe UI", 8)).pack(anchor="e", pady=(2, 0))
        rule = tk.Frame(header, bg=self._colors['accent'], height=2,
                        bd=0, highlightthickness=0)
        rule._theme_surface = 'accent'
        rule.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")

        from account_ui import mount_account
        account_host = ttk.Frame(outer)
        account_host.grid(row=1,column=0,sticky="ew")
        mount_account(account_host,self.account)
        outer.rowconfigure(1,weight=0)
        outer.rowconfigure(2,weight=1)
        body = ttk.Frame(outer, padding=(22, 14, 22, 12))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # 设置 / 日志分页：设置页保持紧凑，日志页占满剩余高度。
        notebook = ttk.Notebook(body)
        self.notebook = notebook
        notebook.grid(row=0, column=0, sticky="nsew")
        from desktop_ui import settings as open_settings
        workspace = _ScrollableFrame(notebook)
        settings_scroll = _ScrollableFrame(notebook)
        pro_scroll = _ScrollableFrame(notebook)
        settings = settings_scroll.inner
        pro_page = pro_scroll.inner
        pro_logs = ttk.Frame(notebook, padding=8)
        logs = ttk.Frame(notebook, padding=8)
        notebook.add(workspace, text="导播工作台")
        notebook.add(settings_scroll, text="设置")
        notebook.add(pro_scroll, text="专业导播")
        notebook.add(pro_logs, text="导播日志")
        notebook.add(logs, text="日志")
        self._developer_tabs = (pro_logs, logs)
        settings.columnconfigure(0, weight=1)
        self._build_pro(pro_page)
        self._build_pro_logs(pro_logs)

        def _card(parent: Any, padding: int = 18) -> tuple[tk.Frame, ttk.Frame]:
            shell = tk.Frame(parent, bg=self._colors['card'],
                             highlightbackground=self._colors['border'],
                             highlightthickness=1, bd=0)
            shell._theme_surface = 'card'
            inner = ttk.Frame(shell)
            inner.pack(fill="both", expand=True, padx=padding, pady=padding)
            return shell, inner

        dash = ttk.Frame(workspace.inner)
        dash.pack(fill="both", expand=True)
        dash.columnconfigure(0, weight=3)
        dash.columnconfigure(1, weight=2)
        dash.rowconfigure(0, weight=1)

        hero_shell, hero = _card(dash, 20)
        hero_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        rail = ttk.Frame(dash)
        rail.grid(row=0, column=1, sticky="nsew")
        quick_shell, quick = _card(rail, 18)
        quick_shell.pack(fill="both", expand=True)

        ttk.Label(hero, text="DIRECTOR CONSOLE  /  导播控制台",
                  style='Eyebrow.TLabel').pack(anchor="w")
        ttk.Label(hero, text="把注意力留给比赛。",
                  style='H1.TLabel').pack(anchor="w", pady=(8, 8))
        hero_copy = ttk.Label(
            hero,
            text="先完成账号与正式模型初始化，再检查游戏连接。"
                 "收到有效观战数据后，程序按现有策略自动导播。",
            style='Body.TLabel', wraplength=560, justify="left")
        hero_copy._own_wrap = True
        hero_copy.pack(anchor="w")
        hero.bind('<Configure>',
                  lambda e: hero_copy.configure(
                      wraplength=max(180, e.width - 8)), add='+')

        actions = ttk.Frame(hero)
        actions.pack(anchor="w", pady=(2, 0))
        ttk.Button(actions, text="打开训练工作台", style='Accent.TButton',
                   command=self._open_trainer).pack(side="left")
        ttk.Button(actions, text="账号与模型状态", style='Link.TButton',
                   command=lambda: open_settings(self)).pack(
            side="left", padx=(10, 0))
        ttk.Button(actions, text="配置游戏与运镜", style='Link.TButton',
                   command=lambda: notebook.select(settings_scroll)).pack(
            side="left", padx=(2, 0))

        status_panel = tk.Frame(hero, bg=self._colors['field'],
                                highlightbackground=self._colors['border'],
                                highlightthickness=1, bd=0)
        status_panel._theme_surface = 'field'
        status_panel.pack(fill="x", pady=(12, 0))
        status_inner = tk.Frame(status_panel, bg=self._colors['field'])
        status_inner._theme_surface = 'field'
        status_inner.pack(fill="x", padx=14, pady=12)
        tk.Label(status_inner, text="DIRECTOR STATUS  /  LIVE",
                 bg=self._colors['field'], fg=self._colors['accent'],
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.workspace_status = ttk.Label(
            status_inner, text="导播尚未启动", style='Status.TLabel',
            wraplength=480, justify="left")
        self.workspace_status._own_wrap = True
        self.workspace_status.pack(anchor="w", pady=(6, 0))
        status_panel.bind('<Configure>',
                          lambda e: self.workspace_status.configure(
                              wraplength=max(180, e.width - 30)), add='+')

        ttk.Label(quick, text="QUICK START  /  首次使用",
                  style='Eyebrow.TLabel').pack(anchor="w")
        ttk.Label(quick, text="三步开始导播",
                  style='H2.TLabel').pack(anchor="w", pady=(8, 4))
        steps = (
            ("01", "登录并绑定设备", "登录账号，确认设备与模型授权。"),
            ("02", "连接游戏与运镜", "选择 cs2.exe，安装 GSI 后重启游戏。"),
            ("03", "启动并观察状态", "打开训练工作台，或直接启动导播。"),
        )
        for number, title, description in steps:
            step = ttk.Frame(quick)
            step.pack(fill="x", pady=(12, 0))
            ttk.Label(step, text=number, style='StepNum.TLabel').pack(
                side="left", anchor="n")
            text_box = ttk.Frame(step)
            text_box.pack(side="left", fill="x", expand=True, padx=(10, 0))
            ttk.Label(text_box, text=title, style='StepTitle.TLabel').pack(anchor="w")
            desc = ttk.Label(text_box, text=description, style='Muted.TLabel',
                             wraplength=300, justify="left")
            desc._own_wrap = True
            desc.pack(anchor="w", pady=(2, 0))
        ttk.Label(quick, text=f"v{VERSION}  ·  鱼籽杯导播控制台",
                  style='Caption.TLabel').pack(anchor="w", pady=(16, 0))

        layout_state = {'narrow': None}
        def _reflow(event: Any = None) -> None:
            width = event.width if event is not None else dash.winfo_width()
            narrow = width < 860
            if layout_state['narrow'] == narrow:
                return
            layout_state['narrow'] = narrow
            if narrow:
                dash.columnconfigure(0, weight=1)
                dash.columnconfigure(1, weight=0)
                hero_shell.grid_configure(row=0, column=0, padx=0, pady=(0, 12))
                rail.grid_configure(row=1, column=0, sticky="ew")
            else:
                dash.columnconfigure(0, weight=3)
                dash.columnconfigure(1, weight=2)
                hero_shell.grid_configure(row=0, column=0, padx=(0, 14), pady=0)
                rail.grid_configure(row=0, column=1, sticky="nsew")
        dash.bind('<Configure>', _reflow, add='+')

        heading = ttk.Frame(settings)
        heading.grid(row=0, column=0, sticky="ew", pady=(2, 10))
        ttk.Label(heading, text="设置中心", style='H2.TLabel').pack(anchor="w")
        ttk.Label(heading, text="账号、游戏连接、运镜与模型。修改后点击底部「保存配置」。",
                  style='Caption.TLabel').pack(anchor="w", pady=(2, 0))

        # 1) 游戏与 GSI
        lf = ttk.LabelFrame(settings, text="1. 游戏与 GSI 配置", padding=10)
        lf.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        lf.columnconfigure(1, weight=1)
        ttk.Label(lf, text="cs2.exe").grid(row=0, column=0, sticky="w")
        ttk.Entry(lf, textvariable=self.var_exe).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(lf, text="自动检测", width=9, command=self._auto_detect).grid(row=0, column=2)
        ttk.Button(lf, text="浏览…", width=8, command=self._browse_exe).grid(row=0, column=3, padx=(4, 0))

        self.lbl_gsi = ttk.Label(
            lf, text="GSI 配置：检查中…", foreground="#555555",
            wraplength=920, justify="left",
        )
        self.lbl_gsi.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        row = ttk.Frame(lf)
        row.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(row, text="一键安装 GSI", command=self._install_gsi).pack(side="left")
        ttk.Button(row, text="打开 cfg 目录", command=self._open_cfg_dir).pack(side="left", padx=6)
        ttk.Label(row, text="安装/覆盖后需要完全重启 CS2 才会生效", foreground=self._colors['accent']).pack(
            side="left", padx=6)

        # 2) MulNX
        lf2 = ttk.LabelFrame(settings, text="2. MulNX 运镜快捷键（与 MulNX 内绑定一致）", padding=10)
        lf2.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        lf2.columnconfigure(0, weight=1)

        def add_pair_row(row: int,
                         left: tuple[str, Any, Any, Any],
                         right: tuple[str, Any, Any, Any]) -> None:
            pair = ttk.Frame(lf2)
            pair.grid(row=row, column=0, columnspan=3, sticky="ew",
                      pady=(0 if row == 0 else 6, 0))
            pair.columnconfigure(0, weight=1, uniform=f"pair{row}")
            pair.columnconfigure(1, weight=1, uniform=f"pair{row}")
            for col, (label, mod_var, key_var, mode_var) in enumerate((left, right)):
                box = ttk.Frame(pair)
                box.grid(row=0, column=col, sticky="ew",
                         padx=(0, 28) if col == 0 else 0)
                box.columnconfigure(4, weight=1)  # 剩余空间留白
                ttk.Label(box, text=label).grid(row=0, column=0, sticky="w")
                ttk.Combobox(box, textvariable=mod_var,
                             values=("请选择",) + SELECTABLE_MODIFIERS,
                             state="readonly", width=6).grid(
                    row=0, column=1, sticky="w", padx=(4, 3))
                ttk.Combobox(box, textvariable=key_var,
                             values=("请选择",) + SELECTABLE_KEYS,
                             state="readonly", width=5).grid(
                    row=0, column=2, sticky="w", padx=(0, 3))
                ttk.Combobox(box, textvariable=mode_var, values=("单次", "双击"),
                             state="readonly", width=5).grid(
                    row=0, column=3, sticky="w")

            def reflow(event):
                narrow = event.width < 760
                for index, child in enumerate(pair.winfo_children()):
                    child.grid_configure(row=index if narrow else 0, column=0 if narrow else index,
                                         padx=0 if narrow or index else (0, 16), pady=4 if narrow else 0)
                pair.columnconfigure(1, weight=0 if narrow else 1)
            pair.bind('<Configure>', reflow, add='+')

        add_pair_row(0,
                     ("CT 出生点", self.var_spawn_ct_mod,
                      self.var_spawn_ct_keyname, self.var_spawn_ct_mode),
                     ("T 出生点", self.var_spawn_t_mod,
                      self.var_spawn_t_keyname, self.var_spawn_t_mode))
        add_pair_row(1,
                     ("A 包点", self.var_bomb_a_mod,
                      self.var_bomb_a_keyname, self.var_bomb_a_mode),
                     ("B 包点", self.var_bomb_b_mod,
                      self.var_bomb_b_keyname, self.var_bomb_b_mode))

        ttk.Label(lf2, text="选择 ALT/SHIFT/CTRL + A–Z/0–9；出生点按上回合胜方选 CT/T，包点按炸弹位置判 A/B；有玩家存活时优先 POV。",
                  foreground="#555555", wraplength=920, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self._checkbutton(lf2, '自动运镜（出生点、包点；切换后立即生效）', self.var_cinematics,
                          command=lambda: self._set_cinematics_enabled(self.var_cinematics.get())).grid(
                              row=3, column=0, sticky='w', pady=(10, 0))
        for row_index, label, variable in (
                (4, '启动 / 停止导播', self.var_director_hotkey),
                (5, '开启 / 关闭运镜', self.var_cinematics_hotkey)):
            shortcut_row = ttk.Frame(lf2)
            shortcut_row.grid(row=row_index, column=0, sticky='ew', pady=(6, 0))
            ttk.Label(shortcut_row, text=label, width=20).pack(side='left')
            ttk.Entry(shortcut_row, textvariable=variable, width=22).pack(side='left', padx=(4, 0))
        ttk.Label(lf2, text='快捷键留空可停用，保存后生效；松开按键触发。关闭运镜后仍自动选择玩家视角。',
                  style='Caption.TLabel', wraplength=840).grid(row=6, column=0, sticky='w', pady=(6, 0))

        # 3) AI
        lf3 = ttk.LabelFrame(settings, text="3. 云端仲裁与本地模型", padding=10)
        lf3.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        lf3.columnconfigure(1, weight=1)
        ttk.Label(lf3, text="个人 API Key").grid(row=0, column=0, sticky="w")
        ttk.Entry(lf3, textvariable=self.var_api, show="*").grid(
            row=0, column=1, sticky="ew", padx=6, columnspan=3)
        ttk.Label(lf3, text="模型").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(lf3, textvariable=self.var_model, width=24).grid(
            row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        modes = ttk.Frame(lf3)
        modes.grid(row=2, column=0, columnspan=2, sticky='w', pady=(6,0))
        for text, value in [('关闭','off'),('平台额度','platform'),('自有 API','personal')]:
            ttk.Radiobutton(modes, text=text, value=value, variable=self.var_mode,
                            command=self._cloud_mode_changed).pack(side='left', padx=3)
        keys = ttk.Frame(lf3)
        keys.grid(row=5, column=0, columnspan=4, sticky='w', pady=(6,0))
        for text, action in [('保存个人密钥','save'),('清除个人密钥','clear'),('接管旧密钥','adopt'),('查看平台额度','budget')]:
            ttk.Button(keys,text=text,command=lambda a=action:self._cloud_key_action(a)).pack(side='left',padx=3)
        ttk.Label(lf3,textvariable=self.var_cloud_status,wraplength=850).grid(row=6,column=0,columnspan=4,sticky='w',pady=(6,0))
        self._checkbutton(lf3, "演练模式（不实际发送按键）",
                         self.var_dry).grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))
        self._checkbutton(lf3, "启用 demo 模型（离线权重，不即时更新）",
                         self.var_learn).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.btn_train = ttk.Button(
            lf3, text="从 demo 训练…", command=self._train_demos
        )
        self.btn_train.grid(row=3, column=2, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Label(lf3, text="成本控制：仅在规则前两名分差很小时调用，默认最多 12 次/分钟，超时 2.5s 自动回退。"
                           " 学习样本 <20 时不影响排序。",
                  foreground="#555555", wraplength=920, justify="left").grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 4) 高级
        lf4 = ttk.LabelFrame(settings, text="4. 高级参数", padding=10)
        lf4.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(lf4, text="GSI 端口").grid(row=0, column=0, sticky="w")
        ttk.Entry(lf4, textvariable=self.var_port, width=8).grid(row=0, column=1, sticky="w", padx=(4, 16))
        ttk.Label(lf4, text="切换阈值").grid(row=0, column=2, sticky="w")
        ttk.Entry(lf4, textvariable=self.var_score, width=8).grid(row=0, column=3, sticky="w", padx=(4, 16))
        ttk.Label(lf4, text="接管暂停(秒)").grid(row=0, column=4, sticky="w")
        ttk.Entry(lf4, textvariable=self.var_pause, width=8).grid(row=0, column=5, sticky="w", padx=(4, 0))
        ttk.Label(lf4, text="阈值越低越容易切；0.30 适合大多数情况。其余参数见 config.json。",
                  foreground="#555555").grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))
        self._checkbutton(
            lf4, "开局控图阶段优先镜头给手持/投掷道具的选手",
            self.var_utility_cam).grid(
            row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # 5) 依赖
        lf5 = ttk.LabelFrame(settings, text="5. 依赖状态", padding=10)
        lf5.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        lf5.columnconfigure(0, weight=1)
        self.lbl_deps = ttk.Label(lf5, text="检查中…", wraplength=760, justify="left")
        self.lbl_deps.grid(row=0, column=0, sticky="w")
        row = ttk.Frame(lf5)
        row.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(row, text="重新检查", command=self._refresh_deps).pack(side="left")
        ttk.Button(row, text="安装缺失依赖", command=self._install_deps_async).pack(side="left", padx=6)

        self._developer_widgets = [lf5] + self._pro_developer_widgets

        # 6) 控制条（始终可见）
        ctrl_shell = ttk.Frame(body)
        ctrl_shell.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        ctrl = ttk.Frame(ctrl_shell)
        ctrl.pack(fill='x')
        self.btn_start = ttk.Button(
            ctrl, text="▶ 启动导播", command=self._start, style="Accent.TButton")
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(
            ctrl, text="■ 停止", command=self._stop, state="disabled",
            style="Stop.TButton")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_cinematics = ttk.Button(ctrl, text='运镜：'+('开启' if self.var_cinematics.get() else '关闭'),
                                         command=self._toggle_cinematics)
        self.btn_cinematics.pack(side='left', padx=(0, 6))
        ttk.Separator(ctrl, orient="vertical").pack(
            side="left", fill="y", padx=10, pady=3)
        ttk.Button(ctrl, text="保存配置", command=self._save_only).pack(side="left")
        ttk.Button(ctrl, text="设置中心",
                   command=lambda: open_settings(self)).pack(side="left", padx=4)
        self.lbl_control_shortcuts = ttk.Label(ctrl_shell, text='', style='Caption.TLabel', wraplength=900)
        self.lbl_control_shortcuts.pack(fill='x', pady=(5, 0))

        # 7) 运行状态（始终可见）
        status_box = ttk.LabelFrame(body, text="运行状态", padding=(12, 5))
        status_box.grid(row=2, column=0, sticky="ew")
        status_box.columnconfigure(0, weight=1)
        self.lbl_play = ttk.Label(
            status_box, text="阶段：-    当前视角：-    AI 目标：-",
            style='CompactStatus.TLabel', justify="left", wraplength=940)
        self.lbl_play.grid(row=0, column=0, sticky="w")
        self.lbl_ai = ttk.Label(
            status_box, text="AI：未启动    运镜：-", style='Compact.TLabel',
            justify="left", wraplength=940)
        self.lbl_ai.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.lbl_reason = ttk.Label(
            status_box, text="决策：-", style='Compact.TLabel',
            justify="left", wraplength=940)
        self.lbl_reason.grid(row=2, column=0, sticky="w", pady=(3, 0))
        self.lbl_story = ttk.Label(
            status_box, text="故事：-", style='Compact.TLabel',
            justify="left", wraplength=940)
        self.lbl_story.grid(row=3, column=0, sticky="w", pady=(3, 0))
        self.lbl_narrative = ttk.Label(
            status_box, text="叙事：-", style='CompactAccent.TLabel',
            justify="left", wraplength=940)
        self.lbl_narrative.grid(row=4, column=0, sticky="w", pady=(3, 0))
        self.lbl_metrics = ttk.Label(
            status_box, text="专业指标：-", style='Compact.TLabel',
            justify="left", wraplength=940)
        self.lbl_metrics.grid(row=5, column=0, sticky="w", pady=(3, 0))
        self.lbl_conn = ttk.Label(
            status_box, text="GSI：未启动", style='Compact.TLabel',
            justify="left", wraplength=940)
        self.lbl_conn.grid(row=6, column=0, sticky="w", pady=(5, 0))
        self._developer_widgets.extend([self.lbl_ai, self.lbl_reason, self.lbl_metrics])

        status_labels = (self.lbl_play, self.lbl_ai, self.lbl_reason,
                         self.lbl_story, self.lbl_narrative, self.lbl_metrics,
                         self.lbl_conn)
        def _wrap_status(event: Any) -> None:
            width = max(220, event.width - 34)
            for label in status_labels:
                label.configure(wraplength=width)
        status_box.bind('<Configure>', _wrap_status, add='+')

        # 8) 日志页
        logs.rowconfigure(0, weight=1)
        logs.columnconfigure(0, weight=1)
        ttk.Button(logs, text='回到最新日志', command=lambda: self.txt.see('end')).grid(row=1, column=0, sticky='e')
        from desktop_ui import ThemedScrolledText
        self.txt = ThemedScrolledText(
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
        self.lbl_deps.configure(text='依赖：后台检查中…')
        def done(dependencies, error):
            if error:
                self.lbl_deps.configure(text='依赖检查失败，请查看日志。')
                return
            parts = []
            for dependency in dependencies:
                mark = '✓' if dependency['installed'] else '✗ 缺失'
                version = f" {dependency['version']}" if dependency.get('version') else ''
                parts.append(f"{dependency['module']}{version} {mark}")
            self.lbl_deps.configure(text='依赖：'+'    '.join(parts))
        self.tasks.submit('dependencies', check_dependencies, done)

    def _train_demos(self) -> None:
        """选择包含 .dem 的文件夹，后台运行 train_from_demos.py。"""
        if self._training:
            return
        folder = filedialog.askdirectory(title="选择包含 .dem 的文件夹")
        if not folder:
            return
        try:
            import demoparser2  # noqa: F401
        except Exception:
            messagebox.showerror(
                "缺少 demoparser2",
                "从 demo 训练需要安装 demoparser2：\n"
                "python -m pip install demoparser2",
            )
            return
        script = Path(__file__).with_name("train_from_demos.py")
        if not script.is_file():
            messagebox.showerror("缺少训练脚本", f"找不到：\n{script}")
            return
        out = learner_path()
        self._training = True
        self.btn_train.configure(state="disabled")
        self.bus.info(f"开始从 demo 训练：{folder} → {out}")

        def worker() -> None:
            exe = sys.executable
            if exe.lower().endswith("pythonw.exe"):
                exe = str(Path(exe).with_name("python.exe"))
            cmd = [exe, str(script), "--demos", folder, "--out", str(out)]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
            code = -1
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=flags,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.bus.info("demo训练 | " + line)
                code = proc.wait()
            except Exception as e:
                self.bus.error(f"demo 训练启动失败：{type(e).__name__}: {e}")
            schedule(self.root,0, lambda: self._train_done(code, out))

        threading.Thread(target=worker, name="demo-train", daemon=True).start()

    def _train_done(self, code: int, out: Path) -> None:
        self._training = False
        try:
            self.btn_train.configure(state="normal")
        except Exception:
            pass
        if code == 0:
            self.bus.info(f"demo 训练完成：{out}（重启导播后加载新权重）")
            messagebox.showinfo(
                "训练完成",
                f"模型已写入：\n{out}\n\n请停止并重新“启动导播”以加载新权重。",
            )
        else:
            messagebox.showerror("训练失败", "详见日志（需要 demoparser2 和有效 .dem 文件）。")

    def _install_deps_async(self) -> None:
        if self._detecting:
            return
        if getattr(sys, 'frozen', False):
            messagebox.showinfo('发行包组件', '发行包已包含所需组件。若自检显示缺失，请在设置中前往官网重新下载完整程序。', parent=self.root)
            return
        self._detecting = True
        self.bus.info("开始安装缺失依赖…")

        def worker() -> None:
            ok = install_dependencies(lambda line: self.bus.info("pip | " + line))
            schedule(self.root,0, lambda: self._deps_done(ok))

        threading.Thread(target=worker, name="pip-install", daemon=True).start()

    def _deps_done(self, ok: bool) -> None:
        self._detecting = False
        self._refresh_deps()
        if ok:
            messagebox.showinfo("依赖安装", "依赖安装完成。\n如果刚安装了 pydirectinput/keyboard，建议重启本程序。")
        else:
            messagebox.showerror("依赖安装", "安装失败，请查看日志或手动执行：\n"
                                 "python -m pip install pydirectinput keyboard pygetwindow obsws-python cryptography")

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
            schedule(self.root,0, lambda: self._detect_done(found))

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
                foreground=self._colors['error'],
            )
            return
        target = cfg_dir / GSI_CFG_NAME
        if target.is_file():
            try:
                text = target.read_text(encoding="utf-8")
                m_uri = re.search(r'"uri"\s*:?\s*"([^"]+)"', text)
                uri = m_uri.group(1) if m_uri else ""
                ok_pos = '"allplayers_position"' in text
                is_keyvalues = not text.lstrip().startswith("{")
                conflicts: list[str] = []
                m = re.search(r":(\d+)", uri)
                if m:
                    conflicts = gsi_port_conflicts(cfg_dir, int(m.group(1)))
                extra = "" if ok_pos else "，缺少 allplayers_position"
                if not is_keyvalues:
                    extra += "，旧 JSON 格式（CS2 会忽略），请重新一键安装"
                try:
                    gui_port = int(self.var_port.get().strip())
                except Exception:
                    gui_port = None
                port_mismatch = bool(
                    gui_port is not None and m and int(m.group(1)) != gui_port
                )
                if port_mismatch:
                    extra += f"；GUI 端口 {gui_port} 与 cfg 不一致，请重新安装"
                if conflicts:
                    extra += f"；端口同时被 {', '.join(conflicts)} 使用"
                self.lbl_gsi.configure(
                    text=f"GSI 配置：已安装 → {target}   ({uri}{extra})",
                    foreground=(self._colors['error'] if (conflicts or not ok_pos or not is_keyvalues
                                               or port_mismatch) else "#1b7f3b"),
                )
            except Exception:
                self.lbl_gsi.configure(text=f"GSI 配置：已存在但无法解析 → {target}", foreground=self._colors['error'])
        else:
            self.lbl_gsi.configure(text=f"GSI 配置：未安装（目标 {target}）", foreground=self._colors['accent'])

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
        conflicts = gsi_port_conflicts(cfg_dir, port)
        if conflicts:
            self.bus.warning(
                f"端口 {port} 同时被其他 GSI 配置使用：{', '.join(conflicts)}"
            )
            messagebox.showwarning(
                "安装成功，但检测到端口冲突",
                f"已写入：\n{path}\n\n"
                f"端口 {port} 还被以下配置使用：\n" + "\n".join(conflicts) + "\n\n"
                "CS2 会把这些配置的数据都发到同一端口；如果对方缺少"
                " allplayers_position，会干扰本程序。\n"
                "建议在 GUI 里换一个独立端口（如 32100），重新“一键安装 GSI”，"
                "再完全重启 CS2。",
            )
        else:
            messagebox.showinfo(
                "安装成功",
                f"已写入：\n{path}\n\n请完全退出并重启 CS2，然后启动导播。\n"
                f"若仍无数据，请检查 CS2 是否带 -gamestateintegration 启动，"
                f"以及防火墙是否拦截本机 127.0.0.1:{port}。",
            )

    def _open_timeline_dir(self) -> None:
        try:
            path = self.timeline.base_dir
            path.mkdir(parents=True, exist_ok=True)
            if IS_WINDOWS:
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror("打开目录", f"无法打开镜头数据目录：{e}")

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
        from config_service import parse_fields
        checked = parse_fields({'gsi_port': self.var_port.get(),
                                'obs_port': self.var_obs_port.get(),
                                'switch_min_score': self.var_score.get(),
                                'manual_pause_seconds': self.var_pause.get()})
        cfg = copy.deepcopy(self.cfg)
        cfg["cs2_exe"] = self.var_exe.get().strip()

        def _combine(mod_var: Any, key_var: Any) -> str:
            mod = str(mod_var.get()).strip()
            key = str(key_var.get()).strip()
            if mod == "请选择" or key == "请选择" or not mod or not key:
                return ""
            return f"{mod.lower()}+{key}"

        cfg["hlae_spawn_ct_key"] = _combine(self.var_spawn_ct_mod, self.var_spawn_ct_keyname)
        cfg["hlae_spawn_t_key"] = _combine(self.var_spawn_t_mod, self.var_spawn_t_keyname)
        cfg["hlae_bomb_a_key"] = _combine(self.var_bomb_a_mod, self.var_bomb_a_keyname)
        cfg["hlae_bomb_b_key"] = _combine(self.var_bomb_b_mod, self.var_bomb_b_keyname)
        cfg["hlae_spawn_ct_mode"] = "double" if self.var_spawn_ct_mode.get() == "双击" else "single"
        cfg["hlae_spawn_t_mode"] = "double" if self.var_spawn_t_mode.get() == "双击" else "single"
        cfg["hlae_bomb_a_mode"] = "double" if self.var_bomb_a_mode.get() == "双击" else "single"
        cfg["hlae_bomb_b_mode"] = "double" if self.var_bomb_b_mode.get() == "双击" else "single"
        # 旧版单出生点/单包点字段保留，方便回退旧版本
        cfg["hlae_round_key"] = cfg["hlae_spawn_ct_key"]
        cfg["hlae_round_mode"] = cfg["hlae_spawn_ct_mode"]
        cfg["hlae_bomb_key"] = cfg["hlae_bomb_a_key"]
        cfg["hlae_bomb_mode"] = cfg["hlae_bomb_a_mode"]
        cfg["obs_host"] = self.var_obs_host.get().strip() or "127.0.0.1"
        try:
            cfg["obs_port"] = int(self.var_obs_port.get().strip())
        except Exception:
            cfg["obs_port"] = 4455
        cfg["obs_password"] = self.var_obs_pwd.get()
        cfg["event_actions_enabled"] = bool(self.var_pro_enabled.get())
        cfg["pause_hotkey"] = self.var_pause_hotkey.get().strip() or "ctrl+alt+p"
        cfg['director_toggle_hotkey'] = self.var_director_hotkey.get().strip()
        cfg['cinematics_toggle_hotkey'] = self.var_cinematics_hotkey.get().strip()
        cfg['cinematics_enabled'] = bool(self.var_cinematics.get())
        cfg["narrative_enabled"] = bool(self.var_narrative_enabled.get())
        cfg["obs_narrative_enabled"] = bool(self.var_obs_narrative_enabled.get())
        cfg["obs_narrative_source"] = self.var_obs_narrative_source.get().strip()
        cfg["deepseek_api_key"] = ""
        cfg["deepseek_model"] = self.var_model.get().strip() or "deepseek-chat"
        cfg["arbitration_mode"] = self.var_mode.get()
        cfg["ai_enabled"] = cfg["arbitration_mode"] in ('platform','personal')
        cfg["demo_model_enabled"] = bool(self.var_learn.get())
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
        cfg["utility_cam_enabled"] = bool(self.var_utility_cam.get())
        cfg.update(checked)
        from desktop_hotkeys import control_bindings, CONTROLS
        parser = keyboard.parse_hotkey if HAS_KEYBOARD and keyboard is not None else None
        control_bindings(cfg, parser)
        if self.engine is not None:
            active = dict(self.engine.cfg)
            active.update({field:cfg[field] for field, _, _ in CONTROLS.values()})
            control_bindings(active, parser)  # Live macro bindings stay immutable until restart.
        return cfg

    def _apply_runtime_cfg(self, cfg):
        cfg = copy.deepcopy(cfg)
        self._config_version += 1
        self._runtime_cfg = cfg
        self.decision_logger.cfg = cfg
        self.model_arbiter.cfg = cfg
        self.timeline.cfg = cfg
        # 让 OBS/事件动作共享同一份最新配置对象。
        self.pro.cfg = cfg
        self.pro.obs.cfg = cfg
        self.pro.actions.cfg = cfg

    def _validate_cfg(self, cfg: dict[str, Any]) -> bool:
        if not (1024 <= int(cfg["gsi_port"]) <= 65535):
            messagebox.showerror("配置错误", "GSI 端口必须是 1024-65535 之间的整数。")
            return False
        for label, key in (
            ("CT 出生点运镜", "hlae_spawn_ct_key"),
            ("T 出生点运镜", "hlae_spawn_t_key"),
            ("A 包点运镜", "hlae_bomb_a_key"),
            ("B 包点运镜", "hlae_bomb_b_key"),
        ):
            value = str(cfg[key]).strip()
            if not value:
                if not cfg.get('cinematics_enabled', True): continue
                messagebox.showerror("配置错误", f"{label}快捷键不能为空。")
                return False
            if value in DIGITS:
                messagebox.showerror("配置错误", f"{label}快捷键不能是单个数字键（会与观察者切换冲突）。")
                return False
            if not validate_hotkey(value):
                messagebox.showerror("配置错误", f"{label}快捷键格式无法识别：{value}")
                return False
        keys = [str(cfg[k]).strip().lower() for k in
                ("hlae_spawn_ct_key", "hlae_spawn_t_key",
                 "hlae_bomb_a_key", "hlae_bomb_b_key") if str(cfg.get(k) or '').strip()]
        if len(set(keys)) < len(keys):
            if not messagebox.askyesno("确认", "CT/T 出生点或 A/B 包点中有重复快捷键，确定继续？"):
                return False
        from arbitration_runtime import owner_of
        try:
            personal_missing = cfg.get('arbitration_mode') == 'personal' and not self.personal_keys.get(owner_of(self.account))
        except Exception:
            messagebox.showerror('个人密钥不可用','请重新登录并保存个人密钥。',parent=self.root)
            return False
        if personal_missing:
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
                "但 MulNX 组合快捷键可能发送失败。建议先安装依赖。",
            )
        return True

    def _start(self) -> None:
        if self.engine is not None or self._closing or getattr(self, '_starting', False) or 'stop' in self.tasks.running:
            return
        self._starting = True
        try: self._start_director()
        finally: self._starting = False

    def _start_director(self) -> None:
        if self.engine is not None or self._closing:
            return
        if 'model-check' in self.tasks.running or 'model-load' in self.tasks.running:
            self.bus.warning('本地模型检查尚未完成，请稍后启动。')
            return
        if not self.account.allowed():
            messagebox.showerror("账号或模型尚未就绪", self.account.status + "\n请登录并取得有效资格及正式模型后启动。", parent=self.root)
            return
        try:
            cfg = self._collect_cfg()
        except ValueError as error:
            messagebox.showerror("配置错误", str(error), parent=self.root)
            return
        if not self._validate_cfg(cfg):
            return
        try:
            save_config(cfg)
        except Exception as e:
            # 配置写不进去（只读目录/杀软拦截）不应该阻止本次运行
            self.bus.warning(f"配置保存失败（将继续启动）：{type(e).__name__}: {e}")
        self.cfg = copy.deepcopy(cfg)
        self._apply_runtime_cfg(cfg)
        cfg = self._runtime_cfg
        self._register_control_hotkeys()
        try:
            from offline_runtime import create_local_engine
            if not self.account.acquire_execution():
                messagebox.showerror("模型尚未就绪", "正式模型正在切换或已有导播占用，请稍后重试。", parent=self.root)
                return
            engine, _ = create_local_engine(self.account.session, self.account.tokens['account_id'], self.account.model.path,
                cfg, self.bus, pro=self.pro, decision_logger=self.decision_logger, timeline=self.timeline)
            # Cache-based account gate avoids credential/network IO in the input loop.
            engine.authorization_gate = self.account
            if cfg.get('ai_enabled'):
                engine.arbiter.configure(self.account,self.personal_keys)
            engine.input.authorization_gate = self.account
            self.pro.obs.authorization_gate = self.account
            engine.start()
        except Exception as e:
            self.bus.error(f"启动失败：{type(e).__name__}: {e}")
            self.account.release_execution()
            messagebox.showerror("启动失败", str(e))
            return
        self.engine = engine
        self.account.record_activity_async('director.start')
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_conn.configure(text="GSI：等待数据…", foreground=self._colors['accent'])

    def _stop(self) -> None:
        if self.engine is None or 'stop' in self.tasks.running:
            return
        engine = self.engine
        self.btn_stop.configure(state='disabled')
        self.lbl_conn.configure(text='正在停止导播…')
        def done(result, error):
            if error:
                self.bus.error(f'停止导播失败：{error}')
                self.btn_stop.configure(state='normal')
                return
            self.engine = None
            self.account.release_execution()
            self.btn_start.configure(state='normal')
            self.lbl_conn.configure(text='GSI：未启动')
        self.tasks.submit('stop', engine.stop, done)

    def _cloud_mode_changed(self):
        self.var_ai.set(self.var_mode.get() != 'off')
        if self.engine is not None:
            # A setting change immediately cancels old responses; a new paid session needs restart.
            self.engine.arbiter.cfg['ai_enabled'] = False
            self.engine.arbiter.invalidate()
            self.var_cloud_status.set('已停止本次云端仲裁。停止并重新启动导播后应用新模式。')
        else:
            self.var_cloud_status.set('模式将在主动启动导播时应用；关闭程序或导入备份后默认关闭。')

    def _cloud_key_action(self, action):
        from arbitration_runtime import owner_of, PlatformProvider
        try:
            owner = owner_of(self.account)
        except Exception:
            messagebox.showerror('需要登录','请先联网登录网站账号。',parent=self.root)
            return
        if action != 'budget' and self.engine is not None:
            messagebox.showerror('请先停止','修改密钥前请停止导播。',parent=self.root)
            return
        key = self.var_api.get().strip()
        if action == 'adopt' and not messagebox.askyesno('接管旧密钥','将本机已加密的旧密钥绑定到当前登录账号？不会自动启用。',parent=self.root):
            return
        def work():
            if owner_of(self.account) != owner:
                raise ValueError('账号已切换')
            if action == 'budget':
                provider = PlatformProvider(self.account)
                from arbitration_runtime import describe_budget
                return describe_budget(provider.read_budget())
            if action == 'save':self.personal_keys.set(owner,key)
            elif action == 'clear':self.personal_keys.clear(owner)
            elif action == 'adopt':self.personal_keys.adopt_legacy(owner)
            return '个人密钥已更新，仅存本机加密目录；需要主动选择自有 API 模式。'
        def done(result,error):
            self.var_api.set('')
            try:
                if owner_of(self.account)!=owner:return
            except Exception:return
            self.var_cloud_status.set('操作失败，请核对登录、产品资格及平台配置。' if error else result)
        self.tasks.submit('arbitration-config',work,done)

    def _export_bundle(self) -> None:
        cfg = self._collect_cfg()
        path = filedialog.asksaveasfilename(
            title="导出模型与配置", defaultextension=".zip",
            initialfile=f"Yuzi_CS2_AIDirector_backup_{datetime.now():%Y%m%d_%H%M%S}.zip",
            filetypes=[("Yuzi 备份", "*.zip"), ("所有文件", "*.*")])
        if not path:
            return
        include = messagebox.askyesno(
            "导出密钥",
            "是否包含 OBS 密码？个人 API 密钥始终不会导出。\n\n"
            "选择“否”更安全，适合分享；导入后需要重新填写。\n"
            "选择“是”会把明文密钥写入 ZIP。",
            parent=self.root, default="no")
        try:
            result = bundle_mod.export_bundle(
                Path(path), cfg, app_data_dir(), VERSION, include)
            self.bus.info(f"已导出备份：{result['path']}（{len(result['files'])} 个文件）")
            messagebox.showinfo(
                "导出完成",
                f"已导出：\n{result['path']}\n\n"
                f"文件：{', '.join(result['files'])}\n"
                + ("包含密钥，请妥善保管。" if include else "未包含密钥。"),
                parent=self.root)
        except Exception as e:
            messagebox.showerror("导出失败", f"{type(e).__name__}: {e}",
                                 parent=self.root)

    def _import_bundle(self) -> None:
        if self.engine is not None:
            messagebox.showwarning("导入备份", "请先停止导播，再导入模型/配置。",
                                   parent=self.root)
            return
        path = filedialog.askopenfilename(
            title="导入模型与配置", filetypes=[("Yuzi 备份", "*.zip"),
                                               ("所有文件", "*.*")])
        if not path:
            return
        try:
            info = bundle_mod.inspect_bundle(Path(path))
            manifest = info["manifest"]
            if not messagebox.askyesno(
                    "确认导入",
                    f"备份时间：{manifest.get('created_at', '?')}\n"
                    f"程序版本：{manifest.get('app_version', '?')}\n"
                    f"包含密钥：{'是' if manifest.get('secrets_included') else '否'}\n\n"
                    "导入会覆盖当前配置和模型，并先自动备份。继续？",
                    parent=self.root, default="no"):
                return
            result = bundle_mod.import_bundle(Path(path), app_data_dir())
            self.model_arbiter.load()
            self._update_model_label()
            messagebox.showinfo(
                "导入完成",
                f"已导入 {len(result['files'])} 个文件。\n"
                f"旧文件备份：\n{result['backup_dir']}\n\n"
                "模型已重新加载；配置（端口/快捷键/密钥等）需要重启程序后生效。",
                parent=self.root)
        except Exception as e:
            messagebox.showerror("导入失败", f"{type(e).__name__}: {e}",
                                 parent=self.root)

    def _save_only(self) -> None:
        try:
            cfg = self._collect_cfg()
            save_config(cfg)
            self.cfg = cfg
            if self.engine is None:
                self._apply_runtime_cfg(cfg)
            self._register_control_hotkeys()
            self.bus.info(f"配置已保存：{config_path()}")
            self._refresh_gsi_status()
            messagebox.showinfo("保存配置", f"已保存到：\n{config_path()}\n控制快捷键已更新；其他运行参数下次启动应用。")
        except Exception as e:
            messagebox.showerror("保存配置", f"保存失败：{e}")

    # ---- 状态轮询 ----
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            self._poll_control_hotkeys()
            self._refresh_cinematic_controls()
            self._drain_logs()
            running = self.engine.status() if self.engine else {}
            self.workspace_status.configure(text=(
                f"导播：{'运行中' if running.get('running') else '已停止'}\n"
                f"游戏数据：{running.get('gsi', '等待启动')}\n"
                f"当前视角：{running.get('observed') or '-'}\n"
                f"提示：{running.get('reason') or '可在设置中心查看账号与模型状态'}"))
            now = time.monotonic()
            if now - self._last_pro_log_refresh > 1.0:
                self._last_pro_log_refresh = now
                self._refresh_pro_logs()
                obs = self.pro.status()
                if obs.get("obs"):
                    self.lbl_obs.configure(text="OBS：已连接", foreground=self._colors['success'])
            if now - self._last_model_check > 5.0:
                self._last_model_check = now
                if self.engine is None and 'model-load' not in self.tasks.running:
                    def reloaded(changed, error):
                        if error:
                            self.bus.warning(f'本地候选模型检查失败：{error}')
                        elif changed:
                            self.bus.info('检测到更新的本地候选模型。')
                            self._update_model_label()
                    self.tasks.submit('model-check', self.model_arbiter.reload_if_changed, reloaded)
            status = self.engine.status() if self.engine else None
            if status and status.get("running"):
                self._render_status(status)
            elif self.engine is None:
                self.lbl_conn.configure(text="GSI：未启动", foreground=self._colors['muted'])
                self.lbl_play.configure(text="阶段：-    当前视角：-    AI 目标：-")
                self.lbl_ai.configure(text="AI：未启动    运镜：-")
                self.lbl_reason.configure(text="决策：-")
                self.lbl_story.configure(text="故事：-    当前镜头：-")
                self.lbl_metrics.configure(text="专业指标：-")
        except Exception as e:
            # 状态栏刷新失败不应打断 after 循环；5 秒最多记一条，避免刷屏
            now = time.monotonic()
            if now - self._last_gui_warn > 5.0:
                self._last_gui_warn = now
                self.bus.warning(f"状态刷新异常（继续运行）：{type(e).__name__}: {e}")
        schedule(self.root,200, self._poll)

    def _render_status(self, st: dict[str, Any]) -> None:
        gsi = st.get("gsi")
        age = st.get("gsi_age")
        packets = st.get("packets", 0)
        if gsi == "ok" and age is not None:
            text = f"GSI：已连接（{age:.1f}s 前，收包 {packets}）"
            color = self._colors['success']
        elif gsi == "stale" and age is not None:
            text = f"GSI：断流 {age:.1f}s"
            color = self._colors['error']
        else:
            text = "GSI：等待数据…（检查是否重启 CS2）"
            color = self._colors['accent']
        self.lbl_conn.configure(text=text, foreground=color)

        observed = st.get("observed")
        target = st.get("target")
        tname = st.get("target_name") or ""
        paused = float(st.get("paused_for") or 0)
        observed_txt = format_slot(observed) if observed is not None else "-"
        target_txt = f"{format_slot(target)} {tname}" if target is not None else "-"
        self.lbl_play.configure(
            text=f"阶段：{st.get('phase', '-')}    当前视角：{observed_txt}"
                 f"    AI 目标：{target_txt}"
                 + (f"    手动接管剩余：{paused:.1f}s" if paused > 0 else "")
        )

        ai = st.get("ai") or {}
        ai_txt = "关闭" if not ai.get("enabled") else f"开启 调用{ai.get('calls', 0)}/失败{ai.get('errors', 0)}"
        learn = st.get("demo_model") or {}
        learn_txt = ("关闭" if not learn.get("enabled")
                     else f"{learn.get('samples', 0)} 样本 / 命中 {learn.get('hit_rate', 0)}%")
        decision = st.get("decision") or {}
        model = st.get("model") or {}
        model_txt = "已加载" if model.get("loaded") else "未加载"
        trig = st.get("triggers") or {}
        self.lbl_ai.configure(
            text=f"AI：{ai_txt}    Demo模型：{learn_txt}"
                 f"    决策样本：{decision.get('rows', 0)}（正 {decision.get('positive', 0)}）"
                 f"    随机森林：{model_txt}"
                 f"    运镜：出生点 {trig.get('round', 0)} 次 / 包点 {trig.get('bomb', 0)} 次"
                 f"    10s切换 {st.get('cuts_10s', 0)} 次"
                 f"    权限：{'管理员' if st.get('admin') else '普通'}"
                 + (f"    运镜保护 {st.get('bomb_cam_for', 0):.1f}s" if st.get("bomb_cam_for", 0) > 0 else "")
        )
        self.lbl_reason.configure(
            text=f"决策：{st.get('reason', '-')}    最高分：{float(st.get('score') or 0):.2f}    候选：{st.get('candidates', 0)}"
        )

        story = st.get("story") or {}
        story_txt = st.get("story_text") or story.get("story") or "-"
        cur = st.get("current_shot") or {}
        if cur:
            target_label = str(cur.get("target", "-"))
            if cur.get("target_slot") is not None:
                key = slot_to_key(cur.get("target_slot"))
                if key is not None:
                    target_label += f"（键{key}）"
            shot_txt = (
                f"{cur.get('type_label', cur.get('type', '?'))} "
                f"{target_label} "
                f"({float(cur.get('duration') or 0):.1f}s / {cur.get('source', '-')})"
            )
        else:
            shot_txt = "-"
        self.lbl_story.configure(
            text=f"故事：{story_txt}    当前镜头：{shot_txt}")
        self.lbl_narrative.configure(
            text=f"叙事：{st.get('narrative') or '等待回合开始…'}")
        self.lbl_pro_shot.configure(
            text=f"当前镜头：{shot_txt}    故事：{story_txt}")

        m = st.get("metrics") or {}

        def _pct(v: Any) -> str:
            return "-" if v is None else f"{float(v) * 100:.0f}%"

        def _num(v: Any, suffix: str = "") -> str:
            return "-" if v is None else f"{float(v):.2f}{suffix}"

        metrics_txt = (
            f"击杀覆盖 {_pct(m.get('kill_coverage'))}"
            f"    交火前覆盖 {_pct(m.get('pre_fight_coverage'))}"
            f"    尸体 {_num(m.get('corpse_time'), 's')}"
            f"    切换 {_num(m.get('cut_rate'), '/min')}"
            f"    POV均长 {_num(m.get('shot_duration_mean'), 's')}"
            f"    人工 {int(m.get('manual_takeover') or 0)}"
            f"    回合 {int(m.get('rounds') or 0)}"
        )
        self.lbl_metrics.configure(text="专业指标：" + metrics_txt)
        self.lbl_pro_metrics.configure(text="专业指标：" + metrics_txt)

    def _drain_logs(self) -> None:
        lines: list[tuple[str, str]] = []
        for _ in range(120):
            try:
                lines.append(self.bus.queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        follow = getattr(self, '_follow_logs', True) and self.txt.yview()[1] >= .999
        stamp = datetime.now().strftime("%H:%M:%S")
        self.txt.configure(state="normal")
        try:
            for level, msg in lines:
                self.txt.insert(
                    "end", f"[{stamp}] {msg}\n",
                    level if level in ("WARN", "ERROR") else "INFO")
            # 每个批次只裁剪/滚动一次，避免每行都触发 Tk 布局。
            if int(self.txt.index("end-1c").split(".")[0]) > 800:
                self.txt.delete("1.0", "200.0")
            if follow:
                self.txt.see("end")
        finally:
            self.txt.configure(state="disabled")

    # ---- 关闭 ----
    def _on_close(self) -> None:
        if getattr(self,'_training',False) or getattr(self,'_calibrating',False):
            messagebox.showinfo('任务运行中','请等待训练或校准完成后再关闭程序。',parent=self.root)
            return
        if self._closing:
            return
        if "stop" in self.tasks.running:
            schedule(self.root, 100, self._on_close)
            return
        self._closing = True
        self._unregister_control_hotkeys()
        self.btn_start.configure(state='disabled')
        self.btn_stop.configure(state='disabled')
        self.btn_cinematics.configure(state='disabled')
        self.lbl_conn.configure(text='正在停止并保存本地记录…')
        try:
            draft = self._collect_cfg()
        except ValueError:
            draft = None
        engine = self.engine
        stopped = threading.Event()
        from desktop_ui import window_preferences
        window_changes = window_preferences(self, 'director')
        def work():
            self.updates.shutdown()
            if window_changes:
                self.preferences.update(**window_changes)
            if engine is not None:
                engine.stop()
            if self.timeline.close(time.monotonic(), '窗口关闭') is False:
                raise RuntimeError('镜头时间线尚未完全落盘，请检查磁盘后重试关闭')
            stopped.set()
            if draft is not None:
                save_config(draft)
            if not self.training_data.close():
                return False
            self.pro.stop()
            if getattr(self, "diagnostics", None): self.diagnostics.close()
            self.account.close()
            return True
        def done(flushed, error):
            if stopped.is_set():
                self.engine = None
                self.account.release_execution()
            if error or not flushed:
                self._closing = False
                self.btn_start.configure(state='normal' if self.engine is None else 'disabled')
                self.btn_stop.configure(state='normal' if self.engine is not None else 'disabled')
                self.btn_cinematics.configure(state='normal')
                self._register_control_hotkeys()
                schedule(self.root, 200, self._poll)
                messagebox.showerror('关闭未完成', str(error) if error else self.training_data.status_text(), parent=self.root)
                return
            from client_update_install import before_destroy
            before_destroy(self)
            self.root.destroy()
        self.tasks.submit('close', work, done)

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
    import multiprocessing
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    from trainer_launcher import default_entry_arguments
    arguments = default_entry_arguments(arguments)
    if getattr(sys,'frozen',False) and not arguments and Path(sys.executable).name=='update-helper.exe':
        from client_update_install import helper_main
        confirm_root=tk.Tk();confirm_root.withdraw()
        confirmed=messagebox.askyesno('恢复中断更新','是否恢复本次更新保留的旧程序？请先关闭导播和训练器窗口。账号及采集数据保持。',parent=confirm_root)
        confirm_root.destroy()
        return helper_main([str(Path(sys.executable).parent/'job.json'),'recover']) if confirmed else 0
    if arguments[:1] == ['--client-update-fetch']:
        from client_update_transport import worker_main
        return worker_main(arguments[1:])
    if arguments[:1] == ['--client-update-download']:
        from client_update_download import worker_main
        return worker_main(arguments[1:])
    if arguments[:1] == ['--client-update-install']:
        from client_update_install import helper_main
        return helper_main(arguments[1:])
    if "--account-check" in arguments:
        from account_client import main as account_main
        return account_main([a for a in arguments if a != "--account-check"])
    if "--data-queue" in arguments:
        from training_data_runtime import main as queue_main
        return queue_main([a for a in arguments if a != "--data-queue"])
    if "--offline-check" in arguments:
        from offline_runtime import main as offline_main
        return offline_main([a for a in arguments if a != "--offline-check"])
    if "--trainer" in arguments:
        # Same executable, separate process: training does not occupy the director UI/GIL.
        from trainer_gui import main as trainer_main
        sys.argv = [sys.argv[0]] + [a for a in arguments if a != "--trainer"]
        trainer_main()
        return 0
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} v{VERSION}")
    parser.add_argument("--check", action="store_true", help="打印环境自检表后退出")
    parser.add_argument('--check-report', type=Path, help='将环境自检写入 JSON，供无控制台 exe 验证')
    parser.add_argument('--check-updates-report', type=Path, help='主动联网检查程序发行并写入 JSON，不安装')
    parser.add_argument('--update-channel', choices=('stable','beta'), default='stable', help='主动检查更新的通道')
    parser.add_argument('--client-update-health',type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--install-deps", action="store_true", help="安装缺失依赖后退出")
    args = parser.parse_args(argv)

    if args.check_updates_report:
        from client_update_transport import report
        return report(args.check_updates_report,args.update_channel)

    if args.check or args.check_report:
        print_env_report()
        if args.check_report:
            import platform
            from director_core import __version__ as core_version
            from demo_live_dataset import PARSER_VERSION
            from public_trust import public_trust_status
            from client_update_download import trust_status
            report = {'client_version': VERSION, 'core_version': core_version,
                      'feature_version': 'pov-v1', 'demo_parser': PARSER_VERSION,
                      'python': sys.version.split()[0], 'platform': platform.platform(),
                      'frozen': bool(getattr(sys, 'frozen', False)),
                      'dependencies': check_dependencies(),
                      'public_trust': public_trust_status(),
                      'application_update_trust': trust_status()}
            args.check_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
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

    from diagnostics_runtime import bootstrap
    bootstrap(app_data_dir())
    cfg = load_config()
    app = DirectorApp(cfg)
    if args.client_update_health:
        from client_update_install import health_ready
        try:health_ready(app,args.client_update_health)
        except (OSError,ValueError):app._on_close()
    app.run()
    return 0


if __name__ == "__main__":
    from client_update_install import application_guard
    with application_guard(sys.argv[1:]):
        sys.exit(main())

