#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cs2_ai_director 的核心逻辑测试（不启动 GUI、不发送真实按键）。"""

import concurrent.futures
import csv
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from unittest import mock

import cs2_ai_director as d
import director_story as st


def player(slot, team, hp=100, pos=(0, 0, 0), fwd=(1, 0, 0),
           weapon="weapon_ak47", typ="Rifle", ammo=30, armor=100,
           flashed=0, defusing=False, name=None, round_kills=0, round_totaldmg=0,
           money=0):
    return {
        "name": name or f"P{slot}",
        "observer_slot": slot,
        "team": team,
        "position": f"{pos[0]}, {pos[1]}, {pos[2]}",
        "forward": f"{fwd[0]}, {fwd[1]}, {fwd[2]}",
        "state": {
            "health": hp, "armor": armor, "helmet": True,
            "flashed": flashed, "defusing": defusing,
            "round_kills": round_kills, "round_totaldmg": round_totaldmg,
            "money": money,
        },
        "weapons": {"weapon_0": {"name": weapon, "type": typ,
                                 "state": "active", "ammo_clip": ammo}},
    }


def payload(players, phase="live", round_no=1, bomb=None, player_slot=0,
            map_name="de_mirage", scores=(0, 0)):
    return {
        "provider": {"steamid": "observer", "name": "obs"},
        "map": {"name": map_name, "round": round_no, "phase": phase,
                "team_ct": {"score": scores[0]}, "team_t": {"score": scores[1]}},
        "round": {"phase": phase},
        "phase_countdowns": {"phase": phase},
        "player": {"steamid": "observer", "observer_slot": player_slot},
        "allplayers": {f"sid{p['observer_slot']}": p for p in players},
        "bomb": bomb,
    }


def snapshot(**kwargs):
    snap = d.parse_snapshot(payload(**kwargs), time.monotonic())
    assert snap is not None
    snap.seq = 1
    return snap


class TestParsing(unittest.TestCase):
    def test_basic(self):
        snap = snapshot(players=[
            player(1, "CT"), player(2, "T", pos=(600, 0, 0), fwd=(-1, 0, 0)),
        ])
        self.assertEqual(snap.phase, "live")
        self.assertEqual(snap.round_num, 1)
        self.assertTrue(snap.is_spectating)
        self.assertEqual(snap.observer_slot, 0)
        self.assertEqual(len(snap.players), 2)
        p = snap.players["sid1"]
        self.assertEqual(p.health, 100)
        self.assertEqual(p.slot, 1)
        self.assertEqual(p.active_weapon["name"], "weapon_ak47")

    def test_local_player_alive_is_not_spectating(self):
        data = payload(players=[player(1, "CT")])
        data["provider"]["steamid"] = "sid1"  # 本地玩家就是 1 号
        snap = d.parse_snapshot(data, time.monotonic())
        self.assertFalse(snap.is_spectating)

    def test_degraded_no_allplayers(self):
        data = payload(players=[])
        data.pop("allplayers")
        snap = d.parse_snapshot(data, time.monotonic())
        self.assertIsNotNone(snap)
        self.assertEqual(snap.players, {})
        self.assertEqual(d.score_candidates(snap, d.DEFAULT_CONFIG), [])

    def test_bomb_and_phase(self):
        snap = snapshot(phase="freezetime", players=[], bomb={
            "state": "planted", "countdown": "7.5", "position": "[100, 200, 0]"})
        self.assertEqual(snap.bomb.state, "planted")
        self.assertAlmostEqual(snap.bomb.countdown, 7.5)
        self.assertEqual(snap.bomb.position, (100.0, 200.0, 0.0))


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.both_aim = [
            player(1, "CT", name="A"),
            player(2, "T", pos=(600, 0, 0), fwd=(-1, 0, 0), name="B"),
        ]

    def test_engagement_scores_above_threshold(self):
        snap = snapshot(players=self.both_aim)
        cands = d.score_candidates(snap, d.DEFAULT_CONFIG, None, time.monotonic())
        self.assertTrue(cands)
        self.assertGreater(cands[0].score, d.DEFAULT_CONFIG["switch_min_score"])
        self.assertEqual(cands[0].aim_self, 1.0)

    def test_far_apart_no_switch(self):
        snap = snapshot(players=[
            player(1, "CT"),
            player(2, "T", pos=(4000, 0, 0), fwd=(-1, 0, 0)),
        ])
        cands = d.score_candidates(snap, d.DEFAULT_CONFIG, None, time.monotonic())
        self.assertEqual(cands, [])

    def test_low_hp_enemy_drives_switch_to_attacker(self):
        snap = snapshot(players=[
            player(1, "CT", hp=100, name="A"),
            player(2, "T", hp=20, pos=(600, 0, 0), fwd=(-1, 0, 0), name="B"),
        ])
        cands = d.score_candidates(snap, d.DEFAULT_CONFIG, None, time.monotonic())
        self.assertGreater(cands[0].score, 0.7)
        self.assertEqual(cands[0].slot, 1)  # 满血的 A 更可能拿到击杀

    def test_knife_holder_is_not_attacker(self):
        snap = snapshot(players=[
            player(1, "CT", weapon="weapon_knife", typ="Knife"),
            player(2, "T", pos=(600, 0, 0), fwd=(-1, 0, 0)),
        ])
        cands = d.score_candidates(snap, d.DEFAULT_CONFIG, None, time.monotonic())
        self.assertTrue(cands)
        self.assertEqual(cands[0].slot, 2)  # 拿刀的人是对面的猎物

    def test_flash_penalty(self):
        snap = snapshot(players=[
            player(1, "CT", flashed=255),
            player(2, "T", pos=(600, 0, 0), fwd=(-1, 0, 0)),
        ])
        cands = d.score_candidates(snap, d.DEFAULT_CONFIG, None, time.monotonic())
        slot1 = next(c for c in cands if c.slot == 1)
        slot2 = next(c for c in cands if c.slot == 2)
        self.assertLess(slot1.win, slot2.win)

    def test_missing_forward_degrades(self):
        data = payload(players=[
            player(1, "CT"), player(2, "T", pos=(300, 0, 0), fwd=(-1, 0, 0)),
        ])
        for p in data["allplayers"].values():
            p.pop("forward")
        snap = d.parse_snapshot(data, time.monotonic())
        cands = d.score_candidates(snap, d.DEFAULT_CONFIG, None, time.monotonic())
        self.assertTrue(cands)  # 没有朝向也能靠距离降级判断


