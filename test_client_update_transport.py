import io
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch, Mock
import urllib.error
import client_update_transport as transport


class Response(io.BytesIO):
    status = 200


class TransportTests(unittest.TestCase):
    def test_retry_after_seconds_dates_and_invalid(self):
        self.assertEqual(transport.retry_delay('120'),120)
        self.assertEqual(transport.retry_delay('999999999'),21600)
        self.assertEqual(transport.retry_delay('Thu, 01 Jan 1970 00:02:00 GMT',now=100),20)
        self.assertIsNone(transport.retry_delay('not a date'))

    def test_anonymous_bounded_response(self):
        opener=Mock()
        opener.open.return_value=Response(b'{"success":true,"code":"OK","data":[]}')
        with patch.object(transport.urllib.request,'build_opener',return_value=opener):
            value=transport.fetch_once('https://example.org','stable')
        self.assertTrue(value['envelope']['success'])
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://example.org/api/director/client-releases?channel=stable')
        self.assertNotIn('Authorization',dict(request.header_items()))
        self.assertNotIn('Cookie',dict(request.header_items()))

    def test_oversized_and_invalid_json(self):
        for body in [b'x'*(transport.MAX_RESPONSE_BYTES+1),b'<html>error</html>']:
            opener=Mock();opener.open.return_value=Response(body)
            with patch.object(transport.urllib.request,'build_opener',return_value=opener):
                self.assertIn('error',transport.fetch_once('https://example.org','stable'))

    def test_redirect_is_never_followed(self):
        self.assertIsNone(transport.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))

    def test_total_timeout_kills_actual_child(self):
        actual_popen=subprocess.Popen
        children=[]
        def sleeper(command,**options):
            child=actual_popen([sys.executable,'-c','import time; time.sleep(60)'],**options)
            children.append(child)
            return child
        started=time.monotonic()
        with patch.object(transport.subprocess,'Popen',side_effect=sleeper):
            value=transport.check('https://example.org','stable',threading.Event(),timeout=.2)
        self.assertEqual(value,{'error':'timeout'})
        self.assertLess(time.monotonic()-started,5)
        self.assertIsNotNone(children[0].poll())

    def test_cancel_before_spawn(self):
        cancel=threading.Event();cancel.set()
        with patch.object(transport.subprocess,'Popen') as spawn:
            self.assertEqual(transport.check('https://example.org','stable',cancel),{'error':'cancelled'})
            spawn.assert_not_called()


if __name__=='__main__':
    unittest.main()
