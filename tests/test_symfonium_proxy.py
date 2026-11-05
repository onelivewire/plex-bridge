import io
import unittest
from unittest.mock import Mock, patch
from plex_bridge.symfonium_proxy import SymfoniumProxy

class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.proxy = SymfoniumProxy('http://192.0.2.10:32400', 'http://192.0.2.20:8794')
        self.h = Mock(path='/symfonium/library/sections?X-Plex-Token=caller', command='GET',
                      headers={'X-Plex-Token':'caller', 'Range':'bytes=0-4'},
                      rfile=io.BytesIO(), wfile=io.BytesIO())
        self.c = Mock()
        self.r = self.c.getresponse.return_value
        self.r.status = 206
        self.r.getheader.return_value = ''
        self.r.getheaders.return_value = [('Content-Length','5'), ('Content-Range','bytes 0-4/20')]
        self.r.read.side_effect = [b'hello', b'']
        p = patch('plex_bridge.symfonium_proxy.http.client.HTTPConnection', return_value=self.c)
        self.factory = p.start()
        self.addCleanup(p.stop)

    def test_listen_reports_never_reach_plex(self):
        for path in ['/:/timeline','/:/scrobble','/%3A/timeline']:
            self.h.path = '/symfonium' + path + '?ratingKey=123'
            self.proxy.handle(self.h)
            self.assertEqual(self.h.send_body.call_args.args[0], 200)
        self.factory.assert_not_called()

    def test_library_range_auth_and_response_preserved(self):
        self.proxy.handle(self.h)
        self.factory.assert_called_once_with('192.0.2.10',32400,timeout=40)
        args=self.c.request.call_args
        self.assertEqual(args.args[:2],('GET','/library/sections?X-Plex-Token=caller'))
        self.assertEqual(args.kwargs['headers']['X-Plex-Token'],'caller')
        self.assertEqual(args.kwargs['headers']['Range'],'bytes=0-4')
        self.assertEqual(self.h.wfile.getvalue(),b'hello')
        self.h.send_header.assert_any_call('Content-Range','bytes 0-4/20')

    def test_control_timeline_is_forwarded(self):
        self.h.path='/symfonium/player/timeline/poll?wait=0'
        self.proxy.handle(self.h)
        self.assertEqual(self.c.request.call_args.args[1],'/player/timeline/poll?wait=0')

    def test_queue_creation_body_and_target_preserved(self):
        self.h.command='POST'
        self.h.path='/symfonium/playQueues?type=audio'
        self.h.headers.update({'Content-Length':'5','X-Plex-Target-Client-Identifier':'room'})
        self.h.rfile=io.BytesIO(b'queue')
        self.proxy.handle(self.h)
        self.assertEqual(self.c.request.call_args.kwargs['body'],b'queue')
        self.assertEqual(self.c.request.call_args.kwargs['headers']['X-Plex-Target-Client-Identifier'],'room')

    def test_proxy_media_address_uses_fixed_server(self):
        target,_=self.proxy.target('/symfonium/player/playback/playMedia?address=192.0.2.20&port=8794&protocol=http&key=123')
        self.assertIn('address=192.0.2.10',target)
        self.assertIn('port=32400',target)
        self.assertIn('key=123',target)

    def test_external_targets_and_traversal_rejected(self):
        for path in ['/symfonium//evil.test/x','/symfonium/%2f%2fevil.test','/symfonium/../login',
                     '/symfonium/%2e%2e/login','http://evil.test/symfonium/library']:
            self.h.path=path
            self.proxy.handle(self.h)
            self.assertEqual(self.h.send_body.call_args.args[0],400)
        self.factory.assert_not_called()

    def test_hop_headers_and_bridge_cookie_not_forwarded(self):
        self.h.headers.update({'Connection':'X-Secret','X-Secret':'secret','Cookie':'bridge-session'})
        self.proxy.handle(self.h)
        headers=self.c.request.call_args.kwargs['headers']
        self.assertNotIn('Cookie',headers)
        self.assertNotIn('X-Secret',headers)

    def test_auth_failure_preserved_without_injected_token(self):
        self.r.status=401
        self.h.headers={'X-Plex-Token':'rejected-by-server'}
        self.h.path='/symfonium/library/sections'
        self.proxy.handle(self.h)
        self.h.send_response.assert_called_once_with(401)
        self.assertEqual(self.c.request.call_args.kwargs['headers']['X-Plex-Token'],'rejected-by-server')

    def test_missing_and_placeholder_tokens_require_login(self):
        for token in ['', 'null', 'undefined', 'None']:
            self.h.path='/symfonium/library/sections'
            self.h.headers={'X-Plex-Token':token}
            self.proxy.handle(self.h)
            self.assertEqual(self.h.send_body.call_args.args[0],401)
        self.factory.assert_not_called()

    def test_cast_placeholder_uses_only_callers_token(self):
        self.h.path='/symfonium/player/playback/playMedia?token=null&key=123'
        self.proxy.handle(self.h)
        self.assertIn('token=caller',self.c.request.call_args.args[1])
        self.assertNotIn('token=null',self.c.request.call_args.args[1])

    def test_valid_cast_token_is_preserved(self):
        self.h.path='/symfonium/player/playback/playMedia?token=media-credential&key=123'
        self.proxy.handle(self.h)
        self.assertIn('token=media-credential',self.c.request.call_args.args[1])

    def test_redirect_does_not_send_token_to_other_origin(self):
        self.r.status=302
        self.r.getheaders.return_value=[('Location','http://evil.test/collect')]
        self.proxy.handle(self.h)
        self.c.request.assert_called_once()
        self.assertNotIn('Location',[c.args[0] for c in self.h.send_header.call_args_list])

    def test_relative_redirect_stays_in_proxy(self):
        self.r.getheaders.return_value=[('Location','/library/sections')]
        self.proxy.handle(self.h)
        self.h.send_header.assert_any_call('Location','/symfonium/library/sections')

if __name__=='__main__': unittest.main()
