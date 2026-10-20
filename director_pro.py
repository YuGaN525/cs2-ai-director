#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专业导播能力：独立日志、事件动作、OBS WebSocket、FFmpeg 击杀回放、玩家改名。

这部分按 CS2-Director-Tool 的行为重写，不复制其 C# 代码；OBS/FFmpeg 都是可选依赖，
未配置时 AI 导播不受影响。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

IS_WINDOWS = os.name == "nt"


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

    CATEGORIES = ("GSI", "OBS", "FFmpeg", "Replay", "EventAction", "Player", "App")

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
    "start_recording": "开始录制",
    "record_kill": "记录击杀点",
    "generate_replay": "生成回放",
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
        for key in (name, name.replace("_", "")):
            if hasattr(obj, key):
                return getattr(obj, key)
        return default

    def start_record(self) -> bool:
        try:
            self._call("start_record")
            self.log.info("OBS", "开始录制")
            return True
        except Exception as e:
            self.log.error("OBS", f"开始录制失败：{e}")
            return False

    def stop_record(self) -> str:
        try:
            resp = self._call("stop_record")
            path = str(self._attr(resp, "output_path", "") or "")
            directory = str(self._attr(self._call("get_record_directory"),
                                      "record_directory", "") or "")
            if path and directory and not Path(path).is_absolute():
                path = str(Path(directory) / path)
            self.log.info("OBS", f"停止录制：{path or '(路径未知)'}")
            return path
        except Exception as e:
            self.log.error("OBS", f"停止录制失败：{e}")
            return ""

    def recording(self) -> bool:
        try:
            resp = self._call("get_record_status")
            return bool(self._attr(resp, "output_active", False))
        except Exception:
            return False

    def current_scene(self) -> str:
        try:
            resp = self._call("get_current_program_scene")
            return str(self._attr(resp, "current_program_scene", "") or "")
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
                self._call("create_scene", name)
                self.log.info("OBS", f"已创建场景：{name}")
            self._call("set_current_program_scene", name)
            self.log.info("OBS", f"切换场景：{name}")
            return True
        except Exception as e:
            self.log.error("OBS", f"切换场景失败：{e}")
            return False

    def inputs(self) -> list[str]:
        try:
            resp = self._call("get_input_list")
            items = self._attr(resp, "inputs", []) or []
            return [str(x.get("inputName", "")) for x in items if isinstance(x, dict)]
        except Exception:
            return []

    def ensure_media_source(self, scene: str, source: str, file_path: str) -> bool:
        try:
            if scene not in self.scenes():
                self._call("create_scene", scene)
            settings = {
                "local_file": file_path,
                "is_local_file": True,
                "restart_on_activate": True,
                "close_when_inactive": False,
            }
            if source in self.inputs():
                self._call("set_input_settings", source, settings, True)
                try:
                    self._call("get_scene_item_id", scene, source)
                except Exception:
                    self._call("create_scene_item", scene, source, True)
            else:
                self._call("create_input", scene, source, "ffmpeg_source", settings, True)
            self.log.info("OBS", f"媒体源 {source} → {file_path}")
            return True
        except Exception as e:
            self.log.error("OBS", f"设置媒体源失败：{e}")
            return False

    def media_action(self, source: str, action: str) -> bool:
        mapping = {
            "play": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
            "stop": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
            "restart": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
        }
        try:
            self._call("trigger_media_input_action", source, mapping[action])
            self.log.info("OBS", f"媒体源 {source}：{action}")
            return True
        except Exception as e:
            self.log.error("OBS", f"媒体操作失败：{e}")
            return False

    def media_state(self, source: str) -> tuple[str, float, float]:
        try:
            resp = self._call("get_media_input_status", source)
            state = str(self._attr(resp, "media_state", "") or "")
            dur = float(self._attr(resp, "media_duration", 0.0) or 0.0)
            cur = float(self._attr(resp, "media_cursor", 0.0) or 0.0)
            return state, dur, cur
        except Exception:
            return "", 0.0, 0.0

    def wait_media_end(self, source: str, timeout: float) -> bool:
        deadline = time.monotonic() + max(1.0, timeout)
        time.sleep(0.5)
        while time.monotonic() < deadline:
            state, dur, cur = self.media_state(source)
            if "ENDED" in state:
                return True
            if dur > 0 and cur >= dur - 0.2:
                return True
            time.sleep(0.25)
        return False


# --------------------------------------------------------------------------
# FFmpeg
# --------------------------------------------------------------------------

@dataclass
class ClipSegment:
    start: float
    duration: float


