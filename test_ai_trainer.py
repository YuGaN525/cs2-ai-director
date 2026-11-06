#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 辅助训练测试：隐私、解析、守卫、无 Key 回退与报告落盘。"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ai_trainer as ai
import train_decision_model as t
from decision_model import CSV_COLUMNS

SECRET_NAME = "SECRET_PLAYER_NAME"
SECRET_ID = "76561190000000001"


def write_csv(path: Path, groups: int = 18, rows_per_group: int = 14) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for g in range(groups):
            for i in range(rows_per_group):
                aim = (i % 7) / 7.0
                win = ((g + i) % 5) / 5.0
                label = 1 if (aim + win + ((g * 3 + i) % 4) / 8.0) > 1.05 else 0
                row = {name: "" for name in CSV_COLUMNS}
                row.update({
                    "timestamp": 1792500000 + g * 120 + i,
                    "iso_time": "2026-10-21 09:00:00",
                    "map": "de_mirage" if g % 2 else "de_inferno",
                    "round": str(g // 2 + 1),
                    "action": "switch" if i % 4 == 0 else "hold",
                    "target_slot": str(i % 10),
                    "target_name": SECRET_NAME,
                    "target_steamid": SECRET_ID,
                    "rule_score": "0.5",
                    "candidate_count": "2",
                    "rule_gap": "0.05" if i % 3 == 0 else "",
                    "is_ambiguous": "1" if i % 3 == 0 else "0",
                    "group": f"g{g}",
                    "group_size": str(rows_per_group),
                    "distance": "600",
                    "target_hp": "100",
                    "enemy_hp": "80",
                    "aim_self": f"{aim:.3f}",
                    "aim_enemy": f"{win:.3f}",
                    "win": f"{win:.3f}",
                    "enemy_low_hp": f"{1 - aim:.3f}",
                    "self_low_hp": f"{1 - win:.3f}",
                    "proximity": f"{aim:.3f}",
                    "enemy_damaged": str(label),
                    "hp_advantage": str(label),
                    "label": str(label),
                    "label_damage": str(label),
                    "label_kill": "0",
                })
                writer.writerow(row)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class AITrainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ai_trainer_test_"))
        self.csv = self.tmp / "decisions.csv"
        write_csv(self.csv)

    def test_load_config_from_file_and_env(self) -> None:
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({
            "deepseek_api_key": "file-key",
            "deepseek_base_url": "https://example.invalid",
            "deepseek_model": "model-a",
        }), encoding="utf-8")
        cfg = ai.load_ai_config(cfg_path, env={})
        self.assertEqual(cfg.api_key, "file-key")
        self.assertEqual(cfg.model, "model-a")
        self.assertIn("config.json", cfg.source)
        override = ai.load_ai_config(cfg_path, env={
            "DEEPSEEK_API_KEY": "env-key", "DEEPSEEK_MODEL": "model-b"})
        self.assertEqual(override.api_key, "env-key")
        self.assertEqual(override.model, "model-b")
        self.assertEqual(override.source, "环境变量")

    def test_profile_and_prompt_have_no_pii(self) -> None:
        profile = ai.profile_dataset(self.csv)
        text = json.dumps(profile, ensure_ascii=False)
        self.assertNotIn(SECRET_NAME, text)
        self.assertNotIn(SECRET_ID, text)
        self.assertEqual(profile["valid"], 18 * 14)
        self.assertIn("de_mirage", profile["maps"])
        self.assertGreater(len(profile["feature_auc"]), 0)
        self.assertTrue(0.0 < profile["positive_rate"] < 1.0)
        prompt = ai.build_advice_prompt(profile)
        self.assertNotIn(SECRET_NAME, prompt)
        self.assertNotIn(SECRET_ID, prompt)
        self.assertIn("aim_self", prompt)
        self.assertIn("search_space", prompt)

    def test_parse_advice_clamps_values(self) -> None:
        text = """```json
        {
          "diagnosis": ["样本偏少", "模糊样本占比高"],
          "data_risks": ["地图不均衡"],
          "search_space": {
            "n_estimators": [99999, "bad"],
            "max_depth": [1, 40, null],
            "min_samples_leaf": [0],
            "max_features": ["bad", 2.0],
            "class_weight": ["bad"],
            "unknown": [1]
          },
          "weight_boost": {"ambiguous": 99, "switch": -1},
          "n_iter": 999,
          "split_mode": "hack",
          "notes": "ok"
        }
        ```"""
        advice = ai.parse_advice(text)
        self.assertEqual(advice.search_space["n_estimators"], [800])
        self.assertEqual(advice.search_space["max_depth"], [2, 24, None])
        self.assertEqual(advice.search_space["min_samples_leaf"], [1])
        self.assertEqual(advice.search_space["max_features"], ["sqrt", 0.5])
        self.assertEqual(advice.search_space["class_weight"], ["balanced"])
        self.assertNotIn("unknown", advice.search_space)
        self.assertEqual(advice.weight_boost["ambiguous"], 5.0)
        self.assertEqual(advice.weight_boost["switch"], 0.0)
        self.assertEqual(advice.n_iter, 24)
        self.assertEqual(advice.split_mode, "auto")

    def test_parse_advice_rejects_garbage(self) -> None:
        with self.assertRaises(ai.AdviceError):
            ai.parse_advice("不是 JSON")
        with self.assertRaises(ai.AdviceError):
            ai.parse_advice("")

    def test_ask_deepseek_sends_only_aggregates(self) -> None:
        captured: dict[str, object] = {}
        content = json.dumps({
            "diagnosis": ["ok"], "data_risks": [],
            "search_space": {"n_estimators": [300]}, "n_iter": 8,
            "split_mode": "group", "notes": "",
        })

        def fake_urlopen(request, timeout=None):
            captured["body"] = request.data.decode("utf-8")
            captured["auth"] = dict(request.headers).get("Authorization", "")
            captured["timeout"] = timeout
            return FakeResponse({"choices": [{"message": {"content": content}}]})

        cfg = ai.AIConfig(api_key="test-key", model="deepseek-chat",
                          source="test")
        profile = ai.profile_dataset(self.csv)
        with mock.patch("ai_trainer.urllib.request.urlopen", fake_urlopen):
            advice = ai.ask_deepseek(profile, cfg, timeout=5)
        body = str(captured["body"])
        self.assertNotIn(SECRET_NAME, body)
        self.assertNotIn(SECRET_ID, body)
        self.assertEqual(captured["auth"], "Bearer test-key")
        self.assertEqual(captured["timeout"], 5)
        self.assertIn("response_format", body)
        self.assertEqual(advice.split_mode, "group")

    def test_choose_result_guardrails(self) -> None:
        base = {"split_hash": "fixture", "auc": 0.70, "top1": 0.50, "model_json_bytes": 2 * 1024 * 1024}
        better = {"split_hash": "fixture", "auc": 0.71, "top1": 0.51, "model_json_bytes": 2_500_000}
        self.assertEqual(ai.choose_result(base, better)[0], "candidate")
        flat = {"split_hash": "fixture", "auc": 0.7005, "top1": 0.50, "model_json_bytes": 2_000_000}
        self.assertEqual(ai.choose_result(base, flat)[0], "baseline")
        big = {"split_hash": "fixture", "auc": 0.75, "top1": 0.60, "model_json_bytes": 30 * 1024 * 1024}
        self.assertEqual(ai.choose_result(base, big)[0], "baseline")
        regress = {"split_hash": "fixture", "auc": 0.72, "top1": 0.40, "model_json_bytes": 2_000_000}
        self.assertEqual(ai.choose_result(base, regress)[0], "baseline")

    def test_time_split_is_recorded(self) -> None:
        metrics = t.train_from_csv(
            self.csv, self.tmp / "model.pkl", self.tmp / "metrics.json",
            min_samples=100, test_size=0.25, trees=40, max_depth=6,
            search_space={"n_estimators": [40], "max_depth": [4],
                          "min_samples_leaf": [2], "max_features": ["sqrt"],
                          "class_weight": ["balanced"]},
            n_iter=1, split_mode="time",
        )
        self.assertEqual(metrics["split"], "time")
        self.assertEqual(metrics["n_iter"], 1)
        self.assertIn("search_space", metrics)

    def test_budget_space_caps_cost(self) -> None:
        space = {"n_estimators": [800], "max_depth": [24, None],
                 "min_samples_leaf": [2], "max_features": ["sqrt"],
                 "class_weight": ["balanced"]}
        large, iters, notes = ai.budget_space(space, 60000, 24)
        self.assertEqual(large["n_estimators"], [300])
        self.assertEqual(large["max_depth"], [18, None])
        self.assertEqual(iters, 8)
        self.assertTrue(notes)
        small, iters_small, _ = ai.budget_space(space, 5000, 24)
        self.assertEqual(small["n_estimators"], [600])
        self.assertEqual(iters_small, 16)

    def test_run_without_llm_falls_back_to_local(self) -> None:
        report = ai.run_ai_training(
            self.csv, self.tmp, min_samples=100, trees=40, max_depth=6,
            use_ai=False, search_iters=2,
        )
        self.assertFalse(report["ai"]["used"])
        self.assertIn(report["decision"], ("baseline", "candidate"))
        self.assertIn(report["adopted"], ("baseline", "local"))
        self.assertTrue((self.tmp / "decision_model.pkl").is_file())
        self.assertTrue((self.tmp / "decision_model.json.gz").is_file())
        self.assertTrue((self.tmp / "ai_training_report.json").is_file())
        metrics = json.loads(
            (self.tmp / "decision_model_metrics.json").read_text(encoding="utf-8"))
        self.assertIn("ai_training", metrics)
        self.assertEqual(Path(metrics["model"]), self.tmp / "decision_model.pkl")
        self.assertEqual(Path(metrics["model_json"]),
                         self.tmp / "decision_model.json.gz")
        self.assertNotIn(".ai_tmp_", json.dumps(metrics))

    def test_run_with_ai_error_falls_back(self) -> None:
        def advisor(profile: dict) -> ai.Advice:
            raise ai.AdviceError("boom")

        report = ai.run_ai_training(
            self.csv, self.tmp, min_samples=100, trees=40, max_depth=6,
            use_ai=True, search_iters=1, advisor=advisor,
            ai_config=ai.AIConfig(api_key="test", source="test"),
        )
        self.assertFalse(report["ai"]["used"])
        self.assertIn("boom", report["ai"]["error"])
        self.assertIn(report["adopted"], ("baseline", "local"))

    def test_run_with_injected_advice(self) -> None:
        def advisor(profile: dict) -> ai.Advice:
            self.assertNotIn(SECRET_NAME, json.dumps(profile, ensure_ascii=False))
            return ai.Advice(
                diagnosis=["测试建议"], search_space={
                    "n_estimators": [40], "max_depth": [4],
                    "min_samples_leaf": [2], "max_features": ["sqrt"],
                    "class_weight": ["balanced"],
                }, n_iter=2, split_mode="group", notes="test")

        report = ai.run_ai_training(
            self.csv, self.tmp, min_samples=100, trees=40, max_depth=6,
            use_ai=True, search_iters=2, advisor=advisor,
            ai_config=ai.AIConfig(api_key="test", source="test"),
        )
        self.assertTrue(report["ai"]["used"])
        self.assertIsNotNone(report["advice"])
        self.assertIn(report["decision"], ("baseline", "candidate"))
        self.assertNotIn(SECRET_NAME, json.dumps(report, ensure_ascii=False))
        self.assertNotIn(SECRET_ID, json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
