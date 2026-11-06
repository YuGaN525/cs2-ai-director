#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用导播记录的 decisions.csv 训练随机森林仲裁模型。

这个文件同时是命令行工具和 trainer_gui.py 的训练后端：
    python train_decision_model.py
    python train_decision_model.py --csv decisions.csv --min-samples 1000

要求至少 1000 条已标注样本；输出 AUC、分类报告和特征重要性。
只用 scikit-learn，不引入深度学习框架。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from decision_model import (
    DECISION_FEATURES,
    decision_csv_path,
    decision_metrics_path,
    decision_model_path,
    export_forest,
)


from training_splits import SplitError as TrainingError, SplitPlan, make_plan, validate_plan


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def load_dataset(
    csv_path: Path,
    weight_boost: Optional[dict[str, Any]] = None,
) -> tuple[
        list[list[float]], list[int], list[str], list[float], list[int],
        dict[str, int], list[float]]:
    """读取 CSV，并返回特征、标签、分组、样本权重、组大小、统计、时间戳。

    旧版 decisions.csv 没有 group/rule_gap/is_ambiguous 列，会自动回退到
    map|round 分组，权重为 1。weight_boost 可调整模糊/switch 样本权重：
    {"ambiguous": 2.0, "switch": 0.25, "gap": 0.75}。
    """
    xs: list[list[float]] = []
    ys: list[int] = []
    groups: list[str] = []
    weights: list[float] = []
    group_sizes: list[int] = []
    times: list[float] = []
    stats = {"rows": 0, "skipped": 0, "positive": 0, "negative": 0,
             "ambiguous": 0, "switches": 0, "missing_maps": 0}
    boost = weight_boost or {}
    ambiguous_boost = _clamp_float(boost.get("ambiguous"), 0.0, 5.0, 2.0)
    switch_boost = _clamp_float(boost.get("switch"), 0.0, 1.5, 0.25)
    gap_cap = _clamp_float(boost.get("gap"), 0.0, 1.5, 0.75)
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
            if not all(math.isfinite(v) for v in features):
                stats["skipped"] += 1
                continue
            y = int(label)
            map_name = str(row.get("map", "") or "").strip()
            if not map_name or map_name == "unknown":
                stats["missing_maps"] += 1
            round_no = str(row.get("round", "") or "").strip()
            group = str(row.get("group", "") or "").strip()
            if not group:
                group = f"{map_name}|{round_no}" if (map_name or round_no)                     else "__unknown__"
            source = str(row.get("match_source_id") or row.get("source_id") or row.get("session_id") or "").strip()
            if source:
                group = source + "|event:" + group
            elif str(row.get("action")) == "demo" and "#" in group:
                group = group.split("#", 1)[0] + "|event:" + group
            try:
                size = max(1, int(float(row.get("group_size", 1) or 1)))
            except (TypeError, ValueError):
                size = 1
            try:
                ambiguous = str(row.get("is_ambiguous", "0")).strip() == "1"
            except Exception:
                ambiguous = False
            try:
                gap = float(row.get("rule_gap", "") or 0.0)
            except (TypeError, ValueError):
                gap = 0.0
            # 模型只在“规则前两名接近”时被调用；对这部分样本加权。
            weight = 1.0
            if ambiguous:
                weight += ambiguous_boost
                stats["ambiguous"] += 1
            elif gap > 0:
                # 分差越接近，越接近实际仲裁场景；但不要完全忽略明显样本。
                weight += min(gap_cap, gap_cap / (1.0 + gap * 10.0))
            if str(row.get("action", "")).strip() == "switch":
                weight += switch_boost
                stats["switches"] += 1
            xs.append(features)
            ys.append(y)
            groups.append(group)
            weights.append(weight)
            group_sizes.append(size)
            try:
                stamp = float(row.get("timestamp", "") or 0.0)
                times.append(stamp if math.isfinite(stamp) else 0.0)
            except (TypeError, ValueError):
                times.append(0.0)
            stats["positive" if y else "negative"] += 1
    return xs, ys, groups, weights, group_sizes, stats, times


