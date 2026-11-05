"""Exercise the actual bridge handler against an isolated HTTP Plex substitute."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import threading
import unittest
from unittest.mock import Mock

from plex_bridge.diagnostics import SafeHandler
from plex_bridge.playback import App, Server
from plex_bridge.symfonium_proxy import SymfoniumProxy


class ProxyHttpTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests
        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                requests.append((self.command, self.path, dict(self.headers), body))
                self.send_response(206)
                self.send_header('Content-Length', '5')
                self.send_header('Content-Range', 'bytes 0-4/20')
                self.end_headers()
                if self.command != 'HEAD': self.wfile.write(b'hello')
            do_POST = do_PUT = do_DELETE = do_HEAD = do_GET
            def log_message(self, *args): pass
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.start(self.upstream)
        self.app = App(Mock(), 'operator', 'unused', 'synthetic-session-secret')
        self.app.symfonium_proxy = SymfoniumProxy(
            'http://127.0.0.1:' + str(self.upstream.server_port), 'http://192.0.2.20:8794')
        self.server = Server(('127.0.0.1', 0), self.app)
        self.start(self.server)
        self.log = io.StringIO()
        logger = logging.getLogger('wiim-focus-control')
        handler = SafeHandler(self.app.diagnostics, self.log)
        old = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.setLevel, old)
        self.addCleanup(logger.removeHandler, handler)

    def start(self, server):
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.shutdown(); server.server_close(); worker.join()
        self.addCleanup(close)

    def request(self, path, method='GET', body=None, auth=True):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'X-Plex-Token': 'synthetic-caller-private', 'Range': 'bytes=0-4'} if auth else {}
        connection.request(method, '/symfonium' + path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_reports_are_suppressed_but_library_and_queue_use_caller_auth(self):
        for method in ('GET', 'POST', 'PUT'):
            self.assertEqual(self.request('/:/timeline?key=private-track', method)[0], 200)
            self.assertEqual(self.request('/:/scrobble?key=private-track', method)[0], 200)
        self.assertEqual(self.requests, [])
        status, headers, body = self.request('/library/sections')
        self.assertEqual((status, body), (206, b'hello'))
        self.assertEqual(headers['Content-Range'], 'bytes 0-4/20')
        self.request('/playQueues', 'POST', b'queue-body')
        self.assertEqual(self.requests[-1][3], b'queue-body')
        self.assertEqual(self.requests[-1][2]['X-Plex-Token'], 'synthetic-caller-private')
        self.assertEqual(self.requests[-1][1], '/playQueues')
        rows = [json.loads(line) for line in self.log.getvalue().splitlines()]
        self.assertIn('proxy_report_suppressed', [r['event'] for r in rows])
        for secret in ('private-track', 'synthetic-caller-private', 'queue-body', '/playQueues'):
            self.assertNotIn(secret, self.log.getvalue())

    def test_missing_auth_is_rejected_before_upstream_and_recorded_safely(self):
        self.assertEqual(self.request('/library/sections', auth=False)[0], 401)
        self.assertEqual(self.requests, [])
        self.assertIn('proxy_auth_required', self.log.getvalue())

    def test_control_poll_and_head_are_not_listen_reports(self):
        self.assertEqual(self.request('/player/timeline/poll?wait=0')[0], 206)
        self.assertEqual(self.request('/library/sections', 'HEAD')[2], b'')
        self.assertEqual(len(self.requests), 2)

    def test_disconnected_upstream_has_safe_failure_event(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.assertEqual(self.request('/library/sections')[0], 502)
        self.assertIn('proxy_forward_failed', self.log.getvalue())
        self.assertNotIn('synthetic-caller-private', self.log.getvalue())
