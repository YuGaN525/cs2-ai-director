#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用导播记录的 decisions.csv 训练随机森林仲裁模型。

要求至少 1000 条已标注样本；输出 AUC、分类报告和特征重要性。
只用 scikit-learn，不引入深度学习框架。

用法：
    python -m pip install -r requirements-train.txt
    python train_decision_model.py
    python train_decision_model.py --csv decisions.csv --min-samples 1000
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional

from decision_model import (
    DECISION_FEATURES,
    decision_csv_path,
    decision_metrics_path,
    decision_model_path,
    export_forest,
)


def load_dataset(csv_path: Path) -> tuple[list[list[float]], list[int], dict[str, int]]:
    xs: list[list[float]] = []
    ys: list[int] = []
    stats = {"rows": 0, "skipped": 0, "positive": 0, "negative": 0}
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["rows"] += 1
            label = str(row.get("label", "")).strip()
            if label not in ("0", "1"):
                stats["skipped"] += 1
                continue
            try:
                features = [float(row[name]) for name in DECISION_FEATURES]
            except (KeyError, TypeError, ValueError):
                stats["skipped"] += 1
                continue
            xs.append(features)
            y = int(label)
            ys.append(y)
            stats["positive" if y else "negative"] += 1
    return xs, ys, stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="训练导播决策仲裁模型")
    parser.add_argument("--csv", default=str(decision_csv_path()))
    parser.add_argument("--out", default=str(decision_model_path()))
    parser.add_argument("--metrics", default=str(decision_metrics_path()))
    parser.add_argument("--min-samples", type=int, default=1000)
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--trees", type=int, default=300)
    parser.add_argument("--max-depth", type=int, default=8)
    args = parser.parse_args(argv)

    try:
        import joblib
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score
        from sklearn.model_selection import train_test_split
    except Exception as e:
        print(f"缺少 scikit-learn/joblib：{e}\n"
              f"请执行：python -m pip install -r requirements-train.txt", file=sys.stderr)
        return 2

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"找不到决策日志：{csv_path}\n先让导播运行并积累样本。", file=sys.stderr)
        return 1
    xs, ys, stats = load_dataset(csv_path)
    print(f"读取 {csv_path}")
    print(f"总行数 {stats['rows']}，有效 {len(ys)}，跳过 {stats['skipped']}，"
          f"正样本 {stats['positive']} / 负样本 {stats['negative']}")
    if len(ys) < args.min_samples:
        print(f"样本不足：{len(ys)} < {args.min_samples}，继续积累后再训练。", file=sys.stderr)
        return 1
    if len(set(ys)) < 2:
        print("只有一个类别，无法训练二分类模型。", file=sys.stderr)
        return 1

    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=int)
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=args.test_size, random_state=42, stratify=y
    )
    model = RandomForestClassifier(
        n_estimators=max(50, args.trees),
        max_depth=max(2, args.max_depth),
        min_samples_leaf=5,
        class_weight="balanced",
        n_jobs=-1,
        random_state=42,
    )
    model.fit(x_train, y_train)
    proba = model.predict_proba(x_test)[:, 1]
    auc = float(roc_auc_score(y_test, proba))
    pred = model.predict(x_test)
    print(f"\n验证集 AUC：{auc:.4f}")
    print("混淆矩阵：")
    print(confusion_matrix(y_test, pred))
    print(classification_report(y_test, pred, digits=3, zero_division=0))

    importance = sorted(
        zip(DECISION_FEATURES, model.feature_importances_),
        key=lambda item: item[1], reverse=True,
    )
    print("特征重要性：")
    for name, value in importance:
        print(f"  {name:<16} {value:.4f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out)
    # exe 里不打包 scikit-learn，导播优先读这个纯 JSON 森林
    json_out = (out.with_suffix(".json.gz") if out.suffix == ".pkl"
                else out.with_name(out.stem + ".json.gz"))
    export_forest(model, json_out)
    metrics = {
        "samples": len(ys),
        "positive": stats["positive"],
        "negative": stats["negative"],
        "auc": auc,
        "features": DECISION_FEATURES,
        "importance": {name: float(v) for name, v in importance},
        "csv": str(csv_path),
        "model": str(out),
        "model_json": str(json_out),
    }
    metrics_path = Path(args.metrics)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n模型已保存：{out}")
    print(f"纯 Python 森林已保存：{json_out}")
    print(f"指标已保存：{args.metrics}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
