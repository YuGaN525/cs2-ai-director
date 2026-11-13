"""Regression coverage for issues observed with xinghuo_dust2 on local Demo."""
import unittest
from unittest import mock

import cs2_ai_director as d
from test_cs2_ai_director import payload, player


class PhysicalHotkeyTests(unittest.TestCase):
    def setUp(self):
        for name, value in [('get_foreground_hwnd', 1), ('game_window_identity', (1, 10, 'c:/game/cs2.exe')), ('modifier_conflict', False)]:
            patch = mock.patch.object(d, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_uppercase_config_sends_physical_lowercase_without_shift(self):
        ctl = d.InputController(dict(d.DEFAULT_CONFIG, dry_run=False), d.LogBus())
        ctl.ensure_foreground = lambda: True
        backend = mock.Mock()
        with mock.patch.object(d, "keyboard", backend), \
                mock.patch.object(d, "HAS_KEYBOARD", True), \
                mock.patch.object(d.time, "sleep"):
            self.assertTrue(ctl.send_hotkey("ALT+M", double=True))
        self.assertEqual(backend.method_calls, [
            mock.call.press("alt"), mock.call.press("m"), mock.call.release("m"),
            mock.call.press("m"), mock.call.release("m"), mock.call.release("alt")])
        self.assertEqual(d.parse_selectable_hotkey("alt+M"), ("ALT", "M"))

    @unittest.skipUnless(d.IS_WINDOWS and d.HAS_KEYBOARD, "Windows keyboard mapping")
    def test_installed_windows_backend_resolves_m_to_letter_vk(self):
        from keyboard import _winkeyboard
        _, keys = d.split_hotkey("alt+M")
        scan = d.keyboard.key_to_scan_codes(keys[0])[0]
        self.assertEqual(_winkeyboard.scan_code_to_vk[scan], 0x4D)
        # Physical-modifier ownership cleanup relies on generic aliases using
        # the left key, as the bundled Windows keyboard backend does.
        for name, expected in [('alt', 0xA4), ('ctrl', 0xA2), ('shift', 0xA0), ('windows', 0x5B)]:
            first = d.keyboard.key_to_scan_codes(name)[0]
            self.assertEqual(_winkeyboard.scan_code_to_vk[first], expected)


class CinematicHandoffTests(unittest.TestCase):
    def setUp(self):
        self.engine = d.DirectorEngine(dict(
            d.DEFAULT_CONFIG, bomb_cam_policy="always", hlae_spawn_ct_key="alt+M",
            hlae_spawn_t_key="alt+N", hlae_bomb_a_key="alt+1", hlae_bomb_b_key="alt+6"), d.LogBus())
        self.send = self.engine.input.send_hotkey = mock.Mock(return_value=True)
        self.press = self.engine.input.press_slot = mock.Mock(return_value=True)
        self.engine.timeline = mock.Mock()

    def snap(self, phase="freezetime", rnd=1, bomb=None, players=None):
        return d.parse_snapshot(payload(
            players=players if players is not None else [player(1, "CT", hp=0), player(2, "T")],
            phase=phase, round_no=rnd, player_slot=1, bomb=bomb), 100.0)

    def start_spawn(self):
        self.engine._handle_triggers(self.snap(), 100.0)
        self.assertIsNotNone(self.engine._cinematic_request)
        self.send.reset_mock()

    def test_spawn_waits_then_stops_once_before_resuming_pov(self):
        self.start_spawn()
        self.engine._decide_and_switch(self.snap(), 101.0, {})
        self.send.assert_not_called()
        self.press.assert_not_called()
        order = mock.Mock()
        order.attach_mock(self.send, "hotkey")
        order.attach_mock(self.press, "pov")
        self.engine._decide_and_switch(self.snap("live"), 102.0, {})
        self.assertEqual(order.mock_calls[:2], [mock.call.hotkey("alt+p", double=False), mock.call.pov(2)])
        self.engine._decide_and_switch(self.snap("live"), 103.0, {})
        self.send.assert_called_once_with("alt+p", double=False)

    def test_failed_release_blocks_pov_and_retries_at_most_once_per_second(self):
        self.start_spawn()
        self.send.side_effect = [False, True]
        self.engine._decide_and_switch(self.snap("live"), 102.0, {})
        self.engine._decide_and_switch(self.snap("live"), 102.1, {})
        self.assertEqual(self.send.call_count, 1)
        self.press.assert_not_called()
        self.engine.timeline.end_cinematic.assert_not_called()
        self.engine._decide_and_switch(self.snap("live"), 103.0, {})
        self.press.assert_called_once_with(2)

    def test_manual_takeover_releases_owned_macro_and_blocks_new_macro(self):
        self.start_spawn()
        with mock.patch.object(d.time, "monotonic", return_value=101.0):
            self.engine._on_manual_key("2")
        self.send.assert_not_called()  # The input hook never blocks sending a chord.
        self.engine._handle_triggers(self.snap(rnd=2), 102.0)
        self.engine._decide_and_switch(self.snap("live", rnd=2), 102.0, {})
        self.send.assert_called_once_with("alt+p", double=False)
        self.press.assert_not_called()

    def test_unowned_preview_does_not_receive_stop_hotkey(self):
        self.engine._decide_and_switch(self.snap("live"), 102.0, {})
        self.send.assert_not_called()

    def test_bomb_grace_releases_even_when_player_data_is_missing(self):
        snap = self.snap("live", bomb={"state": "planted", "countdown": "6.0", "position": "-600,-2200,0"})
        self.engine._handle_triggers(snap, 100.0)
        self.send.reset_mock()
        self.engine._decide_and_switch(snap, 101.0, {})
        self.send.assert_not_called()
        self.engine._decide_and_switch(self.snap("live", players=[]), 101.5, {})
        self.send.assert_called_once_with("alt+p", double=False)
        self.assertEqual(self.engine._bomb_cam_until, 0.0)
        self.press.assert_not_called()

    def test_new_round_releases_old_macro_before_starting_new_one(self):
        self.start_spawn()
        self.engine._handle_triggers(self.snap(rnd=2), 102.0)
        self.assertEqual(self.send.call_args_list, [
            mock.call("alt+p", double=False),
            mock.call(self.engine.cfg["hlae_spawn_ct_key"], double=True, cancel_event=mock.ANY)])

    def test_cancel_or_revoked_authorization_never_bypassed_for_handoff(self):
        self.start_spawn()
        self.engine.input.send_hotkey = d.InputController.send_hotkey.__get__(self.engine.input)
        self.engine.input.ensure_foreground = mock.Mock(return_value=True)
        self.engine._stop.set()
        with mock.patch.object(d, "keyboard") as backend:
            self.engine._decide_and_switch(self.snap("live"), 102.0, {})
            backend.press.assert_not_called()
        self.engine._stop.clear()
        gate = self.engine.input.authorization_gate = mock.Mock()
        gate.allowed.return_value = False
        with mock.patch.object(d, "keyboard") as backend:
            self.engine._decide_and_switch(self.snap("live"), 103.0, {})
            backend.press.assert_not_called()
        self.press.assert_not_called()


if __name__ == "__main__":
    unittest.main()
