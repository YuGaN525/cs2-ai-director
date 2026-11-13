"""Control intent and cinematic cancellation without OS hooks or real game input."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import cs2_ai_director as d
from desktop_hotkeys import ControlHotkeys, control_bindings
from test_cs2_ai_director import payload, player
from test_input_focus import Rig


class Backend:
    def __init__(self): self.callbacks={}; self.removed=[]; self.options=[]
    def parse_hotkey(self, chord): return chord
    def add_hotkey(self, chord, callback, **options):
        handle=object();self.callbacks[chord]=callback;self.options.append(options);return handle
    def remove_hotkey(self, handle): self.removed.append(handle)


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.backend=Backend();self.now=100.
        self.hotkeys=ControlHotkeys(self.backend,lambda:self.now)
        self.hotkeys.configure(d.DEFAULT_CONFIG)
        self.addCleanup(self.hotkeys.close)

    def test_hook_thread_only_queues_and_release_is_required(self):
        thread=threading.Thread(target=self.backend.callbacks['ctrl+alt+d'])
        thread.start();thread.join()
        self.assertEqual(self.hotkeys.drain(),['director'])
        self.assertTrue(all(x==dict(suppress=False,trigger_on_release=True) for x in self.backend.options))

    def test_repeats_are_coalesced_debounced_and_stale_commands_expire(self):
        callback=self.backend.callbacks['ctrl+alt+m']
        for _ in range(50):callback()
        self.assertEqual(self.hotkeys.drain(),['cinematics'])
        callback();self.assertEqual(self.hotkeys.drain(),[])
        self.now+=.5;callback();self.now+=3
        self.assertEqual(self.hotkeys.drain(),[])

    def test_reconfiguration_and_close_discard_old_callbacks(self):
        callback=self.backend.callbacks['ctrl+alt+d'];callback()
        self.hotkeys.configure(dict(d.DEFAULT_CONFIG,director_toggle_hotkey='f9'))
        callback();self.assertEqual(self.hotkeys.drain(),[])
        self.backend.callbacks['f9']();self.assertEqual(self.hotkeys.drain(),['director'])
        self.hotkeys.close();self.backend.callbacks['f9']()
        self.assertEqual(self.hotkeys.drain(),[]);self.assertEqual(len(self.backend.removed),6)

    def test_blank_disables_and_alias_order_conflicts_are_rejected(self):
        self.assertEqual(control_bindings(dict(d.DEFAULT_CONFIG,director_toggle_hotkey=''))['director'],'')
        for change in ({'director_toggle_hotkey':'alt+control+m'},
                       {'director_toggle_hotkey':'alt+p'}, {'director_toggle_hotkey':'1'},
                       {'director_toggle_hotkey':'d'}, {'director_toggle_hotkey':'ctrl+d, ctrl+m'},
                       {'director_toggle_hotkey':'alt+1','hlae_spawn_ct_key':'ALT+1'}):
            with self.subTest(change=change),self.assertRaises(ValueError):control_bindings(dict(d.DEFAULT_CONFIG,**change))

    def test_one_registration_failure_keeps_other_controls_and_only_own_handles_are_removed(self):
        self.hotkeys.close();self.backend.removed.clear()
        add=self.backend.add_hotkey
        def register(chord,*args,**kwargs):
            if chord=='ctrl+alt+d':raise RuntimeError('unavailable')
            return add(chord,*args,**kwargs)
        self.backend.add_hotkey=register
        failed=self.hotkeys.configure(d.DEFAULT_CONFIG)
        self.assertEqual(failed,['启动 / 停止导播'])
        self.assertNotIn('director',self.hotkeys.registered)
        self.assertIn('cinematics',self.hotkeys.registered)
        self.hotkeys.close();self.assertEqual(len(self.backend.removed),2)

    def test_director_toggle_preserves_start_stop_guards(self):
        app=object.__new__(d.DirectorApp)
        app._closing=False;app.engine=None;app.tasks=SimpleNamespace(running=set())
        app._start=mock.Mock();app._stop=mock.Mock()
        app._toggle_director();app._start.assert_called_once()
        app.engine=mock.Mock();app._toggle_director();app._stop.assert_called_once()
        app.tasks.running.add('stop');app._toggle_director();app._stop.assert_called_once()
        app.tasks.running.clear();app.engine=None;app._closing=True
        app._toggle_director();app._start.assert_called_once()

    def test_start_still_requires_account_and_reentrant_start_is_ignored(self):
        app=object.__new__(d.DirectorApp)
        app._closing=False;app.engine=None;app.tasks=SimpleNamespace(running=set())
        app.account=mock.Mock(status='资格未就绪');app.account.allowed.return_value=False
        app.root=mock.Mock()
        with mock.patch.object(d.messagebox,'showerror') as error:
            app._start();error.assert_called_once();app.account.acquire_execution.assert_not_called()
        app._starting=True
        with mock.patch.object(app,'_start_director') as start:app._start();start.assert_not_called()

    def test_disabled_cinematics_allow_start_validation_without_mulnx_bindings(self):
        app=object.__new__(d.DirectorApp);app.root=mock.Mock()
        cfg=dict(d.DEFAULT_CONFIG,cinematics_enabled=False,dry_run=True,cs2_exe=__file__,arbitration_mode='off')
        with mock.patch.object(d.messagebox,'showerror') as error,mock.patch.object(d.messagebox,'askyesno') as question:
            self.assertTrue(app._validate_cfg(cfg));error.assert_not_called();question.assert_not_called()
            cfg['cinematics_enabled']=True
            self.assertFalse(app._validate_cfg(cfg));error.assert_called_once()

    def test_live_enable_requires_prepared_runtime_macro_bindings(self):
        app=object.__new__(d.DirectorApp);app._closing=False;app.root=mock.Mock()
        app.engine=mock.Mock(cfg=dict(d.DEFAULT_CONFIG))
        app.var_cinematics=mock.Mock();app._refresh_cinematic_controls=mock.Mock()
        with mock.patch.object(d.messagebox,'showwarning') as warning:
            app._set_cinematics_enabled(True);warning.assert_called_once()
        app.var_cinematics.set.assert_called_once_with(False)
        app.engine.set_cinematics_enabled.assert_not_called()


class CinematicControlTests(unittest.TestCase):
    def setUp(self):
        self.engine=d.DirectorEngine(dict(d.DEFAULT_CONFIG,bomb_cam_policy='always',hlae_spawn_ct_key='alt+m',
            hlae_spawn_t_key='alt+n',hlae_bomb_a_key='alt+1',hlae_bomb_b_key='alt+6'),d.LogBus())
        self.send=self.engine.input.send_hotkey=mock.Mock(return_value=True)
        self.press=self.engine.input.press_slot=mock.Mock(return_value=True)
        self.engine.timeline=mock.Mock()

    def snap(self,phase='freezetime',rnd=1,bomb=None):
        return d.parse_snapshot(payload(players=[player(1,'CT',hp=0),player(2,'T')],phase=phase,round_no=rnd,player_slot=1,bomb=bomb),100.)

    def test_disabled_skips_spawn_and_bomb_but_keeps_pov(self):
        self.engine.set_cinematics_enabled(False)
        self.engine._handle_triggers(self.snap(),100.)
        self.engine._handle_triggers(self.snap('live',bomb={'state':'planted','countdown':'6.0','position':'-600,-2200,0'}),101.)
        self.engine._decide_and_switch(self.snap('live'),102.,{})
        self.send.assert_not_called();self.press.assert_called_once_with(2)
        self.assertEqual(self.engine.triggers.counts,{'round':0,'bomb':0})

    def test_reenable_does_not_replay_disabled_opportunities(self):
        self.engine.set_cinematics_enabled(False);self.engine._handle_triggers(self.snap(),100.)
        self.engine.set_cinematics_enabled(True);self.engine._handle_triggers(self.snap(),105.)
        self.send.assert_not_called()
        self.engine._handle_triggers(self.snap(rnd=2),106.)
        self.assertEqual(self.send.call_count,1)

    def test_disabled_consumes_pending_retry_without_replaying_on_enable(self):
        self.send.return_value=False;self.engine._handle_triggers(self.snap(),100.)
        self.send.reset_mock();self.send.return_value=True
        self.engine.set_cinematics_enabled(False);self.engine._handle_triggers(self.snap(),100.1)
        self.engine.set_cinematics_enabled(True);self.engine._handle_triggers(self.snap(),110.)
        self.send.assert_not_called()

    def test_owned_preview_stops_once_then_pov_continues(self):
        self.engine._handle_triggers(self.snap(),100.);self.send.reset_mock()
        self.engine.set_cinematics_enabled(False)
        self.assertTrue(self.engine.status()['cinematic_pending_stop'])
        self.engine._handle_triggers(self.snap(),101.)
        self.engine._decide_and_switch(self.snap('live'),102.,{})
        self.send.assert_called_once_with('alt+p',double=False)
        self.press.assert_called_once_with(2)
        self.assertFalse(self.engine.status()['cinematic_pending_stop'])

    def test_rapid_disable_enable_cannot_revive_old_macro(self):
        self.engine._handle_triggers(self.snap(),100.);self.send.reset_mock()
        old=self.engine._cinematic_cancel
        self.engine.set_cinematics_enabled(False);self.engine.set_cinematics_enabled(True)
        self.assertTrue(old.is_set());self.assertTrue(self.engine.cinematics_enabled())
        self.engine._handle_triggers(self.snap(),101.)
        self.send.assert_called_once_with('alt+p',double=False)

    def test_unowned_preview_is_not_stopped_and_stale_gsi_does_not_prevent_owned_release(self):
        self.engine.set_cinematics_enabled(False);self.engine._handle_triggers(self.snap(),100.)
        self.send.assert_not_called()
        self.engine.set_cinematics_enabled(True);self.engine._handle_triggers(self.snap(rnd=2),101.)
        self.send.reset_mock();self.engine.set_cinematics_enabled(False)
        self.engine.store.get=mock.Mock(return_value=self.snap(rnd=2))
        with mock.patch.object(d.time,'monotonic',return_value=200.):self.engine._tick()
        self.send.assert_called_once_with('alt+p',double=False)
        self.assertEqual(self.engine.status()['gsi'],'stale')

    def test_inflight_macro_cancel_releases_keys_and_does_not_send_second_press(self):
        rig=Rig();self.engine.input=rig.ctl
        def cancel():
            if rig.now>=100.10:self.engine.set_cinematics_enabled(False)
        rig.after_sleep=cancel
        self.engine._handle_triggers(self.snap(),100.)
        primary=d.split_hotkey(self.engine.cfg['hlae_spawn_ct_key'])[1][0]
        self.assertEqual(len([e for e in rig.events if e[:2]==('down',primary)]),1)
        self.assertFalse(rig.ctl._held_keys)
        self.assertIsNotNone(self.engine._cinematic_request)
        self.engine._handle_triggers(self.snap(),102.)
        self.assertIsNone(self.engine._cinematic_request)
        self.assertEqual(len([e for e in rig.events if e[:2]==('down','p')]),1)

    def test_cancellation_before_main_key_does_not_claim_an_unowned_preview(self):
        rig=Rig();self.engine.input=rig.ctl
        rig.after_sleep=lambda:self.engine.set_cinematics_enabled(False)
        self.engine._handle_triggers(self.snap(),100.)
        self.engine._handle_triggers(self.snap(),101.)
        self.assertIsNone(self.engine._cinematic_request)
        self.assertFalse(rig.ctl.hotkey_started);self.assertFalse(rig.ctl._held_keys)
        self.assertFalse(any(e[:2]==('down','p') for e in rig.events))


if __name__=='__main__':unittest.main()
