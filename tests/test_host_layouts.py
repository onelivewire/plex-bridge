import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from plex_bridge.config import parse_config
from plex_bridge.playback import Device, PlexBridge, PlexSessionStore, FocusError
from acceptance_helpers import approved_plex_location, plex_location, plex_identity
from test_playback import FakePlexServer, FakeRenderer


class HostLayoutTests(unittest.TestCase):
    def test_same_and_separate_hosts_route_media_to_plex_and_speaker_to_bridge(self):
        bridge_origin = "http://192.0.2.10:8794"
        for plex_origin in ("http://192.0.2.10:32400", "http://192.0.2.20:32400",
                            "https://192.0.2.20:33443"):
            with self.subTest(plex=plex_origin), tempfile.TemporaryDirectory() as directory:
                config = parse_config({"schema_version": 1, "advertised_origin": bridge_origin,
                    "plex_server_origin": plex_origin, "default_player_id": "room",
                    "players": [{"id": "room", "name": "Room", "host": "192.0.2.30"}]})
                server, renderer = FakePlexServer(), FakeRenderer()
                root = Path(directory)
                bridge = PlexBridge(PlexSessionStore(root / "session.json", root / "token.json"),
                    server, "test-endpoint", config.plex_server_origin,
                    [Device("room", "Room", "192.0.2.30")], renderer,
                    public_base=config.advertised_origin, media_secret="synthetic-secret")
                _, address = approved_plex_location(plex_origin, plex_origin)
                params = {key: [value] for key, value in address.items()}
                params.update(machineIdentifier=["test-server"], token=["synthetic-token"],
                              key=["/library/metadata/12"], containerKey=["/playQueues/42"], offset=["0"])
                session = bridge.play_media(params, "test-endpoint")
                self.assertEqual(server.calls[0][0], plex_origin)
                self.assertEqual(session.server_base, plex_origin)
                speaker_url = renderer.started[0][2]
                self.assertTrue(speaker_url.startswith(bridge_origin + "/plex/media/"))
                request = bridge.media_request(urlsplit(speaker_url).path, "bytes=0-99")
                self.assertEqual(request.full_url, plex_origin + "/library/parts/12/file.flac")
                self.assertEqual(request.get_header("Range"), "bytes=0-99")
                self.assertEqual(request.get_header("X-plex-token"), "synthetic-token")
                self.assertNotIn("synthetic-token", speaker_url)
                self.assertIn(b'address="192.0.2.10"', bridge.resources_xml())
                self.assertEqual(config.preset_url("focus"), bridge_origin + "/preset/focus")
                params["address"] = ["192.0.2.99"]
                with self.assertRaises(FocusError):
                    bridge.play_media(params, "test-endpoint")
                self.assertEqual(len(server.calls), 1)

    def test_default_ports_and_trailing_slash_are_normalized(self):
        self.assertEqual(plex_location("http://192.0.2.20/")[0], "http://192.0.2.20:80")
        origin, params = approved_plex_location("https://192.0.2.20", "https://192.0.2.20:443/")
        self.assertEqual(origin, "https://192.0.2.20:443")
        self.assertEqual(params, {"protocol": "https", "address": "192.0.2.20", "port": "443"})

    def test_stale_queue_host_or_port_is_rejected(self):
        for saved in ("http://192.0.2.10:32400", "http://192.0.2.20:32401", "https://192.0.2.20:32400"):
            with self.subTest(saved=saved), self.assertRaises(ValueError):
                approved_plex_location(saved, "http://192.0.2.20:32400")

    def test_unsafe_origins_are_rejected_without_echoing_secrets(self):
        for origin in ("http://user:secret@192.0.2.20", "http://192.0.2.20/?token=secret",
                       "http://192.0.2.20/secret", "http://192.0.2.20/#secret",
                       "http://192.0.2.20:0", "http://192.0.2.20:65536", "file:///secret",
                       "http://127.0.0.1", "http://0.0.0.0", "http://169.254.1.1", "http://224.0.0.1",
                       "http://example.invalid", "http://192.0.2.20\n", "http://[broken"):
            with self.subTest(origin=origin), self.assertRaises(ValueError) as raised:
                plex_location(origin)
            self.assertNotIn("secret", str(raised.exception))

    def test_identity_check_uses_explicit_server_without_credentials(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'<MediaContainer machineIdentifier="server-a" version="1"/>')
        with patch("acceptance_helpers.urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(plex_identity("http://192.0.2.20:32400"), {"machineIdentifier": "server-a", "version": "1"})
        opener.open.assert_called_once_with("http://192.0.2.20:32400/identity", timeout=10)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, None, None, None))

    def test_non_plex_identity_is_rejected(self):
        for body in (b'<html/>', b'<MediaContainer/>'):
            opener = Mock()
            opener.open.return_value = io.BytesIO(body)
            with patch("acceptance_helpers.urllib.request.build_opener", return_value=opener), self.assertRaises(ValueError):
                plex_identity("http://192.0.2.20:32400")
