#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专业导播 Phase 1：ShotTimeline（镜头时间线）、专业指标、回合故事状态。

设计边界
--------
- 只消费主程序解析好的 GSI Snapshot（鸭子类型），不导入 cs2_ai_director，
  避免循环依赖；训练器仓库可直接复制本文件。
- 不读游戏内存、不注入、不发控制台命令；这里只做记录、统计和状态推断。
- 不改评分/模型：本模块不产生 Candidate，也不影响 choose_slot。

产出文件（默认 %APPDATA%\\CS2AIDirector\\）
----------------------------------------
shot_timeline.csv    每个镜头一行：类型/目标/开始/结束/原因/来源/人工接管
shot_events.csv      回合内事件：击杀/死亡/首次伤害/人工接管/下包/拆包
round_metrics.csv    每回合一行：故事状态 + 六项专业指标 + 计数分子分母
director_metrics.json 最近一次汇总：本机累计指标 + 上一回合指标

指标口径（全部可在 cfg 里调）
------------------------------
kill_coverage       击杀前 metric_kill_lookback 秒内，POV 是否在击杀者身上
pre_fight_coverage  每段交火“首次伤害”前 metric_prefight_lookback 秒内，
                    POV 是否在当事人（攻击者或受害者）身上
corpse_time         镜头停在已阵亡选手 POV 上的总秒数（及占 live 时间比例）
cut_rate            每 live 分钟的镜头切换次数
shot_duration       POV 镜头平均/中位停留时长（秒）
manual_takeover     人工裸 0-9 接管次数（另记录 GSI 确认次数）
"""

from __future__ import annotations

import csv
import io
import json
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

SHOT_POV = "POV"
SHOT_BOMB = "BOMB"
SHOT_OVERVIEW = "OVERVIEW"
SHOT_SPAWN = "SPAWN"
SHOT_TYPES = (SHOT_POV, SHOT_BOMB, SHOT_OVERVIEW, SHOT_SPAWN)

SHOT_LABELS = {
    SHOT_POV: "POV",
    SHOT_BOMB: "包点",
    SHOT_OVERVIEW: "俯视/全景",
    SHOT_SPAWN: "出生点",
}

CINEMATIC_TYPES = (SHOT_BOMB, SHOT_OVERVIEW, SHOT_SPAWN)

PHASE_LABELS = {
    "freezetime": "冻结",
    "opening": "开局",
    "control": "地图控制",
    "engage": "交火",
    "post_plant": "下包后",
    "clutch": "残局",
    "round_over": "回合结束",
    "warmup": "热身",
    "paused": "暂停",
    "gameover": "比赛结束",
    "unknown": "未知",
}

ECONOMY_LABELS = {"full": "全起", "force": "强起", "eco": "经济局", "unknown": "未知"}

SHOT_COLUMNS = [
    "iso_time", "epoch", "round_key", "map", "round", "shot_type", "target",
    "target_slot", "target_name", "start_iso", "start_epoch", "end_iso",
    "end_epoch", "duration", "reason", "end_reason", "source", "manual",
    "story",
]

EVENT_COLUMNS = [
    "iso_time", "epoch", "round_key", "map", "round", "event", "slot",
    "other_slot", "value", "note",
]

ROUND_COLUMNS = [
    "iso_time", "epoch", "round_key", "map", "round", "completed", "story",
    "phase", "economy_ct", "economy_t", "clutch", "clutch_side", "bomb_site",
    "score_ct", "score_t", "score_diff", "match_point",
    "live_seconds", "kills", "kill_hits", "kill_coverage",
    "first_damages", "prefight_hits", "pre_fight_coverage",
    "round_first_damage_coverage", "corpse_time", "corpse_ratio",
    "cut_count", "cut_rate", "shot_count", "pov_shots",
    "pov_duration_sum", "pov_duration_count", "shot_duration_mean",
    "shot_duration_median", "shot_duration_mean_all", "manual_takeover",
    "manual_confirmed", "round_started_iso",
]

# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


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


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _data_dir() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    root = root / "CS2AIDirector"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except (OSError, OverflowError, ValueError):
        return ""


def _round_key(snap: Any, auto_round: int = 0) -> str:
    map_name = str(getattr(snap, "map_name", "") or "unknown")
    rnd = getattr(snap, "round_num", None)
    if rnd is not None:
        return f"{map_name}#{rnd}"
    # 没有 map.round 时用 freezetime 上升沿自增的序号；不要把 phase 拼进
    # key，否则 freezetime→live 会被误判成新回合。
    return f"{map_name}#auto{auto_round}"


def _fmt_value(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return v


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _covered(shots: list["Shot"], participants: set[int], t: float,
             lookback: float) -> bool:
    """POV 镜头是否在 [t-lookback, t] 内落在任一当事人身上。"""
    lo = t - max(0.0, lookback)
    for s in shots:
        if s.shot_type != SHOT_POV or s.target_slot is None:
            continue
        if s.target_slot not in participants:
            continue
        start = s.started_at
        end = s.ended_at if s.ended_at is not None else t
        # 允许边界相接（切过去的瞬间正好发生击杀/伤害）。
        if end >= lo and start <= t:
            return True
    return False


# --------------------------------------------------------------------------
# 回合故事状态
# --------------------------------------------------------------------------


@dataclass
class StoryState:
    round_key: str = ""
    map_name: str = ""
    round_num: Optional[int] = None
    phase: str = "unknown"
    score_ct: int = 0
    score_t: int = 0
    score_diff: int = 0
    score_leader: str = ""
    match_point: bool = False
    match_point_team: str = ""
    alive_ct: int = 0
    alive_t: int = 0
    alive_diff: int = 0
    bomb_planted: bool = False
    bomb_defusing: bool = False
    bomb_site: str = ""
    bomb_time_left: Optional[float] = None
    round_time_left: Optional[float] = None
    economy_ct: str = "unknown"
    economy_t: str = "unknown"
    economy_ct_money: int = 0
    economy_t_money: int = 0
    economy_estimated: bool = False
    clutch: str = ""
    clutch_side: str = ""
    clutch_players: int = 0
    story: str = ""
    updated_at: float = 0.0
    events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_key": self.round_key,
            "map": self.map_name,
            "round": self.round_num,
            "phase": self.phase,
            "phase_label": PHASE_LABELS.get(self.phase, self.phase),
            "score_ct": self.score_ct,
            "score_t": self.score_t,
            "score_diff": self.score_diff,
            "score_leader": self.score_leader,
            "match_point": self.match_point,
            "match_point_team": self.match_point_team,
            "alive_ct": self.alive_ct,
            "alive_t": self.alive_t,
            "alive_diff": self.alive_diff,
            "bomb_planted": self.bomb_planted,
            "bomb_defusing": self.bomb_defusing,
            "bomb_site": self.bomb_site,
            "bomb_time_left": self.bomb_time_left,
            "round_time_left": self.round_time_left,
            "economy_ct": self.economy_ct,
            "economy_t": self.economy_t,
            "economy_ct_money": self.economy_ct_money,
            "economy_t_money": self.economy_t_money,
            "economy_estimated": self.economy_estimated,
            "clutch": self.clutch,
            "clutch_side": self.clutch_side,
            "clutch_players": self.clutch_players,
            "story": self.story,
            "updated_at": self.updated_at,
        }


# 武器名称关键字 → 经济档位。只看 GSI 的 weapons 列表，不读内存。
_RIFLE_WORDS = (
    "ak47", "m4a1", "m4a4", "m4a1_silencer", "famas", "galilar", "aug",
    "sg556", "ssg08", "awp", "g3sg1", "scar20", "scar-20",
)
_SMG_WORDS = (
    "mac10", "mp9", "mp7", "mp5", "ump45", "p90", "bizon", "nova",
    "xm1014", "mag7", "sawedoff", "negev", "m249",
)


def _weapon_tier(p: Any) -> float:
    names = []
    for w in (getattr(p, "weapons", None) or []):
        if isinstance(w, dict):
            names.append(str(w.get("name") or "").lower())
    active = getattr(p, "active_weapon", None)
    if isinstance(active, dict):
        names.append(str(active.get("name") or "").lower())
    joined = " ".join(names)
    tier = 0.2  # 默认手枪
    if any(w in joined for w in _RIFLE_WORDS):
        tier = 2.0
    elif any(w in joined for w in _SMG_WORDS):
        tier = 1.0
    if int(getattr(p, "armor", 0) or 0) > 0:
        tier += 0.3
    return tier


def _equipment_class(players: list[Any], cfg: dict[str, Any]) -> str:
    """按“最好的枪 + 护甲”判断全起/强起/经济局；无武器数据时返回 unknown。"""
    alive = [p for p in players if int(getattr(p, "health", 0) or 0) > 0]
    if not alive:
        return "unknown"
    scores = [_weapon_tier(p) for p in alive]
    avg = sum(scores) / len(scores)
    try:
        full = max(0.5, float(cfg.get("economy_equip_full", 1.5)))
        force = max(0.1, float(cfg.get("economy_equip_force", 0.8)))
    except (TypeError, ValueError):
        full, force = 1.5, 0.8
    if avg >= full:
        return "full"
    if avg >= force:
        return "force"
    return "eco"


def _money_class(avg_money: float, cfg: dict[str, Any]) -> str:
    try:
        full = max(500.0, float(cfg.get("economy_full_avg", 3500.0)))
        force = max(200.0, float(cfg.get("economy_force_avg", 2000.0)))
    except (TypeError, ValueError):
        full, force = 3500.0, 2000.0
    if avg_money >= full:
        return "full"
    if avg_money >= force:
        return "force"
    return "eco"


def _team_economy(players: list[Any], cfg: dict[str, Any],
                  money_avg: float, prefer_money: bool = False) -> tuple[str, bool]:
    """返回 (档位, 是否估算)。

    - freezetime 用金钱（尽量在买入前采样）；
    - live 用已经买到手的武器/护甲，避免“买完余额低”被误判成 eco。
    """
    if prefer_money and money_avg > 0:
        return _money_class(money_avg, cfg), False
    equip = _equipment_class(players, cfg)
    if equip != "unknown":
        return equip, False
    return _money_class(money_avg, cfg), True


class StoryTracker:
    """把 GSI 采样变成“回合故事状态”。

    纯状态机，不写文件；引擎负责把 state.story / state.to_dict() 放进状态栏，
    并在 state.events 里拿到阶段/经济/下包/残局/比分变化用于日志。
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._state = StoryState()
        self._round_key = ""
        self._round_started_at: Optional[float] = None
        self._last_damage_at = -1e9
        self._economy_locked = False
        self._economy_live_seen = False
        self._round_bomb_site = ""
        self._round_clutch = ""
        self._round_clutch_side = ""
        self._round_clutch_players = 0
        self._auto_round = 0
        self._last_phase = ""

    @property
    def state(self) -> StoryState:
        return self._state

    # ---- 事件输入 ----
    def note_damage(self, now: float) -> None:
        self._last_damage_at = now

    # ---- 主更新 ----
    def update(self, snap: Any, now: float,
               bomb_site: Optional[str] = None) -> StoryState:
        events: list[str] = []
        map_name = str(getattr(snap, "map_name", "") or "unknown")
        rnd = getattr(snap, "round_num", None)
        if rnd is None:
            phase_now = str(getattr(snap, "phase", "") or "").lower()
            if phase_now == "freezetime" and self._last_phase != "freezetime":
                self._auto_round += 1
            elif phase_now == "live" and self._last_phase in (
                    "", "warmup", "over", "gameover"):
                # 没有 map.round 的 GSI：从热身/上一回合直接进入 live 也算新回合
                self._auto_round += 1
        key = _round_key(snap, self._auto_round)
        if key != self._round_key:
            self._round_key = key
            self._round_started_at = now
            self._last_damage_at = -1e9
            self._economy_locked = False
            self._economy_live_seen = False
            self._round_bomb_site = ""
            self._round_clutch = ""
            self._round_clutch_side = ""
            self._round_clutch_players = 0
            events.append("回合开始")

        players = list((getattr(snap, "players", {}) or {}).values())
        alive = [p for p in players
                 if int(getattr(p, "health", 0) or 0) > 0
                 and str(getattr(p, "team", "")).upper() in ("CT", "T")]
        alive_ct = sum(1 for p in alive if str(getattr(p, "team", "")).upper() == "CT")
        alive_t = sum(1 for p in alive if str(getattr(p, "team", "")).upper() == "T")

        ct_players = [p for p in players if str(getattr(p, "team", "")).upper() == "CT"]
        t_players = [p for p in players if str(getattr(p, "team", "")).upper() == "T"]
        money_ct = sum(int(getattr(p, "money", 0) or 0) for p in ct_players)
        money_t = sum(int(getattr(p, "money", 0) or 0) for p in t_players)
        avg_ct = money_ct / max(1, len(ct_players))
        avg_t = money_t / max(1, len(t_players))

        raw_phase = str(getattr(snap, "phase", "") or "").lower()
        economy_ct, economy_t = self._state.economy_ct, self._state.economy_t
        estimated = self._state.economy_estimated
        changed_economy = False
        if raw_phase == "live" and not self._economy_live_seen:
            # 买入完成后用装备重新定档；这是本回合最终口径。
            economy_ct, est_ct = _team_economy(ct_players, self.cfg, avg_ct)
            economy_t, est_t = _team_economy(t_players, self.cfg, avg_t)
            estimated = est_ct or est_t
            self._economy_live_seen = True
            self._economy_locked = True
            changed_economy = True
        elif not self._economy_locked:
            economy_ct, est_ct = _team_economy(
                ct_players, self.cfg, avg_ct, prefer_money=True)
            economy_t, est_t = _team_economy(
                t_players, self.cfg, avg_t, prefer_money=True)
            estimated = est_ct or est_t
            self._economy_locked = True
            changed_economy = True
        if changed_economy and (economy_ct != "unknown" or economy_t != "unknown"):
            events.append(
                f"经济：CT {ECONOMY_LABELS.get(economy_ct, economy_ct)} / "
                f"T {ECONOMY_LABELS.get(economy_t, economy_t)}"
                + ("（估算）" if estimated else "")
            )

        score_ct = int(getattr(snap, "score_ct", 0) or 0)
        score_t = int(getattr(snap, "score_t", 0) or 0)
        score_diff = abs(score_ct - score_t)
        leader = "CT" if score_ct > score_t else ("T" if score_t > score_ct else "")
        try:
            mp_score = max(1, int(self.cfg.get("match_point_score", 12)))
        except (TypeError, ValueError):
            mp_score = 12
        match_point = max(score_ct, score_t) >= mp_score and score_ct != score_t
        match_point_team = leader if match_point else ""

        bomb = getattr(snap, "bomb", None)
        bomb_state = str(getattr(bomb, "state", "") or "") if bomb else ""
        bomb_planted = bomb_state in ("planted", "defusing")
        bomb_defusing = bomb_state == "defusing" or any(
            bool(getattr(p, "defusing", False)) for p in alive
        )
        bomb_time_left = None
        if bomb is not None and getattr(bomb, "countdown", None) is not None:
            bomb_time_left = _f(getattr(bomb, "countdown"))
        site = str(bomb_site or "").upper() if bomb_planted else ""
        if site:
            self._round_bomb_site = site
        elif self._round_bomb_site:
            # 包点记忆：爆炸/拆除后仍保留本回合下包位置，供指标行使用
            site = self._round_bomb_site
        round_time_left = None
        if bomb_planted:
            round_time_left = bomb_time_left
        elif getattr(snap, "phase_ends_in", None) is not None:
            round_time_left = _f(getattr(snap, "phase_ends_in"))

        # 残局：一方只剩 1 人，另一方至少 1 人。
        clutch, clutch_side, clutch_players = "", "", 0
        if alive_ct == 1 and alive_t == 1:
            clutch, clutch_side, clutch_players = "1v1", "", 1
        elif alive_ct == 1 and alive_t >= 2:
            clutch, clutch_side, clutch_players = f"1v{alive_t}", "CT", alive_t
        elif alive_t == 1 and alive_ct >= 2:
            clutch, clutch_side, clutch_players = f"1v{alive_ct}", "T", alive_ct
        if clutch:
            self._round_clutch = clutch
            self._round_clutch_side = clutch_side
            self._round_clutch_players = clutch_players
        elif self._round_clutch:
            # 残局结束后保留“本回合曾是 1vX”，避免回合结束行丢失叙事
            clutch = self._round_clutch
            clutch_side = self._round_clutch_side
            clutch_players = self._round_clutch_players

        if raw_phase not in ("", "live"):
            phase = raw_phase
        elif bomb_planted:
            phase = "post_plant"
        elif clutch:
            phase = "clutch"
        elif now - self._last_damage_at <= self._engage_recent():
            phase = "engage"
        else:
            elapsed = (now - self._round_started_at) if self._round_started_at else 0.0
            phase = "opening" if elapsed <= self._opening_seconds() else "control"
        if raw_phase in ("over", "gameover"):
            phase = "round_over" if raw_phase == "over" else "gameover"

        part = []
        if rnd is not None:
            part.append(f"第{rnd}回合")
        part.append(f"CT {score_ct}:{score_t} T")
        if match_point_team:
            part.append(f"赛点 {match_point_team}")
        if phase in PHASE_LABELS and phase not in ("unknown", "freezetime"):
            part.append(PHASE_LABELS[phase])
        elif phase == "freezetime":
            part.append("冻结")
        if economy_ct != "unknown" or economy_t != "unknown":
            part.append(
                f"经济 CT{ECONOMY_LABELS.get(economy_ct, economy_ct)}/"
                f"T{ECONOMY_LABELS.get(economy_t, economy_t)}"
            )
        if bomb_planted:
            part.append("下包" + (f" {site}" if site else ""))
        if bomb_defusing:
            part.append("拆包中")
        if clutch:
            part.append("残局 " + (f"{clutch_side} " if clutch_side else "") + clutch)
        story = " · ".join(part)

        state = StoryState(
            round_key=key, map_name=map_name, round_num=rnd, phase=phase,
            score_ct=score_ct, score_t=score_t, score_diff=score_diff,
            score_leader=leader, match_point=match_point,
            match_point_team=match_point_team, alive_ct=alive_ct,
            alive_t=alive_t, alive_diff=alive_ct - alive_t,
            bomb_planted=bomb_planted, bomb_defusing=bomb_defusing,
            bomb_site=site, bomb_time_left=bomb_time_left,
            round_time_left=round_time_left, economy_ct=economy_ct,
            economy_t=economy_t, economy_ct_money=money_ct,
            economy_t_money=money_t, economy_estimated=estimated,
            clutch=clutch, clutch_side=clutch_side,
            clutch_players=clutch_players, story=story, updated_at=now,
        )

        prev = self._state
        if prev.round_key == key:
            if prev.phase != phase and phase not in ("unknown",):
                events.append(f"阶段：{PHASE_LABELS.get(phase, phase)}")
            if prev.clutch != clutch and clutch:
                events.append(f"残局：{(clutch_side + ' ') if clutch_side else ''}{clutch}")
            if (not prev.bomb_planted and bomb_planted):
                events.append("下包" + (f"：{site}" if site else ""))
            if bomb_defusing and not prev.bomb_defusing:
                events.append("开始拆包")
            if (prev.score_ct, prev.score_t) != (score_ct, score_t):
                events.append(f"比分：CT {score_ct}:{score_t} T")
            if match_point and not prev.match_point and match_point_team:
                events.append(f"赛点：{match_point_team}")
        state.events = events
        self._state = state
        self._last_phase = raw_phase
        return state

    def _opening_seconds(self) -> float:
        try:
            return max(1.0, float(self.cfg.get("story_opening_seconds", 12.0)))
        except (TypeError, ValueError):
            return 12.0

    def _engage_recent(self) -> float:
        try:
            return max(0.5, float(self.cfg.get("story_engage_recent_seconds", 3.0)))
        except (TypeError, ValueError):
            return 3.0


