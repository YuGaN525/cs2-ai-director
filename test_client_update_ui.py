import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import client_update_ui as ui
from preferences import Preferences
from test_client_update import envelope


class QueuedTasks:
    def __init__(self):self.pending={}
    def submit(self,key,work,done):
        if key in self.pending:return False
        self.pending[key]=(work,done);return True
    def finish(self,key):
        work,done=self.pending.pop(key)
        try:result,error=work(),None
        except Exception as exc:result,error=None,exc
        done(result,error)


class UpdateUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Mock();root.title.return_value='Test director'
        self.app=SimpleNamespace(root=root,preferences=Preferences(self.temp.name),tasks=QueuedTasks(),_closing=False)
        schedule=patch.object(ui,'schedule');schedule.start();self.addCleanup(schedule.stop)
        trust=patch.object(ui,'read_public_trust',return_value={'server_origin':'https://example.org'})
        trust.start();self.addCleanup(trust.stop)
        self.network=patch.object(ui,'check',return_value={'envelope':envelope('9999.0.0')}).start()
        self.addCleanup(patch.stopall)
        self.updates=ui.Updates(self.app)
        self.app.updates=self.updates

    def run_check(self,manual=True):
        self.updates.request(manual);self.app.tasks.finish('client-update')

    def test_success_cache_ignore_and_no_repeat_network(self):
        self.run_check()
        self.assertEqual(self.updates.result['state'],'update_available')
        self.updates.ignore();self.app.tasks.finish('ignore-client-update')
        self.run_check(False)
        self.assertTrue(self.updates.result['ignored'])
        self.assertTrue(self.updates.result['cached'])
        self.assertEqual(self.network.call_count,1)
        self.app.root.title.assert_called_with('Test director')

    def test_old_channel_result_discarded(self):
        self.updates.request()
        self.app.preferences.update(update_channel='beta')
        self.updates.reconfigure()
        self.app.tasks.finish('client-update')
        self.assertEqual(self.updates.result['state'],'not_checked')

    def test_version_change_invalidates_cached_decision(self):
        self.run_check()
        with patch.object(ui,'VERSION','9999.0.0'):
            self.run_check(False)
        self.assertEqual(self.network.call_count,2)
        self.assertEqual(self.updates.result['state'],'up_to_date')

    def test_snooze_clears_title_but_keeps_update_details(self):
        self.run_check()
        self.updates.later();self.app.tasks.finish('snooze-client-update')
        self.run_check(False)
        self.assertEqual(self.updates.result['state'],'update_available')
        self.assertFalse(self.updates.result['remind'])
        self.app.root.title.assert_called_with('Test director')

    def test_current_exe_hash_conflict_not_latest(self):
        self.network.return_value={'envelope':envelope(ui.VERSION)}
        self.updates.artifact_hash='b'*64
        self.run_check()
        self.assertEqual(self.updates.result['state'],'same_version_conflict')

    def test_malformed_cached_release_forces_network_recheck(self):
        self.run_check()
        store=ui.UpdateState(self.temp.name)
        store.merge(store.key('https://example.org','stable'),result={'state':'update_available','release':[],'checked_at':100})
        self.run_check(False)
        self.assertEqual(self.network.call_count,2)
        self.assertEqual(self.updates.result['state'],'update_available')

    def test_manual_coalesced_and_failure_is_not_latest(self):
        self.network.return_value={'error':'timeout'}
        self.updates.request();self.updates.request()
        self.assertEqual(len(self.app.tasks.pending),1)
        self.app.tasks.finish('client-update')
        self.assertEqual(self.updates.result['state'],'check_failed')
        self.run_check(False)
        self.assertEqual(self.network.call_count,1)
        self.assertEqual(self.updates.result['state'],'check_failed')

    def test_other_window_preferences_refreshed_without_network_in_source_mode(self):
        self.app.preferences.update(auto_check_updates=False,update_channel='beta')
        self.updates.tick();self.app.tasks.finish('update-preferences')
        self.assertEqual(self.updates.options['update_channel'],'beta')
        self.assertEqual(self.updates.result['state'],'disabled')
        self.network.assert_not_called()

    def test_destroy_cancels_late_callback(self):
        self.updates.request()
        self.updates._destroy(SimpleNamespace(widget=self.app.root))
        self.assertTrue(self.updates.cancel.is_set())
        self.app.tasks.finish('client-update')
        self.assertEqual(self.updates.result['state'],'checking')

    def test_old_preferences_are_readable(self):
        self.app.preferences.update(theme='light',update_channel='beta')
        self.assertEqual(Preferences(self.temp.name).read()['theme'],'light')
        self.assertEqual(Preferences(self.temp.name).read()['update_channel'],'beta')
        with self.assertRaises(ValueError):self.app.preferences.update(update_channel='nightly')

    def test_cancel_or_channel_change_never_restores_late_download(self):
        with patch('client_update_download.download',return_value={'state':'ready','payload':{}}):
            self.updates.download();self.updates.cancel_download()
            self.app.tasks.finish('update-download')
            self.assertEqual(self.updates.transfer['state'],'cancelled')
            self.assertIsNone(self.updates.ready)
            self.updates.download();self.updates.reconfigure()
            self.app.tasks.finish('update-download')
            self.assertEqual(self.updates.transfer['state'],'idle')
            self.assertIsNone(self.updates.ready)

    def test_queued_progress_cannot_overwrite_ready(self):
        self.updates.transfer={'state':'ready'}
        self.updates.transfer_events.put((self.updates.generation,{'bytes':10,'total':20}))
        self.updates.transfer_tick()
        self.assertEqual(self.updates.transfer['state'],'ready')


if __name__=='__main__':unittest.main()
