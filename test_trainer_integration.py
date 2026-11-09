import unittest
from pathlib import Path
from unittest import mock
import trainer_launcher as launcher
import train_from_demos as demos


class IntegrationTests(unittest.TestCase):
    def test_source_command_uses_own_absolute_entry(self):
        with mock.patch.object(launcher.sys, "frozen", False, create=True):
            command = launcher.trainer_command()
        self.assertEqual(command[-1], "--trainer")
        self.assertEqual(Path(command[1]), Path(launcher.__file__).with_name("cs2_ai_director.py"))

    def test_frozen_command_uses_same_executable(self):
        with mock.patch.object(launcher.sys, "frozen", True, create=True):
            self.assertEqual(launcher.trainer_command(), [launcher.sys.executable, "--trainer"])

    def test_internal_asset_defaults_to_trainer_without_hiding_maintenance_commands(self):
        with mock.patch.object(launcher.sys,'frozen',True,create=True), \
                mock.patch.object(launcher.sys,'executable','C:/内部/Yuzi_CS2_AIDecisionTrainer.exe'):
            self.assertEqual(launcher.default_entry_arguments([]),['--trainer'])
            for args in (['--check-report','report.json'],['--client-update-install','job'],['--trainer']):
                self.assertEqual(launcher.default_entry_arguments(args),args)

    def test_repeated_open_does_not_start_second_training_process(self):
        current = mock.Mock()
        current.poll.return_value = None
        with mock.patch.object(launcher.subprocess, "Popen") as start:
            self.assertIs(launcher.open_trainer(current), current)
            start.assert_not_called()

    def test_exited_process_can_be_reopened(self):
        current = mock.Mock()
        current.poll.return_value = 0
        with mock.patch.object(launcher.subprocess, "Popen") as start:
            self.assertIs(launcher.open_trainer(current), start.return_value)
            start.assert_called_once()

    def test_empty_demo_has_consistent_new_and_legacy_contracts(self):
        parser = mock.Mock()
        parser.parse_header.return_value = {}
        parser.parse_event.return_value = None
        with mock.patch("demoparser2.DemoParser", return_value=parser):
            self.assertEqual(demos.collect_training_demo_rows(Path("empty.dem"), {}, 160),
                             ([], [], 0, [], []))
            self.assertEqual(demos.collect_demo_rows(Path("empty.dem"), {}, 160),
                             ([], [], 0, []))

    def test_training_close_preserves_running_work(self):
        from trainer_gui import TrainerApp
        app = mock.Mock()
        app._update_job = None
        app._training, app._calibrating, app._submitting = True, False, False
        with mock.patch("trainer_gui.messagebox.showinfo"):
            TrainerApp._on_close(app)
        app.root.destroy.assert_not_called()
        app._training = False
        app._closing = False
        TrainerApp._on_close(app)
        app.root.destroy.assert_not_called()
        key, work, done = app.tasks.submit.call_args.args
        self.assertEqual(key, 'close')
        done(work(), None)
        app.root.destroy.assert_called_once()

    def test_close_failure_retains_window_and_allows_retry(self):
        from trainer_gui import TrainerApp
        app = mock.Mock(_training=False, _calibrating=False, _submitting=False, _closing=False)
        app.training_data.close.return_value = False
        TrainerApp._on_close(app)
        _, work, done = app.tasks.submit.call_args.args
        with mock.patch('trainer_gui.messagebox.showerror'):
            done(work(), None)
        app.root.destroy.assert_not_called()
        app.account.close.assert_not_called()
        self.assertFalse(app._closing)


if __name__ == "__main__":
    unittest.main()
