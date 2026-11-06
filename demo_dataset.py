#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 CS2 .dem 生成决策模型训练 CSV。

输出列与导播的 decisions.csv 完全一致；训练器读取后走同一个随机森林流程。
每个击杀回看 tick_offset（默认 160 tick ≈ 2.5s @64tick），
标签与 decisions.csv 对齐：在“回看点之后的同一窗口”内造成过伤害/击杀的候选 label=1；
同时写入 group/group_size，训练器可按事件分组切分并计算 top-1。

用法：
    python demo_dataset.py --demos "D:/demos" --out demo_decisions.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from decision_model import CSV_COLUMNS, DECISION_FEATURES


def build_demo_csv(
    demos: Iterable[str | Path],
    out_csv: Path,
    tick_offset: int = 160,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """解析 demo 并写出可训练 CSV；返回统计。"""
    emit = log or print
    try:
        import director_core as d
        from train_from_demos import collect_training_demo_rows, expand_demo_paths
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"demo 解析依赖不可用：{e}") from e

    paths = expand_demo_paths([str(p) for p in demos])
    if not paths:
        raise RuntimeError("没有找到 .dem 文件")
    cfg = d.DEFAULT_CONFIG
    records: list[tuple[str, int, list[float], int, dict[str, Any]]] = []
    total_events = 0
    for i, demo in enumerate(paths, 1):
        emit(f"[{i}/{len(paths)}] {demo}")
        try:
            rows, labels, events, groups, metas = collect_training_demo_rows(
                demo, cfg, max(1, int(tick_offset)), label_mode="damage")
        except Exception as e:  # noqa: BLE001
            emit(f"  跳过（{type(e).__name__}: {e}）")
            continue
        emit(f"  有效击杀事件 {events}，候选行 {len(rows)}")
        with Path(demo).open("rb") as source:
            source_id = hashlib.file_digest(source, "sha256").hexdigest()
        offset = 0
        for gi, size in enumerate(groups):
            gid = f"{source_id}#{gi}"
            meta = metas[gi] if gi < len(metas) else {}
            for features, label in zip(
                    rows[offset:offset + size], labels[offset:offset + size]):
                records.append((gid, size, features, label, meta))
            offset += size
        total_events += events

    if not records:
        raise RuntimeError("没有生成任何训练样本")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS + ["source_id", "source_tick"])
        writer.writeheader()
        for gid, size, features, label, meta in records:
            row = {name: "" for name in CSV_COLUMNS}
            row.update({
                "action": "demo",
                "source_id": gid.split("#", 1)[0],
                "source_tick": meta.get("source_tick", ""),
                "map": meta.get("map", "unknown"),
                "target_slot": 1,
                "label": int(label),
                "label_damage": int(label),
                "label_kill": "",
                "group": gid,
                "group_size": size,
                "is_ambiguous": int(meta.get("is_ambiguous", 0)),
                "rule_gap": f"{float(meta.get('rule_gap', 0.0)):.6f}",
            })
            for name, value in zip(DECISION_FEATURES, features):
                row[name] = f"{float(value):.6f}"
            writer.writerow(row)
    stats = {
        "demos": len(paths),
        "events": total_events,
        "rows": len(records),
        "positive": int(sum(r[3] for r in records)),
        "csv": str(out_csv),
    }
    emit(f"已生成 {out_csv}：{stats['rows']} 行，"
         f"正样本 {stats['positive']}，击杀事件 {stats['events']}")
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="从 CS2 demo 生成训练 CSV")
    parser.add_argument("--demos", nargs="+", required=True,
                        help="demo 文件、目录或通配符")
    parser.add_argument("--out", default="demo_decisions.csv")
    parser.add_argument("--tick-offset", type=int, default=160)
    args = parser.parse_args(argv)
    try:
        build_demo_csv(args.demos, Path(args.out), args.tick_offset)
    except Exception as e:  # noqa: BLE001
        print(f"失败：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
