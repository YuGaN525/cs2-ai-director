"""Pure v1 snapshot, features and legacy rules; no UI, IO or process clock."""
from __future__ import annotations
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

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
    round_kills: int
    round_totaldmg: int


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
    round_win_team: str          # "CT"/"T"/""，用于判断上一回合胜方
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


def slot_to_key(slot: Any) -> Optional[str]:
    """GSI observer_slot → 观察者数字键。

    实测 CS2：按数字键 N 会选中 GSI slot (N-1) mod 10
    （键 1→slot 0，键 2→slot 1，键 9→slot 8，键 0→slot 9）。
    所以反推 slot S 对应键 (S+1) mod 10；不能直接把 slot 当按键。
    """
    s = _i(slot)
    if s is None or not (0 <= s <= 9):
        return None
    return str((s + 1) % 10)


def key_to_slot(key: Any) -> Optional[int]:
    """观察者数字键 → GSI observer_slot（slot_to_key 的逆映射）。"""
    k = _i(key)
    if k is None or not (0 <= k <= 9):
        return None
    return (k - 1) % 10


def format_slot(slot: Any) -> str:
    """日志/GUI 用：同时显示 GSI slot 和实际要按的数字键。"""
    s = _i(slot)
    if s is None:
        return "-"
    key = slot_to_key(s)
    return f"slot={s}（键{key}）" if key is not None else f"slot={s}"


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
                round_kills=_i(state.get("round_kills"), 0) or 0,
                round_totaldmg=_i(state.get("round_totaldmg"), 0) or 0,
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

    # map.phase 负责 gameover/intermission/warmup/paused；
    # round/phase_countdowns 负责 freezetime/live/over。
    # 之前优先取 countdowns.phase，比赛结束时它仍可能是 "over"，
    # 会把 map.phase="gameover" 吞掉，导致 game_over 永远不触发。
    map_phase = str(mapinfo.get("phase") or "").lower()
    round_phase = str(roundinfo.get("phase") or "").lower()
    countdown_phase = str(countdowns.get("phase") or "").lower()
    if map_phase == "freeze_time":
        map_phase = "freezetime"
    if round_phase == "freeze_time":
        round_phase = "freezetime"
    if countdown_phase == "freeze_time":
        countdown_phase = "freezetime"
    if map_phase == "pause":
        map_phase = "paused"
    if map_phase in ("gameover", "intermission", "warmup", "paused"):
        phase = map_phase
    else:
        phase = countdown_phase or round_phase or map_phase

    team_ct = mapinfo.get("team_ct") or {}
    team_t = mapinfo.get("team_t") or {}
    win_team = str(roundinfo.get("win_team") or "").upper()
    if win_team not in ("CT", "T"):
        win_team = ""

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
        round_win_team=win_team,
        phase_ends_in=(
            _f(countdowns.get("phase_ends_in"))
            if countdowns.get("phase_ends_in") is not None
            else None
        ),
        is_spectating=is_spectating,
    )


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


def _vertical_limit(cfg: dict[str, Any], map_name: str) -> float:
    """不同楼层的最大高度差；Nuke 上下层结构复杂，放宽。"""
    if str(map_name or "").split("/")[-1].lower() == "de_nuke":
        return 900.0
    try:
        return max(80.0, float(cfg.get("max_vertical_diff", 300.0)))
    except (TypeError, ValueError):
        return 300.0


