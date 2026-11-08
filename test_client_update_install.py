import json
from pathlib import Path
from unittest.mock import Mock,patch
import unittest
import subprocess
import client_update_install as install
from test_client_update_download import DownloadFixture,pe


class InstallTests(DownloadFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.target=self.root/'中文 空格'/'Director.exe';self.target.parent.mkdir();self.old=pe(b'b');self.target.write_bytes(self.old)
        self.fresh=patch.object(install,'fresh_manifest',side_effect=lambda *args:(dict(self.payload),self.signed()))
        self.fresh.start();self.addCleanup(self.fresh.stop)

    def job(self):
        ready=self.run_download()
        return install.prepare(self.root,ready,self.target,trust_path=self.trust_path,current_version='1.14.0')

    def test_success_retains_old_backup_and_does_not_touch_data(self):
        path=self.job();data=self.root/'account.sqlite3';data.write_bytes(b'private-existing-data')
        def launch(command):
            self.assertEqual(command,[str(self.target),'--client-update-health',str(path)])
            job=json.loads(path.read_text())
            (path.parent/'ready.json').write_text(json.dumps(dict(token=job['token'],version=self.payload['version'],sha256=self.payload['sha256'])))
            return Mock(poll=Mock(return_value=None))
        self.assertEqual(install.perform(path,self.root,self.trust_path,launcher=launch),'complete')
        self.assertEqual(self.target.read_bytes(),self.data);self.assertEqual(data.read_bytes(),b'private-existing-data')
        self.assertEqual(len(list(self.target.parent.glob('*.backup'))),1)
        with self.assertRaisesRegex(ValueError,'already_consumed'):install.perform(path,self.root,self.trust_path,launcher=launch)

    def test_failed_new_process_rolls_back(self):
        path=self.job()
        process=Mock(poll=Mock(return_value=1))
        self.assertEqual(install.perform(path,self.root,self.trust_path,launcher=lambda _:process),'rolled_back')
        self.assertEqual(self.target.read_bytes(),self.old)

    def test_replace_failure_restores_old_exe(self):
        path=self.job();replace=install.os.replace
        def fail(source,target):
            if str(source).endswith('.new'):raise PermissionError('fixture locked target')
            return replace(source,target)
        with patch.object(install.os,'replace',side_effect=fail),self.assertRaises(PermissionError):
            install.perform(path,self.root,self.trust_path)
        self.assertEqual(self.target.read_bytes(),self.old)

    def test_power_loss_between_backup_and_replace_recovers(self):
        path=self.job();job,target=install.validated_job(path,self.root)
        _,backup,staged,_=install._paths(path,job,target)
        install.save_job(path,job,'backing_up');target.replace(backup)
        self.assertEqual(install.recover(path,self.root),'rolled_back')
        self.assertEqual(target.read_bytes(),self.old)

    def test_wrong_target_job_path_and_changed_old_exe_rejected(self):
        path=self.job()
        with self.assertRaises(ValueError):install.validated_job(path,self.root/'other-root')
        self.target.write_bytes(b'new user file')
        with self.assertRaisesRegex(ValueError,'target_changed'):install.perform(path,self.root,self.trust_path)
        self.assertEqual(self.target.read_bytes(),b'new user file')

    def test_unresponsive_new_app_is_not_killed_or_overwritten(self):
        path=self.job();process=Mock(poll=Mock(return_value=None))
        process.wait.side_effect=subprocess.TimeoutExpired('fixture',10)
        state=install.perform(path,self.root,self.trust_path,launcher=lambda _:process,health_timeout=.01)
        self.assertEqual(state,'rollback_pending');process.kill.assert_not_called();process.terminate.assert_not_called()
        self.assertEqual(self.target.read_bytes(),self.data)
        self.assertTrue((path.parent/'cancel-health').exists())
        self.assertEqual(install.recover(path,self.root),'rolled_back')
        self.assertEqual(self.target.read_bytes(),self.old)

    def test_replaced_backup_cannot_overwrite_target(self):
        path=self.job();job,target=install.validated_job(path,self.root)
        _,backup,_,_=install._paths(path,job,target)
        backup.write_bytes(b'not the original');install.save_job(path,job,'backed_up')
        with self.assertRaisesRegex(ValueError,'backup_missing_or_changed'):install.recover(path,self.root)
        self.assertEqual(target.read_bytes(),self.old)


if __name__=='__main__':unittest.main()
