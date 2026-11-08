import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from client_update_lock import FileLock
from client_update_state import UpdateState


class UpdateRecoveryTests(unittest.TestCase):
    def test_corrupt_cache_rebuild_preserves_original_and_other_databases(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);cache=root/'client-updates.sqlite3';cache.write_bytes(b'corrupt cache')
            account=root/'account.sqlite3';account.write_bytes(b'account data')
            store=UpdateState(root);store.merge('key',result='ok')
            self.assertEqual(store.read('key')['result'],'ok')
            saved=list(root.glob('client-updates.corrupt-*.sqlite3'))
            self.assertEqual(len(saved),1);self.assertEqual(saved[0].read_bytes(),b'corrupt cache')
            self.assertEqual(account.read_bytes(),b'account data')

    def test_shared_process_lease_blocks_exclusive_until_crash(self):
        with tempfile.TemporaryDirectory() as root:
            lock=Path(root)/'runtime.lock'
            script='from client_update_lock import FileLock; import sys,time; guard=FileLock(sys.argv[1],shared=True); guard.__enter__(); print("ready",flush=True); time.sleep(60)'
            child=subprocess.Popen([sys.executable,'-c',script,str(lock)],stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                self.assertEqual(child.stdout.readline().strip(),'ready')
                with FileLock(lock,shared=True,timeout=.1):pass
                with self.assertRaises(TimeoutError):
                    with FileLock(lock,timeout=.1):pass
                child.terminate();child.wait(timeout=5)
                with FileLock(lock,timeout=.1):pass
            finally:
                if child.poll() is None:child.kill();child.wait(timeout=5)
                child.stdout.close();child.stderr.close()


if __name__=='__main__':unittest.main()
