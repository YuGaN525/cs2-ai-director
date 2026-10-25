#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评估早源 lookahead：把观察器 death 事件与导播的 GSI 时间线对齐。

回答三个问题：
1. 早源真实领先节目画面多少秒（按 GSI 检测到死亡的墙钟计算）；
2. 若按节目时间预切，有多少次击杀本来可以拍到杀手；
3. 哪些死亡无法对齐/缺少身份，避免用错误数据下结论。

只读 CSV/JSONL，不连接游戏、不改变导播。默认读取 %APPDATA%\\CS2AIDirector。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Optional


def default_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    return root / "CS2AIDirector"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _quantile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def read_lookahead_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            envelope = obj if "record" in obj else {"recv": None, "record": obj}
            record = envelope.get("record")
            if not isinstance(record, dict):
                continue
            if record.get("type") != "raw-event" or record.get("name") != "player_death":
                continue
            players = record.get("players") if isinstance(record.get("players"), dict) else {}
            attacker = players.get("attacker") if isinstance(players.get("attacker"), dict) else {}
            userid = players.get("userid") if isinstance(players.get("userid"), dict) else {}
            killer = record.get("actor") or attacker.get("steamId")
            victim = record.get("target") or userid.get("steamId")
            if not killer or not victim:
                continue
            events.append({
                "line": line_no,
                "recv": _f(envelope.get("recv"), 0.0),
                "round": _i(record.get("round")),
                "tick": _i(record.get("eventServerTick")) or _i(record.get("tick"))
                        or _i(record.get("networkTick")) or 0,
                "killer": str(killer),
                "victim": str(victim),
                "killer_slot": _i(attacker.get("slot")),
                "victim_slot": _i(userid.get("slot")),
                "world": record.get("world"),
            })
    events.sort(key=lambda e: (e["recv"], e["tick"]))
    return events


def read_deaths(path: Path) -> list[dict[str, Any]]:
    deaths: list[dict[str, Any]] = []
    seen: set[tuple[Optional[int], Optional[int]]] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if str(row.get("event", "")).strip() != "death":
                continue
            epoch = _f(row.get("epoch"), 0.0)
            slot = _i(row.get("slot"))
            round_no = _i(row.get("round"))
            if epoch <= 0 or slot is None:
                continue
            key = ((round_no, slot) if round_no is not None
                   else (None, slot, round(epoch, 3)))
            if key in seen:
                continue
            seen.add(key)
            deaths.append({"epoch": epoch, "slot": slot, "round": round_no})
    deaths.sort(key=lambda d: d["epoch"])
    return deaths


def read_timeline(path: Path) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    if not path.is_file():
        return shots
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            start = _f(row.get("start_epoch"), 0.0)
            if start <= 0:
                continue
            shots.append({
                "start": start,
                "end": _f(row.get("end_epoch"), 0.0) or None,
                "slot": _i(row.get("target_slot")),
                "shot_type": row.get("shot_type", ""),
                "source": row.get("source", ""),
            })
    shots.sort(key=lambda s: s["start"])
    return shots


def read_slot_map(path: Path) -> tuple[dict[str, int], dict[int, str]]:
    by_steam: dict[str, int] = {}
    by_slot: dict[int, str] = {}
    if not path.is_file():
        return by_steam, by_slot
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = str(row.get("target_steamid", "") or "").strip()
            slot = _i(row.get("target_slot"))
            if not sid or slot is None:
                continue
            by_steam[sid] = slot
            by_slot[slot] = sid
    return by_steam, by_slot


def observed_slot(shots: list[dict[str, Any]], epoch: float) -> tuple[Optional[int], str, str]:
    current = (None, "", "")
    for shot in shots:
        if shot["start"] > epoch:
            break
        end = shot["end"] or float("inf")
        if shot["start"] <= epoch <= end:
            current = (shot["slot"], shot["shot_type"], shot["source"])
    return current


