#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专业导播能力：独立日志、事件动作、OBS WebSocket（场景/媒体源控制）。

这部分按 CS2-Director-Tool 的行为重写，不复制其 C# 代码；OBS 是可选依赖，
未配置时 AI 导播不受影响。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------
# 独立日志
# --------------------------------------------------------------------------

@dataclass
class LogRecord:
    ts: float
    level: str
    category: str
    message: str

    def line(self) -> str:
        return f"{datetime.fromtimestamp(self.ts):%H:%M:%S} [{self.category}] {self.message}"


class ProLog:
    """与 AI 日志分开的导播日志；GUI 可按类别/关键字/时间过滤。"""

    CATEGORIES = ("GSI", "OBS", "EventAction", "App")

    def __init__(self, maxlen: int = 3000) -> None:
        self._records: list[LogRecord] = []
        self._lock = threading.Lock()
        self._maxlen = maxlen
        self._listeners: list[Callable[[LogRecord], None]] = []

    def add_listener(self, fn: Callable[[LogRecord], None]) -> None:
        self._listeners.append(fn)

    def log(self, category: str, message: str, level: str = "INFO") -> None:
        rec = LogRecord(time.time(), level.upper(), category, message)
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._maxlen:
                del self._records[: len(self._records) - self._maxlen]
        for fn in list(self._listeners):
            try:
                fn(rec)
            except Exception:
                pass

    def info(self, category: str, message: str) -> None:
        self.log(category, message, "INFO")

    def warning(self, category: str, message: str) -> None:
        self.log(category, message, "WARN")

    def error(self, category: str, message: str) -> None:
        self.log(category, message, "ERROR")

    def snapshot(self) -> list[LogRecord]:
        with self._lock:
            return list(self._records)

    def filtered(self, category: str = "", keyword: str = "",
                 level: str = "", since: float = 0.0) -> list[LogRecord]:
        kw = keyword.strip().lower()
        cat = category.strip()
        lv = level.strip().upper()
        out = []
        for r in self.snapshot():
            if r.ts < since:
                continue
            if cat and cat != "全部" and r.category != cat:
                continue
            if lv and lv != "全部" and r.level != lv:
                continue
            if kw and kw not in r.message.lower():
                continue
            out.append(r)
        return out


# --------------------------------------------------------------------------
# GSI 事件检测
# --------------------------------------------------------------------------

EVENT_LABELS = {
    "match_started": "比赛开始",
    "round_started": "回合开始",
    "round_ended": "回合结束",
    "game_over": "比赛结束",
    "warmup_started": "热身开始",
    "warmup_over": "热身结束",
    "pause_started": "暂停开始",
    "pause_over": "暂停结束",
    "bomb_planted": "炸弹安装",
    "bomb_defused": "炸弹拆除",
    "bomb_exploded": "炸弹爆炸",
    "player_died": "玩家死亡",
    "kill": "玩家击杀",
}

ACTION_LABELS = {
    "play_media": "播放媒体源",
    "stop_media": "停止媒体源",
    "switch_scene": "切换场景",
}


@dataclass
class GsiEvent:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return EVENT_LABELS.get(self.kind, self.kind)