def merge_clips(clips: list[ClipSegment]) -> list[ClipSegment]:
    if len(clips) <= 1:
        return clips
    ordered = sorted(clips, key=lambda c: c.start)
    merged = [ordered[0]]
    for nxt in ordered[1:]:
        cur = merged[-1]
        if nxt.start <= cur.start + cur.duration:
            cur.duration = max(cur.start + cur.duration,
                               nxt.start + nxt.duration) - cur.start
        else:
            merged.append(nxt)
    return merged


def build_clip_commands(ffmpeg: str, recording: str, out_dir: Path,
                        clips: list[ClipSegment]) -> tuple[list[list[str]], list[Path]]:
    cmds: list[list[str]] = []
    parts: list[Path] = []
    for i, c in enumerate(clips):
        part = out_dir / f"part_{i:03d}.ts"
        parts.append(part)
        cmds.append([
            ffmpeg, "-y", "-ss", f"{max(0.0, c.start):.3f}", "-i", recording,
            "-t", f"{max(0.2, c.duration):.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
            "-avoid_negative_ts", "make_zero", str(part),
        ])
    return cmds, parts


class FfmpegService:
    def __init__(self, cfg: dict[str, Any], log: ProLog) -> None:
        self.cfg = cfg
        self.log = log

    def available(self) -> bool:
        path = str(self.cfg.get("ffmpeg_path", "")).strip()
        return bool(path) and Path(path).is_file()

    def clip_and_concat(self, recording: str, output: str,
                        clips: list[ClipSegment]) -> bool:
        ffmpeg = str(self.cfg.get("ffmpeg_path", "")).strip()
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.log.error("FFmpeg", "未配置有效的 ffmpeg.exe")
            return False
        if not Path(recording).is_file():
            self.log.error("FFmpeg", f"录制文件不存在：{recording}")
            return False
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        work = out.parent / f"{out.stem}_parts"
        work.mkdir(parents=True, exist_ok=True)
        clips = merge_clips(clips)
        cmds, parts = build_clip_commands(ffmpeg, recording, work, clips)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
        try:
            for cmd in cmds:
                self.log.info("FFmpeg", "剪辑 " + " ".join(cmd[-3:]))
                subprocess.run(cmd, check=True, capture_output=True,
                               creationflags=flags)
            concat = work / "concat.txt"
            concat.write_text(
                "\n".join(f"file '{p.as_posix()}'" for p in parts),
                encoding="utf-8",
            )
            final = [
                ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                "-c", "copy", str(out),
            ]
            subprocess.run(final, check=True, capture_output=True, creationflags=flags)
            self.log.info("FFmpeg", f"回放生成：{out}")
            return True
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode("utf-8", "replace")[:300]
            self.log.error("FFmpeg", f"剪辑失败：{err}")
            return False
        except Exception as e:
            self.log.error("FFmpeg", f"剪辑异常：{e}")
            return False


# --------------------------------------------------------------------------
# 击杀回放工作流
# --------------------------------------------------------------------------

