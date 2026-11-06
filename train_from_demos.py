#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 CS2 demo 离线训练“谁更可能拿到击杀”的排序模型。

用法：
    python train_from_demos.py --demos "D:/demos" --out "%APPDATA%/CS2AIDirector/learner.json"

思路：
  - 解析 player_death 事件；
  - 对每个击杀回看 2.5 秒（默认 64 tick × 160）；
  - 用与实时导播完全相同的 8 个特征构造候选；
  - 击杀者标 1、同一时刻其他候选标 0；
  - 训练逻辑回归，把权重写入 learner.json，导播启动时自动加载。

依赖：
    python -m pip install demoparser2

注意：本脚本只做离线训练，不参与游戏运行；导播运行时不需要 demoparser2。
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import math
import os
import random
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

import director_core as d


# demo 里的武器显示名 -> GSI 的 type
WEAPON_TYPES = {
    "AK-47": "Rifle", "M4A1-S": "Rifle", "M4A4": "Rifle", "AUG": "Rifle",
    "SG 553": "Rifle", "FAMAS": "Rifle", "Galil AR": "Rifle",
    "AWP": "SniperRifle", "SSG 08": "SniperRifle",
    "SCAR-20": "SniperRifle", "G3SG1": "SniperRifle",
    "Desert Eagle": "Pistol", "R8 Revolver": "Pistol",
    "Glock-18": "Pistol", "USP-S": "Pistol", "P2000": "Pistol",
    "P250": "Pistol", "Five-SeveN": "Pistol", "Tec-9": "Pistol",
    "CZ75-Auto": "Pistol", "Dual Berettas": "Pistol",
    "MP9": "SMG", "MAC-10": "SMG", "MP7": "SMG", "MP5-SD": "SMG",
    "UMP-45": "SMG", "P90": "SMG", "PP-Bizon": "SMG",
    "Nova": "Shotgun", "XM1014": "Shotgun", "MAG-7": "Shotgun",
    "Sawed-Off": "Shotgun", "M249": "Machine Gun", "Negev": "Machine Gun",
    "Taser": "Pistol", "C4 Explosive": "C4",
}

WEAPON_GSI_NAMES = {
    "AK-47": "weapon_ak47", "M4A1-S": "weapon_m4a1_silencer",
    "M4A4": "weapon_m4a1", "AWP": "weapon_awp", "SSG 08": "weapon_ssg08",
    "Desert Eagle": "weapon_deagle", "R8 Revolver": "weapon_revolver",
    "SCAR-20": "weapon_scar20", "G3SG1": "weapon_g3sg1",
}


def weapon_type(name: Any) -> str:
    text = str(name or "")
    low = text.lower()
    if not text:
        return "Rifle"
    if any(k in low for k in ("knife", "bayonet", "karambit", "talon", "kukri",
                              "stiletto", "ursus", "navaja", "falchion", "bowie",
                              "huntsman", "butterfly", "skeleton", "nomad",
                              "paracord", "survival", "classic", "shadow daggers")):
        return "Knife"
    if any(k in low for k in ("grenade", "flash", "smoke", "molotov", "incendiary",
                              "decoy", "he ")):
        return "Grenade"
    for key, typ in WEAPON_TYPES.items():
        if key.lower() in low:
            return typ
    return "Rifle"


def gsi_weapon_name(name: Any) -> str:
    text = str(name or "")
    if text in WEAPON_GSI_NAMES:
        return WEAPON_GSI_NAMES[text]
    return "weapon_" + text.lower().replace("-", "_").replace(" ", "_")


def forward_from_angles(yaw: Any, pitch: Any) -> tuple[float, float, float]:
    try:
        y = math.radians(float(yaw))
        p = math.radians(float(pitch))
    except (TypeError, ValueError):
        return (1.0, 0.0, 0.0)
    return (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p))


