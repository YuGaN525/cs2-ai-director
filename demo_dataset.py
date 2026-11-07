#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Desktop/CLI adapter for the periodic live Demo parser used by the worker.

Existing decisions.csv files remain readable by train_decision_model.load_dataset.
New imports never choose sample times from future kill events.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from demo_live_dataset import (
    DEFAULT_HORIZON, DEFAULT_STEP, LIVE_COLUMNS, PARSER_VERSION,
    SAMPLING_METHOD, build_live_csv, validate_sampling,
)


def build_demo_csv(
    demos: Iterable[str | Path],
    out_csv: Path,
    tick_offset: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
    *,
    horizon: int = DEFAULT_HORIZON,
    step: int = DEFAULT_STEP,
) -> dict[str, Any]:
    """Build a complete import; preserve existing output on parse errors.

    tick_offset is a deprecated alias for the future label horizon, and no
    longer selects samples by looking backwards from kills.
    """
    from train_from_demos import expand_demo_paths

    emit = log or print
    if tick_offset is not None:
        if horizon != DEFAULT_HORIZON:
            raise ValueError("tick_offset 与 horizon 不能同时指定")
        horizon = tick_offset
        emit("旧 tick_offset 参数仅作为未来标签窗口；采样已改为 live 阶段定时采样。")
    validate_sampling(horizon, step)
    paths = expand_demo_paths([str(p) for p in demos])
    if not paths:
        raise RuntimeError("没有找到 .dem 文件")
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    reports = []
    seen = set()
    duplicates = 0
    with tempfile.TemporaryDirectory(prefix=".demo_import_", dir=out_csv.parent) as directory:
        folder = Path(directory)
        combined = folder / "combined.csv"
        with combined.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=LIVE_COLUMNS)
            writer.writeheader()
            for index, path in enumerate(paths, 1):
                emit(f"[{index}/{len(paths)}] {path}")
                derived = folder / "parsed.csv"
                try:
                    report = build_live_csv(path, derived, horizon=horizon, step=step)
                except Exception as error:
                    raise RuntimeError(f"Demo 解析失败，未替换已有 CSV：{path.name}: {error}") from error
                if report["source_sha256"] in seen:
                    duplicates += 1
                    emit("  重复 Demo 内容，已忽略")
                    continue
                seen.add(report["source_sha256"])
                reports.append(dict(report, source_name=path.name))
                with derived.open("r", newline="", encoding="utf-8") as incoming:
                    writer.writerows(csv.DictReader(incoming))
                emit(f"  live 窗口 {report['sample_windows']}，候选行 {report['rows']}")
        with combined.open("rb") as stream:
            output_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        report_path = out_csv.with_suffix(out_csv.suffix + ".manifest.json")
        stats = {
            "parser": PARSER_VERSION, "sampling_method": SAMPLING_METHOD,
            "demos": len(reports), "duplicate_demos": duplicates,
            **{key: sum(item[key] for item in reports) for key in
               ("rows", "positive", "negative", "sample_windows", "missing_windows")},
            "horizon_ticks": horizon, "step_ticks": step,
            "csv": str(out_csv), "csv_sha256": output_hash,
            "manifest": str(report_path), "sources": reports,
        }
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Readers can verify csv_sha256 after an interruption between replacements.
        os.replace(combined, out_csv)
        os.replace(manifest, report_path)
    emit(f"已生成 {out_csv}：{stats['rows']} 行，正样本 {stats['positive']}；解析清单：{report_path}")
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="从 CS2 Demo 的 live 阶段定时采样生成训练 CSV")
    parser.add_argument("--demos", nargs="+", required=True, help="Demo 文件、目录或通配符")
    parser.add_argument("--out", default="demo_decisions.csv")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--horizon-ticks", type=int, default=DEFAULT_HORIZON,
                        help="未来伤害/击杀标签窗口（默认 192 tick）")
    window.add_argument("--tick-offset", type=int, help="弃用别名：仅设置未来标签窗口，不再按击杀回看")
    parser.add_argument("--step-ticks", type=int, default=DEFAULT_STEP,
                        help="live 阶段定时采样间隔（默认 128 tick）")
    args = parser.parse_args(argv)
    try:
        build_demo_csv(args.demos, Path(args.out), args.tick_offset,
                       horizon=args.horizon_ticks, step=args.step_ticks)
    except Exception as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
