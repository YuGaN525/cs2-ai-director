#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策日志 + 轻量模型仲裁。

- 每次真正的切换/保持决策，把规则特征和目标 slot 写入 CSV；
- 3 秒后按目标选手的 round_totaldmg / round_kills 增量回填 label；
- train_decision_model.py 用 scikit-learn 随机森林训练；
- 导播在规则前两名分差很小时加载模型重新排序。

运行时不依赖 sklearn；只有 ModelArbiter.load() 才导入 joblib。
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

DECISION_FEATURES = [
    "aim_self", "aim_enemy", "win", "enemy_low_hp",
    "self_low_hp", "proximity", "enemy_damaged", "hp_advantage",
]

CSV_COLUMNS = [
    "timestamp", "iso_time", "map", "round", "action", "target_slot",
    "target_name", "target_steamid", "current_slot", "reason", "rule_score",
    "candidate_count", "rule_gap", "is_ambiguous", "group", "group_size",
    "distance", "target_hp", "enemy_hp",
    *DECISION_FEATURES, "label", "label_damage", "label_kill",
]


def _data_dir() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    root = root / "CS2AIDirector"
    root.mkdir(parents=True, exist_ok=True)
    return root


def decision_csv_path() -> Path:
    return _data_dir() / "decisions.csv"


def decision_model_path() -> Path:
    return _data_dir() / "decision_model.pkl"


def decision_model_json_path() -> Path:
    """纯 Python 可加载的随机森林导出；exe 不需要 scikit-learn。"""
    return _data_dir() / "decision_model.json.gz"


def decision_metrics_path() -> Path:
    return _data_dir() / "decision_model_metrics.json"


def candidate_features(c: Any) -> list[float]:
    return [
        float(getattr(c, "aim_self", 0.0)),
        float(getattr(c, "aim_enemy", 0.0)),
        float(getattr(c, "win", 0.0)),
        max(0.0, min(1.0, (100 - int(getattr(c, "enemy_hp", 0))) / 100.0)),
        max(0.0, min(1.0, (100 - int(getattr(c, "hp", 0))) / 100.0)),
        max(0.0, min(1.0, 1.0 - float(getattr(c, "distance", 0.0)) / 1500.0)),
        1.0 if getattr(c, "enemy_damaged", False) else 0.0,
        1.0 if int(getattr(c, "hp", 0)) >= int(getattr(c, "enemy_hp", 0)) else 0.0,
    ]