def build_snapshot(rows: list[dict[str, Any]], map_name: str) -> Optional[d.Snapshot]:
    players: dict[str, d.Player] = {}
    slot = 0
    for row in rows:
        try:
            if not bool(row.get("is_alive")):
                continue
            sid = str(row.get("steamid"))
            if not sid or sid == "nan":
                continue
            team_num = int(row.get("team_num") or 0)
            team = "CT" if team_num == 2 else "T" if team_num == 3 else ""
            if not team:
                continue
            slot += 1
            wname = str(row.get("active_weapon_name") or "")
            typ = weapon_type(wname)
            active = {
                "name": gsi_weapon_name(wname), "type": typ,
                "state": "active", "ammo_clip": 30,
            }
            pos = (float(row["X"]), float(row["Y"]), float(row["Z"]))
            players[sid] = d.Player(
                steamid=sid, name=str(row.get("name") or ""), slot=slot, team=team,
                health=int(row.get("health") or 0),
                armor=int(row.get("armor") or 0), helmet=False,
                flashed=0.0, burning=False, defusing=False,
                pos=pos, forward=forward_from_angles(row.get("yaw"), row.get("pitch")),
                weapons=[active], active_weapon=active, money=0,
                round_kills=0, round_totaldmg=0,
            )
        except Exception:
            continue
    if not players:
        return None
    return d.Snapshot(
        seq=0, received_at=0.0, provider_steamid="", map_name=map_name,
        phase="live", round_num=0, players=players, observer_slot=None,
        bomb=None, score_ct=0, score_t=0, round_win_team="",
        phase_ends_in=None, is_spectating=True,
    )


