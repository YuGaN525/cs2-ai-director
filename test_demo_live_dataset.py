import csv
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import demo_dataset as desktop
import demo_live_dataset as live
from director_core import DECISION_FEATURES
from train_decision_model import load_dataset


class Frame:
    def __init__(self, records):
        self.records = records

    def __getitem__(self, name):
        return [row[name] for row in self.records]

    def to_dict(self, orient):
        return self.records


def event(tick, attacker='1', victim='2'):
    return dict(tick=tick, attacker_steamid=attacker, user_steamid=victim)


def parser_fixture(hurts=(), deaths=(), starts=(0,), ends=(512,)):
    parser = mock.Mock()
    parser.parse_header.return_value = {'map_name': 'de_dust2'}
    events = {
        'round_freeze_end': Frame([{'tick': t} for t in starts]),
        'round_end': Frame([{'tick': t} for t in ends]),
        'player_hurt': Frame(list(hurts)), 'player_death': Frame(list(deaths)),
    }
    parser.parse_event.side_effect = events.get

    def ticks(fields, ticks):
        return Frame([dict(tick=t, steamid=str(i), team_num=i+1, is_alive=True,
                           health=100, armor=100, active_weapon_name='AK-47',
                           X=(i-1)*100, Y=0, Z=0, yaw=(i-1)*180, pitch=0, name='')
                      for t in ticks for i in (1, 2)])
    parser.parse_ticks.side_effect = ticks
    return parser


class LiveDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.demo = self.root / 'match.dem'
        self.demo.write_bytes(b'fixture-demo')

    def parse(self, parser, name='live.csv', **kwargs):
        path = self.root / name
        with mock.patch('demoparser2.DemoParser', return_value=parser):
            report = live.build_live_csv(self.demo, path, **kwargs)
        with path.open(encoding='utf-8', newline='') as stream:
            return list(csv.DictReader(stream)), report

    def test_future_events_change_labels_but_not_times_or_features(self):
        a, _ = self.parse(parser_fixture())
        b, report = self.parse(parser_fixture(hurts=[event(320)]))
        keys = ['source_tick', 'target_slot', *DECISION_FEATURES]
        self.assertEqual([[r[k] for k in keys] for r in a], [[r[k] for k in keys] for r in b])
        self.assertEqual({r['source_tick'] for r in b}, {'128', '256'})
        self.assertTrue(all(r['label'] == '0' for r in a))
        self.assertEqual(report['positive'], 2)

    def test_label_window_excludes_present_includes_exact_horizon(self):
        for tick, expected in ((128, '0'), (129, '1'), (320, '1'), (321, '0')):
            with self.subTest(tick=tick):
                rows, _ = self.parse(parser_fixture(hurts=[event(tick)]))
                row = next(r for r in rows if r['source_tick'] == '128' and r['target_slot'] == '1')
                self.assertEqual(row['label'], expected)

    def test_kill_without_damage_event_is_still_positive(self):
        _, report = self.parse(parser_fixture(deaths=[event(320)]))
        self.assertEqual(report['positive'], 2)

    def test_recent_damage_does_not_cross_live_round_start(self):
        for tick, expected in ((180, '0.0'), (220, '1.0'), (240, '0.0')):
            with self.subTest(tick=tick):
                rows, _ = self.parse(parser_fixture(hurts=[event(tick)], starts=(200,), ends=(600,)), step=32)
                row = next(r for r in rows if r['source_tick'] == '232' and r['target_slot'] == '1')
                self.assertEqual(row['enemy_damaged'], expected)

    def test_round_end_and_truncated_round_windows_are_censored(self):
        windows = live.live_windows([0, 700], [512])
        self.assertEqual(windows, [(1, 128, 512), (1, 256, 512)])
        self.assertEqual(live.live_windows([0, 128], [512]), [(2, 256, 512)])

    def test_invalid_window_parameters_fail_before_parser_loading(self):
        for horizon, step in ((0, 128), (-1, 128), (192, 0), (192, -1), (1.5, 128), (True, 128)):
            with self.subTest(horizon=horizon, step=step), mock.patch('demoparser2.DemoParser') as parser:
                with self.assertRaises(ValueError):
                    live.build_live_csv(self.demo, self.root / 'bad.csv', horizon, step)
                parser.assert_not_called()

    def test_budget_rejects_large_input_without_building_unbounded_list(self):
        with self.assertRaisesRegex(ValueError, 'demo_sample_budget'):
            live.live_windows([0], [10**12], max_samples=2)

    def test_native_parser_panic_becomes_reportable_import_error(self):
        panic = type('PanicException', (BaseException,), {'__module__': 'pyo3_runtime'})
        with mock.patch('demoparser2.DemoParser', side_effect=panic('invalid data')):
            with self.assertRaisesRegex(RuntimeError, 'demo_parser_panic'):
                desktop.build_demo_csv([self.demo], self.root/'invalid.csv', log=lambda _: None)
        self.assertFalse((self.root/'invalid.csv').exists())

    def test_parser_boundary_does_not_swallow_process_interrupts(self):
        for interrupt in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt=interrupt), mock.patch('demoparser2.DemoParser', side_effect=interrupt):
                with self.assertRaises(interrupt):
                    live.build_live_csv(self.demo, self.root/'invalid.csv')

    def test_version_window_source_and_missing_fields_are_recorded(self):
        rows, report = self.parse(parser_fixture())
        self.assertEqual(report['source_sha256'], hashlib.sha256(self.demo.read_bytes()).hexdigest())
        self.assertEqual(report['features'], list(DECISION_FEATURES))
        for row in rows:
            self.assertEqual(row['parser_version'], live.PARSER_VERSION)
            self.assertEqual(row['sampling_method'], 'periodic-live')
            self.assertEqual(row['sample_step_ticks'], '128')
            self.assertEqual(row['label_horizon_ticks'], '192')
            self.assertEqual(row['tickrate_assumption'], '64')
            self.assertTrue(row['missing_fields'])

    def test_desktop_and_worker_parser_are_byte_identical(self):
        _, report = self.parse(parser_fixture(hurts=[event(320)]))
        with mock.patch('demoparser2.DemoParser', return_value=parser_fixture(hurts=[event(320)])):
            stats = desktop.build_demo_csv([self.demo], self.root / 'desktop.csv', log=lambda _: None)
        self.assertEqual((self.root / 'live.csv').read_bytes(), (self.root / 'desktop.csv').read_bytes())
        manifest = json.loads(Path(stats['manifest']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['sources'][0]['source_sha256'], report['source_sha256'])
        self.assertEqual(manifest['csv_sha256'], hashlib.sha256((self.root / 'desktop.csv').read_bytes()).hexdigest())
        self.assertEqual(load_dataset(self.root / 'live.csv'), load_dataset(self.root / 'desktop.csv'))

    def test_failed_import_preserves_existing_csv_and_manifest(self):
        output = self.root / 'desktop.csv'
        output.write_bytes(b'prior-output')
        manifest = output.with_suffix('.csv.manifest.json')
        manifest.write_bytes(b'prior-manifest')
        other = self.root / 'broken.dem'
        other.write_bytes(b'broken')
        with mock.patch('demoparser2.DemoParser', side_effect=[parser_fixture(), ValueError('broken-demo')]):
            with self.assertRaisesRegex(RuntimeError, 'broken-demo'):
                desktop.build_demo_csv([self.demo, other], output, log=lambda _: None)
        self.assertEqual(output.read_bytes(), b'prior-output')
        self.assertEqual(manifest.read_bytes(), b'prior-manifest')
        self.assertFalse(list(self.root.glob('.demo_import_*')))

    def test_duplicate_content_is_not_overweighted(self):
        other = self.root / 'copy.dem'
        other.write_bytes(self.demo.read_bytes())
        with mock.patch('demoparser2.DemoParser', return_value=parser_fixture()):
            stats = desktop.build_demo_csv([self.demo, other], self.root / 'desktop.csv', log=lambda _: None)
        self.assertEqual((stats['demos'], stats['duplicate_demos'], stats['rows']), (1, 1, 4))

    def test_legacy_csv_remains_readable(self):
        path = self.root / 'legacy.csv'
        with path.open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=[*DECISION_FEATURES, 'label', 'map', 'round'])
            writer.writeheader()
            writer.writerow({**{f: 0.5 for f in DECISION_FEATURES}, 'label': 1, 'map': 'de_dust2', 'round': 1})
        xs, ys, groups, _, _, stats, _ = load_dataset(path)
        self.assertEqual((xs, ys, groups), ([[0.5]*8], [1], ['de_dust2|1']))
        self.assertEqual(stats['skipped'], 0)

    def test_dataset_cli_accepts_window_parameters_and_deprecated_alias(self):
        for extra, horizon in (([], 192), (['--horizon-ticks', '256'], 256), (['--tick-offset', '160'], 160)):
            output = self.root / 'cli.csv'
            with self.subTest(extra=extra), mock.patch('demoparser2.DemoParser', return_value=parser_fixture()), redirect_stdout(io.StringIO()):
                self.assertEqual(desktop.main(['--demos', str(self.demo), '--out', str(output), '--step-ticks', '64', *extra]), 0)
                manifest = json.loads(output.with_suffix('.csv.manifest.json').read_text(encoding='utf-8'))
                self.assertEqual((manifest['horizon_ticks'], manifest['step_ticks']), (horizon, 64))

    def test_trainer_cli_default_uses_same_live_settings(self):
        import trainer_gui
        output = self.root / 'cli.csv'
        runtime = mock.Mock()
        runtime.begin_training.return_value=('00000000-0000-0000-0000-000000000001',output)
        with mock.patch('demoparser2.DemoParser', return_value=parser_fixture()), mock.patch('trainer_gui.train_from_csv') as train, redirect_stdout(io.StringIO()):
            self.assertEqual(trainer_gui._demo_cli(['--demos', str(self.demo), '--demo-csv', str(output),'--out',str(self.root/'model.pkl')], runtime), 0)
        runtime.begin_training.assert_called_once_with(output)
        runtime.capture_training_file.assert_not_called()
        runtime.succeeded.assert_called_once()
        train.assert_called_once()
        manifest = json.loads(output.with_suffix('.csv.manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest['horizon_ticks'], manifest['step_ticks']), (192, 128))

    def test_trainer_cli_never_trains_after_invalid_sampling(self):
        import trainer_gui
        with mock.patch('trainer_gui.train_from_csv') as train, redirect_stderr(io.StringIO()):
            self.assertEqual(trainer_gui._demo_cli(['--demos', str(self.demo), '--step-ticks', '0']), 1)
        train.assert_not_called()

    def test_gui_worker_parses_before_recording_and_training(self):
        import trainer_gui
        app = mock.Mock()
        output = self.root / 'gui.csv'
        calls = []
        def freeze(path):
            self.assertTrue(path.with_suffix('.csv.manifest.json').exists())
            calls.append('freeze')
            return '00000000-0000-0000-0000-000000000001',path
        app.datasets.begin_training.side_effect = freeze
        with mock.patch('demoparser2.DemoParser', return_value=parser_fixture()), mock.patch('trainer_gui.threading.Thread') as thread, mock.patch('trainer_gui.train_from_csv', side_effect=lambda *a, **k: calls.append('train')):
            thread.side_effect = lambda **kw: mock.Mock(start=kw['target'])
            trainer_gui.TrainerApp._run_worker(app, output, self.root/'model.pkl', self.root/'metrics.json', (100, 50, 4, .25), [str(self.demo)])
        self.assertEqual(calls, ['freeze', 'train'])
        app.training_data.capture_training_file.assert_not_called()
        self.assertEqual(app._events.put.call_args.args[0][0], 'done')

    def test_worker_training_projection_retains_same_features_and_labels(self):
        import cloud_worker
        source = hashlib.sha256(self.demo.read_bytes()).hexdigest()
        spec = {'jobId': 'fixture', 'request': {'parameters': {'maxMemoryMb': 1024, 'maxSeconds': 30, 'trees': 50, 'maxDepth': 4}},
                'snapshot': {'manifestHash': 'fixture', 'manifest': {'items': [{'id': 'match', 'kind': 'demo', 'matchHash': source}]}}}
        (self.root/'spec.json').write_text(json.dumps(spec), encoding='utf-8')
        (self.root/'candidate.submission.json').write_text('{}', encoding='utf-8')
        with mock.patch('demoparser2.DemoParser', return_value=parser_fixture(hurts=[event(320)])), mock.patch('train_decision_model.train_from_csv', return_value={}):
            cloud_worker.train_child(self.root)
            desktop.build_demo_csv([self.demo], self.root/'desktop.csv', log=lambda _: None)
        self.assertEqual(load_dataset(self.root/'dataset.csv'), load_dataset(self.root/'desktop.csv'))
        report = json.loads((self.root/'result.json').read_text())
        self.assertEqual(report['report']['parserStats'][0]['parser'], live.PARSER_VERSION)


if __name__ == '__main__':
    unittest.main()
