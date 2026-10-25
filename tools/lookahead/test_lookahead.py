#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""早源影子链路测试：桥接进程、隐私、评估对齐与覆盖机会。"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import evaluate_lookahead as ev

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "observer_bridge.py"

FAKE_DRIVER = r'''
import json, sys
sys.stdin.readline()
records = [
    {"kind": "cursor", "tick": 100, "tickInterval": 0.015625, "map": "de_mirage",
     "source": "ahead:test", "channel": 1, "world": 1, "sequence": 1},
    {"kind": "raw-event", "type": "raw-event", "name": "player_death",
     "fields": {}, "players": {"attacker": {"slot": 3, "steamId": "76561190000000003"},
     "userid": {"slot": 4, "steamId": "76561190000000004"}},
     "actor": "76561190000000003", "target": "76561190000000004",
     "round": 0, "tick": 110, "source": "ahead:test", "channel": 1, "world": 1,
     "sequence": 2},
    {"kind": "raw-event", "type": "raw-event", "name": "player_death",
     "fields": {}, "players": {"attacker": {"slot": 5, "steamId": "76561190000000005"},
     "userid": {"slot": 6, "steamId": "76561190000000006"}},
     "actor": "76561190000000005", "target": "76561190000000006",
     "round": 0, "tick": 120, "source": "ahead:test", "channel": 1, "world": 1,
     "sequence": 3},
]
for record in records:
    print(json.dumps(record), flush=True)
'''


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


class BridgeTests(unittest.TestCase):
    def test_bridge_launches_and_records_without_password(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="lookahead_bridge_"))
        package = tmp / "pkg"
        (package / "client").mkdir(parents=True)
        (package / "runtime").mkdir()
        (package / "manifest.json").write_text('{"entrypoints": {}}', encoding="utf-8")
        driver = package / "client" / "fake_driver.py"
        driver.write_text(FAKE_DRIVER, encoding="utf-8")
        native = package / "client" / "cs2_observer.dll"
        native.write_bytes(b"")
        worker = package / "client" / "parser_worker.mjs"
        worker.write_text("// fake", encoding="utf-8")
        node = package / "runtime" / "node.exe"
        node.write_bytes(b"")
        steam_api = tmp / "steam_api64.dll"
        steam_api.write_bytes(b"")
        out = tmp / "run"
        env = dict(os.environ, GOTV_PASSWORD="super-secret")
        result = subprocess.run(
            [sys.executable, str(BRIDGE), "--package", str(package),
             "--steam-api", str(steam_api), "--endpoint", "127.0.0.1:27020",
             "--host-version", "14192", "--out", str(out),
             "--python", sys.executable, "--driver", str(driver),
             "--native", str(native), "--worker", str(worker),
             "--node", str(node), "--quiet"],
            env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((out / "bridge_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["records"], 3)
        self.assertEqual(report["event_names"]["player_death"], 2)
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["password_stored"])
        lines = (out / "lookahead.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        first = json.loads(lines[0])
        self.assertIn("recv", first)
        self.assertEqual(first["record"]["sequence"], 1)
        everything = "".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in out.rglob("*") if p.is_file())
        self.assertNotIn("super-secret", everything)

    def test_endpoint_and_password_validation(self) -> None:
        sys.path.insert(0, str(HERE))
        import observer_bridge as bridge
        self.assertEqual(bridge.validate_endpoint("127.0.0.1:27020"), "127.0.0.1:27020")
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_endpoint("example.com:27020")
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_endpoint("127.0.0.1")
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_password("密码")
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_password("x" * 96)


class EvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="lookahead_eval_"))
        self.events = self.tmp / "lookahead.jsonl"
        self.deaths = self.tmp / "shot_events.csv"
        self.timeline = self.tmp / "shot_timeline.csv"
        self.decisions = self.tmp / "decisions.csv"
        self.out = self.tmp / "eval.json"
        early = [
            {"kind": "raw-event", "type": "raw-event", "name": "player_death",
             "round": 0, "tick": 110, "actor": "76561190000000003",
             "target": "76561190000000004",
             "players": {"attacker": {"slot": 3, "steamId": "76561190000000003"},
                         "userid": {"slot": 4, "steamId": "76561190000000004"}},
             "source": "ahead:test", "channel": 1, "world": 1, "sequence": 2},
            {"kind": "raw-event", "type": "raw-event", "name": "player_death",
             "round": 0, "tick": 120, "actor": "76561190000000005",
             "target": "76561190000000006",
             "players": {"attacker": {"slot": 5, "steamId": "76561190000000005"},
                         "userid": {"slot": 6, "steamId": "76561190000000006"}},
             "source": "ahead:test", "channel": 1, "world": 1, "sequence": 3},
        ]
        with self.events.open("w", encoding="utf-8") as f:
            for i, record in enumerate(early):
                f.write(json.dumps({"recv": 1000.0 + i * 100, "record": record}) + "\n")
        write_csv(self.deaths,
                  ["iso_time", "epoch", "round_key", "map", "round", "event", "slot",
                   "other_slot", "value", "note"],
                  [{"epoch": 1003.0, "round": 1, "event": "death", "slot": 3},
                   {"epoch": 1101.5, "round": 1, "event": "death", "slot": 6}])
        write_csv(self.timeline,
                  ["iso_time", "epoch", "round_key", "map", "round", "shot_type",
                   "target", "target_slot", "target_name", "start_iso", "start_epoch",
                   "end_iso", "end_epoch", "duration", "reason", "end_reason",
                   "source", "manual", "story"],
                  [{"start_epoch": 990, "end_epoch": "", "shot_type": "POV",
                    "target_slot": 2, "source": "rule"}])
        write_csv(self.decisions,
                  ["target_steamid", "target_slot"],
                  [{"target_steamid": "76561190000000003", "target_slot": 3},
                   {"target_steamid": "76561190000000005", "target_slot": 5}])

    def test_evaluate_lead_and_potential(self) -> None:
        events = ev.read_lookahead_events(self.events)
        deaths = ev.read_deaths(self.deaths)
        shots = ev.read_timeline(self.timeline)
        by_steam, _ = ev.read_slot_map(self.decisions)
        result = ev.evaluate(events, deaths, shots, by_steam, 0.8, 300)
        self.assertTrue(result["ok"], result)
        summary = result["summary"]
        self.assertEqual(summary["matched"], 2)
        self.assertEqual(summary["round_offset"], 1)
        self.assertAlmostEqual(summary["lede_seconds"]["median"], 2.25)
        self.assertEqual(summary["coverage"]["potential_precut"], 2)
        self.assertEqual(summary["coverage"]["killer_on_camera_at_death"], 0)
        json_path, csv_path = ev.write_outputs(result, self.out)
        self.assertTrue(json_path.is_file())
        self.assertTrue(csv_path and csv_path.is_file())
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["killer_slot"], "3")
        self.assertEqual(rows[0]["killer_slot_source"], "decisions")
        self.assertEqual(rows[1]["observed_slot"], "2")

    def test_evaluate_no_events(self) -> None:
        result = ev.evaluate([], ev.read_deaths(self.deaths), [],
                             {}, 0.8, 300)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