def collect_training_demo_rows(demo_path: Path, cfg: dict[str, Any],
                      tick_offset: int,
                      label_mode: str = "killer"
                      ) -> tuple[list[list[float]], list[int], int,
                                 list[int], list[dict[str, Any]]]:
    """返回 (特征行, 标签, 有效击杀事件数, 每个事件的候选行数, 元数据)。

    label_mode:
      - "killer"：旧 demo 静态模型，只有击杀者=1；
      - "damage"：决策仲裁模型，窗口内造成过伤害的候选=1，
        与 decisions.csv 的 label（伤害或击杀）对齐；
        同时按 hurt 事件还原 damage_recent，避免 enemy_damaged 恒为 0。

    元数据每个事件一项：rule_gap / is_ambiguous，用于和在线 CSV 对齐。
    """
    from demoparser2 import DemoParser

    parser = DemoParser(str(demo_path))
    header = parser.parse_header()
    map_name = str(header.get("map_name") or "unknown")
    deaths = parser.parse_event("player_death")
    if deaths is None or len(deaths) == 0:
        return [], [], 0, [], []

    events: list[tuple[int, str]] = []
    sample_ticks: set[int] = set()
    for _, row in deaths.iterrows():
        attacker = str(row.get("attacker_steamid") or "")
        if not attacker or attacker in ("0", "nan"):
            continue
        try:
            death_tick = int(row.get("tick"))
        except (TypeError, ValueError):
            continue
        tick = death_tick - tick_offset
        if tick <= 0:
            continue
        events.append((tick, attacker))
        sample_ticks.add(tick)
    if not events:
        return [], [], 0, [], []

    hurt_attacker_ticks: dict[str, list[int]] = {}
    hurt_victim_ticks: dict[str, list[int]] = {}
    if label_mode == "damage":
        try:
            hurts = parser.parse_event("player_hurt")
            for _, hrow in hurts.iterrows():
                attacker = str(hrow.get("attacker_steamid") or "")
                victim = str(hrow.get("user_steamid") or "")
                try:
                    htick = int(hrow.get("tick"))
                except (TypeError, ValueError):
                    continue
                if attacker and attacker not in ("0", "nan"):
                    hurt_attacker_ticks.setdefault(attacker, []).append(htick)
                if victim and victim not in ("0", "nan"):
                    hurt_victim_ticks.setdefault(victim, []).append(htick)
            for values in hurt_attacker_ticks.values():
                values.sort()
            for values in hurt_victim_ticks.values():
                values.sort()
        except Exception:
            # 旧版 demoparser2 或 demo 缺 player_hurt：退回击杀者标签
            hurt_attacker_ticks = {}
            hurt_victim_ticks = {}

    fields = ["X", "Y", "Z", "health", "armor", "team_num", "is_alive",
              "active_weapon_name", "yaw", "pitch", "tick", "steamid", "name"]
    ticks = parser.parse_ticks(fields, ticks=sorted(sample_ticks))
    if ticks is None or len(ticks) == 0:
        return [], [], 0, [], []
    by_tick: dict[int, list[dict[str, Any]]] = {}
    for rec in ticks.to_dict("records"):
        by_tick.setdefault(int(rec["tick"]), []).append(rec)

    rows: list[list[float]] = []
    labels: list[int] = []
    groups: list[int] = []
    metas: list[dict[str, Any]] = []
    used = 0
    recent_ticks = max(1, int(round(tick_offset * 1.6 / 2.5)))
    try:
        ambiguity_margin = max(0.0, float(
            cfg.get("decision_model_margin", 0.08)))
    except (TypeError, ValueError):
        ambiguity_margin = 0.08

    def _damage_recent(steamid: Any, at_tick: int) -> bool:
        values = hurt_victim_ticks.get(str(steamid), [])
        if not values:
            return False
        pos = bisect.bisect_left(values, at_tick - recent_ticks)
        return pos < len(values) and values[pos] <= at_tick

    for tick, attacker in events:
        snap = build_snapshot(by_tick.get(tick, []), map_name)
        if snap is None:
            continue
        if label_mode == "damage" and hurt_victim_ticks:
            cands = d.score_candidates(
                snap, cfg, lambda sid, t=tick: _damage_recent(sid, t), 0.0)
        else:
            cands = d.score_candidates(snap, cfg, None, 0.0)
        if not cands:
            continue
        hit = next((c for c in cands if c.steamid == attacker), None)
        if hit is None:
            continue
        used += 1
        groups.append(len(cands))
        scores = sorted((float(c.score) for c in cands), reverse=True)
        gap = (scores[0] - scores[1]) if len(scores) >= 2 else 0.0
        metas.append({
            "map": map_name,
            "source_tick": tick,
            "rule_gap": gap,
            "is_ambiguous": 1 if len(cands) >= 2 and gap <= ambiguity_margin else 0,
        })
        if label_mode == "damage" and hurt_attacker_ticks:
            active: set[str] = set()
            end_tick = tick + max(1, int(tick_offset))
            for c in cands:
                ticks_for_player = hurt_attacker_ticks.get(str(c.steamid), [])
                pos = bisect.bisect_right(ticks_for_player, tick)
                if pos < len(ticks_for_player) and ticks_for_player[pos] <= end_tick:
                    active.add(str(c.steamid))
            active.add(attacker)
        else:
            active = {attacker}
        for c in cands:
            rows.append(d._candidate_features(c))
            labels.append(1 if str(c.steamid) in active else 0)
    return rows, labels, used, groups, metas


def collect_demo_rows(demo_path: Path, cfg: dict[str, Any], tick_offset: int):
    """Legacy static-model API retains its four-value return contract."""
    return collect_training_demo_rows(demo_path, cfg, tick_offset)[:4]


def sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def train(rows: list[list[float]], labels: list[int], epochs: int,
          lr: float, l2: float) -> tuple[list[float], float]:
    n_features = len(rows[0])
    weights = [0.0] * n_features
    bias = 0.0
    order = list(range(len(rows)))
    rng = random.Random(20261021)
    for _epoch in range(max(1, epochs)):
        rng.shuffle(order)
        for i in order:
            x, y = rows[i], float(labels[i])
            p = sigmoid(bias + sum(w * v for w, v in zip(weights, x)))
            g = p - y
            for k in range(n_features):
                weights[k] -= lr * (g * x[k] + l2 * weights[k])
                weights[k] = max(-3.0, min(3.0, weights[k]))
            bias = max(-5.0, min(5.0, bias - lr * g))
    return weights, bias