class EventDetector:
    """把状态采样变成离散事件；同一事件不会重复触发。"""

    def __init__(self) -> None:
        self._phase = ""
        self._round = None
        self._bomb_state = ""
        self._alive: dict[str, bool] = {}
        self._kills: dict[str, int] = {}
        self._match_started = False
        self._paused = False
        self._last_round_started: Optional[int] = None
        self._game_over = False

    def poll(self, snap: Any) -> list[GsiEvent]:
        out: list[GsiEvent] = []
        phase = str(getattr(snap, "phase", "") or "")
        rnd = getattr(snap, "round_num", None)
        players = getattr(snap, "players", {}) or {}
        bomb = getattr(snap, "bomb", None)
        first = self._phase == "" and self._round is None

        if first:
            self._phase = phase
            self._round = rnd
            self._init_players(players)
            self._bomb_state = str(getattr(bomb, "state", "") or "")
            self._match_started = phase in ("live", "freezetime")
            return out

        # 回合/地图阶段
        if rnd is not None and self._round is not None and rnd != self._round:
            self._round = rnd
            self._reset_round_state(players)
            if self._last_round_started != rnd:
                self._last_round_started = rnd
                out.append(GsiEvent("round_started", {"round": rnd}))
        if phase != self._phase:
            prev, self._phase = self._phase, phase
            if phase == "warmup" and prev != "warmup":
                out.append(GsiEvent("warmup_started"))
            elif prev == "warmup" and phase != "warmup":
                out.append(GsiEvent("warmup_over"))
            if phase == "live" and not self._match_started:
                self._match_started = True
                out.append(GsiEvent("match_started"))
            if phase in ("freezetime", "live"):
                if self._last_round_started != rnd:
                    self._last_round_started = rnd
                    out.append(GsiEvent("round_started", {"round": rnd}))
            if phase == "over" and prev != "over":
                out.append(GsiEvent("round_ended", {"round": rnd}))
            if phase == "gameover" and not self._game_over:
                self._game_over = True
                out.append(GsiEvent("game_over"))
            paused = phase == "paused"
            if paused != self._paused:
                self._paused = paused
                out.append(GsiEvent("pause_started" if paused else "pause_over"))

        # 炸弹
        bstate = str(getattr(bomb, "state", "") or "") if bomb else ""
        if bstate != self._bomb_state:
            prev_b, self._bomb_state = self._bomb_state, bstate
            if bstate == "planted" and prev_b != "planted":
                out.append(GsiEvent("bomb_planted"))
            elif bstate == "defused" and prev_b != "defused":
                out.append(GsiEvent("bomb_defused"))
            elif bstate == "exploded" and prev_b != "exploded":
                out.append(GsiEvent("bomb_exploded"))

        # 死亡/击杀
        for sid, p in players.items():
            alive = int(getattr(p, "health", 0) or 0) > 0
            was = self._alive.get(sid)
            self._alive[sid] = alive
            if was is True and not alive:
                out.append(GsiEvent("player_died", {
                    "steamid": sid, "name": getattr(p, "name", ""),
                }))
            rk = int(getattr(p, "round_kills", 0) or 0)
            prev_k = self._kills.get(sid)
            self._kills[sid] = rk
            if prev_k is not None and rk > prev_k:
                out.append(GsiEvent("kill", {
                    "steamid": sid, "name": getattr(p, "name", ""),
                    "count": rk - prev_k,
                }))
        return out

    def _init_players(self, players: dict[str, Any]) -> None:
        for sid, p in players.items():
            self._alive[sid] = int(getattr(p, "health", 0) or 0) > 0
            self._kills[sid] = int(getattr(p, "round_kills", 0) or 0)

    def _reset_round_state(self, players: dict[str, Any]) -> None:
        self._alive = {}
        self._kills = {}
        self._init_players(players)
        self._bomb_state = ""
        self._game_over = False


# --------------------------------------------------------------------------
# OBS WebSocket
# --------------------------------------------------------------------------

