import subprocess
import unittest
from unittest import mock

from demo_import_worker import verify_sandbox


class SandboxPreflightTests(unittest.TestCase):
    def test_installed_binary_with_unavailable_namespace_is_refused(self):
        with mock.patch('demo_import_worker.parser_command', return_value=['sandbox']), \
             mock.patch('demo_import_worker.subprocess.run', return_value=mock.Mock(returncode=1, stdout=b'')):
            with self.assertRaisesRegex(RuntimeError, 'import_sandbox_unavailable'):
                verify_sandbox('/work', '/7zz')

    def test_probe_requires_exact_acknowledgement(self):
        with mock.patch('demo_import_worker.parser_command', return_value=['sandbox']), \
             mock.patch('demo_import_worker.subprocess.run', return_value=mock.Mock(returncode=0, stdout=b'')):
            with self.assertRaisesRegex(RuntimeError, 'import_sandbox_unavailable'):
                verify_sandbox('/work', '/7zz')

    def test_probe_timeout_is_bounded_and_refused(self):
        with mock.patch('demo_import_worker.parser_command', return_value=['sandbox']), \
             mock.patch('demo_import_worker.subprocess.run', side_effect=subprocess.TimeoutExpired(['sandbox'], 15)):
            with self.assertRaisesRegex(RuntimeError, 'import_sandbox_unavailable'):
                verify_sandbox('/work', '/7zz')

    def test_success_uses_secret_free_environment(self):
        with mock.patch('demo_import_worker.parser_command', return_value=['sandbox']), \
             mock.patch.dict('os.environ', {'PRIVATE_WORKER_KEY': 'do-not-forward'}), \
             mock.patch('demo_import_worker.subprocess.run', return_value=mock.Mock(returncode=0, stdout=b'director-import-sandbox-ready\n')) as run:
            verify_sandbox('/work', '/7zz')
            self.assertNotIn('PRIVATE_WORKER_KEY', run.call_args.kwargs['env'])
            self.assertEqual(run.call_args.args[0][-1], '--probe')


if __name__ == '__main__':
    unittest.main()
