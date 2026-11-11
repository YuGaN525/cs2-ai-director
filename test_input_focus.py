"""DIR-FB: pure fake backend; never installs hooks or sends real input."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from director_adapters.input import InputControllerAdapter
from director_core.config import DEFAULT_CONFIG


class Rig:
    def __init__(self, keyboard=True):
        self.now = 100.0
        self.foreground = 1
        self.pid = 10
        self.events = []
        self.physical = set()
        self.after_sleep = lambda: None
        self.after_identity = lambda: None
        self.allowed = True
        self.activate = mock.Mock()
        self.log = mock.Mock()
        runtime = SimpleNamespace(
            threading=threading, time=SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep),
            HAS_PYDIRECTINPUT=True, HAS_KEYBOARD=keyboard,
            get_foreground_hwnd=lambda: self.foreground,
            game_window_identity=self.identity,
            modifier_conflict=lambda owned: bool(self.physical - owned),
            find_cs2_window=lambda cfg: SimpleNamespace(hwnd=1), is_window=lambda hwnd: True,
            activate_window=self.activate, slot_to_key=lambda slot: str((slot+1) % 10),
            split_hotkey=lambda h: (h.lower().split('+')[:-1], [h.lower().split('+')[-1]]))
        runtime.pydirectinput = SimpleNamespace(keyDown=lambda k: self.event('down', k), keyUp=lambda k: self.event('up', k))
        runtime.keyboard = SimpleNamespace(press=lambda k: self.event('down', k), release=lambda k: self.event('up', k)) if keyboard else None
        self.ctl = InputControllerAdapter(dict(DEFAULT_CONFIG, dry_run=False, auto_focus=False), self.log,
            SimpleNamespace(allowed=lambda: self.allowed), runtime=runtime)

    def identity(self, hwnd, cfg):
        result = (hwnd, self.pid, 'c:/game/cs2.exe') if hwnd == 1 else None
        self.after_identity()
        return result

    def sleep(self, duration):
        self.now += duration
        self.after_sleep()

    def event(self, kind, key):
        self.events.append((kind, key, self.foreground, self.now, self.ctl._send_lock._is_owned()))
        return True


class InputFocusTests(unittest.TestCase):
    def assert_safe(self, r):
        self.assertFalse(any(e[0] == 'down' and e[2] != 1 for e in r.events))
        self.assertFalse(r.ctl._held_keys)
        self.assertTrue(all(e[4] for e in r.events))

    def test_old_positive_cache_never_authorizes_other_window(self):
        r = Rig()
        self.assertTrue(r.ctl.foreground_is_game())
        r.now += .1
        r.foreground = 2
        self.assertFalse(r.ctl.press_slot(0))
        self.assertEqual(r.events, [])

    def test_loss_during_each_macro_stage_cancels_and_releases(self):
        for deadline in (.01, .09, .13, .2, .3):
            with self.subTest(deadline=deadline):
                r = Rig()
                r.after_sleep = lambda: setattr(r, 'foreground', 2) if r.now >= 100 + deadline else None
                self.assertFalse(r.ctl.send_hotkey('alt+m', double=True))
                self.assert_safe(r)
                self.assertLessEqual(r.now, 100 + deadline + .020001)

    def test_direct_fallback_uses_lock_and_fresh_focus(self):
        r = Rig(keyboard=False)
        r.after_sleep = lambda: setattr(r, 'foreground', 2) if r.now >= 100.04 else None
        self.assertFalse(r.ctl.send_hotkey('n', double=True))
        self.assert_safe(r)

    def test_focus_changed_during_identity_check_blocks(self):
        r = Rig()
        r.after_identity = lambda: setattr(r, 'foreground', 2)
        self.assertFalse(r.ctl.press_slot(0))
        self.assertEqual(r.events, [])

    def test_reused_hwnd_with_different_pid_aborts(self):
        r = Rig()
        r.after_sleep = lambda: setattr(r, 'pid', 11)
        self.assertFalse(r.ctl.send_hotkey('alt+m'))
        self.assertEqual([e[1] for e in r.events if e[0] == 'down'], ['alt'])
        self.assert_safe(r)

    def test_physical_modifier_already_held_blocks_without_releasing_it(self):
        for key in ('alt', 'ctrl', 'shift', 'win'):
            r = Rig()
            r.physical.add(key)
            self.assertFalse(r.ctl.send_hotkey('alt+m'))
            self.assertFalse(r.ctl.press_slot(0))
            self.assertEqual(r.events, [])

    def test_physical_other_modifier_during_sequence_cancels(self):
        r = Rig()
        r.after_sleep = lambda: r.physical.add('ctrl')
        self.assertFalse(r.ctl.send_hotkey('alt+m'))
        self.assertEqual([(e[0], e[1]) for e in r.events], [('down', 'alt'), ('up', 'alt')])
        self.assert_safe(r)

    def test_cancel_and_revocation_release_only_owned_keys(self):
        for revoke in (False, True):
            r = Rig()
            r.ctl.cancel_event = threading.Event()
            r.after_sleep = (lambda: setattr(r, 'allowed', False)) if revoke else r.ctl.cancel_event.set
            self.assertFalse(r.ctl.send_hotkey('alt+m'))
            self.assertEqual([(e[0], e[1]) for e in r.events], [('down', 'alt'), ('up', 'alt')])
            self.assert_safe(r)

    def test_normal_mulnx_timing_and_cleanup(self):
        r = Rig()
        self.assertTrue(r.ctl.send_hotkey('alt+m', double=True))
        self.assertEqual([(e[0], e[1]) for e in r.events],
            [('down', 'alt'), ('down', 'm'), ('up', 'm'), ('down', 'm'), ('up', 'm'), ('up', 'alt')])
        self.assertAlmostEqual(r.events[1][3] - r.events[0][3], .08)
        self.assertAlmostEqual(r.events[2][3] - r.events[1][3], .03)
        self.assertAlmostEqual(r.events[3][3] - r.events[1][3], .08)
        self.assertAlmostEqual(r.events[-1][3] - r.events[-2][3], .30)
        self.assert_safe(r)

    def test_focus_loss_does_not_steal_focus_on_next_attempt(self):
        r = Rig()
        r.ctl.cfg['auto_focus'] = True
        self.assertTrue(r.ctl.press_slot(0))
        r.foreground = 2
        self.assertFalse(r.ctl.press_slot(1))
        r.activate.assert_not_called()
        r.foreground = 1
        self.assertTrue(r.ctl.press_slot(1))

    def test_backend_failure_preserves_cleanup(self):
        r = Rig()
        r.ctl._runtime.pydirectinput.keyDown = mock.Mock(return_value=False)
        self.assertFalse(r.ctl.press_slot(0))
        self.assertEqual([e[0] for e in r.events], ['up'])
        self.assert_safe(r)

    def test_non_game_digits_never_reach_manual_callback(self):
        import cs2_ai_director as d
        callback, log = mock.Mock(), mock.Mock()
        watcher = d.ManualKeyWatcher(callback, lambda _: False, log, lambda: False)
        watcher._on_event(SimpleNamespace(event_type='down', scan_code=2, name='1'))
        watcher._handle('2')  # Polling path, too.
        callback.assert_not_called()
        log.assert_not_called()
        self.assertFalse(watcher._last_hit)

    def test_game_digit_still_reaches_manual_callback(self):
        import cs2_ai_director as d
        callback = mock.Mock()
        watcher = d.ManualKeyWatcher(callback, lambda _: False, mock.Mock(), lambda: True)
        with mock.patch.object(d, 'get_async_key_state', return_value=False):
            watcher._on_event(SimpleNamespace(event_type='down', scan_code=2, name='1'))
        callback.assert_called_once_with('1')

    def test_identity_rejects_same_name_wrong_configured_path(self):
        import cs2_ai_director as d
        with mock.patch.object(d, 'is_window', return_value=True), mock.patch.object(d, 'get_window_pid', return_value=10), \
                mock.patch.object(d, 'get_process_exe', return_value='C:/fake/cs2.exe'):
            self.assertIsNone(d.game_window_identity(1, {'cs2_exe': 'C:/real/cs2.exe'}))

    def test_physical_takeover_of_same_held_alt_is_not_released(self):
        from physical_modifiers import PhysicalModifiers
        r = Rig()
        monitor = r.ctl.physical_modifiers = PhysicalModifiers()
        monitor.started = True
        r.after_sleep = lambda: monitor.event(0xA4, 0, 0x100)
        self.assertFalse(r.ctl.send_hotkey('alt+m'))
        self.assertEqual([(e[0], e[1]) for e in r.events], [('down', 'alt')])
        self.assertFalse(r.ctl._held_keys)
        self.assertTrue(monitor.is_down('alt'))
        monitor.event(0xA4, 0, 0x101)
        self.assertFalse(monitor.conflict())

    def test_physical_monitor_discards_all_non_modifier_and_injected_events(self):
        from physical_modifiers import PhysicalModifiers
        monitor = PhysicalModifiers(); monitor.started = True
        for vk in (0x31, 0x41, 0x0D): monitor.event(vk, 0, 0x100)
        monitor.event(0xA4, 0x10, 0x100)
        monitor.event(0xA4, 0x02, 0x100)
        self.assertFalse(monitor.down)
        monitor.event(0xA4, 0, 0x100)
        self.assertTrue(monitor.conflict())
        monitor.event(0xA4, 0x10, 0x101)
        self.assertTrue(monitor.is_down('alt'))

    def test_unavailable_physical_monitor_blocks_input(self):
        from physical_modifiers import PhysicalModifiers
        r = Rig(); r.ctl.physical_modifiers = PhysicalModifiers()
        self.assertFalse(r.ctl.press_slot(0))
        self.assertEqual(r.events, [])

    def test_right_modifier_does_not_leak_our_left_modifier(self):
        from physical_modifiers import PhysicalModifiers
        r = Rig(); monitor = r.ctl.physical_modifiers = PhysicalModifiers(); monitor.started = True
        r.after_sleep = lambda: monitor.event(0x12, 1, 0x100)
        self.assertFalse(r.ctl.send_hotkey('alt+m'))
        self.assertEqual([(e[0], e[1]) for e in r.events], [('down', 'alt'), ('up', 'alt')])
        self.assertTrue(monitor.conflict())
        monitor.event(0x12, 1, 0x101)
        self.assertFalse(monitor.conflict())

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows modifier monitor')
    def test_windows_modifier_monitor_install_and_close_without_input(self):
        from physical_modifiers import PhysicalModifiers
        monitor = PhysicalModifiers()
        try:
            monitor.start()
            self.assertTrue(monitor.started)
        finally: monitor.close()
        self.assertFalse(monitor.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
