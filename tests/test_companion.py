import unittest
from collections import OrderedDict
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from plex_bridge.companion import CompanionRelay


class CompanionTests(unittest.TestCase):
    def test_proxy_request_has_required_platform_and_xml_headers(self):
        cloud = Mock()
        cloud.headers.return_value = {"Accept": "application/json", "X-Plex-Token": "private"}
        relay = CompanionRelay(Mock(), cloud, "http://192.0.2.1:32400")
        with patch("plex_bridge.companion.urllib.request.urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = b"<MediaContainer />"
            relay.request("room-a", "/player/proxy/poll")
        headers = dict((k.lower(), v) for k, v in opener.call_args.args[0].header_items())
        self.assertEqual(headers["accept"], "application/xml")
        self.assertEqual(headers["x-plex-platform-version"], "1")
        self.assertEqual(headers["x-plex-token"], "private")

    def setUp(self):
        self.plex = Mock()
        self.relay = CompanionRelay(self.plex, Mock(), "http://192.0.2.1:32400")
        self.relay.request = Mock(return_value=b"")
        self.relay.publish_timeline = Mock()

    def command(self, path, **kw):
        return ET.Element("Command", dict(path=path, commandID="7",
                          clientIdentifier="controller", **kw))

    def test_play_media_preserves_queue_and_fixes_target_room(self):
        cmd = self.command("/player/playback/playMedia", queryContainerKey="/playQueues/42",
                           queryToken="private", queryOffset="1400", queryKey="/library/metadata/8",
                           queryX_Plex_Target_Client_Identifier="wrong-room")
        self.relay.dispatch("room-a", cmd)
        params, endpoint = self.plex.play_media.call_args.args
        self.assertEqual(endpoint, "room-a")
        self.assertEqual(params["containerKey"], ["/playQueues/42"])
        self.assertEqual(params["offset"], ["1400"])

    def test_ack_failure_does_not_repeat_playback(self):
        cmd = self.command("/player/playback/skipNext")
        completed = OrderedDict()
        self.relay.request.side_effect = [OSError(), b""]
        with self.assertRaises(OSError):
            self.relay.handle("room-a", cmd, completed)
        self.relay.handle("room-a", cmd, completed)
        self.plex.playback_command.assert_called_once_with("skipNext", {"commandID": ["7"]}, endpoint_identifier="room-a")

    def test_unknown_paths_cannot_fetch_or_control_other_services(self):
        for path in ["http://example.com/", "/dashboard/sleep", "/player/playback/play/extra"]:
            self.assertEqual(self.relay.dispatch("room-a", self.command(path)), 404)
        self.plex.play_media.assert_not_called()
        self.plex.playback_command.assert_not_called()

    def test_reconnected_controller_can_reuse_command_id(self):
        completed = OrderedDict()
        cmd = self.command("/player/playback/skipNext")
        with patch("plex_bridge.companion.time.monotonic", side_effect=[0, 61]):
            self.relay.handle("room-a", cmd, completed)
            self.relay.handle("room-a", cmd, completed)
        self.assertEqual(self.plex.playback_command.call_count, 2)

    def test_subscribe_does_not_create_caller_controlled_callback(self):
        self.assertEqual(self.relay.dispatch("room-a", self.command("/player/timeline/subscribe", queryProtocol="http://example.com", queryPort="80")), 200)
        self.plex.subscribe_timeline.assert_not_called()

    def test_failed_command_returns_failure_and_is_not_replayed(self):
        self.plex.playback_command.side_effect = ValueError("invalid")
        completed = OrderedDict()
        cmd = self.command("/player/playback/seekTo", queryOffset="bad")
        self.relay.handle("room-a", cmd, completed)
        self.relay.handle("room-a", cmd, completed)
        self.plex.playback_command.assert_called_once()
        body = self.relay.request.call_args.args[3]
        self.assertEqual(ET.fromstring(body).get("code"), "400")

    def test_timeline_does_not_report_an_extra_listen(self):
        relay = CompanionRelay(self.plex, Mock(), "http://192.0.2.1:32400")
        relay.request = Mock()
        self.plex.timeline_xml.return_value = b'<MediaContainer><Timeline type="music" state="paused" /></MediaContainer>'
        relay.publish_timeline("room-a", "9")
        self.assertEqual(relay.request.call_args.args[1], "/player/proxy/timeline")
        self.assertEqual(relay.request.call_args.args[2], {"commandID": "9", "state": "paused"})


if __name__ == "__main__":
    unittest.main()
