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
    "candidate_count", "distance", "target_hp", "enemy_hp",
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
        row = {
            "timestamp": f"{time.time():.3f}",
            "iso_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "map": str(getattr(snap, "map_name", "") or ""),
            "round": getattr(snap, "round_num", "") or "",
            "action": action,
            "target_slot": target.slot,
            "target_name": getattr(target, "name", ""),
            "target_steamid": getattr(target, "steamid", ""),
            "current_slot": current_slot if current_slot is not None else "",
            "reason": reason,
            "rule_score": f"{float(getattr(target, 'score', 0.0)):.6f}",
            "candidate_count": len(cands),
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


class ModelArbiter:
    """加载 sklearn 模型；只在规则前两名分差 <= margin 时介入。"""

    def __init__(self, cfg: dict[str, Any], path: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.path = path or decision_model_path()
        self.model: Any = None
        self.error = ""
        self.loaded = False
        self.load()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("decision_model_enabled", True))

    def load(self) -> bool:
        self.model = None
        self.loaded = False
        self.error = ""
        if not self.enabled or not self.path.is_file():
            return False
        try:
            import joblib
            self.model = joblib.load(self.path)
            n = getattr(self.model, "n_features_in_", len(DECISION_FEATURES))
            if int(n) != len(DECISION_FEATURES):
                raise ValueError(f"模型特征数 {n} != {len(DECISION_FEATURES)}")
            self.loaded = True
            return True
        except Exception as e:
            self.error = str(e)
            self.model = None
            return False

    def probability(self, c: Any) -> float:
        if not self.loaded:
            return 0.5
        try:
            p = self.model.predict_proba([candidate_features(c)])[0]
            return float(p[1])
        except Exception:
            return 0.5

    def arbitrate(self, cands: list[Any], top_score: float) -> Optional[int]:
        """返回模型推荐 slot；不满足模糊区间/分数保护时返回 None。"""
        if not self.loaded or len(cands) < 2:
            return None
        try:
            margin = max(0.0, float(self.cfg.get("decision_model_margin", 0.08)))
            min_factor = max(0.5, min(1.0, float(
                self.cfg.get("decision_model_min_factor", 0.9))))
        except (TypeError, ValueError):
            margin, min_factor = 0.08, 0.9
        if float(cands[0].score) - float(cands[1].score) > margin:
            return None
        probs = [(self.probability(c), c) for c in cands]
        best_p, best = max(probs, key=lambda x: x[0])
        if best_p <= 0.5:
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
            "path": str(self.path),
            "error": self.error,
        }
