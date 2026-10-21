#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线概率校准：把随机森林的原始概率映射成可用的绝对概率。

只拟合 2 个参数（Platt scaling），不修改森林权重，不依赖 sklearn。
训练器/导播共用；校准结果写入 decision_calibration.json。
"""

from __future__ import annotations

import csv
import json
import math
import random
import time
from pathlib import Path
from typing import Any, Optional

from decision_model import DECISION_FEATURES, ModelArbiter, decision_csv_path, decision_model_path

CALIBRATION_VERSION = 1


def sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def logit(p: float) -> float:
    p = max(1e-6, min(1.0 - 1e-6, float(p)))
    return math.log(p / (1.0 - p))


def apply_platt(p: float, a: float, b: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, sigmoid(a * logit(p) + b)))


def _logloss(y: list[int], p: list[float]) -> float:
    total = 0.0
    for yi, pi in zip(y, p):
        pi = max(1e-6, min(1.0 - 1e-6, pi))
        total -= yi * math.log(pi) + (1 - yi) * math.log(1.0 - pi)
    return total / max(1, len(y))


def _brier(y: list[int], p: list[float]) -> float:
    return sum((pi - yi) ** 2 for yi, pi in zip(y, p)) / max(1, len(y))


def _auc(y: list[int], p: list[float]) -> float:
    """带并列处理的 ROC AUC（成对比较，避免排序 rank 的边界错误）。"""
    pos = [pi for yi, pi in zip(y, p) if yi == 1]
    neg = [pi for yi, pi in zip(y, p) if yi == 0]
    if not pos or not neg:
        return 0.5
    wins = 0.0
    for a in pos:
        for b in neg:
            if a > b:
                wins += 1.0
            elif a == b:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def fit_platt(y: list[int], p: list[float]) -> tuple[float, float]:
    """Newton 法拟合 logloss；失败时退回 identity。"""
    if not y or len(set(y)) < 2:
        return 1.0, 0.0
    a, b = 1.0, 0.0
    for _ in range(60):
        ga = gb = 0.0
        haa = hab = hbb = 0.0
        for yi, pi in zip(y, p):
            x = logit(pi)
            q = sigmoid(a * x + b)
            d = q - yi
            w = max(1e-6, q * (1.0 - q))
            ga += d * x
            gb += d
            haa += w * x * x
            hab += w * x
            hbb += w
        # 很小的 L2，防止完全分离时参数爆炸
        ga += 1e-6 * a
        hbb += 1e-6
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a -= max(-1.0, min(1.0, da))
        b -= max(-1.0, min(1.0, db))
        if abs(da) + abs(db) < 1e-8:
            break
    if not (math.isfinite(a) and math.isfinite(b)):
        return 1.0, 0.0
    return float(a), float(b)


def load_labelled_rows(csv_path: Path) -> tuple[list[list[float]], list[int], list[str]]:
    xs: list[list[float]] = []
    ys: list[int] = []
    groups: list[str] = []
    with Path(csv_path).open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, 1):
            label = str(row.get("label", "")).strip()
            if label not in ("0", "1"):
                continue
            try:
                x = [float(row[name]) for name in DECISION_FEATURES]
            except (KeyError, TypeError, ValueError):
                continue
            group = str(row.get("group", "") or "").strip()
            if not group:
                group = f"{row.get('map', '')}|{row.get('round', '')}"
            xs.append(x)
            ys.append(int(label))
            groups.append(group or f"row{idx}")
    return xs, ys, groups


def split_groups(groups: list[str], test_size: float = 0.3,
                 seed: int = 42) -> tuple[list[int], list[int], str]:
    """按 group 切分；保证两边都有正负样本，否则退回随机行切分。"""
    unique = sorted(set(groups))
    if len(unique) >= 4:
        rng = random.Random(seed)
        rng.shuffle(unique)
        n_test = max(1, min(len(unique) - 1, int(round(len(unique) * test_size))))
        test_groups = set(unique[:n_test])
        train = [i for i, g in enumerate(groups) if g not in test_groups]
        test = [i for i, g in enumerate(groups) if g in test_groups]
        return train, test, "group"
    idx = list(range(len(groups)))
    random.Random(seed).shuffle(idx)
    n_test = max(1, int(round(len(idx) * test_size)))
    return idx[n_test:], idx[:n_test], "random"


def load_calibration(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(data.get("version", 0)) != CALIBRATION_VERSION:
            return None
        a = float(data["a"])
        b = float(data["b"])
        if not (math.isfinite(a) and math.isfinite(b)):
            return None
        return data
    except Exception:
        return None


def save_calibration(path: Path, a: float, b: float,
                     meta: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": CALIBRATION_VERSION,
        "a": round(float(a), 8),
        "b": round(float(b), 8),
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **meta,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def calibrate_csv(csv_path: Path, model_path: Path, out_path: Path,
                  min_samples: int = 500, test_size: float = 0.3,
                  cfg: Optional[dict[str, Any]] = None,
                  min_improvement: float = 0.01) -> dict[str, Any]:
    """从线上 decisions.csv 拟合 Platt 校准；不达标不写文件。"""
    cfg = cfg or {"decision_model_enabled": True}
    xs, ys, groups = load_labelled_rows(csv_path)
    if len(xs) < min_samples:
        return {"ok": False, "reason": f"样本不足 {len(xs)} < {min_samples}"}
    if len(set(ys)) < 2:
        return {"ok": False, "reason": "只有一个类别"}
    arb = ModelArbiter(cfg, path=model_path)
    if not arb.loaded:
        return {"ok": False, "reason": f"模型未加载：{arb.error or model_path}"}
    raw = [arb.probability_from_features(x) for x in xs]
    train_idx, test_idx, split = split_groups(groups, test_size)
    y_train = [ys[i] for i in train_idx]
    p_train = [raw[i] for i in train_idx]
    y_test = [ys[i] for i in test_idx]
    p_test = [raw[i] for i in test_idx]
    if len(set(y_train)) < 2 or len(set(y_test)) < 2:
        return {"ok": False, "reason": "切分后某一侧只有一个类别"}
    a, b = fit_platt(y_train, p_train)
    before = _brier(y_test, p_test)
    after = _brier(y_test, [apply_platt(p, a, b) for p in p_test])
    ll_before = _logloss(y_test, p_test)
    ll_after = _logloss(y_test, [apply_platt(p, a, b) for p in p_test])
    auc = _auc(y_test, p_test)
    improved = (after <= before * (1.0 - min_improvement)
                and ll_after <= ll_before * (1.0 - min_improvement)
                and a > 0.05)
    meta = {
        "samples": len(xs), "train": len(train_idx), "test": len(test_idx),
        "positive": sum(ys), "split": split,
        "brier_before": round(before, 6), "brier_after": round(after, 6),
        "logloss_before": round(ll_before, 6), "logloss_after": round(ll_after, 6),
        "auc": round(auc, 6), "source": str(csv_path),
        "model": str(model_path),
        "model_mtime": (Path(model_path).stat().st_mtime
                        if Path(model_path).is_file() else 0.0),
    }
    if not improved:
        return {"ok": False, "reason": "校准未改善验证集", **meta, "a": a, "b": b}
    save_calibration(out_path, a, b, meta)
    return {"ok": True, "a": a, "b": b, **meta, "path": str(out_path)}


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="用线上 decisions.csv 校准仲裁概率")
    parser.add_argument("--csv", default=str(decision_csv_path()))
    parser.add_argument("--model", default=str(decision_model_path()))
    parser.add_argument("--out", default=str(Path(decision_model_path()).with_name("decision_calibration.json")))
    parser.add_argument("--min-samples", type=int, default=500)
    parser.add_argument("--test-size", type=float, default=0.3)
    args = parser.parse_args(argv)
    result = calibrate_csv(Path(args.csv), Path(args.model), Path(args.out),
                           min_samples=args.min_samples, test_size=args.test_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
