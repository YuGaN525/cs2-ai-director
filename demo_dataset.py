#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 CS2 .dem 生成决策模型训练 CSV。

输出列与导播的 decisions.csv 完全一致；训练器读取后走同一个随机森林流程。
每个击杀回看 tick_offset（默认 160 tick ≈ 2.5s @64tick），
击杀者 label=1，其余同刻候选 label=0。

用法：
    python demo_dataset.py --demos "D:/demos" --out demo_decisions.csv
"""

from __future__ import annotations

import argparse
import csv
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
        import cs2_ai_director as d
        from train_from_demos import collect_demo_rows, expand_demo_paths
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"demo 解析依赖不可用：{e}") from e

    paths = expand_demo_paths([str(p) for p in demos])
    if not paths:
        raise RuntimeError("没有找到 .dem 文件")
    cfg = d.DEFAULT_CONFIG
    all_rows: list[list[float]] = []
    all_labels: list[int] = []
    total_events = 0
    for i, demo in enumerate(paths, 1):
        emit(f"[{i}/{len(paths)}] {demo}")
        try:
            rows, labels, events, _groups = collect_demo_rows(
                demo, cfg, max(1, int(tick_offset)))
        except Exception as e:  # noqa: BLE001
            emit(f"  跳过（{type(e).__name__}: {e}）")
            continue
        emit(f"  有效击杀事件 {events}，候选行 {len(rows)}")
        all_rows.extend(rows)
        all_labels.extend(labels)
        total_events += events

    if not all_rows:
        raise RuntimeError("没有生成任何训练样本")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for features, label in zip(all_rows, all_labels):
            row = {name: "" for name in CSV_COLUMNS}
            row.update({
                "action": "demo",
                "target_slot": 1,
                "label": int(label),
                "label_damage": "",
                "label_kill": 1 if label else 0,
            })
            for name, value in zip(DECISION_FEATURES, features):
                row[name] = f"{float(value):.6f}"
            writer.writerow(row)
    stats = {
        "demos": len(paths),
        "events": total_events,
        "rows": len(all_rows),
        "positive": int(sum(all_labels)),
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