class DecisionLogger:
    """只在内存里保留 3 秒的待标注样本；标注完成后再追加 CSV。"""

    def __init__(self, cfg: dict[str, Any], path: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.path = path or decision_csv_path()
        self._lock = threading.Lock()
        self.pending: list[dict[str, Any]] = []
        self.rows = 0
        self.positive = 0
        self._last_log_at = 0.0
        self._last_reason = ""
        self._ensure_header()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("decision_log_enabled", True))

    @property
    def window(self) -> float:
        try:
            return max(0.5, min(15.0, float(self.cfg.get("decision_label_window", 3.0))))
        except (TypeError, ValueError):
            return 3.0

    def _ensure_header(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.is_file() or self.path.stat().st_size == 0:
                with self.path.open("w", newline="", encoding="utf-8-sig") as f:
                    csv.DictWriter(f, fieldnames=CSV_COLUMNS).writeheader()
                return
            with self.path.open("r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                header = list(reader.fieldnames or [])
                old_rows = list(reader) if any(
                    name not in header for name in CSV_COLUMNS) else None
            if old_rows is not None:
                # v1.10.7: 旧 CSV 没有 rule_gap/is_ambiguous/group 列。
                # 保留旧数据并补空列，避免追加写错位。
                tmp = self.path.with_name(self.path.name + ".tmp")
                with tmp.open("w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                    writer.writeheader()
                    for row in old_rows:
                        writer.writerow({name: row.get(name, "") for name in CSV_COLUMNS})
                tmp.replace(self.path)
                self.rows = len(old_rows)
            else:
                with self.path.open("r", newline="", encoding="utf-8-sig") as f:
                    self.rows = max(0, sum(1 for _ in f) - 1)
        except Exception:
            pass

    def _should_log(self, action: str, reason: str, now: float) -> bool:
        if action == "switch":
            return True
        try:
            interval = max(0.2, float(self.cfg.get("decision_log_interval", 1.0)))
        except (TypeError, ValueError):
            interval = 1.0
        return (now - self._last_log_at >= interval) or (reason != self._last_reason)

    def record(self, snap: Any, cands: list[Any], target_slot: Optional[int],
               action: str, reason: str, current_slot: Optional[int],
               now: float) -> None:
        """action: switch / hold。hold 时优先记录当前观察目标。"""
        if not self.enabled or not cands:
            return
        if not self._should_log(action, reason, now):
            return
        by_slot = {int(c.slot): c for c in cands if getattr(c, "slot", None) is not None}
        target = by_slot.get(int(target_slot)) if target_slot is not None else None
        if target is None and action == "hold" and current_slot is not None:
            target = by_slot.get(int(current_slot))
        if target is None:
            target = cands[0]
        if target is None or target.slot is None:
            return

        player = next(
            (p for p in snap.players.values()
             if getattr(p, "slot", None) == target.slot), None
        )
        if player is None:
            return
        feats = candidate_features(target)
        scores = sorted(
            (float(getattr(c, "score", 0.0)) for c in cands if c is not None),
            reverse=True)
        rule_gap = (scores[0] - scores[1]) if len(scores) >= 2 else 0.0
        try:
            margin = max(0.0, float(
                self.cfg.get("decision_model_margin", 0.08)))
        except (TypeError, ValueError):
            margin = 0.08
        is_ambiguous = 1 if len(cands) >= 2 and rule_gap <= margin else 0
        map_name = str(getattr(snap, "map_name", "") or "")
        round_num = getattr(snap, "round_num", None)
        group = f"{map_name}|{round_num}" if round_num is not None else map_name
        row = {
            "timestamp": f"{time.time():.3f}",
            "iso_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "map": map_name,
            "round": round_num or "",
            "action": action,
            "target_slot": target.slot,
            "target_name": getattr(target, "name", ""),
            "target_steamid": getattr(target, "steamid", ""),
            "current_slot": current_slot if current_slot is not None else "",
            "reason": reason,
            "rule_score": f"{float(getattr(target, 'score', 0.0)):.6f}",
            "candidate_count": len(cands),
            "rule_gap": f"{rule_gap:.6f}",
            "is_ambiguous": is_ambiguous,
            "group": group,
            "group_size": 1,
            "distance": f"{float(getattr(target, 'distance', 0.0)):.3f}",
            "target_hp": int(getattr(target, "hp", 0)),
            "enemy_hp": int(getattr(target, "enemy_hp", 0)),
            "label": "",
            "label_damage": "",
            "label_kill": "",
        }
        for name, value in zip(DECISION_FEATURES, feats):
            row[name] = f"{value:.6f}"

        baseline = {
            "steamid": str(getattr(player, "steamid", "")),
            "totaldmg": int(getattr(player, "round_totaldmg", 0) or 0),
            "kills": int(getattr(player, "round_kills", 0) or 0),
            "round": getattr(snap, "round_num", None),
        }
        with self._lock:
            self.pending.append({
                "row": row,
                "due": now + self.window,
                "baseline": baseline,
            })
            self._last_log_at = now
            self._last_reason = reason

    def poll(self, snap: Any, now: float) -> None:
        if not self.enabled:
            return
        ready: list[dict[str, Any]] = []
        with self._lock:
            keep = []
            for item in self.pending:
                base = item["baseline"]
                if base["round"] != getattr(snap, "round_num", None):
                    continue  # 回合切换后 round_totaldmg 已重置，丢弃
                if now >= float(item["due"]):
                    ready.append(item)
                else:
                    keep.append(item)
            self.pending = keep
        for item in ready:
            self._finalize(snap, item)

    def _finalize(self, snap: Any, item: dict[str, Any]) -> None:
        base = item["baseline"]
        player = next(
            (p for p in snap.players.values()
             if str(getattr(p, "steamid", "")) == base["steamid"]), None
        )
        if player is None:
            return
        dmg = max(0, int(getattr(player, "round_totaldmg", 0) or 0) - base["totaldmg"])
        kills = max(0, int(getattr(player, "round_kills", 0) or 0) - base["kills"])
        label = 1 if (dmg > 0 or kills > 0) else 0
        row = dict(item["row"])
        row["label"] = label
        row["label_damage"] = dmg
        row["label_kill"] = kills
        try:
            with self._lock, self.path.open("a", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=CSV_COLUMNS).writerow(row)
            self.rows += 1
            if label:
                self.positive += 1
        except Exception:
            pass

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rows": self.rows,
            "positive": self.positive,
            "pending": len(self.pending),
            "csv": str(self.path),
        }


def export_forest(model: Any, path: Path) -> None:
    """把 sklearn RandomForest 导出为纯 Python 可读的 gzip JSON。"""
    trees = []
    for est in model.estimators_:
        t = est.tree_
        trees.append({
            "left": t.children_left.tolist(),
            "right": t.children_right.tolist(),
            "feature": t.feature.tolist(),
            "threshold": t.threshold.tolist(),
            "value": t.value[:, 0, :].tolist(),
        })
    data = {
        "n_features": int(model.n_features_in_),
        "classes": [int(c) for c in model.classes_],
        "trees": trees,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    tmp.replace(path)


class ModelArbiter:
    """加载随机森林；优先纯 JSON，只有旧 pkl 才需要 sklearn/joblib。"""

    def __init__(self, cfg: dict[str, Any], path: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.path = path or decision_model_path()
        self.json_path = (self.path.with_suffix(".json.gz")
                          if self.path.suffix == ".pkl"
                          else self.path.with_name(self.path.stem + ".json.gz"))
        self.model: Any = None
        self.forest: Optional[dict[str, Any]] = None
        self.error = ""
        self.loaded = False
        self.format = ""
        self.calibration: Optional[dict[str, Any]] = None
        self.calibration_path = self.json_path.with_name("decision_calibration.json")
        self._calibration_stamp = 0.0
        self._lock = threading.Lock()
        self._stamp = 0.0
        self.load()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("decision_model_enabled", True))

    def _stamp_now(self) -> float:
        stamps = []
        for p in (self.json_path, self.path):
            try:
                if p.is_file():
                    stamps.append(p.stat().st_mtime)
            except Exception:
                pass
        return max(stamps) if stamps else 0.0

    def load(self) -> bool:
        self.model = None
        self.forest = None
        self.loaded = False
        self.error = ""
        self.format = ""
        self.calibration = None
        self._stamp = self._stamp_now()
        self._load_calibration()
        try:
            self._calibration_stamp = (self.calibration_path.stat().st_mtime
                                       if self.calibration_path.is_file() else 0.0)
        except Exception:
            self._calibration_stamp = 0.0
        if not self.enabled:
            return False
        if self.json_path.is_file():
            try:
                with gzip.open(self.json_path, "rt", encoding="utf-8") as f:
                    data = json.load(f)
                if int(data.get("n_features", 0)) != len(DECISION_FEATURES):
                    raise ValueError("模型特征数不匹配")
                if not data.get("trees"):
                    raise ValueError("模型没有树")
                with self._lock:
                    self.forest = data
                    self.loaded = True
                    self.format = "json"
                return True
            except Exception as e:
                self.error = str(e)
        if not self.path.is_file():
            return False
        try:
            import joblib
            model = joblib.load(self.path)
            n = getattr(model, "n_features_in_", len(DECISION_FEATURES))
            if int(n) != len(DECISION_FEATURES):
                raise ValueError(f"模型特征数 {n} != {len(DECISION_FEATURES)}")
            with self._lock:
                self.model = model
                self.loaded = True
                self.format = "pkl"
            return True
        except Exception as e:
            self.error = str(e)
            self.model = None
            return False

    def _forest_probability(self, x: list[float],
                            forest: dict[str, Any]) -> float:
        total = 0.0
        trees = forest.get("trees", [])
        for tree in trees:
            left = tree["left"]
            right = tree["right"]
            feature = tree["feature"]
            threshold = tree["threshold"]
            value = tree["value"]
            node = 0
            while left[node] != -1:
                node = left[node] if x[feature[node]] <= threshold[node] else right[node]
            counts = value[node]
            denom = float(sum(counts))
            total += (float(counts[1]) / denom) if denom > 0 else 0.0
        return total / max(1, len(trees))

    def _load_calibration(self) -> None:
        self.calibration = None
        try:
            from decision_calibration import load_calibration
            data = load_calibration(self.calibration_path)
            if data is not None:
                # 模型重训后旧校准失效，必须重新拟合。
                try:
                    model_mtime = (self.path.stat().st_mtime
                                   if self.path.is_file() else
                                   (self.json_path.stat().st_mtime
                                    if self.json_path.is_file() else 0.0))
                    if data.get("model_mtime") and abs(
                            float(data["model_mtime"]) - model_mtime) > 0.5:
                        return
                except Exception:
                    pass
                self.calibration = data
        except Exception:
            pass

    def _apply_calibration(self, p: float) -> float:
        cal = self.calibration
        if not cal:
            return p
        try:
            from decision_calibration import apply_platt
            return apply_platt(p, float(cal["a"]), float(cal["b"]))
        except Exception:
            return p

    def probability_from_features(self, x: list[float]) -> float:
        """未校准的原始森林概率，用于校准器拟合，避免递归校准。"""
        if not self.loaded:
            return 0.5
        try:
            with self._lock:
                forest = self.forest
                model = self.model
            if forest is not None:
                return self._forest_probability(x, forest)
            if model is None:
                return 0.5
            return float(model.predict_proba([x])[0][1])
        except Exception:
            return 0.5

    def probability(self, c: Any) -> float:
        if not self.loaded:
            return 0.5
        try:
            return self._apply_calibration(
                self.probability_from_features(candidate_features(c)))
        except Exception:
            return 0.5

    def reload_if_changed(self) -> bool:
        """训练器写出新模型后，导播无需重启即可切换。"""
        if not self.enabled:
            return False
        stamp = self._stamp_now()
        if stamp and stamp != self._stamp:
            return self.load()
        try:
            cal_stamp = (self.calibration_path.stat().st_mtime
                         if self.calibration_path.is_file() else 0.0)
        except Exception:
            cal_stamp = 0.0
        if cal_stamp != self._calibration_stamp:
            self._calibration_stamp = cal_stamp
            self._load_calibration()
            return True
        return False

    def arbitrate(self, cands: list[Any], top_score: float,
                  current_slot: Optional[int] = None) -> Optional[int]:
        """返回模型推荐 slot；只有模型明显优于当前目标时才接管。

        导播只在规则前两名分差 <= margin 时调用本方法；这里再加一道
        “概率边际”，避免 50.1% vs 49.9% 这种噪声把镜头切走。
        """
        if not self.loaded or len(cands) < 2:
            return None
        try:
            margin = max(0.0, float(self.cfg.get("decision_model_margin", 0.08)))
            min_factor = max(0.5, min(1.0, float(
                self.cfg.get("decision_model_min_factor", 0.9))))
            if self.calibration:
                min_prob_key = "decision_model_calibrated_min_prob"
                margin_key = "decision_model_calibrated_prob_margin"
            else:
                min_prob_key = "decision_model_min_prob"
                margin_key = "decision_model_prob_margin"
            min_prob = max(0.01, min(0.95, float(
                self.cfg.get(min_prob_key, 0.16 if self.calibration else 0.55))))
            prob_margin = max(0.0, min(0.5, float(
                self.cfg.get(margin_key, 0.02 if self.calibration else 0.05))))
        except (TypeError, ValueError):
            margin, min_factor, min_prob, prob_margin = 0.08, 0.9, 0.55, 0.05
        if float(cands[0].score) - float(cands[1].score) > margin:
            return None
        probs = [(self.probability(c), c) for c in cands]
        best_p, best = max(probs, key=lambda x: x[0])
        if best_p < min_prob:
            return None
        current = None
        if current_slot is not None:
            current = next(
                (c for c in cands if getattr(c, "slot", None) == current_slot),
                None)
        if current is not None and best.slot != current_slot:
            current_p = self.probability(current)
            if best_p - current_p < prob_margin:
                return None
        try:
            min_score = float(self.cfg.get("switch_min_score", 0.30))
        except (TypeError, ValueError):
            min_score = 0.30
        if float(best.score) < max(min_score, top_score * min_factor):
            return None
        return int(best.slot) if best.slot is not None else None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "loaded": self.loaded,
            "format": self.format,
            "path": str(self.json_path if self.format == "json" else self.path),
            "error": self.error,
            "calibrated": bool(self.calibration),
            "calibration": (self.calibration or {}).get("trained_at", ""),
        }
