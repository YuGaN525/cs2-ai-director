import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from preferences import Preferences


class PreferencesTests(unittest.TestCase):
    def test_two_clients_merge_fields_and_invalid_changes_are_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Preferences(directory), Preferences(directory)
            a.update(developer_mode=True)
            b.update(theme='light')
            self.assertEqual(a.read()['theme'], 'light')
            self.assertTrue(b.read()['developer_mode'])
            with self.assertRaises(ValueError):
                a.update(developer_mode=False, theme='invalid')
            self.assertTrue(a.read()['developer_mode'])

    def test_font_and_independent_window_sizes_preserve_other_process_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            a,b=Preferences(directory),Preferences(directory)
            a.update(font_size=12,director_size='1000x700',theme='dark')
            b.update(trainer_size='800x600')
            self.assertEqual(a.read()['trainer_size'],'800x600')
            self.assertEqual(b.read()['font_size'],12)
            self.assertEqual(b.read()['director_size'],'1000x700')
            with self.assertRaises(ValueError):a.update(theme='light',font_size=30)
            self.assertEqual(a.read()['theme'],'dark')
            for invalid in ('-1x600','100000x600',False):
                with self.assertRaises(ValueError):a.update(director_size=invalid)

    def test_defaults_do_not_enable_developer_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(Preferences(directory).read()['developer_mode'])
            self.assertEqual(Preferences(directory).read()['theme'],'light')


class ActionShutdownTests(unittest.TestCase):
    def test_full_queue_cancels_remaining_actions_and_stops(self):
        from director_pro import EventActionEngine, ProLog
        entered, release = threading.Event(), threading.Event()
        obs = mock.Mock()
        def blocking(_):
            entered.set()
            release.wait(3)
            return True
        obs.switch_scene.side_effect = blocking
        engine = EventActionEngine({}, obs, ProLog())
        rule = {'actions': [{'type': 'switch_scene', 'target': 'fixture'}]}
        engine._queue.put(rule)
        self.assertTrue(entered.wait(1))
        for _ in range(100):
            engine._queue.put_nowait(rule)
        stop = threading.Thread(target=engine.stop)
        stop.start()
        self.assertTrue(engine._stop_event.wait(1))
        release.set()
        stop.join(3)
        self.assertFalse(engine._worker.is_alive())
        self.assertEqual(obs.switch_scene.call_count, 1)


class ConfigurationTests(unittest.TestCase):
    def test_non_finite_and_invalid_fields_rejected(self):
        from config_service import parse_fields, ConfigError
        for field, value in [('gsi_port','0'),('gsi_port','12.2'),('switch_min_score','NaN'),('manual_pause_seconds','inf'),('obs_port','65536')]:
            with self.subTest(field=field,value=value):
                with self.assertRaises(ConfigError) as error:
                    parse_fields({field:value})
                self.assertEqual(error.exception.field,field)

    def test_valid_values_are_converted_without_mutating_input(self):
        from config_service import parse_fields
        values={'gsi_port':'3000','switch_min_score':'0.3'}
        self.assertEqual(parse_fields(values),{'gsi_port':3000,'switch_min_score':.3})
        self.assertEqual(values['gsi_port'],'3000')


class ExecutionLifetimeTests(unittest.TestCase):
    def test_obs_expiry_and_session_cancel_prevent_old_actions(self):
        import time
        from director_pro import EventActionEngine, ProLog
        obs = mock.Mock()
        engine = EventActionEngine({}, obs, ProLog())
        rule = {'actions': [{'type': 'switch_scene', 'target': 'old'}],
                '_created': time.monotonic()-10, '_generation': 0}
        try:
            engine.execute(rule)
            obs.switch_scene.assert_not_called()
            rule['_created'] = time.monotonic()
            engine.cancel_pending()
            engine.execute(rule)
            obs.switch_scene.assert_not_called()
            rule['_generation'] = engine._generation
            engine.execute(rule)
            obs.switch_scene.assert_called_once_with('old')
        finally:
            engine.stop()

    def test_config_draft_does_not_mutate_running_snapshot(self):
        from cs2_ai_director import DirectorApp
        app = mock.Mock(_config_version=0)
        draft = {'event_rules': [{'actions': [{'target': 'Game'}]}]}
        DirectorApp._apply_runtime_cfg(app, draft)
        draft['event_rules'][0]['actions'][0]['target'] = 'Changed'
        self.assertEqual(app.pro.actions.cfg['event_rules'][0]['actions'][0]['target'], 'Game')
        self.assertEqual(app._config_version, 1)

    def test_event_worker_does_not_leak_after_100_cycles(self):
        from director_pro import EventActionEngine, ProLog
        before = {t.ident for t in threading.enumerate()}
        for _ in range(100):
            engine = EventActionEngine({}, mock.Mock(), ProLog())
            engine.stop()
            engine.stop()
            self.assertFalse(engine._worker.is_alive())
        after = {t.ident for t in threading.enumerate()}
        self.assertEqual(after-before, set())


