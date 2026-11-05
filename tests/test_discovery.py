import io
import unittest
from unittest.mock import Mock, patch

from plex_bridge.discovery import location, describe, gdm_candidate, verify_plex, discover


class DiscoveryTests(unittest.TestCase):
    def test_ssdp_locations_are_bound_to_responder(self):
        def packet(url):
            return ("HTTP/1.1 200 OK\r\nLOCATION: " + url + "\r\n\r\n").encode()
        self.assertEqual(location(packet("http://192.168.1.2:49152/description.xml"), "192.168.1.2"),
                         "http://192.168.1.2:49152/description.xml")
        for url in ("http://192.168.1.3/x", "http://evil.invalid/x", "http://user@192.168.1.2/x",
                    "file:///x", "http://192.168.1.2/x?secret=x", "http://192.168.1.2:99999/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                location(packet(url), "192.168.1.2")
        for host in ("127.0.0.1", "169.254.1.2", "8.8.8.8", "0.0.0.0"):
            with self.assertRaises(ValueError):
                location(packet("http://" + host + "/x"), host)

    def test_wiim_description_and_non_wiim_filter(self):
        for model, expected in (("WiiM Pro", True), ("Unrelated Renderer", False)):
            body = ('<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
                    '<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>'
                    '<friendlyName>Office &amp; Desk</friendlyName><modelName>' + model +
                    '</modelName><manufacturer>Linkplay</manufacturer></device></root>').encode()
            opener = Mock()
            opener.open.return_value = io.BytesIO(body)
            with patch("plex_bridge.discovery.urllib.request.build_opener", return_value=opener) as build:
                result = describe("192.168.1.2", "http://192.168.1.2:49152/description.xml")
                self.assertEqual(bool(result), expected)
                self.assertEqual(build.call_args.args[0].proxies, {})
                self.assertIsNone(build.call_args.args[1].redirect_request(None, None, None, None, None))
                if result:
                    self.assertEqual(result["name"], "Office & Desk")
                    self.assertEqual(result["kind"], "wiim")

    def test_hostile_or_oversized_xml_is_rejected(self):
        for body in (b"x" * 65537, b'<!DOCTYPE x><root/>', b'<broken'):
            opener = Mock()
            opener.open.return_value = io.BytesIO(body)
            with patch("plex_bridge.discovery.urllib.request.build_opener", return_value=opener):
                self.assertIsNone(describe("192.168.1.2", "http://192.168.1.2/x"))

    def test_gdm_uses_packet_sender_not_advertised_hostname(self):
        packet = b'HTTP/1.0 200 OK\r\nContent-Type: plex/media-server\r\nPort: 32400\r\nName: Library\r\nHost: evil.invalid\r\n'
        item = gdm_candidate(packet, "192.168.1.20")
        self.assertEqual(item["origin"], "http://192.168.1.20:32400")
        for invalid in (packet.replace(b"32400", b"0"), packet.replace(b"media-server", b"media-player"), b"garbage"):
            with self.assertRaises(ValueError):
                gdm_candidate(invalid, "192.168.1.20")

    def test_plex_requires_identity(self):
        item = {"origin": "http://192.168.1.20:32400"}
        for body, valid in ((b'<MediaContainer machineIdentifier="server"/>', True), (b'<html/>', False)):
            opener = Mock()
            opener.open.return_value = io.BytesIO(body)
            with patch("plex_bridge.discovery.urllib.request.build_opener", return_value=opener):
                self.assertEqual(bool(verify_plex(item)), valid)
                opener.open.assert_called_once_with(item["origin"] + "/identity", timeout=2)

    def test_discovery_merges_protocols_and_tolerates_one_unavailable(self):
        item = {"kind": "plex", "name": "Plex", "host": "192.168.1.20", "origin": "http://192.168.1.20:32400"}
        with patch("plex_bridge.discovery.discover_ssdp", return_value=[item]), patch("plex_bridge.discovery.discover_plex", return_value=[item]):
            self.assertEqual(discover("192.168.1.10"), [item])
        with patch("plex_bridge.discovery.discover_ssdp", side_effect=OSError), patch("plex_bridge.discovery.discover_plex", return_value=[item]):
            self.assertEqual(discover("192.168.1.10"), [item])
        with patch("plex_bridge.discovery.discover_ssdp", side_effect=OSError), patch("plex_bridge.discovery.discover_plex", side_effect=OSError):
            with self.assertRaises(OSError):
                discover("192.168.1.10")