def _uniq(values: list[Any]) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def normalize_search_space(
    space: Optional[dict[str, Any]],
    trees: int = 300,
    max_depth: int = 8,
) -> dict[str, list[Any]]:
    """把 AI/用户给出的搜索空间收敛到安全可训练范围。"""
    default: dict[str, list[Any]] = {
        "n_estimators": [max(120, min(300, int(trees)))],
        "max_depth": _uniq([max(2, int(max_depth) - 2), max(2, int(max_depth)), None]),
        "min_samples_leaf": [3, 5, 10],
        "max_features": ["sqrt", 0.5],
        "class_weight": ["balanced"],
    }
    if not isinstance(space, dict) or not space:
        return default
    resolved = dict(default)

    def as_list(key: str, limit: int = 5) -> list[Any]:
        raw = space.get(key)
        if not isinstance(raw, (list, tuple)):
            return []
        return list(raw)[:limit]

    values = [
        max(50, min(800, int(_clamp_float(v, 50, 800, default["n_estimators"][0]))))
        for v in as_list("n_estimators") if isinstance(v, (int, float))
    ]
    if values:
        resolved["n_estimators"] = _uniq(values)

    values = []
    for v in as_list("max_depth"):
        if v is None:
            values.append(None)
        elif isinstance(v, (int, float)):
            values.append(max(2, min(24, int(v))))
    if values:
        resolved["max_depth"] = _uniq(values)

    values = [
        max(1, min(50, int(_clamp_float(v, 1, 50, 5))))
        for v in as_list("min_samples_leaf") if isinstance(v, (int, float))
    ]
    if values:
        resolved["min_samples_leaf"] = _uniq(values)

    values = []
    for v in as_list("max_features"):
        if isinstance(v, str) and v in ("sqrt", "log2"):
            values.append(v)
        elif isinstance(v, (int, float)) and 0.05 <= float(v) <= 1.0:
            values.append(round(float(v), 3))
    if values:
        resolved["max_features"] = _uniq(values)

    values = []
    for v in as_list("class_weight"):
        if v is None or v in ("balanced", "balanced_subsample"):
            values.append(v)
    if values:
        resolved["class_weight"] = _uniq(values)
    return resolved


def model_nodes(model: Any) -> int:
    try:
        return int(sum(int(est.tree_.node_count) for est in model.estimators_))
    except Exception:
        return 0


def group_top1_accuracy(xs: Any, ys: Any, proba: Any, groups: list[str],
                        group_sizes: list[int]) -> tuple[Optional[float], int]:
    """按事件组计算 top-1：组内概率最高且 label=1 记为命中。

    只有 group_size > 1（demo 的同一击杀候选组）才参与；decisions.csv
    每行只有一个目标，不参与 top-1。
    """
    buckets: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        buckets.setdefault(g, []).append(i)
    correct = total = 0
    for idxs in buckets.values():
        if len(idxs) < 2:
            continue
        if max((group_sizes[i] for i in idxs), default=1) < 2:
            continue
        if sum(int(ys[i]) for i in idxs) <= 0:
            continue
        best = max(idxs, key=lambda i: float(proba[i]))
        total += 1
        if int(ys[best]) == 1:
            correct += 1
    if total == 0:
        return None, 0
    return correct / total, total