class OrderedPersistenceTests(unittest.TestCase):
    def test_failed_head_is_retained_and_retry_preserves_order(self):
        from ordered_writer import OrderedWriter
        fail = threading.Event()
        fail.set()
        entered = threading.Event()
        output = []
        def first():
            entered.set()
            if fail.is_set():
                raise OSError('disk full fixture')
            output.append(1)
        writer = OrderedWriter(capacity=2)
        self.assertTrue(writer.offer(first))
        self.assertTrue(entered.wait(1))
        self.assertTrue(writer.offer(lambda: output.append(2)))
        self.assertFalse(writer.close())
        self.assertEqual(writer.stats()['buffered'], 2)
        self.assertEqual(output, [])
        fail.clear()
        writer.start()
        self.assertTrue(writer.close())
        self.assertEqual(output, [1, 2])
        self.assertEqual(writer.stats()['buffered'], 0)

    def test_backpressure_is_reported_separately_from_accepted_data(self):
        from ordered_writer import OrderedWriter
        release, entered = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(2)
        writer = OrderedWriter(capacity=1)
        try:
            self.assertTrue(writer.offer(blocked))
            self.assertTrue(entered.wait(1))
            self.assertTrue(writer.offer(lambda: None))
            self.assertFalse(writer.offer(lambda: None))
            self.assertEqual(writer.stats()['not_accepted'], 1)
        finally:
            release.set()
            self.assertTrue(writer.close())
        self.assertEqual(writer.stats()['persisted'], 2)

    def test_async_timeline_matches_synchronous_csv_and_summary(self):
        from director_story import ShotTimeline
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with mock.patch('director_story.time.time', return_value=1000):
                sync=ShotTimeline({},root/'sync')
                async_writer=ShotTimeline({},root/'async',async_writes=True)
                for timeline in (sync,async_writer):
                    for i in range(100):
                        timeline._write_event('fixture',slot=i%10,value=i,note='引号,"中文"')
                    self.assertTrue(timeline.close(100))
            for name in ('shot_events.csv','shot_timeline.csv','round_metrics.csv'):
                self.assertEqual((root/'sync'/name).read_bytes(),(root/'async'/name).read_bytes())
            summaries=[]
            for sub in ('sync','async'):
                value=json.loads((root/sub/'director_metrics.json').read_text(encoding='utf-8'))
                value.pop('paths')
                summaries.append(value)
            self.assertEqual(*summaries)


class GsiLifecycleTests(unittest.TestCase):
    def test_real_listener_and_engine_100_start_stop_cycles(self):
        import cs2_ai_director as d
        initial={t.ident for t in threading.enumerate()}
        cfg=dict(d.DEFAULT_CONFIG,gsi_port=0,ai_enabled=False,demo_model_enabled=False)
        with mock.patch.object(d, 'ManualKeyWatcher'):
            for _ in range(100):
                engine=d.DirectorEngine(cfg,mock.Mock())
                engine.input=mock.Mock()
                engine.start()
                engine.start()
                engine.stop()
                engine.stop()
                self.assertIsNone(engine.server)
                self.assertIsNone(engine._thread)
        self.assertEqual({t.ident for t in threading.enumerate()}-initial,set())

    def test_stop_signal_blocks_input_before_a_key_can_be_pressed(self):
        import cs2_ai_director as d
        gate=mock.Mock()
        gate.allowed.return_value=True
        controller=d.InputController(dict(d.DEFAULT_CONFIG),mock.Mock(),gate)
        controller.cancel_event=threading.Event()
        controller.cancel_event.set()
        with mock.patch.object(d,'pydirectinput') as backend:
            self.assertFalse(controller.press_slot(1))
            backend.keyDown.assert_not_called()


class PartialWriteRecoveryTests(unittest.TestCase):
    def test_partial_csv_write_retries_at_original_row_boundary(self):
        import csv
        from director_story import ShotTimeline
        with tempfile.TemporaryDirectory() as directory:
            timeline=ShotTimeline({},Path(directory),async_writes=True)
            failed=threading.Event()
            original=Path.open
            target=timeline.event_path
            class FaultyFile:
                def __init__(self, stream):self.stream=stream
                def __enter__(self):return self
                def __exit__(self,*args):return self.stream.__exit__(*args)
                def __getattr__(self,key):return getattr(self.stream,key)
                def write(self,data):
                    self.stream.write(data[:8]);self.stream.flush()
                    failed.set()
                    raise OSError('simulated partial disk write')
            def opening(path,*args,**kwargs):
                stream=original(path,*args,**kwargs)
                if path==target and args and args[0]=='r+b' and not failed.is_set():
                    return FaultyFile(stream)
                return stream
            with mock.patch.object(Path,'open',opening):
                timeline._write_event('fixture',slot=1)
                self.assertTrue(failed.wait(1))
                # First stop retains the failed row, then an explicit retry completes it.
                timeline.close(100)
                self.assertTrue(timeline.close(101))
            with target.open(encoding='utf-8-sig',newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['event'],'fixture')


if __name__ == '__main__':
    unittest.main()
