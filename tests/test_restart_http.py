from http.client import HTTPConnection
import threading
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
from plex_bridge.playback import App, Server


class RestartHttpTests(unittest.TestCase):
    def test_restart_requires_authentication_and_same_origin(self):
        plex = SimpleNamespace(public_base='http://192.0.2.10:8794')
        app = App(Mock(), 'operator', 'unused', 'synthetic-session-secret', plex=plex)
        app.dashboard_ui = Mock()
        app.dashboard_ui.act.return_value = 'Restart requested'
        server = Server(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            cookie = 'wiim_focus_session=' + app.session_token()
            for headers, expected in [({}, 303), ({'Cookie':cookie}, 403),
                    ({'Cookie':cookie, 'Origin':'http://evil.invalid'}, 403),
                    ({'Cookie':cookie, 'Origin':plex.public_base}, 200)]:
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                conn.request('POST', '/dashboard/action', 'action=restart&requester=room&confirm=yes',
                             headers={**headers, 'Accept':'application/json'})
                response = conn.getresponse()
                self.assertEqual(response.status, expected)
                response.read(); conn.close()
                if expected != 200: app.dashboard_ui.act.assert_not_called()
            app.dashboard_ui.act.assert_called_once()
        finally:
            server.shutdown();server.server_close();thread.join()