def has_possible_engagement(snap: Snapshot, cfg: dict[str, Any]) -> bool:
    """是否还存在“可能交火”的存活敌我对；用于区分 POV 与包点运镜。

    距离用 3D，楼层差超过阈值视为不可能交火（例如 CT 在 2 楼、T 在 1 楼）。
    """
    alive = [p for p in snap.players.values()
             if p.health > 0 and p.team in ("CT", "T") and p.pos]
    if len(alive) < 2:
        return False
    vlimit = _vertical_limit(cfg, snap.map_name)
    try:
        max_range = max(2600.0, float(cfg.get("engage_range", 1500.0)) * 1.8)
    except (TypeError, ValueError):
        max_range = 2600.0
    for i, a in enumerate(alive):
        for b in alive[i + 1:]:
            if a.team == b.team:
                continue
            d = _dist(a.pos, b.pos)
            if d is None or d > max_range:
                continue
            if abs(a.pos[2] - b.pos[2]) > vlimit:
                continue
            return True
    return False


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
    vlimit = _vertical_limit(cfg, snap.map_name)

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
            if abs(a.pos[2] - b.pos[2]) > vlimit:
                continue  # 不同楼层/高度差过大，视为不可能交火
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
            recent_a = bool(damage_recent and damage_recent(a.steamid))
            recent_b = bool(damage_recent and damage_recent(b.steamid))
            if recent_a:
                urgency += 0.10       # A 也在挨打：正在对枪/换血
            if recent_b:
                urgency += 0.12       # B 刚掉血：交火已经在发生
            if bomb_planted and b.defusing:
                urgency += 0.25       # 拆包者是最可能被击杀的目标

            win = _win_probability(a, b, aim_a, aim_b, prof_a, prof_b)
            # 更偏向“胜率更高的一方”：0.25 + 0.75*win
            score = prox * awareness * urgency * (0.25 + 0.75 * win) * ready_a

            cand = Candidate(
                slot=int(a.slot), steamid=a.steamid, name=a.name or f"slot{a.slot}",
                team=a.team, score=score, win=win,
                enemy_slot=b.slot, enemy_name=b.name or (f"slot{b.slot}" if b.slot is not None else "?"),
                distance=d, aim_self=aim_a, aim_enemy=aim_b,
                hp=a.health, armor=a.armor, enemy_hp=b.health,
                weapon=_short_weapon(prof_a.name), enemy_weapon=_short_weapon(prof_b.name),
                enemy_damaged=recent_b,
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


def detect_bomb_site(map_name: str, bomb_pos: Any,
                     cfg: dict[str, Any]) -> Optional[str]:
    """按地图 + 炸弹世界坐标判定 A/B 包点；无法判定返回 None。

    Nuke 的 A/B 上下重叠，必须用 3D 距离；其他地图用 x/y，
    避免雷达相机高度差影响判定。
    """
    if not bomb_pos or len(bomb_pos) < 2:
        return None
    name = str(map_name or "").split("/")[-1].lower()
    refs = cfg.get("bomb_site_refs") or {}
    ref = refs.get(name)
    if not isinstance(ref, dict):
        return None
    a = ref.get("A")
    b = ref.get("B")
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
        return None
    if len(a) < 2 or len(b) < 2:
        return None
    dims = 3 if name == "de_nuke" else 2
    try:
        da = sum((float(bomb_pos[i]) - float(a[i])) ** 2 for i in range(dims))
        db = sum((float(bomb_pos[i]) - float(b[i])) ** 2 for i in range(dims))
    except (TypeError, ValueError, IndexError):
        return None
    if abs(da - db) < 1e-9:
        return None
    return "A" if da < db else "B"


def format_candidate(c: Candidate) -> str:
    # 注意：不要在每行里重复 slot/昵称，调用方已经在“切换视角”前缀里打印过。
    return (
        f"分={c.score:.2f} 距敌={c.distance:.0f} "
        f"瞄准={c.aim_self:.2f}/{c.aim_enemy:.2f} HP={c.hp}/{c.enemy_hp} "
        f"胜率={c.win:.2f} 武器={c.weapon} 对手={c.enemy_name}"
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

    try:
        hard_floor = max(0.2, float(cfg.get("hard_min_switch_interval", 0.2)))
    except (TypeError, ValueError):
        hard_floor = 0.2
    if last_switch_at is not None and (now - last_switch_at) < hard_floor:
        return Decision(None, "切换硬间隔不足 200ms")

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



def _candidate_features(c: Candidate) -> list[float]:
    return [
        c.aim_self,
        c.aim_enemy,
        c.win,
        max(0.0, min(1.0, (100 - c.enemy_hp) / 100.0)),
        max(0.0, min(1.0, (100 - c.hp) / 100.0)),
        max(0.0, min(1.0, 1.0 - c.distance / 1500.0)),
        1.0 if c.enemy_damaged else 0.0,
        1.0 if c.hp >= c.enemy_hp else 0.0,
    ]