class ReplayManager:
    def __init__(self, cfg: dict[str, Any], obs: ObsController,
                 ffmpeg: FfmpegService, log: ProLog) -> None:
        self.cfg = cfg
        self.obs = obs
        self.ffmpeg = ffmpeg
        self.log = log
        self._lock = threading.Lock()
        self._in_round = False
        self._recording = False
        self._started = 0.0
        self._kills: list[float] = []
        self._playing = False

    @property
    def prerequisites(self) -> bool:
        return self.obs.connected and self.ffmpeg.available()

    def start_recording(self) -> bool:
        if not self.prerequisites:
            self.log.warning("Replay", "前置条件不满足（OBS/FFmpeg）")
            return False
        with self._lock:
            if self._in_round or self._playing:
                return False
            self._in_round = True
            self._kills = []
            # 清理旧录制，避免片段定位到上一回合
            if self.obs.recording():
                self.obs.stop_record()
                time.sleep(0.3)
            game = str(self.cfg.get("obs_game_scene", "Game")) or "Game"
            self.obs.switch_scene(game)
            if not self.obs.start_record():
                self._in_round = False
                return False
            self._started = time.monotonic()
            self._recording = True
            self.log.info("Replay", "回合录制已开始")
            return True

    def record_kill(self) -> None:
        with self._lock:
            if not self._recording:
                return
            t = max(0.0, time.monotonic() - self._started)
            self._kills.append(t)
            self.log.info("Replay", f"记录击杀点 {t:.2f}s")

    def generate_replay(self) -> bool:
        with self._lock:
            if not self._in_round or self._playing:
                return False
            points = list(self._kills)
            self._in_round = False
            self._recording = False
            self._playing = True
        try:
            path = self.obs.stop_record()
            if not path or not Path(path).is_file():
                self.log.error("Replay", f"找不到录制文件：{path}")
                return False
            if not points:
                self.log.info("Replay", "本回合没有击杀点，跳过回放")
                return False
            clips: list[ClipSegment] = []
            for i, ts in enumerate(sorted(points)):
                clips.append(ClipSegment(
                    max(0.0, ts - (4.0 if i == 0 else 1.0)),
                    5.0 if i == 0 else 2.0,
                ))
            out_dir_cfg = str(self.cfg.get("replay_output_dir", "")).strip()
            out_dir = Path(out_dir_cfg) if out_dir_cfg else Path(
                os.environ.get("TEMP", ".")) / "CS2AIDirectorReplay"
            out_dir.mkdir(parents=True, exist_ok=True)
            output = out_dir / f"replay_{datetime.now():%Y%m%d_%H%M%S}.mp4"
            if not self.ffmpeg.clip_and_concat(path, str(output), clips):
                return False
            return self._play_output(output, sum(c.duration for c in clips))
        finally:
            with self._lock:
                self._playing = False

    def _play_output(self, output: Path, duration: float) -> bool:
        scene = str(self.cfg.get("obs_replay_scene", "Replay")) or "Replay"
        source = str(self.cfg.get("obs_replay_source", "Replay")) or "Replay"
        game = str(self.cfg.get("obs_game_scene", "Game")) or "Game"
        try:
            self.obs.switch_scene(scene)
            if not self.obs.ensure_media_source(scene, source, str(output)):
                return False
            self.obs.media_action(source, "restart")
            self.log.info("Replay", f"播放回放 {output.name}")
            self.obs.wait_media_end(source, duration + 5.0)
        finally:
            self.obs.switch_scene(game)
            self.obs.start_record()
            with self._lock:
                self._started = time.monotonic()
                self._recording = True
                self._in_round = True
            self.log.info("Replay", "回放结束，恢复录制")
        return True

    def reset(self) -> None:
        with self._lock:
            self._in_round = False
            self._recording = False
            self._playing = False
            self._kills = []


# --------------------------------------------------------------------------
# 事件动作
# --------------------------------------------------------------------------

class EventActionEngine:
    def __init__(self, cfg: dict[str, Any], obs: ObsController,
                 replay: ReplayManager, log: ProLog) -> None:
        self.cfg = cfg
        self.obs = obs
        self.replay = replay
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
                    self.obs.media_action(target, "restart")
                elif typ == "stop_media":
                    self.obs.media_action(target, "stop")
                elif typ == "switch_scene":
                    self.obs.switch_scene(target)
                elif typ == "start_recording":
                    self.replay.start_recording()
                elif typ == "record_kill":
                    self.replay.record_kill()
                elif typ == "generate_replay":
                    self.replay.generate_replay()
                else:
                    self.log.warning("EventAction", f"未知动作：{typ}")
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
# 玩家改名
# --------------------------------------------------------------------------

def build_rename_commands(players: dict[str, Any], base_url: str,
                          timeout: float = 10.0) -> tuple[list[str], list[str]]:
    base = base_url.strip().rstrip("/")
    commands: list[str] = []
    failed: list[str] = []
    for sid, p in players.items():
        name = str(getattr(p, "name", "") or "")
        if not sid:
            continue
        url = f"{base}/api/v1/players/steam/{urllib.parse.quote(str(sid))}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CS2AIDirector/1.8"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            registered = str(data.get("name") or "").strip()
            if registered:
                safe = registered.replace('"', "'")
                commands.append(
                    f'mirv_replace_name byXuid add "{sid}" "{safe}"'
                )
        except Exception:
            failed.append(name or str(sid))
    return commands, failed


# --------------------------------------------------------------------------
# 门面
# --------------------------------------------------------------------------

class ProDirector:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.log = ProLog()
        self.obs = ObsController(cfg, self.log)
        self.ffmpeg = FfmpegService(cfg, self.log)
        self.replay = ReplayManager(cfg, self.obs, self.ffmpeg, self.log)
        self.actions = EventActionEngine(cfg, self.obs, self.replay, self.log)
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
        self.replay.reset()
        self.last_events = []

    def stop(self) -> None:
        self.actions.stop()
        self.obs.disconnect()

    def status(self) -> dict[str, Any]:
        return {
            "obs": self.obs.connected,
            "obs_error": self.obs.last_error,
            "replay_ready": self.replay.prerequisites,
            "latest_events": [e.label for e in self.last_events[-6:]],
        }
