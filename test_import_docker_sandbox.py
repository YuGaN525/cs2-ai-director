from pathlib import Path
import tempfile
import unittest
from unittest import mock

from demo_import_worker import safe_result_file
from import_docker_sandbox import DockerParserSandbox


class DockerSandboxTests(unittest.TestCase):
    def test_mutable_tag_is_refused(self):
        with self.assertRaises(ValueError):
            DockerParserSandbox('parser:latest')

    def test_command_has_only_job_mount_and_no_host_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch('import_docker_sandbox.os.name', 'posix'):
                sandbox = DockerParserSandbox('sha256:' + 'a' * 64)
            command = sandbox.command(Path(temp), {'maxMemoryMb': 2048})
        self.assertEqual(command.count('--mount'), 1)
        self.assertIn('none', command)
        self.assertIn(f'{sandbox.uid}:{sandbox.gid}', command)
        self.assertGreater(sandbox.uid, 0)
        self.assertIn('--read-only', command)
        self.assertIn('ALL', command)
        self.assertIn('no-new-privileges', command)
        self.assertNotIn('/var/run/docker.sock', ' '.join(command))
        self.assertNotIn('/run/secrets', ' '.join(command))

    def test_result_read_refuses_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with self.assertRaises(ValueError):
                safe_result_file(folder, folder.parent / 'outside.json')
            with self.assertRaises(ValueError):
                safe_result_file(folder, folder / '..' / 'outside.json')

    def test_internal_origin_is_explicit_and_fixed(self):
        from demo_import_worker import ImportClient
        self.assertEqual(ImportClient('http://127.0.0.1:8002', 'k', True).origin, 'http://127.0.0.1:8002')
        with self.assertRaises(ValueError):
            ImportClient('http://evil.example:8002', 'k', True)

    def test_result_read_uses_regular_job_file(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = folder / 'receipt.json'
            path.write_text('{}')
            self.assertEqual(path.resolve(), safe_result_file(folder, path))


if __name__ == '__main__':
    unittest.main()
