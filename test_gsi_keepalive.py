"""Real loopback HTTP connection regressions; never sends input to the game."""
import http.client
import json
import socket
import time
import unittest

from director_adapters.gsi import GSIServer, SnapshotStore
from test_cs2_ai_director import payload, player


class GsiKeepAliveTests(unittest.TestCase):
    def test_stop_closes_persistent_client_and_restart_receives_new_session(self):
        first = SnapshotStore()
        old = GSIServer('127.0.0.1', 0, first, lambda _: None)
        old.start()
        port = old._httpd.server_port
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        body = json.dumps(payload(players=[player(1, 'CT')])).encode()
        headers = {'Content-Type': 'application/json'}
        fresh = None
        new_connection = None
        try:
            for _ in range(3):
                connection.request('POST', '/', body, headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            self.assertEqual(first.stats()[0], 3)
            live_socket = connection.sock
            self.assertIsNotNone(live_socket)
            old.stop()
            # A keepalive client must see EOF immediately; a two-second idle
            # timeout alone does not work for CS2 sending every 200ms.
            live_socket.settimeout(0.5)
            try:
                self.assertEqual(live_socket.recv(1), b'')
            except ConnectionResetError:
                pass
            second = SnapshotStore()
            fresh = GSIServer('127.0.0.1', port, second, lambda _: None)
            fresh.start()
            new_connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            new_connection.request('POST', '/', body, headers)
            response = new_connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            self.assertEqual(second.stats()[0], 1)
            self.assertEqual(first.stats()[0], 3)
        finally:
            connection.close()
            if new_connection: new_connection.close()
            old.stop()
            if fresh: fresh.stop()

    def test_stop_interrupts_client_waiting_for_incomplete_body(self):
        server = GSIServer('127.0.0.1', 0, SnapshotStore(), lambda _: None)
        server.start()
        connection = socket.create_connection(('127.0.0.1', server._httpd.server_port), 2)
        httpd = server._httpd
        try:
            connection.sendall(b'POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{')
            deadline = time.monotonic()+1
            while time.monotonic()<deadline:
                with httpd._connections_lock:
                    if httpd._connections: break
                time.sleep(.01)
            else:
                self.fail('Client was not accepted')
            server.stop()
            connection.settimeout(.5)
            try:
                self.assertEqual(connection.recv(1), b'')
            except ConnectionResetError:
                pass
            self.assertEqual(server.store.stats()[0], 0)
        finally:
            connection.close()
            server.stop()


if __name__ == '__main__':
    unittest.main()