def _pair_rounds(events: list[dict[str, Any]], deaths: list[dict[str, Any]],
                 offset: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    e_by_round: dict[int, list[dict[str, Any]]] = {}
    d_by_round: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        if event["round"] is None:
            continue
        e_by_round.setdefault(event["round"] + offset, []).append(event)
    for death in deaths:
        if death["round"] is None:
            continue
        d_by_round.setdefault(death["round"], []).append(death)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for round_no in sorted(set(e_by_round) & set(d_by_round)):
        early = sorted(e_by_round[round_no], key=lambda e: (e["tick"], e["recv"]))
        late = sorted(d_by_round[round_no], key=lambda d: d["epoch"])
        pairs.extend(zip(early, late))
    return pairs


def evaluate(events: list[dict[str, Any]], deaths: list[dict[str, Any]],
             shots: list[dict[str, Any]], by_steam: dict[str, int],
             min_lead: float, max_lead: float) -> dict[str, Any]:
    warnings: list[str] = []
    if not events:
        return {"ok": False, "error": "没有可用的 player_death 早源事件", "warnings": warnings}
    if not deaths:
        return {"ok": False, "error": "shot_events.csv 没有 death 记录", "warnings": warnings}

    lo = min(e["recv"] for e in events) - 30
    hi = max(e["recv"] for e in events) + max_lead
    deaths_in_window = [d for d in deaths if lo <= d["epoch"] <= hi]
    if not deaths_in_window:
        return {"ok": False, "error": "时间窗内没有对应的 GSI death 记录",
                "warnings": ["检查导播与观察器是否同一场、同一时间"]}
    if len(deaths_in_window) != len(deaths):
        warnings.append(f"过滤掉时间窗外 death：{len(deaths) - len(deaths_in_window)} 条")

    best: list[tuple[dict[str, Any], dict[str, Any]]] = []
    best_offset = 0
    for offset in (-1, 0, 1):
        pairs = _pair_rounds(events, deaths_in_window, offset)
        if len(pairs) > len(best):
            best, best_offset = pairs, offset
    if not best:
        warnings.append("按回合无法配对，回退到整场序号配对")
        best = list(zip(sorted(events, key=lambda e: (e["recv"], e["tick"])),
                        sorted(deaths_in_window, key=lambda d: d["epoch"])))
        best_offset = 0
    elif best_offset:
        warnings.append(f"回合号存在偏移，已按 offset={best_offset:+d} 对齐")

    rows: list[dict[str, Any]] = []
    leads: list[float] = []
    unknown_killer = 0
    skipped_pairs = 0
    for event, death in best:
        lead = death["epoch"] - event["recv"]
        if lead > max_lead or lead < -max_lead:
            skipped_pairs += 1
            continue
        leads.append(lead)
        killer_slot = by_steam.get(event["killer"])
        if killer_slot is None:
            killer_slot = event["killer_slot"]
            unknown_killer += 1
        observed, shot_type, source = observed_slot(shots, death["epoch"])
        killer_observed = (killer_slot is not None and observed is not None
                           and killer_slot == observed)
        potential = (lead >= min_lead and killer_slot is not None
                     and observed is not None and killer_slot != observed)
        rows.append({
            "round": death["round"],
            "gsi_epoch": round(death["epoch"], 3),
            "recv_epoch": round(event["recv"], 3),
            "lead_seconds": round(lead, 3),
            "victim_steamid": event["victim"],
            "victim_slot": death["slot"],
            "killer_steamid": event["killer"],
            "killer_slot": killer_slot,
            "killer_slot_source": "decisions" if event["killer"] in by_steam else "observer",
            "observed_slot": observed,
            "observed_shot": shot_type,
            "observed_source": source,
            "killer_observed": killer_observed,
            "potential_precut": potential,
        })

    positive = [x for x in leads if x > 0]
    median = statistics.median(leads) if leads else None
    if median is not None and median <= 0:
        warnings.append("领先中位数 ≤0：早源没有领先节目画面，或两边不是同一时间线")
    if unknown_killer:
        pct = unknown_killer / len(rows)
        warnings.append(f"{unknown_killer}/{len(rows)}（{pct:.1%}）杀手槽位来自观察器，"
                        "未在 decisions.csv 中找到 GSI 映射")
    if not shots:
        warnings.append("没有 shot_timeline.csv，无法判断死亡时实际在拍谁")

    matched = len(rows)
    killer_covered = sum(1 for r in rows if r["killer_observed"])
    potential = sum(1 for r in rows if r["potential_precut"])
    return {
        "ok": True,
        "summary": {
            "early_deaths": len(events),
            "gsi_deaths_in_window": len(deaths_in_window),
            "matched": matched,
            "unmatched_gsi_deaths": len(deaths_in_window) - matched,
            "skipped_out_of_window_pairs": skipped_pairs,
            "lede_seconds": {
                "median": round(median, 3) if median is not None else None,
                "p10": round(_quantile(leads, 0.1), 3) if leads else None,
                "p90": round(_quantile(leads, 0.9), 3) if leads else None,
                "min": round(min(leads), 3) if leads else None,
                "max": round(max(leads), 3) if leads else None,
                "positive_rate": round(len(positive) / len(leads), 3) if leads else None,
            },
            "coverage": {
                "killer_on_camera_at_death": killer_covered,
                "killer_on_camera_rate": round(killer_covered / matched, 3) if matched else None,
                "potential_precut": potential,
                "potential_precut_rate": round(potential / matched, 3) if matched else None,
            },
            "thresholds": {"min_lead": min_lead, "max_lead": max_lead},
            "round_offset": best_offset,
        },
        "warnings": warnings,
        "rows": rows,
    }


def write_outputs(result: dict[str, Any], out_base: Path) -> tuple[Path, Optional[Path]]:
    json_path = out_base.with_suffix(".json") if out_base.suffix else out_base.with_suffix(".json")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    csv_path = None
    if result.get("ok"):
        csv_path = json_path.with_name(json_path.stem + "_deaths.csv")
        columns = list(result["rows"][0].keys()) if result["rows"] else []
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(result["rows"])
    return json_path, csv_path


def main(argv: Optional[list[str]] = None) -> int:
    base = default_data_dir()
    parser = argparse.ArgumentParser(description="评估早源 lookahead 的领先与覆盖机会")
    parser.add_argument("--events", required=True, help="bridge 输出的 lookahead.jsonl")
    parser.add_argument("--shot-events", default=str(base / "shot_events.csv"))
    parser.add_argument("--shot-timeline", default=str(base / "shot_timeline.csv"))
    parser.add_argument("--decisions", default=str(base / "decisions.csv"))
    parser.add_argument("--out", default="lookahead_eval.json")
    parser.add_argument("--min-lead", type=float, default=0.8,
                        help="低于该领先秒数不计入可预切")
    parser.add_argument("--max-lead", type=float, default=300.0,
                        help="时间窗上限，过滤旧场次死亡")
    args = parser.parse_args(argv)

    events = read_lookahead_events(Path(args.events))
    deaths = read_deaths(Path(args.shot_events))
    shots = read_timeline(Path(args.shot_timeline))
    by_steam, _by_slot = read_slot_map(Path(args.decisions))
    result = evaluate(events, deaths, shots, by_steam,
                      min_lead=args.min_lead, max_lead=args.max_lead)
    json_path, csv_path = write_outputs(result, Path(args.out))
    if not result.get("ok"):
        print(f"评估失败：{result.get('error')}", file=sys.stderr)
        for warning in result.get("warnings", []):
            print(f"  警告：{warning}", file=sys.stderr)
        return 1
    summary = result["summary"]
    print(f"早源 death {summary['early_deaths']}，GSI death "
          f"{summary['gsi_deaths_in_window']}，配对 {summary['matched']}")
    lede = summary["lede_seconds"]
    if lede["median"] is None:
        print("没有可计算领先")
    else:
        print(f"领先中位 {lede['median']}s（p10 {lede['p10']} / p90 {lede['p90']}，"
              f"正领先 {lede['positive_rate']:.0%}）")
    cov = summary["coverage"]
    rate = "--" if cov["killer_on_camera_rate"] is None else f"{cov['killer_on_camera_rate']:.0%}"
    potential_rate = ("--" if cov["potential_precut_rate"] is None
                      else f"{cov['potential_precut_rate']:.0%}")
    print(f"死亡时杀手在拍 {cov['killer_on_camera_at_death']}（{rate}），"
          f"可预切机会 {cov['potential_precut']}（{potential_rate}）")
    for warning in result["warnings"]:
        print(f"警告：{warning}", file=sys.stderr)
    print(f"报告：{json_path}")
    if csv_path:
        print(f"明细：{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