def top1_accuracy(rows: list[list[float]], labels: list[int],
                  groups: list[int], weights: list[float],
                  bias: float) -> tuple[int, int]:
    """按每个击杀事件一组计算 top-1（每组恰好一个击杀者）。"""
    correct = total = offset = 0
    for size in groups:
        group = rows[offset:offset + size]
        group_labels = labels[offset:offset + size]
        offset += size
        if len(group) < 2 or sum(group_labels) != 1:
            continue
        scores = [sum(w * v for w, v in zip(weights, x)) + bias for x in group]
        pred = scores.index(max(scores))
        total += 1
        if group_labels[pred] == 1:
            correct += 1
    return correct, total


def expand_demo_paths(values: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for value in values:
        p = Path(value)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.dem")))
        elif p.is_file():
            out.append(p)
        else:
            for item in glob.glob(value):
                q = Path(item)
                if q.is_dir():
                    out.extend(sorted(q.rglob("*.dem")))
                elif q.is_file():
                    out.append(q)
    # 去重，保留顺序
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="从 CS2 demo 训练导播排序模型")
    parser.add_argument("--demos", nargs="+", required=True,
                        help="demo 文件、目录或通配符，可多个")
    parser.add_argument("--out", default=str((Path(os.environ.get("APPDATA") or Path.home()) / "CS2AIDirector" / "learner.json")),
                        help="输出 learner.json 路径")
    parser.add_argument("--tick-offset", type=int, default=160,
                        help="从击杀回看多少 tick（64tick 下 160≈2.5s）")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--lr", type=float, default=0.05)
    parser.add_argument("--l2", type=float, default=0.001)
    parser.add_argument("--holdout", type=float, default=0.2,
                        help="留出验证比例（按 demo 文件），0 表示不验证")
    args = parser.parse_args(argv)

    try:
        import demoparser2  # noqa: F401
    except Exception:
        print("缺少 demoparser2，请先执行：python -m pip install demoparser2", file=sys.stderr)
        return 2

    demos = expand_demo_paths(args.demos)
    if not demos:
        print("没有找到 .dem 文件", file=sys.stderr)
        return 2

    cfg = d.DEFAULT_CONFIG
    all_rows: list[list[float]] = []
    all_labels: list[int] = []
    all_groups: list[int] = []
    total_events = 0
    for i, demo in enumerate(demos, 1):
        print(f"[{i}/{len(demos)}] {demo}")
        try:
            rows, labels, events, groups = collect_demo_rows(
                demo, cfg, args.tick_offset)
        except Exception as e:
            print(f"  跳过（{type(e).__name__}: {e}）")
            continue
        print(f"  有效击杀事件 {events}，候选行 {len(rows)}")
        all_rows.extend(rows)
        all_labels.extend(labels)
        all_groups.extend(groups)
        total_events += events

    if len(all_rows) < 20 or total_events < 5:
        print("样本太少，无法训练（至少 5 个击杀事件 / 20 行）", file=sys.stderr)
        return 1

    # 按“击杀事件”留出验证，避免同一事件的行被拆到两边
    holdout = max(0.0, min(0.5, args.holdout))
    split_events = int(len(all_groups) * (1.0 - holdout))
    split_events = max(1, min(len(all_groups) - 1, split_events)) if len(all_groups) > 1 else len(all_groups)
    split_row = sum(all_groups[:split_events])
    train_rows, train_labels = all_rows[:split_row], all_labels[:split_row]
    val_rows = all_rows[split_row:]
    val_labels = all_labels[split_row:]
    val_groups = all_groups[split_events:]

    weights, bias = train(train_rows, train_labels, args.epochs, args.lr, args.l2)

    correct, total = top1_accuracy(val_rows, val_labels, val_groups, weights, bias)
    if total:
        print(f"验证 top-1：{correct}/{total} = {correct/total:.1%}")

    data = {
        "version": 1,
        "features": list(d.DECISION_FEATURES),
        "weights": [round(x, 6) for x in weights],
        "bias": round(bias, 6),
        "samples": len(all_rows),
        # attempts/hits 记录验证集 top-1，便于 GUI 显示离线模型质量
        "attempts": total,
        "hits": correct,
        "source": "demo-training",
    }
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {out}")
    print("权重：" + ", ".join(
        f"{name}={w:+.3f}" for name, w in zip(d.DECISION_FEATURES, weights)
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