class TestDecision(unittest.TestCase):
    def make_cands(self, *scores):
        return [
            d.Candidate(slot=i + 1, steamid=f"s{i}", name=f"P{i}", team="CT",
                        score=s, win=0.5, enemy_slot=9, enemy_name="E", distance=500,
                        aim_self=0.8, aim_enemy=0.6, hp=100, armor=100, enemy_hp=100,
                        weapon="ak47", enemy_weapon="m4", enemy_damaged=False)
            for i, s in enumerate(scores)
        ]

    def test_switch_when_score_high(self):
        dec = d.choose_slot(self.make_cands(0.6), 5, 0.0, None, None, d.DEFAULT_CONFIG)
        self.assertEqual(dec.slot, 1)

    def test_already_watching(self):
        dec = d.choose_slot(self.make_cands(0.6), 1, 0.0, None, None, d.DEFAULT_CONFIG)
        self.assertIsNone(dec.slot)
        self.assertIn("已经在", dec.reason)

    def test_below_threshold(self):
        dec = d.choose_slot(self.make_cands(0.1), 5, 0.0, None, None, d.DEFAULT_CONFIG)
        self.assertIsNone(dec.slot)

    def test_hysteresis_keeps_current(self):
        cands = self.make_cands(0.52, 0.48)
        dec = d.choose_slot(cands, 2, 10.0, 0.0, 2, d.DEFAULT_CONFIG)
        self.assertIsNone(dec.slot)

    def test_margin_switches(self):
        cands = self.make_cands(0.75, 0.40)
        dec = d.choose_slot(cands, 2, 10.0, 0.0, 2, d.DEFAULT_CONFIG)
        self.assertEqual(dec.slot, 1)

    def test_dwell(self):
        cands = self.make_cands(0.9, 0.2)
        dec = d.choose_slot(cands, 2, 0.4, 0.0, 1, d.DEFAULT_CONFIG)
        self.assertIsNone(dec.slot)
        self.assertIn("冷却", dec.reason)

    def test_repeat_guard_same_slot(self):
        cands = self.make_cands(0.9)
        # GSI 还没刷新 observer_slot，但 2 秒前刚按过 1（已过 dwell，未过 repeat guard）
        dec = d.choose_slot(cands, 5, 2.0, 0.0, 1, d.DEFAULT_CONFIG)
        self.assertIsNone(dec.slot)
        self.assertIn("刚刚按过", dec.reason)

    def test_hard_200ms_floor(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["min_dwell_seconds"] = 0.05
        cfg["hard_min_switch_interval"] = 0.2
        cands = self.make_cands(0.9)
        dec = d.choose_slot(cands, 5, 0.15, 0.0, None, cfg)
        self.assertIsNone(dec.slot)
        self.assertIn("200ms", dec.reason)
        dec = d.choose_slot(cands, 5, 0.25, 0.0, None, cfg)
        self.assertEqual(dec.slot, 1)


class TestTriggers(unittest.TestCase):
    def test_round_cam_once_per_round(self):
        cfg = dict(d.DEFAULT_CONFIG)
        tr = d.TriggerTracker(cfg)
        snap = snapshot(phase="freezetime", round_no=3, players=[])
        evs = tr.poll(snap, 0.0)
        self.assertEqual([e.kind for e in evs], ["round"])
        tr.confirm(evs[0])
        self.assertEqual(tr.poll(snap, 5.0), [])
        snap2 = snapshot(phase="freezetime", round_no=4, players=[])
        evs2 = tr.poll(snap2, 20.0)
        self.assertEqual([e.kind for e in evs2], ["round"])
        self.assertEqual(tr.counts["round"], 1)

    def test_bomb_cam_threshold_and_once(self):
        cfg = dict(d.DEFAULT_CONFIG)
        tr = d.TriggerTracker(cfg)
        s10 = snapshot(players=[], round_no=7, bomb={"state": "planted", "countdown": "10.0"})
        self.assertEqual(tr.poll(s10, 0.0), [])
        s7 = snapshot(players=[], round_no=7, bomb={"state": "planted", "countdown": "7.0"})
        evs = tr.poll(s7, 2.0)
        self.assertEqual([e.kind for e in evs], ["bomb"])
        tr.confirm(evs[0])
        s6 = snapshot(players=[], round_no=7, bomb={"state": "planted", "countdown": "6.0"})
        self.assertEqual(tr.poll(s6, 3.0), [])
        # 新回合的安放可以再次触发
        s_next = snapshot(players=[], round_no=8, bomb={"state": "planted", "countdown": "5.0"})
        self.assertEqual([e.kind for e in tr.poll(s_next, 100.0)], ["bomb"])

    def test_freezetime_retry_after_failed_focus(self):
        tr = d.TriggerTracker(dict(d.DEFAULT_CONFIG))
        snap = snapshot(phase="freezetime", players=[], round_no=1)
        self.assertEqual(len(tr.poll(snap, 0.0)), 1)      # 第一次尝试
        self.assertEqual(tr.poll(snap, 0.2), [])          # 失败后 1s 内不重试
        self.assertEqual(len(tr.poll(snap, 1.2)), 1)      # 1s 后重试


class TestGSIConfig(unittest.TestCase):
    def test_all_required_fields(self):
        conf = d.gsi_config_dict(3456)
        self.assertEqual(conf["uri"], "http://127.0.0.1:3456")
        for name in d.GSI_REQUIRED_DATA:
            self.assertEqual(conf["data"].get(name), 1, name)
        self.assertIn("allplayers_position", conf["data"])

    def test_keyvalues_text(self):
        """CS2 只认 Valve KeyValues；JSON 会被静默忽略。"""
        text = d.gsi_config_text(3456)
        self.assertIn('"uri" "http://127.0.0.1:3456"', text)
        self.assertIn('"allplayers_position" "1"', text)
        self.assertIn('"phase_countdowns" "1"', text)
        self.assertNotIn('"uri":', text)  # 不是 JSON
        self.assertTrue(text.lstrip().startswith('"CS2 AI Director"'))

    def test_install_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "game" / "csgo" / "cfg"
            cfg_dir.mkdir(parents=True)
            exe = Path(tmp) / "game" / "cs2.exe"
            exe.write_bytes(b"")
            self.assertEqual(d.derive_cfg_dir(str(exe)), cfg_dir)
            target = d.install_gsi_config(cfg_dir, 3000, overwrite=True)
            self.assertTrue(target.is_file())
            text = target.read_text(encoding="utf-8")
            self.assertIn('"allplayers_position" "1"', text)
            self.assertIn('"uri" "http://127.0.0.1:3000"', text)
            target2 = d.install_gsi_config(cfg_dir, 3002, overwrite=True)
            self.assertIn('"uri" "http://127.0.0.1:3002"',
                          target2.read_text(encoding="utf-8"))
            self.assertTrue(list(cfg_dir.glob("*.bak_*")))
            with self.assertRaises(FileExistsError):
                d.install_gsi_config(cfg_dir, 3003, overwrite=False)

    def test_derive_cfg_dir_rejects_other_exe(self):
        self.assertIsNone(d.derive_cfg_dir("C:/Windows/notepad.exe"))
        self.assertIsNone(d.derive_cfg_dir(""))

    def test_derive_cfg_dir_win64_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_dir = root / "game" / "csgo" / "cfg"
            cfg_dir.mkdir(parents=True)
            exe = root / "game" / "bin" / "win64" / "cs2.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.assertEqual(d.derive_cfg_dir(str(exe)), cfg_dir)

    def test_port_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            (cfg_dir / d.GSI_CFG_NAME).write_text(
                '{"uri": "http://127.0.0.1:3000"}', encoding="utf-8")
            (cfg_dir / "gamestate_integration_other.cfg").write_text(
                '"x" {"uri" "http://localhost:3000/"}', encoding="utf-8")
            (cfg_dir / "gamestate_integration_other2.cfg").write_text(
                '{"uri": "http://127.0.0.1:3001"}', encoding="utf-8")
            self.assertEqual(d.gsi_port_conflicts(cfg_dir, 3000),
                             ["gamestate_integration_other.cfg"])
            self.assertEqual(d.gsi_port_conflicts(cfg_dir, 3999), [])


class TestAI(unittest.TestCase):
    def test_parse_response(self):
        self.assertEqual(d.parse_ai_response('{"slot": 3, "reason": "对枪"}')[0], 3)
        self.assertEqual(d.parse_ai_response('```json\n{"slot": 4}\n```')[0], 4)
        self.assertIsNone(d.parse_ai_response("")[0])
        self.assertIsNone(d.parse_ai_response("not json")[0])
        self.assertIsNone(d.parse_ai_response('{"slot": -1}')[0])
        self.assertIsNone(d.parse_ai_response('{"reason": "x"}')[0])

    def test_failure_falls_back(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["deepseek_api_key"] = "sk-test"
        bus = d.LogBus()
        arb = d.AIArbiter(cfg, bus)

        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        arb._request_chat = boom  # type: ignore[assignment]
        slot, _ = arb._ask("prompt", {1, 2})
        self.assertIsNone(slot)
        self.assertGreaterEqual(arb.errors, 1)

    def test_empty_content(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["deepseek_api_key"] = "sk-test"
        arb = d.AIArbiter(cfg, d.LogBus())
        arb._request_chat = lambda *a, **k: None  # type: ignore[assignment]
        slot, _ = arb._ask("prompt", {1})
        self.assertIsNone(slot)
        self.assertEqual(arb.errors, 1)

    def test_timeout_does_not_retry(self):
        """超时只发一次请求，避免双倍等待与潜在重复计费。"""
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["deepseek_api_key"] = "sk-test"
        arb = d.AIArbiter(cfg, d.LogBus())
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        arb._request_chat = boom  # type: ignore[assignment]
        slot, _ = arb._ask("prompt", {1, 2})
        self.assertIsNone(slot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(arb.errors, 1)

    def test_format_error_retries_once(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["deepseek_api_key"] = "sk-test"
        arb = d.AIArbiter(cfg, d.LogBus())
        calls = []

        def flaky(model, prompt, json_mode):
            calls.append(json_mode)
            if len(calls) == 1:
                raise RuntimeError("response_format is not supported")
            return '{"slot": 2, "reason": "test"}'

        arb._request_chat = flaky  # type: ignore[assignment]
        slot, _ = arb._ask("prompt", {1, 2})
        self.assertEqual(slot, 2)
        self.assertEqual(calls, [True, False])

    def test_prompt_mentions_candidates(self):
        snap = snapshot(players=[
            player(1, "CT", name="A"), player(2, "T", pos=(600, 0, 0), fwd=(-1, 0, 0)),
        ])
        cands = d.score_candidates(snap, d.DEFAULT_CONFIG, None, time.monotonic())
        text = d.build_ai_prompt(snap, cands)
        self.assertIn("slot=1", text)
        self.assertIn("JSON", text)


class TestGSIServer(unittest.TestCase):
    def test_http_roundtrip(self):
        store = d.SnapshotStore()
        server = d.GSIServer("127.0.0.1", 0, store, lambda m: None)
        server.start()
        try:
            port = server._httpd.server_address[1]  # type: ignore[union-attr]
            body = json.dumps(payload(players=[player(1, "CT")])).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                self.assertEqual(resp.status, 200)
            for _ in range(50):
                if store.get() is not None:
                    break
                time.sleep(0.02)
            snap = store.get()
            self.assertIsNotNone(snap)
            self.assertEqual(snap.seq, 1)
        finally:
            server.stop()

    def test_bad_json_returns_400(self):
        store = d.SnapshotStore()
        server = d.GSIServer("127.0.0.1", 0, store, lambda m: None)
        server.start()
        try:
            port = server._httpd.server_address[1]  # type: ignore[union-attr]
            req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"{bad",
                                         method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.stop()

    def test_foreign_gsipayload_cannot_overwrite_good_snapshot(self):
        """同端口其他 GSI 配置的精简包不能覆盖带位置的快照。"""
        store = d.SnapshotStore()
        server = d.GSIServer("127.0.0.1", 0, store, lambda m: None)
        server.start()
        try:
            port = server._httpd.server_address[1]  # type: ignore[union-attr]

            def post(obj):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/", data=json.dumps(obj).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    return resp.status

            good = payload(players=[
                player(1, "CT"), player(2, "T", pos=(600, 0, 0)),
            ])
            self.assertEqual(post(good), 200)
            self.assertTrue(store.has_positions())

            # 另一份配置：没有 provider，且没有 allplayers position
            bad = payload(players=[player(1, "CT")])
            bad.pop("provider", None)
            for p in bad["allplayers"].values():
                p.pop("position", None)
                p.pop("forward", None)
            self.assertEqual(post(bad), 200)
            time.sleep(0.2)
            snap = store.get()
            self.assertIsNotNone(snap)
            self.assertEqual(snap.provider_steamid, "observer")
            self.assertTrue(any(p.pos for p in snap.players.values()))
            self.assertEqual(store.stats()[0], 1)
        finally:
            server.stop()


class TestBombSiteDetection(unittest.TestCase):
    def test_known_maps(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cases = [
            ("de_mirage", (-600, -2200), "A"),
            ("de_mirage", (-2200, 600), "B"),
            ("de_inferno", (2000, 400), "A"),
            ("de_inferno", (400, 2800), "B"),
            ("de_dust2", (1200, 2400), "A"),
            ("de_dust2", (-1500, 2400), "B"),
            ("de_anubis", (1500, 2000), "A"),
            ("de_anubis", (-1300, 600), "B"),
            ("de_ancient", (-1500, 800), "A"),
            ("de_ancient", (800, 100), "B"),
            ("de_cache", (-300, 1800), "A"),
            ("de_cache", (0, -1200), "B"),
            ("de_nuke", (300, -300, -100), "A"),
            ("de_nuke", (350, -570, -700), "B"),
        ]
        for map_name, pos, expected in cases:
            self.assertEqual(d.detect_bomb_site(map_name, pos, cfg), expected, (map_name, pos))

    def test_missing_or_unknown(self):
        cfg = dict(d.DEFAULT_CONFIG)
        self.assertIsNone(d.detect_bomb_site("de_mirage", None, cfg))
        self.assertIsNone(d.detect_bomb_site("de_train", (0, 0, 0), cfg))
        self.assertIsNone(d.detect_bomb_site("de_mirage", (1,), cfg))

    def test_custom_ref_override(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["bomb_site_refs"] = {"de_train": {"A": [0, 0], "B": [1000, 0]}}
        self.assertEqual(d.detect_bomb_site("de_train", (10, 10, 0), cfg), "A")
        self.assertEqual(d.detect_bomb_site("de_train", (990, 10, 0), cfg), "B")


class TestBombTriggerSelection(unittest.TestCase):
    def _engine(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["hlae_round_key"] = "alt+f6"
        cfg["hlae_bomb_a_key"] = "alt+1"
        cfg["hlae_bomb_b_key"] = "alt+2"
        cfg["hlae_bomb_a_mode"] = "double"
        cfg["hlae_bomb_b_mode"] = "single"
        cfg["bomb_cam_policy"] = "always"  # 本组只验证 A/B 键选择
        return cfg, d.DirectorEngine(cfg, d.LogBus())

    def _snap(self, map_name, pos, rnd=1):
        data = payload(
            players=[player(1, "CT")], map_name=map_name, round_no=rnd,
            bomb={"state": "planted", "countdown": "6.0",
                  "position": f"{pos[0]}, {pos[1]}, {pos[2]}"},
        )
        snap = d.parse_snapshot(data, time.monotonic())
        assert snap is not None
        return snap

    def _run(self, map_name, pos, rnd=1):
        cfg, eng = self._engine()
        calls = []
        eng.input.send_hotkey = lambda key, double=False: calls.append((key, double)) or True  # type: ignore[assignment]
        eng._handle_triggers(self._snap(map_name, pos, rnd), time.monotonic())
        return calls, eng

    def test_a_site_uses_a_key_and_double(self):
        calls, eng = self._run("de_mirage", (-600, -2200, 0))
        self.assertEqual(calls, [("alt+1", True)])
        self.assertEqual(eng.triggers.counts["bomb"], 1)

    def test_b_site_uses_b_key_and_single(self):
        calls, eng = self._run("de_mirage", (-2200, 600, 0))
        self.assertEqual(calls, [("alt+2", False)])
        self.assertEqual(eng.triggers.counts["bomb"], 1)

    def test_unknown_map_skips_without_retry(self):
        calls, eng = self._run("de_train", (0, 0, 0))
        self.assertEqual(calls, [])
        self.assertEqual(eng.triggers.counts["bomb"], 0)
        # 已确认，避免同一回合每秒重试
        self.assertTrue(eng.triggers._fired_bomb)


class TestBombCamPolicy(unittest.TestCase):
    def _run(self, hp, policy, enemy_pos=(600, 0, 0)):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg.update({
            "hlae_round_key": "alt+f6",
            "hlae_bomb_a_key": "alt+1",
            "hlae_bomb_b_key": "alt+2",
            "bomb_cam_policy": policy,
        })
        eng = d.DirectorEngine(cfg, d.LogBus())
        calls = []
        eng.input.send_hotkey = lambda key, double=False: calls.append((key, double)) or True  # type: ignore[assignment]
        players = [player(1, "CT", hp=hp)]
        if hp > 0:
            players.append(player(2, "T", hp=100, pos=enemy_pos))
        data = payload(
            players=players, map_name="de_mirage",
            bomb={"state": "planted", "countdown": "6.0",
                  "position": "-600, -2200, 0"},
        )
        snap = d.parse_snapshot(data, time.monotonic())
        assert snap is not None
        eng._handle_triggers(snap, time.monotonic())
        return calls, eng

    def test_pov_first_skips_when_players_alive(self):
        calls, eng = self._run(hp=100, policy="pov_first")
        self.assertEqual(calls, [])
        self.assertEqual(eng.triggers.counts["bomb"], 0)
        self.assertTrue(eng.triggers._fired_bomb)  # 已确认，不重复重试

    def test_pov_first_fires_when_all_dead(self):
        calls, eng = self._run(hp=0, policy="pov_first")
        self.assertEqual(calls, [("alt+1", True)])
        self.assertEqual(eng.triggers.counts["bomb"], 1)

    def test_pov_first_fires_when_players_far_apart(self):
        calls, _ = self._run(hp=100, policy="pov_first", enemy_pos=(4000, 0, 0))
        self.assertEqual(calls, [("alt+1", True)])

    def test_pov_first_fires_when_different_floor(self):
        calls, _ = self._run(hp=100, policy="pov_first", enemy_pos=(100, 0, 600))
        self.assertEqual(calls, [("alt+1", True)])

    def test_always_fires_even_with_alive_players(self):
        calls, eng = self._run(hp=100, policy="always")
        self.assertEqual(calls, [("alt+1", True)])
        self.assertEqual(eng.triggers.counts["bomb"], 1)


class TestSpawnTriggerSelection(unittest.TestCase):
    def _engine(self, winner=None, default_side="CT"):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg.update({
            "hlae_spawn_ct_key": "alt+f6",
            "hlae_spawn_t_key": "alt+f7",
            "hlae_spawn_ct_mode": "double",
            "hlae_spawn_t_mode": "single",
            "spawn_cam_default_side": default_side,
        })
        eng = d.DirectorEngine(cfg, d.LogBus())
        eng._last_round_winner = winner
        return eng

    def _snap(self, rnd=2):
        data = payload(players=[player(1, "CT")], phase="freezetime", round_no=rnd)
        snap = d.parse_snapshot(data, time.monotonic())
        assert snap is not None
        return snap

    def _run(self, winner, default_side="CT"):
        eng = self._engine(winner, default_side)
        calls = []
        eng.input.send_hotkey = lambda key, double=False: calls.append((key, double)) or True  # type: ignore[assignment]
        eng._handle_triggers(self._snap(), time.monotonic())
        return calls, eng

    def test_ct_won_uses_ct_spawn(self):
        calls, eng = self._run("CT")
        self.assertEqual(calls, [("alt+f6", True)])
        self.assertEqual(eng.triggers.counts["round"], 1)

    def test_t_won_uses_t_spawn(self):
        calls, _ = self._run("T")
        self.assertEqual(calls, [("alt+f7", False)])

    def test_no_previous_round_uses_default(self):
        calls, _ = self._run(None, default_side="T")
        self.assertEqual(calls, [("alt+f7", False)])
        calls, _ = self._run(None, default_side="CT")
        self.assertEqual(calls, [("alt+f6", True)])

    def test_winner_from_win_team(self):
        eng = self._engine()
        data = payload(players=[player(1, "CT")], scores=(0, 0))
        data["round"]["win_team"] = "T"
        snap = d.parse_snapshot(data, time.monotonic())
        eng._update_round_winner(snap)
        self.assertEqual(eng._last_round_winner, "T")

    def test_winner_from_score_delta(self):
        eng = self._engine()
        eng._last_map_name = "de_mirage"
        eng._last_scores = (3, 2)
        snap = d.parse_snapshot(
            payload(players=[player(1, "CT")], scores=(3, 3)),
            time.monotonic(),
        )
        eng._update_round_winner(snap)
        self.assertEqual(eng._last_round_winner, "T")


class TestDeathFallback(unittest.TestCase):
    def _snap(self, observed_hp=0, enemy_pos=(100, 0, 0)):
        data = payload(
            players=[
                player(1, "CT", hp=observed_hp),
                player(2, "T", hp=100, pos=enemy_pos),
            ],
            player_slot=1,
        )
        snap = d.parse_snapshot(data, time.monotonic())
        assert snap is not None
        return snap

    def test_switch_to_nearest_enemy_after_observed_death(self):
        cfg = dict(d.DEFAULT_CONFIG)
        eng = d.DirectorEngine(cfg, d.LogBus())
        pressed = []
        eng.input.press_slot = lambda slot: pressed.append(slot) or True  # type: ignore[assignment]
        snap = self._snap(observed_hp=0, enemy_pos=(100, 0, 0))
        eng._decide_and_switch(snap, time.monotonic(), {})
        self.assertEqual(pressed, [2])

    def test_no_switch_when_observed_alive(self):
        cfg = dict(d.DEFAULT_CONFIG)
        eng = d.DirectorEngine(cfg, d.LogBus())
        pressed = []
        eng.input.press_slot = lambda slot: pressed.append(slot) or True  # type: ignore[assignment]
        snap = self._snap(observed_hp=100, enemy_pos=(4000, 0, 0))
        eng._decide_and_switch(snap, time.monotonic(), {})
        self.assertEqual(pressed, [])

    def test_observer_slot_cleared_after_death_uses_last_seen(self):
        cfg = dict(d.DEFAULT_CONFIG)
        eng = d.DirectorEngine(cfg, d.LogBus())
        eng._last_observed_info = (1, "CT", (0.0, 0.0, 0.0))
        pressed = []
        eng.input.press_slot = lambda slot: pressed.append(slot) or True  # type: ignore[assignment]
        data = payload(
            players=[player(1, "CT", hp=0), player(2, "T", hp=100, pos=(100, 0, 0))],
            player_slot=None,
        )
        snap = d.parse_snapshot(data, time.monotonic())
        eng._decide_and_switch(snap, time.monotonic(), {})
        self.assertEqual(pressed, [2])


class TestVerticalEngagement(unittest.TestCase):
    def test_same_floor_possible(self):
        snap = snapshot(players=[
            player(1, "CT", pos=(0, 0, 0)),
            player(2, "T", pos=(600, 0, 50)),
        ])
        self.assertTrue(d.has_possible_engagement(snap, d.DEFAULT_CONFIG))

    def test_far_or_different_floor_impossible(self):
        snap = snapshot(players=[
            player(1, "CT", pos=(0, 0, 0)),
            player(2, "T", pos=(4000, 0, 0)),
        ])
        self.assertFalse(d.has_possible_engagement(snap, d.DEFAULT_CONFIG))
        snap = snapshot(players=[
            player(1, "CT", pos=(0, 0, 0)),
            player(2, "T", pos=(100, 0, 600)),
        ])
        self.assertFalse(d.has_possible_engagement(snap, d.DEFAULT_CONFIG))


class TestProDirectorModule(unittest.TestCase):
    def _snap(self, phase="live", rnd=1, bomb="", players=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            phase=phase, round_num=rnd, bomb=SimpleNamespace(state=bomb) if bomb else None,
            players=players or {},
        )

    def _p(self, health=100, kills=0, name="p"):
        from types import SimpleNamespace
        return SimpleNamespace(health=health, round_kills=kills, name=name)

    def test_pro_log_filter(self):
        log = d.pro_mod.ProLog()
        log.info("OBS", "connected")
        log.error("EventAction", "failed")
        self.assertEqual(len(log.filtered(category="OBS")), 1)
        self.assertEqual(len(log.filtered(keyword="fail")), 1)
        self.assertEqual(len(log.filtered(level="ERROR")), 1)

    def test_event_detector_round_kill_bomb(self):
        det = d.pro_mod.EventDetector()
        p1 = self._p(kills=0)
        det.poll(self._snap(players={"1": p1}))
        evs = det.poll(self._snap(phase="freezetime", rnd=2, players={"1": p1}))
        self.assertIn("round_started", [e.kind for e in evs])
        p1.round_kills = 1
        evs = det.poll(self._snap(phase="live", rnd=2, bomb="planted", players={"1": p1}))
        kinds = [e.kind for e in evs]
        self.assertIn("kill", kinds)
        self.assertIn("bomb_planted", kinds)

    def test_event_action_executes_actions(self):
        class FakeObs:
            def __init__(self):
                self.calls = []
            def media_action(self, target, action):
                self.calls.append((target, action))
            def switch_scene(self, target):
                self.calls.append((target, "scene"))
        cfg = dict(d.DEFAULT_CONFIG)
        obs = FakeObs()
        eng = d.pro_mod.EventActionEngine(cfg, obs, d.pro_mod.ProLog())
        try:
            eng.execute({"name": "r", "actions": [
                {"type": "play_media", "target": "music"},
                {"type": "stop_media", "target": "music"},
                {"type": "switch_scene", "target": "Game"},
            ]})
            self.assertEqual(obs.calls, [
                ("music", "restart"), ("music", "stop"), ("Game", "scene")])
        finally:
            eng.stop()


class TestDecisionModel(unittest.TestCase):
    def _snap(self, dmg=0):
        data = payload(players=[
            player(1, "CT", round_totaldmg=dmg),
            player(2, "T", pos=(600, 0, 0), fwd=(-1, 0, 0)),
        ])
        snap = d.parse_snapshot(data, time.monotonic())
        assert snap is not None
        return snap

    def test_logger_labels_damage_after_window(self):
        import csv as _csv
        import tempfile
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["decision_label_window"] = 3.0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.csv"
            logger = d.dm.DecisionLogger(cfg, path)
            snap = self._snap(dmg=0)
            cands = d.score_candidates(snap, cfg, None, 10.0)
            logger.record(snap, cands, 1, "switch", "test", None, 10.0)
            logger.poll(self._snap(dmg=60), 13.1)
            with path.open(encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["label"], "1")
            self.assertEqual(rows[0]["label_damage"], "60")

    def test_logger_negative_when_no_damage(self):
        import tempfile
        cfg = dict(d.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.csv"
            logger = d.dm.DecisionLogger(cfg, path)
            snap = self._snap(dmg=0)
            cands = d.score_candidates(snap, cfg, None, 10.0)
            logger.record(snap, cands, 1, "switch", "test", None, 10.0)
            logger.poll(self._snap(dmg=0), 13.1)
            self.assertEqual(logger.rows, 1)
            self.assertEqual(logger.positive, 0)


class TestConfigPersistence(unittest.TestCase):
    def test_roundtrip(self):
        old = os.environ.get("APPDATA")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["APPDATA"] = tmp
                cfg = dict(d.DEFAULT_CONFIG)
                cfg["gsi_port"] = 3123
                cfg["hlae_round_key"] = "f8"
                cfg["deepseek_api_key"] = "sk-test"
                d.save_config(cfg)
                loaded = d.load_config()
                self.assertEqual(loaded["gsi_port"], 3123)
                self.assertEqual(loaded["hlae_round_key"], "f8")
                self.assertEqual(loaded["deepseek_api_key"], "sk-test")
                # 旧配置缺少的键会被默认值补齐
                loaded.pop("switch_margin", None)
                d.save_config(loaded)
                self.assertIn("switch_margin", d.load_config())
        finally:
            if old is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old

    def test_legacy_single_bomb_key_migrates_to_ab(self):
        old = os.environ.get("APPDATA")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["APPDATA"] = tmp
                p = Path(tmp) / "CS2AIDirector" / "config.json"
                p.parent.mkdir(parents=True)
                p.write_text(json.dumps({
                    "hlae_round_key": "alt+f8",
                    "hlae_round_mode": "single",
                    "hlae_bomb_key": "alt+9",
                    "hlae_bomb_mode": "single",
                    "bomb_site_refs": {"de_mirage": {"A": [-1, -2], "B": [3, 4]}},
                }), encoding="utf-8")
                cfg = d.load_config()
                self.assertEqual(cfg["hlae_spawn_ct_key"], "alt+f8")
                self.assertEqual(cfg["hlae_spawn_t_key"], "alt+f8")
                self.assertEqual(cfg["hlae_spawn_ct_mode"], "single")
                self.assertEqual(cfg["hlae_spawn_t_mode"], "single")
                self.assertEqual(cfg["hlae_bomb_a_key"], "alt+9")
                self.assertEqual(cfg["hlae_bomb_b_key"], "alt+9")
                self.assertEqual(cfg["hlae_bomb_a_mode"], "single")
                self.assertEqual(cfg["hlae_bomb_b_mode"], "single")
                # 只覆盖 de_mirage，内置的其他地图仍然保留
                self.assertEqual(cfg["bomb_site_refs"]["de_mirage"]["A"], [-1, -2])
                self.assertIn("de_inferno", cfg["bomb_site_refs"])
        finally:
            if old is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old


class TestInputSafety(unittest.TestCase):
    def test_self_press_suppression(self):
        cfg = dict(d.DEFAULT_CONFIG)
        ctl = d.InputController(cfg, d.LogBus())
        self.assertFalse(ctl.is_self_press("3"))
        ctl.mark_self_press("3")
        self.assertTrue(ctl.is_self_press("3"))
        self.assertFalse(ctl.is_self_press("4"))

    def test_hotkey_modifier_hold_default_supports_mulnx(self):
        # MulNX InputSystem uses a 200ms combo buffer; Alt must still be down.
        self.assertGreaterEqual(
            d.DEFAULT_CONFIG.get("hotkey_modifier_hold_after", 0), 0.20)

    def test_hotkey_holds_alt_after_last_press(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["hotkey_modifier_hold_after"] = 0.30
        cfg["hotkey_modifier_release_delay"] = 0.05
        ctl = d.InputController(cfg, d.LogBus())
        ctl.ensure_foreground = lambda: True  # type: ignore[assignment]

        class FakeKB:
            def press(self, key):
                pass

            def release(self, key):
                pass

            def press_and_release(self, hk):
                pass

        old = d.keyboard
        d.keyboard = FakeKB()  # type: ignore[assignment]
        try:
            with mock.patch.object(d.time, "sleep") as sleep:
                self.assertTrue(ctl.send_hotkey("alt+N", double=False))
                sleep.assert_any_call(0.30)
        finally:
            d.keyboard = old

    def test_hotkey_validation(self):
        self.assertTrue(d.validate_hotkey("f6"))
        self.assertTrue(d.validate_hotkey("ctrl+shift+f9"))
        self.assertFalse(d.validate_hotkey(""))
        self.assertFalse(d.validate_hotkey("ctrl+"))

    def test_selectable_hotkey_parse(self):
        self.assertEqual(d.parse_selectable_hotkey("alt+N"), ("ALT", "N"))
        self.assertEqual(d.parse_selectable_hotkey("ctrl+7"), ("CTRL", "7"))
        self.assertEqual(d.parse_selectable_hotkey("shift+a"), ("SHIFT", "A"))
        self.assertEqual(d.parse_selectable_hotkey("alt+f6"), ("ALT", ""))
        self.assertEqual(d.parse_selectable_hotkey("f6"), ("", ""))

    def test_manual_handler_exception_is_logged(self):
        bus = d.LogBus()

        def boom(_digit):
            raise RuntimeError("boom")

        watcher = d.ManualKeyWatcher(boom, lambda d: False, bus)
        watcher._modifier_down = lambda: False  # type: ignore[assignment]
        watcher._handle("3")
        logs = [msg for _, msg in list(bus.queue.queue)]
        self.assertTrue(any("手动接管处理异常" in m for m in logs))

    def test_alt_digit_is_not_manual_takeover(self):
        """alt+1 是运镜宏，不是手动接管；裸 1 才算。"""
        bus = d.LogBus()
        hits = []
        watcher = d.ManualKeyWatcher(hits.append, lambda d: False, bus)
        old = d.get_async_key_state
        try:
            d.get_async_key_state = lambda vk: vk == 0x12  # Alt 按下
            watcher._handle("1")
            self.assertEqual(hits, [])
            d.get_async_key_state = lambda vk: False
            watcher._handle("1")
            self.assertEqual(hits, ["1"])
        finally:
            d.get_async_key_state = old

    def test_send_hotkey_single_and_double(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["dry_run"] = False
        cfg["hotkey_double_interval"] = 0.001
        cfg["hotkey_press_hold"] = 0.001
        cfg["hotkey_modifier_hold_after"] = 0.001
        ctl = d.InputController(cfg, d.LogBus())
        ctl.ensure_foreground = lambda: True  # type: ignore[assignment]
        calls = []

        class FakeKB:
            def press(self, key):
                calls.append(("down", key))

            def release(self, key):
                calls.append(("up", key))

            def press_and_release(self, hk):
                calls.append(("combo", hk))

        old = d.keyboard
        d.keyboard = FakeKB()  # type: ignore[assignment]
        try:
            self.assertTrue(ctl.send_hotkey("alt+1"))
            self.assertEqual(calls, [
                ("down", "alt"), ("down", "1"), ("up", "1"), ("up", "alt"),
            ])
            calls.clear()
            # 双击必须“按住 Alt 连点两次”，而不是两次完整 alt+2
            self.assertTrue(ctl.send_hotkey("alt+2", double=True))
            self.assertEqual(calls, [
                ("down", "alt"),
                ("down", "2"), ("up", "2"),
                ("down", "2"), ("up", "2"),
                ("up", "alt"),
            ])
        finally:
            d.keyboard = old

    def test_hook_ignores_simulated_key(self):
        """模拟按键的钩子事件不能触发手动接管；用户按其他数字仍然生效。"""
        bus = d.LogBus()
        ctl = d.InputController(dict(d.DEFAULT_CONFIG), bus)
        hits = []
        watcher = d.ManualKeyWatcher(hits.append, ctl.is_self_press, bus)
        watcher._modifier_down = lambda: False  # type: ignore[assignment]

        class Event:
            event_type = "down"

            def __init__(self, scan_code, name):
                self.scan_code = scan_code
                self.name = name

        ctl.mark_self_press("3")
        watcher._on_event(Event(0x04, "3"))   # 顶排 3
        self.assertEqual(hits, [])
        watcher._on_event(Event(0x05, "4"))   # 顶排 4
        self.assertEqual(hits, ["4"])


class _FakePD:
    def __init__(self):
        self.keys: list[str] = []

    def keyDown(self, key):
        self.keys.append(key)

    def keyUp(self, key):
        self.keys.append(key)


class TestSlotKeyMapping(unittest.TestCase):
    """GSI observer_slot 与观察者数字键的实测映射：键 N → slot (N-1) mod 10。"""

    def test_slot_to_key_examples(self):
        self.assertEqual(d.slot_to_key(0), "1")
        self.assertEqual(d.slot_to_key(1), "2")
        self.assertEqual(d.slot_to_key(3), "4")
        self.assertEqual(d.slot_to_key(4), "5")
        self.assertEqual(d.slot_to_key(9), "0")

    def test_key_to_slot_examples(self):
        self.assertEqual(d.key_to_slot(1), 0)
        self.assertEqual(d.key_to_slot(2), 1)
        self.assertEqual(d.key_to_slot(9), 8)
        self.assertEqual(d.key_to_slot(0), 9)

    def test_roundtrip_all_slots(self):
        for slot in range(10):
            key = d.slot_to_key(slot)
            self.assertIsNotNone(key)
            self.assertEqual(d.key_to_slot(key), slot)
        self.assertIsNone(d.slot_to_key(-1))
        self.assertIsNone(d.slot_to_key(10))
        self.assertIsNone(d.key_to_slot(-1))
        self.assertIsNone(d.key_to_slot(10))

    def test_manual_key_is_converted_for_timeline(self):
        class FakeTimeline:
            def __init__(self):
                self.calls = []

            def note_manual(self, *args):
                self.calls.append(args)

        eng = d.DirectorEngine(dict(d.DEFAULT_CONFIG), d.LogBus())
        fake = FakeTimeline()
        eng.timeline = fake  # type: ignore[assignment]
        eng._last_observed_slot = 4
        eng._on_manual_key("0")
        self.assertEqual(fake.calls[0][0], 9)   # 键 0 → GSI slot 9
        self.assertEqual(fake.calls[0][1], 4)   # from_slot 仍是 GSI slot 4


@unittest.skipUnless(d.HAS_PYDIRECTINPUT, "pydirectinput 未安装")
class TestInputIdentity(unittest.TestCase):
    """slot→按键必须用实测映射，且按键前必须通过焦点检查。"""

    def setUp(self):
        self.cfg = dict(d.DEFAULT_CONFIG)
        self.cfg["dry_run"] = False
        self.ctl = d.InputController(self.cfg, d.LogBus())
        self.old_pd = d.pydirectinput
        self.old_kb = d.keyboard
        self.fake = _FakePD()
        d.pydirectinput = self.fake  # type: ignore[assignment]
        d.keyboard = None  # 强制走单键分支
        self.ctl.ensure_foreground = lambda: True  # type: ignore[assignment]

    def tearDown(self):
        d.pydirectinput = self.old_pd
        d.keyboard = self.old_kb

    def test_slot_to_key_mapping_0_to_9(self):
        expected = {0: "1", 1: "2", 2: "3", 3: "4", 4: "5",
                    5: "6", 6: "7", 7: "8", 8: "9", 9: "0"}
        for slot, key in expected.items():
            self.fake.keys.clear()
            self.assertTrue(self.ctl.press_slot(slot), slot)
            self.assertEqual(self.fake.keys, [key, key])
            self.assertTrue(self.ctl.is_self_press(key))

    def test_out_of_range_slots_rejected(self):
        for slot in (-1, 10, 99):
            self.fake.keys.clear()
            self.assertFalse(self.ctl.press_slot(slot))
            self.assertEqual(self.fake.keys, [])

    def test_no_key_without_focus(self):
        self.ctl.ensure_foreground = lambda: False  # type: ignore[assignment]
        self.fake.keys.clear()
        self.assertFalse(self.ctl.press_slot(3))
        self.assertEqual(self.fake.keys, [])

    def test_hotkey_also_requires_focus(self):
        calls = []

        class FakeKB:
            def press(self, key):
                calls.append(("down", key))

            def release(self, key):
                calls.append(("up", key))

            def press_and_release(self, hk):
                calls.append(("combo", hk))

        d.keyboard = FakeKB()  # type: ignore[assignment]
        self.assertTrue(self.ctl.send_hotkey("f6"))
        self.assertEqual(calls, [("down", "f6"), ("up", "f6")])
        self.ctl.ensure_foreground = lambda: False  # type: ignore[assignment]
        self.assertFalse(self.ctl.send_hotkey("f6"))
        self.assertEqual(len(calls), 2)


class TestEngineSafety(unittest.TestCase):
    def _snap(self, age=0.0):
        data = payload(players=[
            player(1, "CT", name="A"),
            player(2, "T", pos=(600, 0, 0), fwd=(-1, 0, 0), hp=25),
        ])
        snap = d.parse_snapshot(data, time.monotonic() - age)
        assert snap is not None
        return snap

    def test_stale_snapshot_skips_decision(self):
        eng = d.DirectorEngine(dict(d.DEFAULT_CONFIG), d.LogBus())
        eng.store.update(self._snap(age=10.0))
        called = []
        eng._decide_and_switch = lambda *a, **k: called.append(1)  # type: ignore[assignment]
        eng._tick()
        self.assertEqual(eng.status()["gsi"], "stale")
        self.assertEqual(called, [])

    def test_manual_pause_blocks_switch(self):
        eng = d.DirectorEngine(dict(d.DEFAULT_CONFIG), d.LogBus())
        snap = self._snap()
        eng.store.update(snap)
        eng._manual_until = time.monotonic() + 10.0
        pressed = []
        eng.input.press_slot = lambda slot: pressed.append(slot) or True  # type: ignore[assignment]
        status = {}
        eng._decide_and_switch(snap, time.monotonic(), status)
        self.assertEqual(pressed, [])
        self.assertIn("手动接管", status["reason"])

    def test_loop_survives_tick_exception(self):
        bus = d.LogBus()
        eng = d.DirectorEngine(dict(d.DEFAULT_CONFIG), bus)
        count = []

        def fake_tick():
            count.append(1)
            if len(count) == 1:
                raise RuntimeError("boom")
            if len(count) >= 3:
                eng._stop.set()

        eng._tick = fake_tick  # type: ignore[assignment]
        thread = threading.Thread(target=eng._loop, daemon=True)
        thread.start()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(count), 3)
        logs = [msg for _, msg in list(bus.queue.queue)]
        self.assertTrue(any("异常" in m for m in logs))


class TestStoryTracker(unittest.TestCase):
    """回合故事状态：阶段/经济/残局/下包/比分/赛点。"""

    def setUp(self):
        self.cfg = dict(d.DEFAULT_CONFIG)
        self.tracker = st.StoryTracker(self.cfg)

    def test_economy_clutch_post_plant_match_point(self):
        ct = [player(i, "CT", weapon="weapon_ak47", money=5000)
              for i in range(1, 6)]
        t = [player(i, "T", weapon="weapon_glock", typ="Pistol", armor=0, money=800)
             for i in range(6, 11)]
        snap = snapshot(players=ct + t, phase="freezetime", round_no=22,
                        scores=(12, 10), player_slot=1)
        state = self.tracker.update(snap, 100.0)
        self.assertEqual(state.economy_ct, "full")
        self.assertEqual(state.economy_t, "eco")
        self.assertTrue(state.match_point)
        self.assertEqual(state.match_point_team, "CT")
        self.assertEqual(state.score_ct, 12)
        self.assertEqual(state.score_t, 10)
        self.assertIn("赛点", state.story)

        # live：CT 只剩 1 人、T 剩 2 人 → CT 1v2 残局
        snap = snapshot(players=[
            player(1, "CT", weapon="weapon_ak47"),
            player(2, "T"), player(3, "T"),
        ], phase="live", round_no=22, scores=(12, 10), player_slot=1)
        state = self.tracker.update(snap, 110.0)
        self.assertEqual(state.clutch, "1v2")
        self.assertEqual(state.clutch_side, "CT")
        self.assertEqual(state.phase, "clutch")
        self.assertEqual(state.alive_ct, 1)
        self.assertEqual(state.alive_t, 2)

        # 下包后：阶段优先 post_plant，同时保留残局字段
        snap = snapshot(players=[
            player(1, "CT", weapon="weapon_ak47"),
            player(2, "T"), player(3, "T"),
        ], phase="live", round_no=22, scores=(12, 10), player_slot=1,
            bomb={"state": "planted", "countdown": "12.5",
                  "position": "[100, 200, 0]"})
        state = self.tracker.update(snap, 115.0, bomb_site="A")
        self.assertEqual(state.phase, "post_plant")
        self.assertTrue(state.bomb_planted)
        self.assertEqual(state.bomb_site, "A")
        self.assertAlmostEqual(state.bomb_time_left, 12.5)
        self.assertEqual(state.clutch, "1v2")

        # 回合结束：包点/残局记忆保留，指标行不会丢掉“下包 A / 曾 1v2”
        snap = snapshot(players=[
            player(1, "CT", weapon="weapon_ak47"),
            player(2, "T"), player(3, "T"),
        ], phase="over", round_no=22, scores=(13, 10), player_slot=1,
            bomb={"state": "exploded", "countdown": "0",
                  "position": "[100, 200, 0]"})
        state = self.tracker.update(snap, 120.0, bomb_site="A")
        self.assertEqual(state.bomb_site, "A")
        self.assertEqual(state.clutch, "1v2")
        self.assertEqual(state.phase, "round_over")
        self.assertTrue(state.match_point)

    def test_phase_transitions_and_events(self):
        snap = snapshot(players=[
            player(1, "CT"), player(2, "CT"),
            player(3, "T", pos=(600, 0, 0)), player(4, "T", pos=(900, 0, 0)),
        ], phase="live", round_no=1, player_slot=1)
        state = self.tracker.update(snap, 100.0)
        self.assertEqual(state.phase, "opening")
        self.assertIn("回合开始", state.events)
        # 12 秒后进入地图控制
        state = self.tracker.update(snap, 120.0)
        self.assertEqual(state.phase, "control")
        self.assertTrue(any(e.startswith("阶段") for e in state.events))


class TestShotTimeline(unittest.TestCase):
    """镜头时间线 + 六项专业指标。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(d.DEFAULT_CONFIG)
        self.cfg["metric_kill_lookback"] = 2.5
        self.cfg["metric_prefight_lookback"] = 2.5
        self.cfg["metric_engagement_gap"] = 3.0
        self.cfg["metric_min_live_seconds"] = 5.0
        self.story = st.StoryTracker(self.cfg)
        self.timeline = st.ShotTimeline(self.cfg, base_dir=Path(self.tmp.name))

    def tearDown(self):
        self.timeline.close(999.0, "测试结束")
        self.tmp.cleanup()

    def _tick(self, players, phase, round_no, slot, now, scores=(0, 0), bomb=None):
        snap = snapshot(players=players, phase=phase, round_no=round_no,
                        scores=scores, player_slot=slot, bomb=bomb)
        state = self.story.update(snap, now)
        self.timeline.tick(snap, state, now)
        return snap, state

    def test_timeline_kill_coverage_corpse_cut_rate_manual(self):
        players = [player(1, "CT"), player(2, "T", pos=(600, 0, 0)),
                   player(3, "T", pos=(800, 0, 0))]
        self._tick(players, "live", 1, 1, 100.0)
        self.assertEqual(self.timeline.current.target_slot, 1)

        self.timeline.expect_switch(2, "切到交火点", "rule", 102.0)
        self._tick(players, "live", 1, 2, 103.0)
        self.assertEqual(self.timeline.current.target_slot, 2)
        self.assertEqual(self.timeline.current.reason, "切到交火点")

        # T2 在 104.5 首次打中 CT1，105 击杀；当前镜头在 T2。
        self.timeline.note_damage([2], [1], 104.5, 100)
        self.timeline.note_kill([2], [1], 105.0)
        self.timeline.note_death(1, 105.0)

        # 观察者切到已阵亡的 CT1，停留 0.5s（尸体时间）。
        players_dead = [player(1, "CT", hp=0), player(2, "T", pos=(600, 0, 0)),
                        player(3, "T", pos=(800, 0, 0))]
        self._tick(players_dead, "live", 1, 1, 106.0)
        self.assertEqual(self.timeline.current.target_slot, 1)

        # 人工按 3 接管：GSI 在 106.5 确认。
        self.timeline.note_manual(3, 1, 106.2, 10.0)
        players_manual = [player(1, "CT", hp=0), player(2, "T", pos=(600, 0, 0)),
                          player(3, "T", pos=(800, 0, 0))]
        self._tick(players_manual, "live", 1, 3, 106.5)
        self.assertEqual(self.timeline.current.target_slot, 3)
        self.assertTrue(self.timeline.current.manual)

        # 回合结束 → 计算并落盘
        self._tick(players_manual, "over", 1, 3, 108.0)
        m = self.timeline._last_round_metrics
        self.assertIsNotNone(m)
        self.assertEqual(m["kills"], 1)
        self.assertAlmostEqual(m["kill_coverage"], 1.0)
        self.assertEqual(m["first_damages"], 1)
        self.assertAlmostEqual(m["pre_fight_coverage"], 1.0)
        self.assertAlmostEqual(m["corpse_time"], 0.5)
        self.assertAlmostEqual(m["shot_duration_mean"], 2.0)
        self.assertEqual(m["manual_takeover"], 1)
        self.assertEqual(m["manual_confirmed"], 1)
        self.assertAlmostEqual(m["live_seconds"], 8.0)
        # 4 个镜头 = 3 次切换；8 秒 live → 22.5 次/分钟
        self.assertAlmostEqual(m["cut_rate"], 22.5)

        self.assertTrue(self.timeline.shot_path.is_file())
        self.assertTrue(self.timeline.round_path.is_file())
        self.assertTrue(self.timeline.summary_path.is_file())
        with self.timeline.shot_path.open("r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["manual"], "1")
        with self.timeline.round_path.open("r", newline="", encoding="utf-8-sig") as f:
            rounds = list(csv.DictReader(f))
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["kill_coverage"], "1.0000")
        self.assertEqual(rounds[0]["phase"], "round_over")
        summary = json.loads(self.timeline.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["session"]["rounds"], 1)
        self.assertAlmostEqual(summary["session"]["kill_coverage"], 1.0)

        # 重启后从 round_metrics.csv 恢复累计指标
        self.timeline.close(109.0, "测试结束")
        tl2 = st.ShotTimeline(self.cfg, base_dir=Path(self.tmp.name))
        self.assertEqual(tl2.session_metrics()["rounds"], 1)
        self.assertEqual(tl2.session_metrics()["kills"], 1)
        self.assertAlmostEqual(tl2.session_metrics()["kill_coverage"], 1.0)

    def test_kill_coverage_zero_when_watching_other_player(self):
        players = [player(1, "CT"), player(2, "T", pos=(600, 0, 0))]
        self._tick(players, "freezetime", 2, 1, 200.0)
        # freezetime 不抢在出生点运镜前记 0 秒 POV
        self.assertIsNone(self.timeline.current)
        self._tick(players, "live", 2, 1, 201.0)
        self.assertEqual(self.timeline.current.target_slot, 1)
        # 击杀者是 T2，但镜头一直在 CT1。
        self.timeline.note_damage([2], [1], 202.0, 100)
        self.timeline.note_kill([2], [1], 202.5)
        self.timeline.note_death(1, 202.5)
        players_dead = [player(1, "CT", hp=0), player(2, "T", pos=(600, 0, 0))]
        self._tick(players_dead, "over", 2, 1, 203.0)
        m = self.timeline._last_round_metrics
        self.assertEqual(m["kill_coverage"], 0.0)
        # 首次伤害的当事人包含 CT1（受害者），所以交火前覆盖仍为 1。
        self.assertAlmostEqual(m["pre_fight_coverage"], 1.0)

    def test_disabled_does_not_create_files(self):
        cfg = dict(self.cfg)
        cfg["shot_timeline_enabled"] = False
        base = Path(self.tmp.name) / "disabled"
        base.mkdir()
        tl = st.ShotTimeline(cfg, base_dir=base)
        snap = snapshot(players=[player(1, "CT")], phase="live", round_no=3)
        state = st.StoryTracker(cfg).update(snap, 300.0)
        tl.tick(snap, state, 300.0)
        self.assertFalse(tl.enabled)
        self.assertEqual(list(base.iterdir()), [])


class TestEngineTimelineIntegration(unittest.TestCase):
    """DirectorEngine 只做编排：故事/时间线不影响评分与按键安全。"""

    def test_tick_updates_story_timeline_and_damage_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(d.DEFAULT_CONFIG)
            cfg["dry_run"] = True
            cfg["shot_timeline_enabled"] = True
            timeline = st.ShotTimeline(cfg, base_dir=Path(tmp))
            eng = d.DirectorEngine(cfg, d.LogBus(), timeline=timeline)
            snap = snapshot(players=[
                player(1, "CT"), player(2, "T", pos=(600, 0, 0)),
            ], phase="live", round_no=1, player_slot=1)
            eng.store.update(snap)
            eng._tick()
            status = eng.status()
            self.assertEqual(status["gsi"], "ok")
            self.assertTrue(status.get("story_text"))
            self.assertEqual(status["current_shot"]["target_slot"], 1)
            self.assertIn("kill_coverage", status["metrics"])

            snap2 = snapshot(players=[
                player(1, "CT", hp=50),
                player(2, "T", pos=(600, 0, 0), round_totaldmg=50),
            ], phase="live", round_no=1, player_slot=1)
            eng.store.update(snap2)
            eng._tick()
            self.assertTrue(any(ev["attackers"] == [2] for ev in timeline._damages))
            self.assertTrue(timeline.shot_path.is_file())
            timeline.close(time.monotonic())
            eng.timeline = None  # 避免 eng 析构/后续状态影响


class TestDirectorStability(unittest.TestCase):
    """防碎切：交火锁定、最短停留、10 秒切率上限。"""

    def _engine(self):
        cfg = dict(d.DEFAULT_CONFIG)
        cfg["fight_lock_enabled"] = True
        cfg["min_shot_duration"] = 2.0
        cfg["max_cuts_per_10s"] = 3
        eng = d.DirectorEngine(cfg, d.LogBus())
        pressed = []
        eng.input.press_slot = lambda slot: pressed.append(slot) or True  # type: ignore[assignment]
        return eng, pressed

    def _snap(self, enemy_pos=(600, 0, 0), enemy_fwd=(-1, 0, 0)):
        return snapshot(players=[
            player(1, "CT", pos=(0, 0, 0), fwd=(1, 0, 0)),
            player(2, "T", pos=enemy_pos, fwd=enemy_fwd),
        ], phase="live", round_no=1, player_slot=1)

    def test_fight_lock_blocks_switch_away(self):
        eng, pressed = self._engine()
        snap = self._snap()
        now = time.monotonic()
        # 双方最近都掉过血，说明正在对枪。
        eng._damage_at["sid1"] = now
        eng._damage_at["sid2"] = now
        status = {}
        eng._decide_and_switch(snap, now, status)
        self.assertEqual(pressed, [])
        self.assertIn("交火", status["reason"])

    def test_min_shot_duration_blocks_immediate_switch(self):
        eng, pressed = self._engine()
        snap = self._snap(enemy_pos=(1200, 0, 0), enemy_fwd=(0, 1, 0))
        now = time.monotonic()
        eng._last_switch_at = now - 1.0
        eng._last_switch_slot = 1
        status = {}
        eng._decide_and_switch(snap, now, status)
        self.assertEqual(pressed, [])
        self.assertIn("最短停留", status["reason"])

    def test_max_cuts_per_10s_blocks_switch(self):
        eng, pressed = self._engine()
        snap = self._snap(enemy_pos=(1200, 0, 0), enemy_fwd=(0, 1, 0))
        now = time.monotonic()
        eng._last_switch_at = now - 3.0
        eng._last_switch_slot = 1
        eng._cut_times = deque([now - 1.0, now - 2.0, now - 3.0])
        status = {}
        eng._decide_and_switch(snap, now, status)
        self.assertEqual(pressed, [])
        self.assertIn("上限", status["reason"])


class TestGSIConcurrency(unittest.TestCase):
    def test_concurrent_posts_are_consistent(self):
        store = d.SnapshotStore()
        server = d.GSIServer("127.0.0.1", 0, store, lambda m: None)
        server.start()
        try:
            port = server._httpd.server_address[1]  # type: ignore[union-attr]
            body = json.dumps(payload(players=[
                player(1, "CT"),
                player(2, "T", pos=(600, 0, 0), fwd=(-1, 0, 0)),
            ])).encode()

            def post(_i):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/", data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                codes = list(pool.map(post, range(30)))
            self.assertTrue(all(c == 200 for c in codes))
            self.assertEqual(store.stats()[0], 30)
            self.assertEqual(len(store.get().players), 2)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
