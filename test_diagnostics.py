import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import uuid

from diagnostics_privacy import sanitize_log, event_line, exception_lines, read_own_history
from diagnostics_store import DiagnosticStore, binding_key, TTL
from diagnostics_client import DiagnosticClient, DiagnosticFailure, validate_payload
from diagnostics_runtime import Diagnostics, SessionMarker
from upload_outbox import Binding
from upload_worker import UploadIdentity


def identity():
    return UploadIdentity(Binding(str(uuid.uuid4()), 'https://example.test', str(uuid.uuid4())), 'fake-device-token')


CAP = dict(schemaVersion=1, protocol='director-diagnostics-v1', enabled=True,
           maxLogBytes=131072, maxRequestBytes=1048576, retentionDays=30, maxReportsPerDay=5)
RECEIPT = dict(id=str(uuid.uuid4()), status='received', receivedAt='2026-11-11T01:00:00Z', expiresAt='2026-12-11T01:00:00Z')


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps({'success': True, 'data': data}).encode())


class PrivacyTests(unittest.TestCase):
    def test_free_text_tokens_paths_contacts_and_legacy_digits_never_export(self):
        raw = '\n'.join(['2026-11-11 10:00:00 INFO 检测到手动切换（按 1，slot=0）',
            'manual_takeover 123456', 'GUI 回调异常：ValueError: Bearer d1_secret C:\\Users\\Alice\\secret',
            '2026-11-11 10:00:00 ERROR token=sk-secret password=hello 13812345678 someone@example.test',
            '微信聊天 body', '配置目录：C:\\secret', '导播已启动：private-name'])
        value = sanitize_log(raw)
        self.assertIn('event=gui.error', value)
        self.assertIn('event=runtime.error', value)
        self.assertIn('event=director.started', value)
        for secret in ('123456', 'd1_secret', 'sk-secret', 'Alice', 'password', 'someone', 'private-name', '微信', 'manual'):
            self.assertNotIn(secret, value)

    def test_safe_projection_idempotent_and_bounded(self):
        raw = '\n'.join(event_line('runtime.error', 'ERROR', 100+i) for i in range(10000))
        clean = sanitize_log(raw)
        self.assertEqual(clean, sanitize_log(clean))
        self.assertLessEqual(len(clean.encode()), 131072)
        long_lines = '\n'.join(event_line('runtime.error', 'ERROR', i) + ' exception=' + 'E'*64 + ' frame=cs2_ai_director.py:123' for i in range(2000))
        clean = sanitize_log(long_lines)
        self.assertLessEqual(len(clean.encode()), 131072)
        self.assertEqual(clean, sanitize_log(clean))

    def test_traceback_has_no_message_or_external_path(self):
        try: raise ValueError('secret-token-user-path')
        except ValueError as error: text = '\n'.join(exception_lines(type(error), error.__traceback__))
        self.assertIn('exception=ValueError', text)
        self.assertNotIn('secret-token-user-path', text)
        self.assertNotIn(str(Path(__file__).parent), text)

    def test_only_named_own_logs_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'director.log').write_text('导播已启动：secret\n检测到手动切换 1', encoding='utf-8')
            (root/'config.json').write_text('训练完成 private', encoding='utf-8')
            (root/'other.log').write_text('训练失败 private', encoding='utf-8')
            value = read_own_history(root)
            self.assertEqual(value, 'INFO event=director.started')

    def test_unrecognized_structured_field_is_rejected(self):
        self.assertEqual(sanitize_log('INFO event=director.started token=secret'), '')
        self.assertEqual(sanitize_log('ERROR event=runtime.error exception=ValueError frame=private.txt:12'), '')
        self.assertNotIn('d1_secret', sanitize_log('ERROR event=runtime.error exception=d1_secret'))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = 100000
        self.store = DiagnosticStore(self.tmp.name, clock=lambda: self.now)
        self.ident = identity(); self.owner = binding_key(self.ident.binding)

    def add(self, owner=None, kind='manual', fingerprint=''):
        return self.store.enqueue(self.owner if owner is None else owner, kind, '1.16.3', event_line('runtime.error'), fingerprint=fingerprint)

    def test_durable_and_account_device_origin_isolation(self):
        ident = self.add()
        other = identity()
        self.assertIsNone(self.store.claim(binding_key(other.binding)))
        self.assertIsNone(self.store.claim(binding_key(Binding(self.ident.binding.account, self.ident.binding.origin, str(uuid.uuid4())))))
        again = DiagnosticStore(self.tmp.name, clock=lambda: self.now)
        self.assertEqual(again.claim(self.owner)['id'], ident)

    def test_auto_fault_dedup_per_owner_version_24h(self):
        one = self.add(kind='error', fingerprint='ValueError:same-frame')
        self.assertEqual(one, self.add(kind='error', fingerprint='ValueError:same-frame'))
        self.now += 86401
        self.assertNotEqual(one, self.add(kind='error', fingerprint='ValueError:same-frame'))

    def test_twenty_report_bound_and_expiry(self):
        for _ in range(20): self.add()
        with self.assertRaisesRegex(ValueError, 'queue_full'): self.add()
        self.now += TTL+1
        self.assertEqual(self.store.rows(self.owner), [])
        self.add()

    def test_retry_keeps_id_and_content_and_honors_429(self):
        ident = self.add()
        row = self.store.claim(self.owner)
        self.store.finish(row, status=429, retry_after=86400)
        self.assertIsNone(self.store.claim(self.owner))
        self.now += 86401
        retry = self.store.claim(self.owner)
        self.assertEqual((retry['id'], retry['payload']), (ident, row['payload']))

    def test_permanent_4xx_never_loop(self):
        for status in (400, 409, 413):
            ident = self.add(); row = self.store.claim(self.owner)
            self.store.finish(row, status=status)
            self.assertEqual(next(r for r in self.store.rows(self.owner) if r['id'] == ident)['state'], 'rejected')
            self.now += 100000
            self.assertIsNone(self.store.claim(self.owner))

    def test_crash_during_send_recovers_same_payload(self):
        self.add(); first = self.store.claim(self.owner)
        self.assertIsNone(self.store.claim(self.owner))
        self.now += 61
        second = self.store.claim(self.owner)
        self.assertEqual(first['payload'], second['payload'])

    def test_unbound_needs_explicit_binding(self):
        ident = self.add(owner='')
        self.assertIsNone(self.store.claim(self.owner))
        self.store.bind_unbound(ident, self.owner)
        row = self.store.claim(self.owner)
        self.assertEqual(row['id'], ident)
        self.assertEqual(json.loads(row['payload'])['kind'], 'manual')


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ident = identity()
        store = DiagnosticStore(self.tmp.name)
        store.enqueue(binding_key(self.ident.binding), 'manual', '1.16.3', event_line('diagnostics.manual'))
        self.row = store.claim(binding_key(self.ident.binding))
        self.opener = mock.Mock()
        self.client = DiagnosticClient(lambda: self.ident, opener=self.opener)

    def test_exact_frozen_protocol_and_receipt(self):
        self.opener.open.side_effect = [Response(CAP), Response(RECEIPT)]
        self.assertEqual(self.client.upload(self.ident, self.row), RECEIPT)
        request = self.opener.open.call_args_list[1].args[0]
        self.assertEqual(request.full_url, 'https://example.test/api/director/diagnostics')
        self.assertEqual(json.loads(request.data), json.loads(self.row['payload']))
        self.assertNotIn('account', json.loads(request.data))

    def test_identity_changed_between_capability_and_post_blocks(self):
        original = self.ident
        def response(*args, **kwargs): self.ident = identity(); return Response(CAP)
        self.opener.open.side_effect = response
        with self.assertRaises(DiagnosticFailure): self.client.upload(original, self.row)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_disabled_setting_between_capability_and_post_blocks(self):
        enabled = [True]
        self.client.permitted = lambda: enabled[0]
        def response(*args, **kwargs): enabled[0] = False; return Response(CAP)
        self.opener.open.side_effect = response
        with self.assertRaises(DiagnosticFailure): self.client.upload(self.ident, self.row)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_429_header_not_server_body(self):
        self.opener.open.side_effect = urllib.error.HTTPError('https://example.test', 429, 'secret-message', {'Retry-After': '600'}, io.BytesIO(b'secret-body'))
        with self.assertRaises(DiagnosticFailure) as error: self.client.upload(self.ident, self.row)
        self.assertEqual(error.exception.retry_after, 600)
        self.assertNotIn('secret', str(error.exception))

    def test_invalid_receipt_stays_retryable(self):
        self.opener.open.side_effect = [Response(CAP), Response(dict(RECEIPT, expiresAt='2027-01-11T01:00:00Z'))]
        with self.assertRaises(DiagnosticFailure) as error: self.client.upload(self.ident, self.row)
        self.assertEqual(error.exception.status, 502)

    def test_tampered_local_payload_never_transmitted(self):
        value = json.loads(self.row['payload']); value['log'] = 'token=private'
        self.row['payload'] = json.dumps(value)
        with self.assertRaises(DiagnosticFailure): self.client.upload(self.ident, self.row)
        self.opener.open.assert_not_called()

    def test_unknown_request_fields_rejected(self):
        value = json.loads(self.row['payload']); value['userId'] = 'fake'
        with self.assertRaises(DiagnosticFailure): validate_payload(value)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ident = identity()
        self.account = mock.Mock()
        self.account.binding.side_effect = lambda: self.ident.binding
        self.account.upload_identity.side_effect = lambda: self.ident
        self.runtime = Diagnostics(self.tmp.name, account=self.account, start=False)
        self.addCleanup(self.runtime.close)

    def test_capture_never_performs_network_or_persistence_on_input_thread(self):
        with mock.patch.object(self.runtime.store, 'enqueue') as save, mock.patch('urllib.request.OpenerDirector.open') as network:
            self.runtime.capture('error', ValueError)
            save.assert_not_called(); network.assert_not_called()
        self.runtime.drain()
        self.assertEqual(len(self.runtime.store.rows(self.runtime.owner())), 1)

    def test_disabled_stops_capture_and_pending_send_but_manual_allowed(self):
        self.runtime.capture('error'); self.runtime.drain()
        self.runtime.set_enabled(False)
        self.runtime.capture('crash')
        self.assertTrue(self.runtime.events.empty())
        with mock.patch('diagnostics_runtime.DiagnosticClient') as client:
            self.runtime.send_one(); client.assert_not_called()
            preview = self.runtime.preview()
            ident = self.runtime.submit_manual(preview, self.runtime.owner())
            client.return_value.upload.return_value = RECEIPT
            self.runtime.send_one(ident)
            client.return_value.upload.assert_called_once()

    def test_account_changed_since_preview_requires_new_confirmation(self):
        old = self.runtime.owner(); preview = self.runtime.preview()
        self.ident = identity()
        with self.assertRaises(ValueError): self.runtime.submit_manual(preview, old)

    def test_automatic_ring_is_cleared_when_account_changes(self):
        self.runtime.observe(event_line('director.started'))
        self.ident = identity()
        self.runtime.capture('error', ValueError); self.runtime.drain()
        row = self.runtime.store.rows(self.runtime.owner())[0]
        self.assertNotIn('director.started', json.loads(row['payload'])['log'])

    def test_live_session_marker_is_not_a_crash(self):
        marker = self.runtime.marker
        marker.update(self.runtime.owner(), event_line('runtime.started'))
        other = SessionMarker(self.runtime.store.root, self.runtime.store)
        other.recover()
        self.assertEqual(self.runtime.store.rows(self.runtime.owner()), [])
        marker.close()

    def test_abandoned_session_recovered_without_raw_file_contents(self):
        marker = self.runtime.marker
        marker.update(self.runtime.owner(), 'token=secret\n'+event_line('runtime.started'))
        marker.handle.close(); marker.handle = None  # Simulate kernel releasing process locks.
        other = SessionMarker(self.runtime.store.root, self.runtime.store)
        other.recover()
        rows = self.runtime.store.rows(self.runtime.owner())
        self.assertEqual(len(rows), 1)
        value = json.loads(rows[0]['payload'])
        self.assertNotIn('secret', value['log'])
        self.assertIn('runtime.unclean_exit', value['log'])
        self.assertEqual(value['summary'], '程序异常退出（原因待确认）')


if __name__ == '__main__': unittest.main()