class ObsController:
    def __init__(self, cfg: dict[str, Any], log: ProLog) -> None:
        self.cfg = cfg
        self.log = log
        self.client: Any = None
        self._lock = threading.Lock()
        self.last_error = ""

    @property
    def connected(self) -> bool:
        return self.client is not None

    def connect(self) -> bool:
        try:
            from obsws_python import ReqClient
        except Exception:
            self.last_error = "未安装 obsws-python，请执行 pip install obsws-python"
            self.log.error("OBS", self.last_error)
            return False
        host = str(self.cfg.get("obs_host", "127.0.0.1")).strip() or "127.0.0.1"
        try:
            port = int(self.cfg.get("obs_port", 4455))
        except (TypeError, ValueError):
            port = 4455
        pwd = str(self.cfg.get("obs_password", ""))
        try:
            self.client = ReqClient(host=host, port=port, password=pwd, timeout=5)
            # 触发一次请求验证认证
            self.client.get_version()
            self.log.info("OBS", f"已连接 {host}:{port}")
            return True
        except Exception as e:
            self.client = None
            self.last_error = str(e)
            self.log.error("OBS", f"连接失败：{e}")
            return False

    def disconnect(self) -> None:
        try:
            if self.client is not None:
                self.client.disconnect()
        except Exception:
            pass
        self.client = None
        self.log.info("OBS", "已断开")

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self.client is None:
            raise RuntimeError("OBS 未连接")
        with self._lock:
            return getattr(self.client, name)(*args, **kwargs)

    @staticmethod
    def _attr(obj: Any, name: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            for key in (name, name.replace("_", ""),
                        name.replace("_", "") .lower()):
                if key in obj:
                    return obj[key]
        for key in (name, name.replace("_", ""), f"{name}_name"):
            if hasattr(obj, key):
                return getattr(obj, key)
        return default

    def inputs(self) -> list[dict[str, Any]]:
        try:
            resp = self._call("get_input_list")
            items = self._attr(resp, "inputs", []) or []
            out: list[dict[str, Any]] = []
            for item in items:
                if isinstance(item, dict):
                    out.append(item)
                else:
                    out.append({
                        "inputName": self._attr(item, "input_name", ""),
                        "inputKind": self._attr(item, "input_kind", ""),
                    })
            return out
        except Exception:
            return []

    def current_scene(self) -> str:
        try:
            resp = self._call("get_current_program_scene")
            # obsws-python 不同版本返回 dataclass/类/dict，统一兜底。
            for key in ("current_program_scene_name", "scene_name",
                        "current_program_scene", "sceneName"):
                value = self._attr(resp, key, "")
                if value:
                    return str(value)
            return ""
        except Exception:
            return ""

    def scenes(self) -> list[str]:
        try:
            resp = self._call("get_scene_list")
            items = self._attr(resp, "scenes", []) or []
            return [str(x.get("sceneName", "")) for x in items if isinstance(x, dict)]
        except Exception:
            return []

    def switch_scene(self, name: str) -> bool:
        if not name:
            return False
        try:
            if name not in self.scenes():
                self.log.error(
                    "OBS",
                    f"场景不存在：{name}；OBS 场景名必须完全一致（区分大小写）")
                return False
            self._call("set_current_program_scene", name)
            self.log.info("OBS", f"切换场景：{name}")
            return True
        except Exception as e:
            self.log.error("OBS", f"切换场景失败：{e}")
            return False

    def media_action(self, source: str, action: str) -> bool:
        mapping = {
            "play": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
            "stop": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
            "restart": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
        }
        source = str(source or "")
        if not source:
            self.log.error("OBS", "媒体源名称为空")
            return False
        items = self.inputs()
        found = next(
            (i for i in items if str(i.get("inputName", "")) == source), None)
        if found is None:
            if items:
                self.log.error(
                    "OBS",
                    f"找不到输入「{source}」；OBS 输入名必须完全一致。"
                    f"当前输入：{', '.join(str(i.get('inputName', '')) for i in items)}")
                return False
            # 输入列表读取失败时不阻断，让触发请求返回真实错误
            self.log.warning("OBS", "无法读取 OBS 输入列表，直接尝试触发媒体动作")
        else:
            kind = str(found.get("inputKind", ""))
            if kind not in ("ffmpeg_source", "vlc_source", "media_source"):
                self.log.error(
                    "OBS",
                    f"「{source}」不是媒体源（kind={kind or '?'}）；"
                    f"如果要切场景，请把动作改成“切换场景”")
                return False
        try:
            self._call("trigger_media_input_action", source, mapping[action])
            self.log.info("OBS", f"媒体源 {source}：{action}")
            return True
        except Exception as e:
            self.log.error("OBS", f"媒体操作失败：{e}")
            return False



# --------------------------------------------------------------------------
# 事件动作
# --------------------------------------------------------------------------

class EventActionEngine:
    def __init__(self, cfg: dict[str, Any], obs: ObsController,
                 log: ProLog) -> None:
        self.cfg = cfg
        self.obs = obs
        self.log = log
        self._queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(maxsize=100)
        self._worker = threading.Thread(target=self._run, name="event-actions",
                                        daemon=True)
        self._worker.start()

    def _rules(self) -> list[dict[str, Any]]:
        rules = self.cfg.get("event_rules", [])
        return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []

    def dispatch(self, events: list[GsiEvent]) -> None:
        if not bool(self.cfg.get("event_actions_enabled", True)):
            return
        for ev in events:
            matched = [
                r for r in self._rules()
                if r.get("enabled", True) and r.get("event") == ev.kind
            ]
            if not matched:
                continue
            self.log.info("EventAction", f"事件触发：{ev.label}，命中 {len(matched)} 条规则")
            for rule in matched:
                try:
                    self._queue.put_nowait(rule)
                except queue.Full:
                    self.log.warning("EventAction", "动作队列已满，丢弃规则")

    def _run(self) -> None:
        while True:
            rule = self._queue.get()
            if rule is None:
                return
            try:
                self.execute(rule)
            except Exception as e:
                self.log.error("EventAction", f"规则执行异常：{e}")

    def execute(self, rule: dict[str, Any]) -> None:
        name = str(rule.get("name", "未命名"))
        for action in rule.get("actions", []):
            if not isinstance(action, dict):
                continue
            typ = str(action.get("type", ""))
            target = str(action.get("target", "") or "")
            label = ACTION_LABELS.get(typ, typ)
            try:
                if typ == "play_media":
                    ok = self.obs.media_action(target, "restart")
                elif typ == "stop_media":
                    ok = self.obs.media_action(target, "stop")
                elif typ == "switch_scene":
                    ok = self.obs.switch_scene(target)
                else:
                    self.log.warning("EventAction", f"未知动作：{typ}")
                    continue
                if not ok:
                    self.log.error(
                        "EventAction",
                        f"规则「{name}」动作「{label} {target}」未生效，详见 OBS 日志")
                    continue
                self.log.info("EventAction", f"规则「{name}」→ {label} {target}".strip())
            except Exception as e:
                self.log.error("EventAction", f"规则「{name}」动作「{label}」失败：{e}")

    def stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass


# --------------------------------------------------------------------------
# 门面
# --------------------------------------------------------------------------

class ProDirector:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.log = ProLog()
        self.obs = ObsController(cfg, self.log)
        self.actions = EventActionEngine(cfg, self.obs, self.log)
        self.detector = EventDetector()
        self.latest_snap: Any = None
        self.last_events: list[GsiEvent] = []
        self._lock = threading.Lock()

    def tick(self, snap: Any, _now: float) -> None:
        with self._lock:
            self.latest_snap = snap
            events = self.detector.poll(snap)
            self.last_events = events[-20:]
        if events:
            for ev in events:
                if ev.kind not in ("kill",):
                    self.log.info("GSI", f"事件：{ev.label} {ev.data or ''}".strip())
            self.actions.dispatch(events)

    def reset(self) -> None:
        self.detector = EventDetector()
        self.last_events = []

    def stop(self) -> None:
        self.actions.stop()
        self.obs.disconnect()

    def status(self) -> dict[str, Any]:
        return {
            "obs": self.obs.connected,
            "obs_error": self.obs.last_error,
            "latest_events": [e.label for e in self.last_events[-6:]],
        }
