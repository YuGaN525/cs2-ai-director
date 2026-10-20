#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cs2_ai_director 的核心逻辑测试（不启动 GUI、不发送真实按键）。"""

import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import cs2_ai_director as d


def player(slot, team, hp=100, pos=(0, 0, 0), fwd=(1, 0, 0),
           weapon="weapon_ak47", typ="Rifle", ammo=30, armor=100,
           flashed=0, defusing=False, name=None):
    return {
        "name": name or f"P{slot}",
        "observer_slot": slot,
        "team": team,
        "position": f"{pos[0]}, {pos[1]}, {pos[2]}",
        "forward": f"{fwd[0]}, {fwd[1]}, {fwd[2]}",
        "state": {
            "health": hp, "armor": armor, "helmet": True,
            "flashed": flashed, "defusing": defusing,
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

    def test_install_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "game" / "csgo" / "cfg"
            cfg_dir.mkdir(parents=True)
            exe = Path(tmp) / "game" / "cs2.exe"
            exe.write_bytes(b"")
            self.assertEqual(d.derive_cfg_dir(str(exe)), cfg_dir)
            target = d.install_gsi_config(cfg_dir, 3000, overwrite=True)
            self.assertTrue(target.is_file())
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["data"]["allplayers_position"], 1)
            target2 = d.install_gsi_config(cfg_dir, 3002, overwrite=True)
            self.assertEqual(json.loads(target2.read_text(encoding="utf-8"))["uri"],
                             "http://127.0.0.1:3002")
            self.assertTrue(list(cfg_dir.glob("*.bak_*")))
            with self.assertRaises(FileExistsError):
                d.install_gsi_config(cfg_dir, 3003, overwrite=False)

    def test_derive_cfg_dir_rejects_other_exe(self):
        self.assertIsNone(d.derive_cfg_dir("C:/Windows/notepad.exe"))
        self.assertIsNone(d.derive_cfg_dir(""))


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


class TestInputSafety(unittest.TestCase):
    def test_self_press_suppression(self):
        cfg = dict(d.DEFAULT_CONFIG)
        ctl = d.InputController(cfg, d.LogBus())
        self.assertFalse(ctl.is_self_press("3"))
        ctl.mark_self_press("3")
        self.assertTrue(ctl.is_self_press("3"))
        self.assertFalse(ctl.is_self_press("4"))

    def test_hotkey_validation(self):
        self.assertTrue(d.validate_hotkey("f6"))
        self.assertTrue(d.validate_hotkey("ctrl+shift+f9"))
        self.assertFalse(d.validate_hotkey(""))
        self.assertFalse(d.validate_hotkey("ctrl+"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