# --------------------------------------------------------------------------
# 镜头记录
# --------------------------------------------------------------------------


@dataclass
class Shot:
    shot_type: str
    target: str
    target_slot: Optional[int]
    target_name: str
    started_at: float                 # time.monotonic()
    started_wall: float
    reason: str
    source: str
    manual: bool = False
    round_key: str = ""
    map_name: str = ""
    round_num: Optional[int] = None
    story: str = ""
    expires_at: Optional[float] = None
    ended_at: Optional[float] = None
    ended_wall: Optional[float] = None
    end_reason: str = ""

    def duration(self, now: Optional[float] = None) -> float:
        end = self.ended_at if self.ended_at is not None else now
        if end is None:
            return 0.0
        return max(0.0, end - self.started_at)

    def to_dict(self, now: Optional[float] = None) -> dict[str, Any]:
        return {
            "type": self.shot_type,
            "type_label": SHOT_LABELS.get(self.shot_type, self.shot_type),
            "target": self.target,
            "target_slot": self.target_slot,
            "target_name": self.target_name,
            "duration": self.duration(now),
            "reason": self.reason,
            "source": self.source,
            "manual": self.manual,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    def to_row(self) -> dict[str, Any]:
        return {
            "iso_time": _iso(self.started_wall),
            "epoch": f"{self.started_wall:.3f}",
            "round_key": self.round_key,
            "map": self.map_name,
            "round": self.round_num if self.round_num is not None else "",
            "shot_type": self.shot_type,
            "target": self.target,
            "target_slot": self.target_slot if self.target_slot is not None else "",
            "target_name": self.target_name,
            "start_iso": _iso(self.started_wall),
            "start_epoch": f"{self.started_wall:.3f}",
            "end_iso": _iso(self.ended_wall),
            "end_epoch": f"{self.ended_wall:.3f}" if self.ended_wall else "",
            "duration": f"{self.duration():.3f}",
            "reason": self.reason,
            "end_reason": self.end_reason,
            "source": self.source,
            "manual": 1 if self.manual else 0,
            "story": self.story,
        }


class ShotTimeline:
    """线程安全的镜头时间线 + 回合指标聚合。

    引擎每个 tick 调一次 tick(snap, story, now)：
      - 自动识别回合切换、live 时长、observer_slot 变化；
      - 通过 expect_switch / note_manual / begin_cinematic
        接收导演意图，等 GSI 确认后写成一条 Shot；
      - 回合结束时计算指标并落盘。
    """

    def __init__(self, cfg: dict[str, Any], base_dir: Optional[Path] = None,
                 log_fn: Optional[Callable[[str], None]] = None,
                 async_writes: bool = False) -> None:
        self.cfg = cfg
        self._writer = None
        self._async_writes = async_writes
        self.enabled = bool(cfg.get("shot_timeline_enabled", True))
        self._log_fn = log_fn or (lambda _msg: None)
        if base_dir is None:
            custom = str(cfg.get("shot_timeline_dir") or "").strip()
            base_dir = Path(custom) if custom else _data_dir()
        self.base_dir = Path(base_dir)
        self.shot_path = self.base_dir / "shot_timeline.csv"
        self.event_path = self.base_dir / "shot_events.csv"
        self.round_path = self.base_dir / "round_metrics.csv"
        self.summary_path = self.base_dir / "director_metrics.json"

        self._lock = threading.RLock()
        self._write_error_logged = False
        self._shot_seq = 0
        self._recent: deque[Shot] = deque(maxlen=30)

        self._round_key = ""
        self._map_name = ""
        self._round_num: Optional[int] = None
        self._story_text = ""
        self._round_started_at: Optional[float] = None
        self._round_started_wall: Optional[float] = None
        self._round_live_seconds = 0.0
        self._live_started_at: Optional[float] = None
        self._round_live_end: Optional[float] = None
        self._finalized = False
        self._current: Optional[Shot] = None
        self._round_shots: list[Shot] = []
        self._kills: list[dict[str, Any]] = []
        self._damages: list[dict[str, Any]] = []
        self._first_damages: list[dict[str, Any]] = []
        self._deaths: dict[int, float] = {}
        self._manual_events: list[dict[str, Any]] = []
        self._last_damage_at = -1e9
        self._last_tick_at: Optional[float] = None
        self._last_observed_slot: Optional[int] = None
        self._pending_switch: Optional[dict[str, Any]] = None
        self._pending_manual: Optional[dict[str, Any]] = None
        self._last_round_metrics: Optional[dict[str, Any]] = None
        self._session_durations: list[float] = []
        self._history_rounds = 0
        self._last_story: dict[str, Any] = {}

        self._agg: dict[str, float] = {
            "rounds": 0, "completed_rounds": 0, "kills": 0, "kill_hits": 0,
            "first_damages": 0, "prefight_hits": 0, "live_seconds": 0.0,
            "corpse_seconds": 0.0, "cut_count": 0, "shot_count": 0,
            "pov_shots": 0, "pov_duration_sum": 0.0,
            "pov_duration_count": 0, "manual_takeover": 0,
            "manual_confirmed": 0,
        }

        if self.enabled:
            try:
                self.base_dir.mkdir(parents=True, exist_ok=True)
                self._ensure_csv(self.shot_path, SHOT_COLUMNS)
                self._ensure_csv(self.event_path, EVENT_COLUMNS)
                self._ensure_csv(self.round_path, ROUND_COLUMNS)
                self._load_history()
            except Exception as e:
                self._log(f"镜头时间线初始化失败（已禁用落盘）：{type(e).__name__}: {e}")

        if self.enabled and self._async_writes:
            from ordered_writer import OrderedWriter
            self._writer = OrderedWriter()

    def persistence_status(self):
        return self._writer.stats() if self._writer else {'error': 'write_failed' if self._write_error_logged else '', 'buffered': 0}

    def _persist(self, operation):
        if self._writer is None:
            operation()
        elif not self._writer.offer(operation):
            self._write_error_logged = True
            self._log('镜头记录缓冲已满或停止，本条未接收；请查看诊断计数')

    # ---- 基础 ----
    def _log(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            pass

    def _ensure_csv(self, path: Path, columns: list[str]) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=columns).writeheader()

    def _append_csv(self, path: Path, columns: list[str],
                    row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        buffer = io.StringIO(newline='')
        csv.DictWriter(buffer, fieldnames=list(columns)).writerow(dict(row))
        encoded = buffer.getvalue().encode('utf-8')
        offset = None
        def write():
            nonlocal offset
            if offset is None:
                offset = path.stat().st_size
            # Retry from the original row boundary after a partial disk write.
            # Only this ordered writer owns these files during the session.
            with path.open('r+b') as stream:
                stream.seek(offset)
                stream.truncate()
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            self._persist(write)
        except Exception as error:
            self._write_error_logged = True
            self._log(f'镜头记录写入失败：{type(error).__name__}')

    def _write_event(self, event: str, slot: Optional[int] = None,
                     other_slot: Optional[int] = None, value: Any = "",
                     note: str = "", now: Optional[float] = None) -> None:
        if not self.enabled:
            return
        wall = time.time()
        row = {
            "iso_time": _iso(wall),
            "epoch": f"{wall:.3f}",
            "round_key": self._round_key,
            "map": self._map_name,
            "round": self._round_num if self._round_num is not None else "",
            "event": event,
            "slot": slot if slot is not None else "",
            "other_slot": other_slot if other_slot is not None else "",
            "value": _fmt_value(value),
            "note": note,
        }
        self._append_csv(self.event_path, EVENT_COLUMNS, row)

    @property
    def current(self) -> Optional[Shot]:
        with self._lock:
            return self._current

    @property
    def round_key(self) -> str:
        with self._lock:
            return self._round_key

    # ---- 回合生命周期 ----
    def start(self, now: float) -> None:
        if self._writer:
            self._writer.start()
        with self._lock:
            self._current = None
            self._round_key = ""
            self._finalized = False
            self._last_tick_at = None
            self._pending_switch = None
            self._pending_manual = None

    def _start_round(self, snap: Any, now: float, key: str) -> None:
        self._round_key = key
        self._map_name = str(getattr(snap, "map_name", "") or "unknown")
        self._round_num = getattr(snap, "round_num", None)
        self._story_text = ""
        self._round_started_at = now
        self._round_started_wall = time.time()
        self._round_live_seconds = 0.0
        self._live_started_at = None
        self._round_live_end = None
        self._finalized = False
        self._round_shots = []
        self._kills = []
        self._damages = []
        self._first_damages = []
        self._deaths = {}
        self._manual_events = []
        self._last_damage_at = -1e9
        self._pending_switch = None
        self._pending_manual = None
        self._last_observed_slot = getattr(snap, "observer_slot", None)
        self._write_event("round_start", note=key, now=now)

    def _track_live(self, snap: Any, now: float) -> None:
        phase = str(getattr(snap, "phase", "") or "").lower()
        if phase == "live":
            if self._live_started_at is None:
                self._live_started_at = now
        elif self._live_started_at is not None:
            self._round_live_seconds += max(0.0, now - self._live_started_at)
            self._live_started_at = None
            self._round_live_end = now

    # ---- 镜头开关 ----
    def _open(self, shot_type: str, target: str,
              target_slot: Optional[int], target_name: str,
              reason: str, source: str, now: float, manual: bool = False,
              expires_at: Optional[float] = None) -> Shot:
        self._shot_seq += 1
        shot = Shot(
            shot_type=shot_type, target=target, target_slot=target_slot,
            target_name=target_name, started_at=now, started_wall=time.time(),
            reason=reason, source=source, manual=manual,
            round_key=self._round_key, map_name=self._map_name,
            round_num=self._round_num, story=self._story_text,
            expires_at=expires_at,
        )
        self._current = shot
        return shot

    def _close(self, now: float, end_reason: str) -> Optional[Shot]:
        shot = self._current
        if shot is None:
            return None
        shot.ended_at = max(now, shot.started_at)
        shot.ended_wall = time.time()
        shot.end_reason = end_reason
        self._round_shots.append(shot)
        self._recent.append(shot)
        self._current = None
        if self.enabled:
            self._append_csv(self.shot_path, SHOT_COLUMNS, shot.to_row())
        return shot

    def _pov_target(self, slot: int, snap: Any) -> tuple[str, str]:
        name = ""
        for p in (getattr(snap, "players", {}) or {}).values():
            if getattr(p, "slot", None) == slot:
                name = str(getattr(p, "name", "") or "")
                break
        label = f"slot={slot}" + (f" {name}" if name else "")
        return label, name

    def _open_pov(self, slot: int, snap: Any, now: float, reason: str,
                  source: str, manual: bool = False) -> Shot:
        label, name = self._pov_target(slot, snap)
        return self._open(SHOT_POV, label, int(slot), name, reason, source,
                          now, manual=manual)

    def _take_pending(self, slot: int, now: float) -> tuple[str, str, bool]:
        """返回 (reason, source, manual)。"""
        pm = self._pending_manual
        if pm and int(pm["slot"]) == int(slot) and now <= float(pm["expires"]):
            self._pending_manual = None
            self._confirm_manual(slot, now)
            return "人工接管", "manual", True
        ps = self._pending_switch
        if ps and int(ps["slot"]) == int(slot) and now <= float(ps["expires"]):
            self._pending_switch = None
            return str(ps["reason"]), str(ps["source"]), False
        return "观察者视角变化", "observed", False

    def _confirm_manual(self, slot: int, now: float) -> None:
        for ev in reversed(self._manual_events):
            if not ev.get("confirmed") and int(ev.get("target", -1)) == int(slot) \
                    and now - float(ev.get("t", 0.0)) <= 4.0:
                ev["confirmed"] = True
                return

    def _track_observed(self, snap: Any, now: float) -> None:
        observed = getattr(snap, "observer_slot", None)
        if observed is None:
            return
        observed = _i(observed)
        if observed is None:
            return
        self._last_observed_slot = observed
        cur = self._current
        if cur is not None and cur.shot_type in CINEMATIC_TYPES:
            return  # 运镜期间 observer_slot 不代表画面
        if cur is None:
            # freezetime 通常紧接着触发出生点运镜；不要先记一个 0 秒 POV，
            # 否则会虚增 cut_rate、拉低 shot_duration。运镜失败时从 live 再记 POV。
            if str(getattr(snap, "phase", "") or "").lower() == "freezetime":
                return
            reason, source, manual = self._take_pending(observed, now)
            self._open_pov(observed, snap, now, reason, source, manual)
            return
        if cur.shot_type != SHOT_POV:
            return
        if cur.target_slot == observed:
            # GSI 确认已经切到目标：把决策原因补到当前镜头。
            ps = self._pending_switch
            if ps and int(ps["slot"]) == observed and now <= float(ps["expires"]):
                cur.reason = str(ps["reason"])
                cur.source = str(ps["source"])
                self._pending_switch = None
            pm = self._pending_manual
            if pm and int(pm["slot"]) == observed and now <= float(pm["expires"]):
                cur.manual = True
                if "人工接管" not in cur.reason:
                    cur.reason = (cur.reason + "，人工接管").strip("，")
                self._pending_manual = None
                self._confirm_manual(observed, now)
            if not cur.target_name:
                _, cur.target_name = self._pov_target(observed, snap)
            return
        # 真正的画面变化
        reason, source, manual = self._take_pending(observed, now)
        self._close(now, f"视角切换到 slot={observed}")
        self._open_pov(observed, snap, now, reason, source, manual)

    # ---- 对外：导演意图 ----
    def expect_switch(self, slot: int, reason: str, source: str, now: float,
                      window: float = 2.0) -> None:
        with self._lock:
            if not self.enabled:
                return
            slot = _i(slot)
            if slot is None:
                return
            cur = self._current
            if cur is not None and cur.shot_type == SHOT_POV \
                    and cur.target_slot == slot:
                cur.reason = reason or cur.reason
                cur.source = source or cur.source
                return
            self._pending_switch = {
                "slot": slot, "reason": reason, "source": source,
                "at": now, "expires": now + max(0.5, window),
            }

    def begin_cinematic(self, shot_type: str, target: str, reason: str,
                        source: str, now: float,
                        expires_at: Optional[float] = None) -> None:
        with self._lock:
            if not self.enabled:
                return
            if shot_type not in CINEMATIC_TYPES:
                return
            self._pending_switch = None
            self._close(now, f"切换到{SHOT_LABELS.get(shot_type, shot_type)}")
            self._open(shot_type, target, None, "", reason, source, now,
                       expires_at=expires_at)

    def end_cinematic(self, now: float, reason: str = "运镜结束，恢复 POV") -> None:
        with self._lock:
            cur = self._current
            if cur is None or cur.shot_type not in CINEMATIC_TYPES:
                return
            self._close(now, reason)
            if self._last_observed_slot is not None:
                # 用最近一次 GSI 观察 slot 恢复 POV；名字留空，下个 tick 补齐。
                self._open(SHOT_POV, f"slot={self._last_observed_slot}",
                           int(self._last_observed_slot), "", reason,
                           "observed", now)

    def note_manual(self, target_slot: int, from_slot: Optional[int],
                    now: float, pause_seconds: float = 10.0) -> None:
        with self._lock:
            if not self.enabled:
                return
            slot = _i(target_slot)
            if slot is None:
                return
            ev = {"t": now, "target": slot, "from": from_slot, "confirmed": False}
            self._manual_events.append(ev)
            self._write_event("manual_takeover", slot, from_slot, 1,
                              f"暂停 {pause_seconds:.0f}s", now)
            self._pending_manual = {
                "slot": slot, "at": now, "expires": now + 3.0,
            }
            cur = self._current
            if cur is not None and cur.shot_type == SHOT_POV \
                    and cur.target_slot == slot:
                cur.manual = True
                if "人工接管" not in cur.reason:
                    cur.reason = (cur.reason + "，人工接管").strip("，")
                self._pending_manual = None
                ev["confirmed"] = True
            elif cur is not None and cur.shot_type in CINEMATIC_TYPES:
                self._close(now, "人工接管打断运镜")

    # ---- 对外：事件 ----
    def note_damage(self, attackers: list[int], victims: list[int],
                    now: float, amount: int = 0) -> None:
        with self._lock:
            if not self.enabled:
                return
            a = sorted({int(x) for x in attackers if x is not None})
            v = sorted({int(x) for x in victims if x is not None})
            if not a and not v:
                return
            try:
                gap = max(0.5, float(self.cfg.get("metric_engagement_gap", 3.0)))
            except (TypeError, ValueError):
                gap = 3.0
            first = (now - self._last_damage_at) >= gap
            self._last_damage_at = now
            event = {"t": now, "attackers": a, "victims": v,
                     "amount": int(amount or 0), "first": first}
            self._damages.append(event)
            if first:
                self._first_damages.append(event)
                self._write_event(
                    "first_damage", a[0] if a else None,
                    v[0] if v else None, int(amount or 0),
                    f"attackers={a} victims={v}", now)
            else:
                self._write_event(
                    "damage", a[0] if a else None, v[0] if v else None,
                    int(amount or 0), f"attackers={a} victims={v}", now)

    def note_kill(self, killers: list[int], victims: list[int],
                  now: float) -> None:
        with self._lock:
            if not self.enabled:
                return
            ks = [int(x) for x in killers if x is not None]
            vs = [int(x) for x in victims if x is not None]
            if not ks:
                return
            for i, k in enumerate(ks):
                victim = vs[i] if len(vs) == len(ks) else (vs[0] if len(vs) == 1 else None)
                self._kills.append({"t": now, "killer": k, "victim": victim})
                self._write_event("kill", k, victim, 1, "", now)

    def note_death(self, slot: int, now: float) -> None:
        with self._lock:
            if not self.enabled:
                return
            s = _i(slot)
            if s is None or s in self._deaths:
                return
            self._deaths[s] = now
            self._write_event("death", s, None, 1, "", now)

    def note_bomb(self, event: str, site: str = "", now: Optional[float] = None) -> None:
        with self._lock:
            if not self.enabled:
                return
            self._write_event(event, None, None, 1, site, now)

    # ---- 主 tick ----
    def tick(self, snap: Any, story: StoryState, now: float) -> None:
        if not self.enabled:
            return
        key = story.round_key or _round_key(snap)
        with self._lock:
            phase = str(getattr(snap, "phase", "") or "").lower()
            if key != self._round_key:
                # 先结算上一回合（包括它自己的故事状态），再启动新回合，
                # 否则 round_metrics.csv 会把新回合的故事写到上一回合行。
                if self._round_key:
                    self._finalize(
                        self._last_tick_at or now,
                        completed=phase in ("over", "gameover"))
                # CS2 在回合结束阶段就可能把 map.round 改成下一回合。
                # 此时不能启动下一回合，否则会立刻 finalize 出 live=0 的空行，
                # 并且真正进入 freezetime/live 后因 _finalized=True 丢掉整回合。
                if phase in ("over", "gameover"):
                    self._last_tick_at = now
                    return
                self._start_round(snap, now, key)
            self._last_story = story.to_dict()
            self._map_name = str(getattr(snap, "map_name", "") or self._map_name)
            self._round_num = getattr(snap, "round_num", self._round_num)
            self._story_text = story.story
            if self._current is not None:
                self._current.story = self._story_text
            self._track_live(snap, now)
            if str(getattr(snap, "phase", "") or "").lower() == "over":
                if not self._finalized:
                    self._finalize(now, completed=True)
                self._last_tick_at = now
                return
            if self._finalized:
                self._last_tick_at = now
                return
            self._expire_cinematic(now)
            self._track_observed(snap, now)
            self._last_tick_at = now

    def _expire_cinematic(self, now: float) -> None:
        cur = self._current
        if cur is None or cur.shot_type not in CINEMATIC_TYPES:
            return
        if cur.expires_at is not None and now >= cur.expires_at:
            self.end_cinematic(now, "运镜超时，恢复 POV")

    # ---- 指标 ----
    def _compute_metrics(self, end_time: float) -> dict[str, Any]:
        shots = list(self._round_shots)
        try:
            look_kill = max(0.0, float(self.cfg.get("metric_kill_lookback", 2.5)))
            look_fight = max(0.0, float(self.cfg.get("metric_prefight_lookback", 2.5)))
        except (TypeError, ValueError):
            look_kill, look_fight = 2.5, 2.5
        live = max(0.0, self._round_live_seconds)
        round_end = self._round_live_end or end_time

        # kill_coverage
        kill_hits = 0
        kill_total = 0
        for k in self._kills:
            killer = k.get("killer")
            if killer is None:
                continue
            kill_total += 1
            if _covered(shots, {int(killer)}, float(k["t"]), look_kill):
                kill_hits += 1

        # pre_fight_coverage（每段交火首次伤害）
        pref_hits = 0
        for ev in self._first_damages:
            participants = {int(x) for x in ev.get("attackers", [])} | {
                int(x) for x in ev.get("victims", [])}
            if not participants:
                continue
            if _covered(shots, participants, float(ev["t"]), look_fight):
                pref_hits += 1
        pref_total = sum(
            1 for ev in self._first_damages
            if (ev.get("attackers") or ev.get("victims"))
        )

        # round_first_damage_coverage：整回合第一次伤害
        round_first_hit = 0
        round_first_total = 0
        if self._first_damages:
            ev = self._first_damages[0]
            participants = {int(x) for x in ev.get("attackers", [])} | {
                int(x) for x in ev.get("victims", [])}
            if participants:
                round_first_total = 1
                if _covered(shots, participants, float(ev["t"]), look_fight):
                    round_first_hit = 1

        # corpse_time：POV 停在已阵亡选手身上的秒数
        corpse = 0.0
        corpse_shots = 0
        for s in shots:
            if s.shot_type != SHOT_POV or s.target_slot is None:
                continue
            death = self._deaths.get(int(s.target_slot))
            if death is None:
                continue
            start = max(s.started_at, death)
            end = min(s.ended_at if s.ended_at is not None else round_end, round_end)
            if end > start:
                corpse += end - start
                corpse_shots += 1

        # cut_rate / shot_duration
        cut_count = max(0, len(shots) - 1)
        pov_durations = [s.duration() for s in shots if s.shot_type == SHOT_POV]
        all_durations = [s.duration() for s in shots]
        pov_sum = sum(pov_durations)
        pov_n = len(pov_durations)
        try:
            min_live = max(0.0, float(self.cfg.get("metric_min_live_seconds", 5.0)))
        except (TypeError, ValueError):
            min_live = 5.0
        cut_rate = (cut_count / (live / 60.0)) if live >= min_live and live > 0 else None
        corpse_ratio = (corpse / live) if live > 0 else None

        manual = len(self._manual_events)
        manual_confirmed = sum(1 for e in self._manual_events if e.get("confirmed"))

        m: dict[str, Any] = {
            "round_key": self._round_key,
            "map": self._map_name,
            "round": self._round_num,
            "live_seconds": live,
            "kills": kill_total,
            "kill_hits": kill_hits,
            "kill_coverage": (kill_hits / kill_total) if kill_total else None,
            "first_damages": pref_total,
            "prefight_hits": pref_hits,
            "pre_fight_coverage": (pref_hits / pref_total) if pref_total else None,
            "round_first_damage_total": round_first_total,
            "round_first_damage_hits": round_first_hit,
            "round_first_damage_coverage": (
                round_first_hit / round_first_total if round_first_total else None),
            "corpse_time": corpse,
            "corpse_ratio": corpse_ratio,
            "corpse_shots": corpse_shots,
            "cut_count": cut_count,
            "cut_rate": cut_rate,
            "shot_count": len(shots),
            "pov_shots": pov_n,
            "pov_duration_sum": pov_sum,
            "pov_duration_count": pov_n,
            "shot_duration_mean": (pov_sum / pov_n) if pov_n else None,
            "shot_duration_median": (
                statistics.median(pov_durations) if pov_durations else None),
            "shot_duration_mean_all": (
                sum(all_durations) / len(all_durations) if all_durations else None),
            "manual_takeover": manual,
            "manual_confirmed": manual_confirmed,
        }
        return m

    def _finalize(self, now: float, completed: bool) -> None:
        if self._finalized:
            return
        end_time = self._round_live_end or now
        if self._current is not None:
            # 回合切换/结束时不要把镜头拖到下一回合的时间点
            self._close(min(now, end_time), "回合结束" if completed else "回合切换")
        if self._live_started_at is not None:
            self._round_live_seconds += max(0.0, now - self._live_started_at)
            self._live_started_at = None
            if self._round_live_end is None:
                self._round_live_end = now
        if not self._round_key:
            self._finalized = True
            return
        m = self._compute_metrics(end_time)
        m["completed"] = bool(completed)
        m["story"] = self._story_text
        m["round_started_iso"] = _iso(self._round_started_wall)
        self._last_round_metrics = m
        self._update_aggregate(m)
        self._write_round(m)
        self._write_summary()
        self._log(
            "回合指标 | " + self._story_text
            + f" | 击杀覆盖 {self._pct(m['kill_coverage'])}"
            + f" | 交火前覆盖 {self._pct(m['pre_fight_coverage'])}"
            + f" | 镜头均长 {self._secs(m['shot_duration_mean'])}"
            + f" | 切换 {self._rate(m['cut_rate'])}"
            + f" | 尸体 {m['corpse_time']:.1f}s"
            + f" | 人工 {m['manual_takeover']}"
        )
        self._write_event("round_end", note="completed" if completed else "partial",
                          now=now)
        self._finalized = True

    @staticmethod
    def _pct(v: Optional[float]) -> str:
        return "--" if v is None else f"{v * 100:.0f}%"

    @staticmethod
    def _secs(v: Optional[float]) -> str:
        return "--" if v is None else f"{v:.2f}s"

    @staticmethod
    def _rate(v: Optional[float]) -> str:
        return "--" if v is None else f"{v:.1f}/min"

    def _update_aggregate(self, m: dict[str, Any]) -> None:
        a = self._agg
        a["rounds"] += 1
        if m.get("completed"):
            a["completed_rounds"] += 1
        a["kills"] += int(m.get("kills", 0))
        a["kill_hits"] += int(m.get("kill_hits", 0))
        a["first_damages"] += int(m.get("first_damages", 0))
        a["prefight_hits"] += int(m.get("prefight_hits", 0))
        a["live_seconds"] += float(m.get("live_seconds", 0.0))
        a["corpse_seconds"] += float(m.get("corpse_time", 0.0))
        a["cut_count"] += int(m.get("cut_count", 0))
        a["shot_count"] += int(m.get("shot_count", 0))
        a["pov_shots"] += int(m.get("pov_shots", 0))
        a["pov_duration_sum"] += float(m.get("pov_duration_sum", 0.0))
        a["pov_duration_count"] += int(m.get("pov_duration_count", 0))
        a["manual_takeover"] += int(m.get("manual_takeover", 0))
        a["manual_confirmed"] += int(m.get("manual_confirmed", 0))
        for s in self._round_shots:
            if s.shot_type == SHOT_POV:
                self._session_durations.append(s.duration())
        if len(self._session_durations) > 5000:
            self._session_durations = self._session_durations[-5000:]

    def _write_round(self, m: dict[str, Any]) -> None:
        if not self.enabled:
            return
        wall = time.time()
        st = self._last_story
        row = {
            "iso_time": _iso(wall),
            "epoch": f"{wall:.3f}",
            "round_key": m.get("round_key", ""),
            "map": m.get("map", ""),
            "round": m.get("round") if m.get("round") is not None else "",
            "completed": 1 if m.get("completed") else 0,
            "story": m.get("story", ""),
            "phase": st.get("phase", ""),
            "economy_ct": st.get("economy_ct", "unknown"),
            "economy_t": st.get("economy_t", "unknown"),
            "clutch": st.get("clutch", ""),
            "clutch_side": st.get("clutch_side", ""),
            "bomb_site": st.get("bomb_site", ""),
            "score_ct": st.get("score_ct", ""),
            "score_t": st.get("score_t", ""),
            "score_diff": st.get("score_diff", ""),
            "match_point": 1 if st.get("match_point") else 0,
            "live_seconds": f"{float(m.get('live_seconds', 0.0)):.3f}",
            "kills": m.get("kills", 0),
            "kill_hits": m.get("kill_hits", 0),
            "kill_coverage": _fmt_value(m.get("kill_coverage")),
            "first_damages": m.get("first_damages", 0),
            "prefight_hits": m.get("prefight_hits", 0),
            "pre_fight_coverage": _fmt_value(m.get("pre_fight_coverage")),
            "round_first_damage_coverage": _fmt_value(
                m.get("round_first_damage_coverage")),
            "corpse_time": f"{float(m.get('corpse_time', 0.0)):.3f}",
            "corpse_ratio": _fmt_value(m.get("corpse_ratio")),
            "cut_count": m.get("cut_count", 0),
            "cut_rate": _fmt_value(m.get("cut_rate")),
            "shot_count": m.get("shot_count", 0),
            "pov_shots": m.get("pov_shots", 0),
            "pov_duration_sum": f"{float(m.get('pov_duration_sum', 0.0)):.3f}",
            "pov_duration_count": m.get("pov_duration_count", 0),
            "shot_duration_mean": _fmt_value(m.get("shot_duration_mean")),
            "shot_duration_median": _fmt_value(m.get("shot_duration_median")),
            "shot_duration_mean_all": _fmt_value(m.get("shot_duration_mean_all")),
            "manual_takeover": m.get("manual_takeover", 0),
            "manual_confirmed": m.get("manual_confirmed", 0),
            "round_started_iso": m.get("round_started_iso", ""),
        }
        self._append_csv(self.round_path, ROUND_COLUMNS, row)

    def _write_summary(self) -> None:
        if not self.enabled:
            return
        data = {
            "updated_at": _iso(time.time()),
            "session": self.session_metrics(),
            "last_round": self._last_round_metrics,
            "paths": {
                "shot_timeline": str(self.shot_path),
                "shot_events": str(self.event_path),
                "round_metrics": str(self.round_path),
                "summary": str(self.summary_path),
            },
        }
        encoded = json.dumps(data, ensure_ascii=False, indent=2)
        def write():
            tmp = self.summary_path.with_suffix('.json.tmp')
            tmp.write_text(encoded, encoding='utf-8')
            tmp.replace(self.summary_path)
        try:
            self._persist(write)
        except Exception as e:
            if not self._write_error_logged:
                self._write_error_logged = True
                self._log(f"指标汇总写入失败：{type(e).__name__}: {e}")

    def session_metrics(self) -> dict[str, Any]:
        with self._lock:
            a = self._agg
            live = float(a["live_seconds"])
            kills = int(a["kills"])
            first = int(a["first_damages"])
            try:
                min_live = max(0.0, float(self.cfg.get("metric_min_live_seconds", 5.0)))
            except (TypeError, ValueError):
                min_live = 5.0
            pov_n = int(a["pov_duration_count"])
            return {
                "rounds": int(a["rounds"]),
                "completed_rounds": int(a["completed_rounds"]),
                "live_seconds": live,
                "kills": kills,
                "kill_hits": int(a["kill_hits"]),
                "kill_coverage": (a["kill_hits"] / kills) if kills else None,
                "first_damages": first,
                "prefight_hits": int(a["prefight_hits"]),
                "pre_fight_coverage": (a["prefight_hits"] / first) if first else None,
                "corpse_time": float(a["corpse_seconds"]),
                "corpse_ratio": (a["corpse_seconds"] / live) if live > 0 else None,
                "cut_count": int(a["cut_count"]),
                "cut_rate": (
                    a["cut_count"] / (live / 60.0)
                    if live >= min_live and live > 0 else None),
                "shot_count": int(a["shot_count"]),
                "pov_shots": int(a["pov_shots"]),
                "shot_duration_mean": (a["pov_duration_sum"] / pov_n) if pov_n else None,
                "shot_duration_median": (
                    statistics.median(self._session_durations)
                    if self._session_durations else None),
                "manual_takeover": int(a["manual_takeover"]),
                "manual_confirmed": int(a["manual_confirmed"]),
            }

    def _load_history(self) -> None:
        # 历史 CSV 只用于复盘，不再累加进“本次会话”。
        # 旧实现会把前几次启动的回合/人工接管算进当前指标，
        # 导致用户看到 223 次人工接管，而本次实际只有 13 次。
        self._history_rounds = 0
        try:
            if self.round_path.is_file():
                with self.round_path.open("r", newline="", encoding="utf-8-sig") as f:
                    self._history_rounds = max(0, sum(1 for _ in f) - 1)
        except Exception:
            self._history_rounds = 0

    # ---- 生命周期 ----
    def close(self, now: float, reason: str = "导播停止") -> bool:
        if self._writer and not self._writer.accepting:
            self._writer.start()
        with self._lock:
            if not self.enabled:
                return True
            if self._current is not None:
                self._close(now, reason)
            if not self._finalized and self._round_key:
                self._finalize(now, completed=False)
            self._write_summary()
        return self._writer.close() if self._writer else not self._write_error_logged

    def status(self, now: Optional[float] = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        with self._lock:
            cur = self._current.to_dict(now) if self._current is not None else None
            return {
                "enabled": self.enabled,
                "current": cur,
                "session": self.session_metrics(),
                "last_round": self._last_round_metrics,
                "paths": {
                    "shot_timeline": str(self.shot_path),
                    "shot_events": str(self.event_path),
                    "round_metrics": str(self.round_path),
                    "summary": str(self.summary_path),
                },
            }

    def set_story(self, story: StoryState) -> None:
        """保存最近一次故事状态，供回合指标行使用。"""
        with self._lock:
            self._last_story = story.to_dict()
