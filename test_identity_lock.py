"""Real Windows mandatory byte-lock regression; no account or network access."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from device_identity import DeviceSession


@unittest.skipUnless(os.name == 'nt', 'Windows mandatory byte locking')
class IdentityLockTests(unittest.TestCase):
    def test_other_process_waits_for_existing_identity_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            script = (
                "import sys; from device_identity import DeviceSession; "
                "session=DeviceSession(sys.argv[1],'https://test.invalid',{}); "
                "guard=session._transaction(); guard.__enter__(); "
                "print('held',flush=True); sys.stdin.readline(); guard.__exit__(None,None,None)"
            )
            child = subprocess.Popen(
                [sys.executable, '-B', '-c', script, folder], cwd=Path(__file__).parent,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            started, entered, finished = threading.Event(), threading.Event(), threading.Event()
            errors = []
            session = DeviceSession(folder, 'https://test.invalid', {})

            def acquire():
                started.set()
                try:
                    with session._transaction():
                        entered.set()
                except Exception as error:
                    errors.append(type(error).__name__)
                finally:
                    finished.set()

            waiter = threading.Thread(target=acquire, daemon=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'held')
                waiter.start()
                self.assertTrue(started.wait(5))
                finished_while_held = finished.wait(.2)
                entered_while_held = entered.is_set()
                child.stdin.write('\n')
                child.stdin.flush()
                child.wait(timeout=5)
                self.assertTrue(finished.wait(5), 'waiter did not acquire released lock')
                self.assertEqual(child.returncode, 0)
                self.assertFalse(entered_while_held, 'mutual exclusion was lost')
                self.assertFalse(finished_while_held, errors)
                self.assertEqual(errors, [])
                self.assertTrue(entered.is_set())
                with session._transaction():
                    pass  # The released lock remains reusable.
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                if waiter.ident is not None:
                    waiter.join(timeout=5)
                child.stdin.close()
                child.stdout.close()
                child.stderr.close()


if __name__ == '__main__':
    unittest.main()