def train_from_csv(
    csv_path: Path,
    out_path: Path,
    metrics_path: Path,
    min_samples: int = 1000,
    test_size: float = 0.25,
    trees: int = 300,
    max_depth: int = 8,
    log: Optional[Callable[[str], None]] = None,
    search_space: Optional[dict[str, Any]] = None,
    n_iter: int = 8,
    weight_boost: Optional[dict[str, Any]] = None,
    split_mode: str = "auto",
    split_plan: Optional[SplitPlan] = None,
    evaluate_final: bool = True,
) -> dict[str, Any]:
    """训练并写出 pkl + 纯 JSON 森林 + metrics；失败抛 TrainingError。

    search_space / n_iter / weight_boost / split_mode 供 AI 辅助训练覆盖默认行为；
    默认按完整组隔离并保留最终测试集；无法安全切分时明确拒绝。
    """
    emit = log or print
    try:
        import joblib
        import numpy as np
        from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
        from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score
        from sklearn.model_selection import (
            RandomizedSearchCV,
        )
    except Exception as e:  # noqa: BLE001
        raise TrainingError(
            f"缺少 scikit-learn/joblib：{e}\n"
            f"请执行：python -m pip install -r requirements-train.txt"
        ) from e

    if not csv_path.is_file():
        raise TrainingError(f"找不到决策日志：{csv_path}\n先让导播运行并积累样本。")
    xs, ys, groups, weights, group_sizes, stats, times = load_dataset(
        csv_path, weight_boost)
    emit(f"读取 {csv_path}")
    emit(f"总行数 {stats['rows']}，有效 {len(ys)}，跳过 {stats['skipped']}，"
         f"正样本 {stats['positive']} / 负样本 {stats['negative']}，"
         f"模糊仲裁样本 {stats['ambiguous']}，switch 样本 {stats['switches']}")
    if len(ys) < min_samples:
        raise TrainingError(
            f"样本不足：{len(ys)} < {min_samples}，继续积累后再训练。"
        )
    if len(set(ys)) < 2:
        raise TrainingError("只有一个类别，无法训练二分类模型。")

    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=int)
    w = np.asarray(weights, dtype=float)
    g = np.asarray(groups, dtype=object)
    test_size = max(0.1, min(0.5, float(test_size)))
    isolation = [v.split("|event:", 1)[0] for v in groups]
    plan = split_plan or make_plan(xs, ys, isolation, times, split_mode, test_size)
    validate_plan(plan, xs, ys, isolation, times)
    split_mode = plan.mode
    train_idx, test_idx = list(plan.train), list(plan.validation)
    x_train, x_test = x[train_idx], x[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]
    w_train = w[train_idx]
    g_test = [groups[i] for i in test_idx]
    sizes_test = [group_sizes[i] for i in test_idx]
    emit(f"切分：{split_mode}，训练 {len(y_train)} / 验证 {len(y_test)}，"
         f"验证组 {len(set(g_test))}")
    # 在 RF / ExtraTrees 之间做小规模随机搜索；只用开源 scikit-learn，
    # 不引入深度学习。最终导出仍是同一套纯 JSON 树森林。
    # search_space / n_iter 可由 AI 顾问建议，但会先被 normalize_search_space 收敛。
    param_dist = normalize_search_space(search_space, trees, max_depth)
    import os
    if os.environ.get("DIRECTOR_CLOUD_BUDGET")=="1":
        param_dist["n_estimators"]=[min(int(trees),int(v)) for v in param_dist["n_estimators"]]
        param_dist["max_depth"]=[int(max_depth)]
    combinations = 1
    for values in param_dist.values():
        combinations *= max(1, len(values))
    iterations = int(_clamp_float(n_iter, 4, 24, 8))
    iterations = max(1, min(iterations, combinations))
    emit(f"搜索空间：{param_dist}（n_iter={iterations}）")
    cv = [(list(a), list(b)) for a, b in plan.cv]
    candidates: list[tuple[str, Any]] = []
    comparison: dict[str, dict[str, Any]] = {}
    for name, cls in (
        ("RandomForest", RandomForestClassifier),
        ("ExtraTrees", ExtraTreesClassifier),
    ):
        emit(f"交叉验证 {name} …")
        # 单进程：PyInstaller onefile 下 joblib 多进程容易卡住/无法回收。
        search = RandomizedSearchCV(
            cls(n_jobs=1, random_state=42),
            param_distributions=param_dist,
            n_iter=iterations, scoring="roc_auc", cv=cv, n_jobs=1,
            random_state=42, refit=True, error_score="raise",
        )
        search.fit(x_train, y_train, sample_weight=w_train)
        best = search.best_estimator_
        test_proba = best.predict_proba(x_test)[:, 1]
        test_auc = float(roc_auc_score(y_test, test_proba))
        top1, top1_groups = group_top1_accuracy(
            x_test, y_test, test_proba, g_test, sizes_test)
        comparison[name] = {
            "cv_auc": float(search.best_score_),
            "test_auc": test_auc,
            "top1": top1,
            "top1_groups": top1_groups,
            "nodes": model_nodes(best),
            "params": {k: str(v) for k, v in search.best_params_.items()},
        }
        emit(f"  {name}: CV AUC={search.best_score_:.4f}, "
             f"验证 AUC={test_auc:.4f}, "
             f"top1={'-' if top1 is None else f'{top1:.1%}'} "
             f"({top1_groups} 组), 参数={search.best_params_}")
        candidates.append((name, best))
    # 指标接近时优先选节点更少的模型：避免为了 0.2% AUC 把 JSON
    # 从几 MB 涨到几十 MB，拖慢导播启动/推理。
    best_cv = max(comparison[name]["cv_auc"] for name, _ in candidates)
    eligible = [
        (name, model) for name, model in candidates
        if comparison[name]["cv_auc"] >= best_cv - 0.005
    ]
    if not eligible:
        eligible = candidates
    algorithm, model = min(
        eligible,
        key=lambda item: (comparison[item[0]]["nodes"],
                          -comparison[item[0]]["cv_auc"]))
    emit(f"选择 {algorithm}（指标接近时优先小模型，nodes="
         f"{comparison[algorithm]['nodes']}）")
    proba = model.predict_proba(x_test)[:, 1]
    auc = float(roc_auc_score(y_test, proba))
    top1, top1_groups = group_top1_accuracy(
        x_test, y_test, proba, g_test, sizes_test)
    pred = model.predict(x_test)
    cm = confusion_matrix(y_test, pred).tolist()
    report = classification_report(y_test, pred, digits=3, zero_division=0)
    emit(f"验证集 AUC：{auc:.4f}")
    emit(f"验证集 top-1：{'-' if top1 is None else f'{top1:.1%}'}"
         f"（{top1_groups} 个候选组）")
    emit("混淆矩阵：")
    for row in cm:
        emit("  " + str(row))
    emit(report)

    importance = sorted(
        zip(DECISION_FEATURES, model.feature_importances_),
        key=lambda item: item[1], reverse=True,
    )
    emit("特征重要性：")
    for name, value in importance:
        emit(f"  {name:<16} {value:.4f}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out_path)
    # 推理优先使用纯JSON，不反序列化网络来源的pkl。
    json_out = (out_path.with_suffix(".json.gz") if out_path.suffix == ".pkl"
                else out_path.with_name(out_path.stem + ".json.gz"))
    export_forest(model, json_out)
    metrics = {
        "samples": len(ys),
        "positive": stats["positive"],
        "negative": stats["negative"],
        "auc": auc,
        "top1": top1,
        "top1_groups": top1_groups,
        "split": split_mode,
        "split_seed": 42,
        "split_hash": plan.split_hash,
        "dataset_hash": plan.dataset_hash,
        "partitions": {"train": len(plan.train), "validation": len(plan.validation), "final_test": len(plan.final_test)},
        "diagnostics": {"missing_timestamps": sum(t <= 0 for t in times), "unknown_groups": sum(g == "__unknown__" for g in isolation), "missing_maps": stats["missing_maps"]},
        "search_space": {k: [str(v) for v in vals]
                         for k, vals in param_dist.items()},
        "n_iter": iterations,
        "weight_boost": {
            "ambiguous": _clamp_float((weight_boost or {}).get("ambiguous"),
                                      0.0, 5.0, 2.0),
            "switch": _clamp_float((weight_boost or {}).get("switch"),
                                   0.0, 1.5, 0.25),
            "gap": _clamp_float((weight_boost or {}).get("gap"),
                                0.0, 1.5, 0.75),
        },
        "ambiguous": stats["ambiguous"],
        "switches": stats["switches"],
        "algorithm": algorithm,
        "comparison": comparison,
        "features": DECISION_FEATURES,
        "importance": {name: float(v) for name, v in importance},
        "confusion": cm,
        "report": report,
        "csv": str(csv_path),
        "model": str(out_path),
        "model_json": str(json_out),
        "model_nodes": model_nodes(model),
        "model_json_bytes": int(json_out.stat().st_size),
    }
    if evaluate_final:
        final_idx = list(plan.final_test)
        final_p = model.predict_proba(x[final_idx])[:, 1]
        final_top, final_groups = group_top1_accuracy(x[final_idx], y[final_idx], final_p,
            [groups[i] for i in final_idx], [group_sizes[i] for i in final_idx])
        metrics["final_test"] = {"auc": float(roc_auc_score(y[final_idx], final_p)),
                                 "top1": final_top, "groups": final_groups, "samples": len(final_idx),
                                 "selection_used": False}
        try:
            from model_submission import write_submission
            submission=write_submission(json_out,x[final_idx],y[final_idx],plan.split_hash)
            emit(f"候选发布评估文件：{submission}（仍需后台审核与发布）")
        except (ValueError,OSError) as error:
            emit(f"模型已保存，发布评估文件未生成：{error}")
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metrics_path.with_suffix(".split.json").write_text(json.dumps(plan.manifest(), indent=2), encoding="utf-8")
    emit(f"模型已保存：{out_path}")
    emit(f"纯 Python 森林已保存：{json_out}")
    emit(f"指标已保存：{metrics_path}")
    return metrics


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="训练导播决策仲裁模型")
    parser.add_argument("--csv", default=str(decision_csv_path()))
    parser.add_argument("--out", default=str(decision_model_path()))
    parser.add_argument("--metrics", default=str(decision_metrics_path()))
    parser.add_argument("--split-plan", type=Path, help="复用已冻结的metrics.split.json")
    parser.add_argument("--split-mode", choices=["auto", "group", "time"], default="auto")
    parser.add_argument("--min-samples", type=int, default=1000)
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--trees", type=int, default=300)
    parser.add_argument("--max-depth", type=int, default=8)
    args = parser.parse_args(argv)

    try:
        train_from_csv(
            Path(args.csv), Path(args.out), Path(args.metrics),
            min_samples=args.min_samples, test_size=args.test_size,
            trees=args.trees, max_depth=args.max_depth, split_mode=args.split_mode,
            split_plan=(SplitPlan.from_manifest(json.loads(args.split_plan.read_text(encoding="utf-8"))) if args.split_plan else None),
        )
    except TrainingError as e:
        print(e, file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"训练失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
