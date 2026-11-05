import html
import base64
import json
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, Mock

from plex_bridge.playback import (
    Coordinator,
    Device,
    EndpointAdvertisement,
    EventHistory,
    FocusError,
    GroupClient,
    PlexBridge,
    PlexCloudClient,
    PlexCloudHttp,
    PlexEndpointPublisher,
    PlexQueueItem,
    PlexSession,
    RendererSnapshot,
    PlexSessionStore,
    PlexServerClient,
    Server,
    SpotifyClient,
    WiiMRendererClient,
    load_devices,
    silent_wav,
)


ROOM = Device("office", "Office", "192.0.2.33")
SITTING = Device("study", "Study", "192.0.2.143")
DESK = Device("desk", "Studio", "192.0.2.13")


def player(device, role="standalone/leader", master_host="", playback="play"):
    return {
        "device": {"id": device.id, "name": device.name, "host": device.host},
        "online": True,
        "role": role,
        "master_host": master_host,
        "playback": playback,
    }


class FakeGroups:
    def __init__(self, players):
        self.current = players
        self.commands = []

    def players(self):
        return self.current

    def disband(self, requester):
        self.commands.append(("disband", requester))
        for item in self.current:
            item["role"] = "standalone/leader"
            item["master_host"] = ""

    def sleep(self, requester):
        self.commands.append(("sleep", requester))

    def join(self, requester, leader):
        self.commands.append(("join", requester, leader))

    def leave(self, requester):
        self.commands.append(("leave", requester))
        current = next(
            (item for item in self.current if item["device"]["id"] == requester),
            None,
        )
        if current is None:
            return
        if current.get("role") == "follower":
            current["role"] = "standalone/leader"
            current["master_host"] = ""
            return
        for item in self.current:
            if item.get("master_host") == current["device"]["host"]:
                item["role"] = "standalone/leader"
                item["master_host"] = ""

    def stop(self, requester):
        self.commands.append(("stop", requester))


class FakeSpotify:
    def __init__(self):
        self.authorized = True
        self.commands = []
        self.progress = 1000
        self.fail_verification = False

    def playback(self):
        self.progress += 1000
        return {
            "device": {"id": "room-spotify"},
            "is_playing": not self.fail_verification,
            "progress_ms": self.progress,
            "context": {"uri": "spotify:album:album"},
            "item": {"uri": "spotify:track:track"},
        }

    def devices(self):
        return [
            {"id": "room-spotify", "name": ROOM.name, "is_restricted": False},
            {"id": "sitting-spotify", "name": SITTING.name, "is_restricted": False},
        ]

    def transfer(self, device_id):
        self.commands.append(("transfer", device_id))

    def reconstruct(self, device_id, snapshot):
        self.commands.append(("reconstruct", device_id, snapshot["item"]["uri"]))


class UnauthorizedSpotify(FakeSpotify):
    def __init__(self):
        super().__init__()
        self.authorized = False


class FakePlexFocus:
    class Store:
        def snapshot(self):
            return "plex-snapshot"

    def __init__(self, fail=False):
        self.store = self.Store()
        self.fail = fail
        self.commands = []
        self.operation_lock = threading.RLock()

    def recoverable(self):
        return True

    def retarget(self, device):
        self.commands.append(("retarget", device.id))
        if self.fail:
            raise FocusError("Plex verification failed")

    def restore(self, snapshot):
        self.commands.append(("restore", snapshot))


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def coordinator(self):
        groups = FakeGroups(
            [
                player(ROOM, "follower", SITTING.host),
                player(SITTING),
                player(DESK, playback="stop"),
            ]
        )
        spotify = FakeSpotify()
        coordinator = Coordinator(
            [ROOM, SITTING, DESK],
            groups,
            spotify,
            EventHistory(Path(self.temporary.name) / "history.json"),
        )
        return coordinator, groups, spotify

    @patch("plex_bridge.playback.time.sleep")
    def test_focus_follower_transfers_then_sleeps_former_leader(self, _sleep):
        coordinator, groups, spotify = self.coordinator()

        coordinator.focus(ROOM)

        self.assertEqual(
            groups.commands,
            [("disband", ROOM.id), ("sleep", SITTING.id)],
        )
        self.assertEqual(spotify.commands, [("transfer", "room-spotify")])
        self.assertEqual(coordinator.history.snapshot()[0]["outcome"], "focused")

    @patch("plex_bridge.playback.time.sleep")
    def test_failed_verification_attempts_restore_and_regroup(self, _sleep):
        coordinator, groups, spotify = self.coordinator()
        spotify.fail_verification = True

        with self.assertRaises(FocusError):
            coordinator.focus(ROOM)

        self.assertIn(("reconstruct", "sitting-spotify", "spotify:track:track"), spotify.commands)
        self.assertIn(("join", ROOM.id, SITTING.id), groups.commands)

    def test_disband_sleeps_every_former_member(self):
        coordinator, groups, _ = self.coordinator()

        coordinator.disband(ROOM)

        self.assertEqual(
            groups.commands,
            [("disband", ROOM.id), ("sleep", ROOM.id), ("sleep", SITTING.id)],
        )

    def test_unknown_preset_source_is_rejected(self):
        coordinator, groups, _ = self.coordinator()

        scheduled = coordinator.schedule("focus", "192.0.2.250")

        self.assertFalse(scheduled)
        self.assertEqual(groups.commands, [])
        self.assertEqual(coordinator.history.snapshot()[0]["outcome"], "rejected")

    @patch("plex_bridge.playback.threading.Thread")
    def test_duplicate_preset_stream_request_is_debounced(self, thread):
        coordinator, groups, _ = self.coordinator()
        coordinator.focus = lambda _requester: None
        thread.side_effect = lambda target, daemon: type(
            "ImmediateThread", (), {"start": lambda _self: target()}
        )()

        self.assertTrue(coordinator.schedule("focus", ROOM.host))
        self.assertFalse(coordinator.schedule("focus", ROOM.host))

        self.assertEqual(groups.commands, [])
        self.assertEqual(coordinator.history.snapshot()[0]["outcome"], "debounced")

    @patch("plex_bridge.playback.threading.Thread")
    def test_failed_scheduled_action_stops_repeating_preset_stream(self, thread):
        coordinator, groups, _ = self.coordinator()

        def fail(_requester):
            raise FocusError("dependency unavailable")

        coordinator.focus = fail
        thread.side_effect = lambda target, daemon: type(
            "ImmediateThread", (), {"start": lambda _self: target()}
        )()

        self.assertTrue(coordinator.schedule("focus", ROOM.host))

        self.assertEqual(groups.commands, [("stop", ROOM.id)])
        self.assertEqual(coordinator.history.snapshot()[0]["outcome"], "failed")

    def test_preset_audio_allows_five_seconds_for_async_action(self):
        wav = silent_wav()

        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(len(wav), 44 + (8000 * 5 * 2))

    def test_unsupported_source_stops_action_stream_without_disbanding(self):
        coordinator, groups, _ = self.coordinator()
        coordinator.spotify = UnauthorizedSpotify()

        with self.assertRaises(FocusError):
            coordinator.focus(ROOM)

        self.assertEqual(groups.commands, [("stop", ROOM.id)])

    def test_plex_focus_from_group_retargets_then_sleeps_every_former_room(self):
        coordinator, groups, spotify = self.coordinator()
        plex = FakePlexFocus()
        coordinator.plex = plex

        coordinator.focus(ROOM)

        self.assertEqual(plex.commands, [("retarget", ROOM.id)])
        self.assertEqual(
            groups.commands,
            [
                ("leave", ROOM.id),
                ("leave", ROOM.id),
                ("stop", ROOM.id),
                ("leave", SITTING.id),
                ("sleep", SITTING.id),
            ],
        )
        self.assertEqual(spotify.commands, [])
        self.assertEqual(coordinator.history.snapshot()[0]["outcome"], "focused")

    def test_failed_plex_focus_restores_renderer_and_native_group(self):
        coordinator, groups, _spotify = self.coordinator()
        plex = FakePlexFocus(fail=True)
        coordinator.plex = plex

        with self.assertRaisesRegex(FocusError, "attempted Plex restoration"):
            coordinator.focus(ROOM)

        self.assertEqual(
            plex.commands,
            [("retarget", ROOM.id), ("restore", "plex-snapshot")],
        )
        self.assertIn(("join", ROOM.id, SITTING.id), groups.commands)

    def test_plex_focus_from_standalone_room_transfers_remote_playback_here(self):
        coordinator, groups, _spotify = self.coordinator()
        groups.current = [player(ROOM), player(SITTING)]
        plex = FakePlexFocus()
        plex.store.snapshot = lambda: type(
            "Snapshot", (), {"target_device_id": ROOM.id}
        )()
        coordinator.plex = plex

        coordinator.focus(SITTING)

        self.assertEqual(plex.commands, [("retarget", SITTING.id)])
        self.assertEqual(
            groups.commands,
            [
                ("leave", SITTING.id),
                ("leave", SITTING.id),
                ("stop", SITTING.id),
                ("leave", ROOM.id),
                ("sleep", ROOM.id),
            ],
        )
        self.assertNotIn(("sleep", SITTING.id), groups.commands)
        self.assertEqual(coordinator.history.snapshot()[0]["outcome"], "focused")

    def test_plex_focus_from_unrelated_group_transfers_and_sleeps_both_old_sides(self):
        coordinator, groups, _spotify = self.coordinator()
        groups.current = [
            player(ROOM, "follower", DESK.host),
            player(DESK, playback="stop"),
            player(SITTING),
        ]
        plex = FakePlexFocus()
        plex.store.snapshot = lambda: type(
            "Snapshot", (), {"target_device_id": SITTING.id}
        )()
        coordinator.plex = plex

        coordinator.focus(ROOM)

        self.assertEqual(plex.commands, [("retarget", ROOM.id)])
        self.assertEqual(
            groups.commands,
            [
                ("leave", ROOM.id),
                ("leave", ROOM.id),
                ("stop", ROOM.id),
                ("leave", DESK.id),
                ("sleep", DESK.id),
                ("leave", SITTING.id),
                ("sleep", SITTING.id),
            ],
        )
        self.assertNotIn(("sleep", ROOM.id), groups.commands)
        self.assertEqual(coordinator.history.snapshot()[0]["outcome"], "focused")


class GroupClientTests(unittest.TestCase):
    def test_mutations_allow_verified_topology_transition_to_finish(self):
        calls = []

        class RecordingHttp:
            def request(self, url, method, headers, payload, timeout):
                calls.append((url, method, payload, timeout))
                return {"event": {"outcome": "left", "detail": "verified"}}

        GroupClient("http://groups", "token", RecordingHttp()).leave(ROOM.id)

        self.assertEqual(calls[0][1:], ("POST", {"requester": ROOM.id}, 30))

    def test_unified_dashboard_actions_use_group_control_api(self):
        calls = []

        class RecordingHttp:
            def request(
                self, url, method="GET", headers=None, payload=None, timeout=8
            ):
                calls.append((url, method, payload, timeout))
                if url.endswith("/events"):
                    return {"events": [{"outcome": "joined"}]}
                return {"event": {"outcome": "updated", "detail": "ok"}}

        groups = GroupClient("http://groups", "token", RecordingHttp())
        groups.auto_join(ROOM.id)
        groups.leave_and_sleep(ROOM.id)
        groups.set_leadership([ROOM.id, DESK.id])
        self.assertEqual(groups.events(), [{"outcome": "joined"}])

        self.assertEqual(calls[0][0:3], (
            "http://groups/api/v1/groups/auto-join",
            "POST",
            {"requester": ROOM.id},
        ))
        self.assertEqual(calls[1][0:3], (
            "http://groups/api/v1/groups/leave-and-sleep",
            "POST",
            {"requester": ROOM.id},
        ))
        self.assertEqual(calls[2][0:3], (
            "http://groups/api/v1/policy/leadership",
            "POST",
            {"enabled": [ROOM.id, DESK.id]},
        ))


class SpotifyTests(unittest.TestCase):
    def test_pkce_authorization_url_contains_minimal_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            spotify = SpotifyClient(
                "client-id",
                "http://127.0.0.1:8794/oauth/callback",
                Path(directory) / "token.json",
            )
            url = spotify.begin_authorization()
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("user-read-playback-state", url)
        self.assertIn("user-modify-playback-state", url)

    def test_device_inventory_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(json.dumps([{"id": "one", "name": "One", "host": "1.2.3.4"}]))
            devices = load_devices(str(path))
        self.assertEqual(devices[0].id, "one")


class FakePlexServer:
    def __init__(self):
        self.calls = []
        self.timeline_calls = []

    def play_queue(self, server_base, play_queue_id, token, client_identifier):
        self.calls.append((server_base, play_queue_id, token))
        return ET.fromstring("""
            <MediaContainer playQueueID="42" playQueueVersion="7"
              playQueueSelectedItemID="102" playQueueShuffled="1">
              <Track playQueueItemID="101" ratingKey="11"
                key="/library/metadata/11" duration="180000">
                <Media><Part key="/library/parts/11/file.flac" /></Media>
              </Track>
              <Track playQueueItemID="102" ratingKey="12" title="Wanted Song"
                grandparentTitle="Wanted Artist" parentTitle="Wanted Album"
                thumb="/library/metadata/12/thumb/123"
                key="/library/metadata/12" duration="240000">
                <Media><Part key="/library/parts/12/file.flac" /></Media>
              </Track>
              <Track playQueueItemID="103" ratingKey="13"
                key="/library/metadata/13" duration="300000">
                <Media><Part key="/library/parts/13/file.flac" /></Media>
              </Track>
            </MediaContainer>
        """)

    def report_timeline(self, session, token, client_identifier, device_name):
        self.timeline_calls.append((session, token, client_identifier, device_name))


class FakeRenderer:
    def __init__(self):
        self.started = []
        self.applied = []
        self.transport = []
        self.reinforced = []

    def start_session(self, device, session, media_url, artwork_url=""):
        self.started.append((device, session, media_url, artwork_url))

    def snapshot(self, device):
        _, session, url, _ = self.started[-1]
        return RendererSnapshot(session.state, session.current_index,
                                session.offset_ms, url, session.volume)

    def apply_session(self, device, before, after):
        self.applied.append((device, before, after))

    def set_transport_state(self, device, state):
        self.transport.append((device, state))

    def reinforce_initial_offset(self, device, session):
        self.reinforced.append((device, session.offset_ms))

    def verify_session(self, device, session):
        self.verified = (device, session)


class PlexBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.state_path = root / "plex-session.json"
        self.secret_path = root / "plex-token.json"
        self.server = FakePlexServer()
        self.renderer = FakeRenderer()
        self.bridge = PlexBridge(
            PlexSessionStore(self.state_path, self.secret_path),
            self.server,
            "stable-client-id",
            "http://192.0.2.206:32400",
            [ROOM, SITTING, DESK],
            self.renderer,
            public_base="http://192.0.2.206:8794",
            media_secret="test-media-secret",
        )
        self.addCleanup(self.wait_for_track_worker)

    def wait_for_track_worker(self):
        worker = getattr(self.bridge, "last_track_worker", None)
        if worker is not None:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_sleep_stops_plex_before_topology_and_cancels_pending_track(self):
        session = self.play()
        groups = Mock()
        coordinator = Coordinator(
            [ROOM, SITTING, DESK], groups, FakeSpotify(),
            EventHistory(Path(self.temporary.name) / 'sleep-history.json'),
            plex=self.bridge,
        )
        def sleeping(_):
            self.assertEqual(self.bridge.store.snapshot().state, 'stopped')
            self.assertTrue(self.bridge.operation_lock._is_owned())
        groups.leave_and_sleep.side_effect = sleeping
        coordinator.sleep(ROOM)
        started = len(self.renderer.started)
        self.bridge._finish_deferred_track_change(session, session, ROOM, 0)
        self.assertEqual(len(self.renderer.started), started)
        self.assertEqual(self.bridge.store.snapshot().state, 'stopped')
        groups.leave_and_sleep.assert_called_once_with(ROOM.id)

    def test_sleep_other_room_preserves_plex_playback(self):
        self.play()
        groups = Mock()
        coordinator = Coordinator(
            [ROOM, SITTING, DESK], groups, FakeSpotify(),
            EventHistory(Path(self.temporary.name) / 'sleep-history.json'),
            plex=self.bridge,
        )
        coordinator.sleep(SITTING)
        self.assertEqual(self.bridge.store.snapshot().state, 'playing')
        groups.leave_and_sleep.assert_called_once_with(SITTING.id)

    def play(self):
        return self.bridge.play_media({
            "protocol": ["http"],
            "address": ["192.0.2.206"],
            "port": ["32400"],
            "machineIdentifier": ["server-id"],
            "token": ["top-secret-token"],
            "key": ["/library/metadata/12"],
            "containerKey": ["/playQueues/42?own=1"],
            "offset": ["45678"],
            "repeat": ["2"],
        }, "stable-client-id")

    def test_play_media_preserves_exact_session_identity(self):
        session = self.play()

        self.assertEqual([item.rating_key for item in session.items], ["11", "12", "13"])
        self.assertEqual(session.current_index, 1)
        self.assertEqual(session.current.play_queue_item_id, "102")
        self.assertEqual(session.offset_ms, 45678)
        self.assertTrue(session.shuffle)
        self.assertEqual(session.repeat, 2)
        self.assertEqual(session.state, "playing")
        self.assertEqual(session.target_device_id, ROOM.id)
        self.assertEqual(self.renderer.started[0][0], ROOM)
        self.assertEqual(self.renderer.started[0][1].offset_ms, 0)
        media_url = self.renderer.started[0][2]
        self.assertTrue(
            media_url.startswith(
                "http://192.0.2.206:8794/plex/media/"
                + session.renderer_session_id
                + "/102/"
            )
        )
        self.assertFalse(hasattr(self.renderer, "verified"))
        self.assertEqual(self.renderer.reinforced, [(ROOM, 45678)])
        self.assertEqual(
            self.server.timeline_calls,
            [(session, "top-secret-token", "stable-client-id", "Office")],
        )
        self.assertIn("/plex/art/", self.renderer.started[0][3])

    def test_zero_offset_play_waits_for_selection_and_rejects_failed_start(self):
        original = self.play()
        with patch.object(self.renderer, "reinforce_initial_offset",
                          side_effect=FocusError("did not settle")) as verify:
            with self.assertRaisesRegex(FocusError, "did not settle"):
                self.bridge.play_media({
                    "protocol": ["http"], "address": ["192.0.2.206"],
                    "port": ["32400"], "machineIdentifier": ["server-id"],
                    "token": ["test"], "key": ["/library/metadata/13"],
                    "containerKey": ["/playQueues/42"], "offset": ["0"],
                }, "stable-client-id")
        self.assertEqual(verify.call_args[0][1].current_index, 2)
        self.assertEqual(verify.call_args[0][1].offset_ms, 0)
        self.assertEqual(self.bridge.store.snapshot(), original)

    def test_new_album_head_key_uses_server_selected_track(self):
        params = {
            "protocol": ["http"], "address": ["192.0.2.206"], "port": ["32400"],
            "machineIdentifier": ["server-id"], "token": ["test"],
            "key": ["/library/metadata/11"], "containerKey": ["/playQueues/42"],
        }
        session = self.bridge.play_media(params, "stable-client-id")
        self.assertEqual(session.current.play_queue_item_id, "102")
        self.assertEqual(session.renderer_start_index, 1)
        self.assertEqual(self.renderer.started[-1][1].current_index, 1)
        # Once the queue is active, an explicit request for its first song
        # must win over the server's older selected-item marker.
        selected_first = self.bridge.play_media(params, "stable-client-id")
        self.assertEqual(selected_first.current.play_queue_item_id, "101")

    def test_switching_album_head_key_uses_new_queue_selection(self):
        self.play()
        original = self.server.play_queue
        def next_queue(*args):
            root = original(*args)
            root.set("playQueueID", "43")
            root.set("playQueueSelectedItemID", "103")
            return root
        self.server.play_queue = next_queue
        session = self.bridge.play_media({
            "protocol": ["http"], "address": ["192.0.2.206"], "port": ["32400"],
            "machineIdentifier": ["server-id"], "token": ["test"],
            "key": ["/library/metadata/11"], "containerKey": ["/playQueues/43"],
        }, "stable-client-id")
        self.assertEqual(session.current.play_queue_item_id, "103")
        self.assertEqual(self.renderer.started[-1][1].current_index, 2)

    def test_new_queue_invalid_selected_item_is_rejected(self):
        original = self.server.play_queue
        def invalid_selection(*args):
            root = original(*args)
            root.set("playQueueSelectedItemID", "missing")
            return root
        self.server.play_queue = invalid_selection
        with self.assertRaisesRegex(FocusError, "not unique"):
            self.bridge.play_media({
                "protocol": ["http"], "address": ["192.0.2.206"], "port": ["32400"],
                "machineIdentifier": ["server-id"], "token": ["test"],
                "key": ["/library/metadata/11"], "containerKey": ["/playQueues/42"],
            }, "stable-client-id")
        self.assertEqual(self.renderer.started, [])

    def test_queue_ownership_uses_target_room_identifier(self):
        with patch.object(self.server, "play_queue", wraps=self.server.play_queue) as fetch:
            self.play()
        self.assertEqual(fetch.call_args[0][3], "stable-client-id")
        class RecordingHttp:
            def request(self, url, headers):
                self.url, self.headers = url, headers
                return ET.Element("MediaContainer")
        http = RecordingHttp()
        server = PlexServerClient(http)
        server.play_queue("http://nas:32400", "42", "secret", "room-id")
        self.assertIn("own=1", http.url)
        self.assertEqual(http.headers["X-Plex-Client-Identifier"], "room-id")
        self.assertEqual(http.headers["X-Plex-Token"], "secret")
        with self.assertRaisesRegex(FocusError, "requires a room"):
            server.play_queue("http://nas:32400", "42", "secret", "")

    def test_starting_another_room_stops_the_former_renderer(self):
        self.play()

        session = self.bridge.play_media({
            "protocol": ["http"],
            "address": ["192.0.2.206"],
            "port": ["32400"],
            "machineIdentifier": ["server-id"],
            "token": ["top-secret-token"],
            "key": ["/library/metadata/12"],
            "containerKey": ["/playQueues/42?own=1"],
        }, "stable-client-id-study")

        self.assertEqual(session.target_device_id, SITTING.id)
        self.assertEqual(self.renderer.started[-1][0], SITTING)
        self.assertEqual(self.renderer.transport, [(ROOM, "stopped")])

    def test_playlist_play_command_key_overrides_stale_selected_item_marker(self):
        original_queue = self.server.play_queue

        def stale_selection(*args):
            root = original_queue(*args)
            root.set("playQueueSelectedItemID", "101")
            return root

        self.server.play_queue = stale_selection

        session = self.play()

        self.assertEqual(session.current.rating_key, "12")
        self.assertEqual(session.current.play_queue_item_id, "102")

    def test_explicit_queue_item_id_still_rejects_selection_disagreement(self):
        original_queue = self.server.play_queue

        def stale_selection(*args):
            root = original_queue(*args)
            root.set("playQueueSelectedItemID", "101")
            return root

        self.server.play_queue = stale_selection
        params = {
            "protocol": ["http"],
            "address": ["192.0.2.206"],
            "port": ["32400"],
            "machineIdentifier": ["server-id"],
            "token": ["top-secret-token"],
            "key": ["/library/metadata/12"],
            "containerKey": ["/playQueues/42?own=1"],
            "playQueueItemID": ["102"],
        }

        with self.assertRaisesRegex(FocusError, "selection disagrees"):
            self.bridge.play_media(params, "stable-client-id")

    def test_resources_expose_room_endpoints_backed_by_one_bridge(self):
        root = ET.fromstring(self.bridge.resources_xml())
        players = root.findall("./Player")

        self.assertEqual(root.get("size"), "3")
        self.assertEqual(
            {player.get("title") for player in players},
            {"Plex – Office", "Plex – Study", "Plex – Studio"},
        )
        self.assertIn(
            "stable-client-id",
            {player.get("machineIdentifier") for player in players},
        )
        default = next(
            player
            for player in players
            if player.get("machineIdentifier") == "stable-client-id"
        )
        self.assertEqual(default.get("deviceClass"), "speaker")
        self.assertEqual(default.get("state"), "stopped")
        self.assertEqual(default.get("address"), "192.0.2.206")
        self.assertEqual(default.get("port"), "8794")
        self.assertEqual(default.get("provides"), "client,player,pubsub-player")
        self.assertIn("navigation", default.get("protocolCapabilities"))
        self.assertEqual(
            default.find("./Connection").get("uri"),
            "http://192.0.2.206:8794",
        )

    def test_targeted_resources_expose_only_the_requested_room(self):
        endpoint = "stable-client-id-study"

        root = ET.fromstring(self.bridge.resources_xml(endpoint))
        players = root.findall("./Player")

        self.assertEqual(root.get("size"), "1")
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0].get("machineIdentifier"), endpoint)
        self.assertEqual(players[0].get("title"), "Plex – Study")

    def test_targeted_resources_preserve_hidden_follower_state(self):
        endpoint = "stable-client-id-study"
        advertisements = self.bridge.individual_advertisements()
        advertisements[endpoint] = EndpointAdvertisement(
            "Study", visible=False, provides="client"
        )
        self.bridge.set_endpoint_advertisements(advertisements)

        root = ET.fromstring(self.bridge.resources_xml(endpoint))

        self.assertEqual(root.get("size"), "0")
        self.assertEqual(root.findall("./Player"), [])

    def test_targeted_resources_reject_unknown_endpoint(self):
        with self.assertRaisesRegex(FocusError, "unknown room endpoint"):
            self.bridge.resources_xml("unknown-client-id")

    def test_http_accept_queue_covers_multi_room_plexamp_fanout(self):
        self.assertGreaterEqual(Server.request_queue_size, 32)

    def test_root_descriptor_uses_authorized_default_identity(self):
        root = ET.fromstring(self.bridge.root_xml())
        player = root.find("./Player")

        self.assertEqual(root.get("machineIdentifier"), "stable-client-id")
        self.assertEqual(player.get("machineIdentifier"), "stable-client-id")
        self.assertEqual(player.get("title"), "Plex – Office")

    def test_root_descriptor_honors_target_room_identity(self):
        endpoint = "stable-client-id-study"

        root = ET.fromstring(self.bridge.root_xml(endpoint))
        player = root.find("./Player")

        self.assertEqual(root.get("machineIdentifier"), endpoint)
        self.assertEqual(player.get("machineIdentifier"), endpoint)
        self.assertEqual(player.get("title"), "Plex – Study")

    def test_endpoint_group_states_describe_leaders_followers_and_standalone(self):
        states = self.bridge.endpoint_group_states(
            [
                player(ROOM, playback="stop"),
                player(SITTING, "follower", ROOM.host),
                player(DESK, playback="stop"),
            ]
        )

        self.assertEqual(states["stable-client-id"], "Leader with Study")
        self.assertEqual(
            states["stable-client-id-study"],
            "Follower of Office",
        )
        self.assertEqual(states["stable-client-id-desk"], "Standalone")

    def test_endpoint_group_states_report_missing_or_offline_players(self):
        offline_room = player(ROOM)
        offline_room["online"] = False

        states = self.bridge.endpoint_group_states([offline_room, player(SITTING)])

        self.assertEqual(states["stable-client-id"], "Unavailable")
        self.assertEqual(states["stable-client-id-study"], "Standalone")
        self.assertEqual(states["stable-client-id-desk"], "Unavailable")

    def test_group_advertisements_combine_leader_and_hide_follower(self):
        advertisements = self.bridge.build_group_advertisements(
            [
                player(ROOM),
                player(SITTING, "follower", ROOM.host),
                player(DESK, playback="stop"),
            ]
        )

        self.assertEqual(
            advertisements["stable-client-id"],
            EndpointAdvertisement("Office + Study"),
        )
        self.assertEqual(
            advertisements["stable-client-id-study"],
            EndpointAdvertisement("Study", False, "client"),
        )
        self.bridge.set_endpoint_advertisements(advertisements)
        root = ET.fromstring(self.bridge.resources_xml())
        self.assertEqual(root.get("size"), "2")
        self.assertEqual(
            {item.get("title") for item in root.findall("./Player")},
            {"Office + Study", "Studio"},
        )

    def test_large_group_advertisement_uses_follower_count(self):
        advertisements = self.bridge.build_group_advertisements(
            [
                player(ROOM),
                player(SITTING, "follower", ROOM.host),
                player(DESK, "follower", ROOM.host),
            ]
        )

        self.assertEqual(
            advertisements["stable-client-id"].title,
            "Office + 2 rooms",
        )
        self.assertFalse(advertisements["stable-client-id-study"].visible)
        self.assertFalse(advertisements["stable-client-id-desk"].visible)

    def test_active_renderer_is_not_hidden_after_external_leader_reversal(self):
        self.play()

        with self.assertRaisesRegex(FocusError, "no longer the group leader"):
            self.bridge.build_group_advertisements(
                [
                    player(ROOM, "follower", SITTING.host),
                    player(SITTING),
                    player(DESK, playback="stop"),
                ]
            )

    def test_endpoint_publisher_requires_two_matching_topology_snapshots(self):
        groups = FakeGroups(
            [
                player(ROOM),
                player(SITTING, "follower", ROOM.host),
                player(DESK, playback="stop"),
            ]
        )
        calls = []

        class Cloud:
            def linked(self, _identifier):
                return True

            def publish_endpoint(self, identifier, device_name="", provides=""):
                calls.append((identifier, device_name, provides))

        publisher = PlexEndpointPublisher(
            self.bridge, groups, Cloud(), mode="cloud"
        )

        self.assertFalse(publisher.poll_once())
        self.assertEqual(calls, [])
        self.assertTrue(publisher.poll_once())
        self.assertEqual(len(calls), 3)
        follower = next(
            call for call in calls if call[0] == "stable-client-id-study"
        )
        self.assertEqual(follower[1:], ("Study", "client"))
        self.assertEqual(
            self.bridge.advertisement_snapshot()["stable-client-id"].title,
            "Office + Study",
        )

    def test_endpoint_publisher_fails_open_when_topology_is_incomplete(self):
        groups = FakeGroups([player(ROOM), player(SITTING)])
        publisher = PlexEndpointPublisher(self.bridge, groups, mode="local")

        publisher.poll_once()
        publisher.poll_once()

        advertisements = self.bridge.advertisement_snapshot()
        self.assertTrue(all(item.visible for item in advertisements.values()))
        self.assertEqual(advertisements["stable-client-id"].title, "Office")

    def test_idle_timeline_exposes_stopped_media_types(self):
        root = ET.fromstring(self.bridge.timeline_xml("3"))
        timelines = {item.get("type"): item for item in root.findall("./Timeline")}

        self.assertEqual(root.get("commandID"), "3")
        self.assertEqual(set(timelines), {"music", "video", "photo"})
        self.assertEqual(timelines["music"].get("state"), "stopped")
        self.assertEqual(timelines["music"].get("time"), "0")
        self.assertIn("playPause", timelines["music"].get("controllable"))
        self.assertEqual(timelines["video"].get("state"), "stopped")
        self.assertEqual(timelines["photo"].get("state"), "stopped")

    def test_token_is_separate_from_public_session_state(self):
        self.play()

        self.assertNotIn("top-secret-token", self.state_path.read_text())
        self.assertIn("top-secret-token", self.secret_path.read_text())
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.secret_path.stat().st_mode & 0o777, 0o600)

    def test_playlist_preserves_order_without_exposing_plex_token(self):
        session = self.play()

        playlist = self.bridge.playlist_m3u(self.bridge.playlist_path(session)).decode()

        renderer_items = session.items[session.renderer_start_index :]
        positions = [playlist.index(f"/{item.play_queue_item_id}/") for item in renderer_items]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(playlist.count("http://192.0.2.206:8794/plex/media/"), 2)
        self.assertNotIn("/101/", playlist)
        self.assertLess(playlist.index("/102/"), playlist.index("/103/"))
        self.assertNotIn("top-secret-token", playlist)

    def test_tampered_or_expired_playlist_path_is_rejected(self):
        session = self.play()
        path = self.bridge.playlist_path(session)
        replacement = "0" if path[-5] != "0" else "1"

        with self.assertRaisesRegex(FocusError, "invalid Plex playlist signature"):
            self.bridge.resolve_playlist(path[:-5] + replacement + ".m3u")
        with self.assertRaisesRegex(FocusError, "expired Plex renderer session"):
            self.bridge.resolve_playlist(path.replace(session.renderer_session_id, "old"))

    def test_signed_media_path_proxies_token_and_range_as_headers(self):
        session = self.play()
        path = self.bridge.media_path(session, session.current)

        request = self.bridge.media_request(path, "bytes=4096-")

        self.assertEqual(
            request.full_url,
            "http://192.0.2.206:32400/library/parts/12/file.flac",
        )
        self.assertEqual(request.get_header("X-plex-token"), "top-secret-token")
        self.assertEqual(request.get_header("Range"), "bytes=4096-")
        self.assertNotIn("top-secret-token", request.full_url)

    def test_signed_artwork_path_proxies_token_as_header(self):
        session = self.play()
        path = self.bridge.art_path(session, session.current)

        request = self.bridge.art_request(path)

        self.assertEqual(
            request.full_url,
            "http://192.0.2.206:32400/library/metadata/12/thumb/123",
        )
        self.assertEqual(request.get_header("X-plex-token"), "top-secret-token")
        self.assertNotIn("top-secret-token", request.full_url)

    def test_tampered_media_path_is_rejected(self):
        session = self.play()
        path = self.bridge.media_path(session, session.current)

        with self.assertRaisesRegex(FocusError, "invalid Plex media signature"):
            self.bridge.resolve_media(path[:-1] + ("0" if path[-1] != "0" else "1"))

    def test_pre_upgrade_session_keeps_existing_signed_urls(self):
        session = self.play()
        path = self.bridge.media_path(session, session.current)
        old = json.loads(self.state_path.read_text())
        del old["renderer_queue_version"]
        self.state_path.write_text(json.dumps(old))
        self.bridge.store = PlexSessionStore(self.state_path, self.secret_path)
        self.bridge.resolve_media(path)

    def test_stable_signatures_still_reject_removed_items_and_old_sessions(self):
        session = self.play()
        path = self.bridge.media_path(session, session.items[0])
        self.bridge.store.save(replace(session, items=session.items[1:],
                                      current_index=0, play_queue_version="8"), "test")
        with self.assertRaisesRegex(FocusError, "absent from the active queue"):
            self.bridge.resolve_media(path)
        self.bridge.store.save(replace(session, renderer_session_id="new"), "test")
        with self.assertRaisesRegex(FocusError, "expired Plex renderer session"):
            self.bridge.resolve_media(path)

    def test_persisted_session_round_trips_without_losing_queue_state(self):
        expected = self.play()

        restored = PlexSessionStore(self.state_path, self.secret_path)

        self.assertEqual(restored.snapshot(), expected)
        self.assertEqual(restored.token, "top-secret-token")

    def test_timeline_reports_queue_item_position_and_modes(self):
        self.play()

        root = ET.fromstring(self.bridge.timeline_xml("19"))
        timeline = root.find("./Timeline")

        self.assertEqual(root.get("commandID"), "19")
        self.assertEqual(timeline.get("playQueueID"), "42")
        self.assertEqual(timeline.get("playQueueItemID"), "102")
        self.assertEqual(timeline.get("playQueueVersion"), "7")
        self.assertEqual(timeline.get("time"), "45678")
        self.assertEqual(timeline.get("shuffle"), "1")
        self.assertEqual(timeline.get("repeat"), "2")
        self.assertEqual(timeline.get("token"), "top-secret-token")
        self.assertIn("skipNext", timeline.get("controllable"))

    def test_timeline_is_active_only_for_the_session_room_endpoint(self):
        self.play()

        active = ET.fromstring(
            self.bridge.timeline_xml("20", "stable-client-id")
        ).find("./Timeline")
        idle = ET.fromstring(
            self.bridge.timeline_xml(
                "21", "stable-client-id-study"
            )
        ).find("./Timeline")

        self.assertEqual(active.get("state"), "playing")
        self.assertEqual(active.get("playQueueID"), "42")
        self.assertEqual(idle.get("state"), "stopped")
        self.assertIsNone(idle.get("playQueueID"))
        self.assertIsNone(idle.get("ratingKey"))

    def test_timeline_subscription_tracks_command_and_expires_on_unsubscribe(self):
        endpoint = "stable-client-id"
        with patch.object(self.bridge, "_post_timeline") as post:
            self.bridge.subscribe_timeline(
                endpoint,
                "phone-controller",
                "192.0.2.44",
                "http",
                "32500",
                "7",
            )
            for thread in list(threading.enumerate()):
                if thread is not threading.current_thread() and thread.name != "MainThread":
                    thread.join(timeout=0.1)

        key = ("phone-controller", endpoint)
        self.assertEqual(
            self.bridge.subscribers[key]["url"],
            "http://192.0.2.44:32500/:/timeline",
        )
        self.bridge.note_command(endpoint, "phone-controller", "8")
        self.assertEqual(self.bridge.subscribers[key]["command_id"], "8")
        self.bridge.unsubscribe_timeline(endpoint, "phone-controller")
        self.assertNotIn(key, self.bridge.subscribers)
        post.assert_called_once()

    def test_waiting_timeline_poll_wakes_on_player_state_change(self):
        self.play()
        result = {}

        def poll():
            result["xml"] = self.bridge.timeline_poll("21", wait=True, timeout=1)

        thread = threading.Thread(target=poll)
        thread.start()
        time.sleep(0.02)
        self.bridge.playback_command("pause", {})
        thread.join(timeout=0.5)

        self.assertFalse(thread.is_alive())
        root = ET.fromstring(result["xml"])
        self.assertEqual(root.get("commandID"), "21")
        self.assertEqual(root.find("./Timeline").get("state"), "paused")

    def test_inactive_endpoint_poll_ignores_active_room_position_change(self):
        self.play()
        endpoint = "stable-client-id-study"
        result = {}

        def poll():
            result["xml"] = self.bridge.timeline_poll(
                "22", wait=True, timeout=0.15, endpoint_identifier=endpoint
            )

        thread = threading.Thread(target=poll)
        thread.start()
        time.sleep(0.02)
        active = self.bridge.store.snapshot()
        self.bridge.store.update_runtime(replace(active, offset_ms=12000))
        self.bridge.notify_subscribers()
        time.sleep(0.03)

        self.assertTrue(thread.is_alive())
        thread.join(timeout=0.5)
        self.assertFalse(thread.is_alive())
        root = ET.fromstring(result["xml"])
        self.assertEqual(root.get("commandID"), "22")
        self.assertEqual(root.find("./Timeline").get("state"), "stopped")

    def test_companion_controls_preserve_queue_while_changing_state(self):
        original = self.play()

        paused = self.bridge.playback_command("pause", {})
        sought = self.bridge.playback_command("seekTo", {"offset": ["120000"]})
        skipped = self.bridge.playback_command(
            "skipTo", {"key": ["/library/metadata/13"]}
        )
        changed = self.bridge.playback_command(
            "setParameters", {"shuffle": ["0"], "repeat": ["1"], "volume": ["73"]}
        )

        self.assertEqual(paused.state, "paused")
        self.assertEqual(sought.offset_ms, 120000)
        self.assertEqual(skipped.current.rating_key, "13")
        self.assertEqual(skipped.offset_ms, 0)
        self.assertFalse(changed.shuffle)
        self.assertEqual(changed.repeat, 1)
        self.assertEqual(changed.volume, 73)
        self.assertEqual(changed.items, original.items)
        self.assertEqual(changed.play_queue_id, original.play_queue_id)

    def test_companion_skip_and_step_commands_keep_queue_identity(self):
        original = self.play()

        self.bridge.playback_command("seekTo", {"offset": ["0"]})
        previous = self.bridge.playback_command("skipPrevious", {})
        stepped = self.bridge.playback_command("stepForward", {})
        following = self.bridge.playback_command("skipNext", {})
        self.wait_for_track_worker()

        self.assertEqual(previous.current.rating_key, "11")
        self.assertEqual(previous.offset_ms, 0)
        self.assertEqual(stepped.offset_ms, 30000)
        self.assertEqual(following.current.rating_key, "12")
        self.assertEqual(following.items, original.items)
        self.assertEqual(following.play_queue_id, original.play_queue_id)
        self.assertEqual(following.playback_session_id, original.playback_session_id)
        self.assertNotEqual(following.renderer_session_id, original.renderer_session_id)

    def test_track_change_publishes_new_item_buffering_at_zero_before_renderer(self):
        original = self.play()
        observed = {}
        bridge = self.bridge

        class InspectTransition(FakeRenderer):
            def start_session(self, device, session, media_url, artwork_url=""):
                if session.current.rating_key == "13":
                    timeline = ET.fromstring(bridge.timeline_xml()).find("./Timeline")
                    observed.update(timeline.attrib)
                super().start_session(device, session, media_url, artwork_url)

        self.bridge.renderer = InspectTransition()

        following = self.bridge.playback_command("skipNext", {})
        self.wait_for_track_worker()

        self.assertEqual(original.current.rating_key, "12")
        self.assertEqual(observed["ratingKey"], "13")
        self.assertEqual(observed["playQueueItemID"], "103")
        self.assertEqual(observed["state"], "buffering")
        self.assertEqual(observed["time"], "0")
        self.assertEqual(following.state, "playing")
        self.assertEqual(following.offset_ms, 0)

    def test_track_skip_acknowledges_while_slow_renderer_is_still_loading(self):
        self.play()
        renderer_started = threading.Event()
        release_renderer = threading.Event()

        class SlowRenderer(FakeRenderer):
            def start_session(self, device, session, media_url, artwork_url=""):
                renderer_started.set()
                release_renderer.wait(timeout=1)
                super().start_session(device, session, media_url, artwork_url)

        self.bridge.renderer = SlowRenderer()

        returned = self.bridge.playback_command("skipNext", {})
        self.assertTrue(renderer_started.wait(timeout=0.2))
        timeline = ET.fromstring(self.bridge.timeline_xml()).find("./Timeline")

        self.assertEqual(returned.current.rating_key, "13")
        self.assertEqual(timeline.get("ratingKey"), "13")
        self.assertEqual(timeline.get("state"), "buffering")
        self.assertEqual(timeline.get("time"), "0")

        release_renderer.set()
        self.wait_for_track_worker()
        final = ET.fromstring(self.bridge.timeline_xml()).find("./Timeline")
        self.assertEqual(final.get("state"), "playing")
        self.assertEqual(final.get("ratingKey"), "13")

    def test_track_skip_delivers_zero_position_before_command_returns(self):
        self.play()
        delivered = []
        self.bridge.subscribers[("phone", "stable-client-id")] = {
            "endpoint": "stable-client-id",
            "controller": "phone",
            "url": "http://192.0.2.44:32500/:/timeline",
            "command_id": "27",
            "expires": time.monotonic() + 90,
        }

        def capture(_subscriber, data=None):
            timeline = ET.fromstring(data).find("./Timeline")
            delivered.append(dict(timeline.attrib))

        with patch.object(self.bridge, "_post_timeline", side_effect=capture):
            returned = self.bridge.playback_command("skipNext", {})

        self.assertEqual(returned.current.rating_key, "13")
        self.assertTrue(delivered)
        self.assertEqual(delivered[0]["ratingKey"], "13")
        self.assertEqual(delivered[0]["state"], "buffering")
        self.assertEqual(delivered[0]["time"], "0")
        self.wait_for_track_worker()

    def test_failed_track_change_restores_previous_timeline(self):
        original = self.play()

        class RejectTrack(FakeRenderer):
            def start_session(self, *_args, **_kwargs):
                raise FocusError("renderer rejected track")

        self.bridge.renderer = RejectTrack()

        self.bridge.playback_command("skipNext", {})
        self.wait_for_track_worker()

        restored = self.bridge.store.snapshot()
        self.assertEqual(restored.current.rating_key, original.current.rating_key)
        self.assertEqual(restored.offset_ms, original.offset_ms)
        self.assertEqual(restored.state, original.state)

    def test_refresh_play_queue_reloads_order_and_preserves_current_item(self):
        original = self.play()
        loaded_paths = [self.bridge.media_path(original, item) for item in original.items]
        playlist_path = self.bridge.playlist_path(original)
        self.server.play_queue = lambda *_: ET.fromstring("""
            <MediaContainer playQueueID="42" playQueueVersion="8"
              playQueueSelectedItemID="102" playQueueShuffled="0">
              <Track playQueueItemID="103" ratingKey="13"
                key="/library/metadata/13" duration="300000">
                <Media><Part key="/library/parts/13/file.flac" /></Media>
              </Track>
              <Track playQueueItemID="102" ratingKey="12"
                key="/library/metadata/12" duration="240000">
                <Media><Part key="/library/parts/12/file.flac" /></Media>
              </Track>
              <Track playQueueItemID="101" ratingKey="11"
                key="/library/metadata/11" duration="180000">
                <Media><Part key="/library/parts/11/file.flac" /></Media>
              </Track>
            </MediaContainer>
        """)

        refreshed = self.bridge.playback_command(
            "refreshPlayQueue", {"playQueueID": ["42"]}
        )

        self.assertEqual([item.rating_key for item in refreshed.items], ["13", "12", "11"])
        self.assertEqual(refreshed.current.rating_key, "12")
        self.assertEqual(refreshed.current_index, 1)
        self.assertEqual(refreshed.offset_ms, original.offset_ms)
        self.assertEqual(refreshed.play_queue_version, "8")
        self.assertEqual(refreshed.renderer_session_id, original.renderer_session_id)
        self.assertEqual(len(self.renderer.started), 1)
        for path in loaded_paths:
            self.bridge.media_request(path, "bytes=4096-")
        self.bridge.resolve_playlist(playlist_path)
        self.bridge.store = PlexSessionStore(self.state_path, self.secret_path)
        for path in loaded_paths:
            self.bridge.resolve_media(path)
        self.bridge.resolve_playlist(playlist_path)


    def test_queue_item_identity_disambiguates_repeated_radio_track(self):
        root = ET.fromstring("""
            <MediaContainer playQueueID="42" playQueueVersion="9"
              playQueueSelectedItemID="202">
              <Track playQueueItemID="201" ratingKey="12"
                key="/library/metadata/12" duration="240000">
                <Media><Part key="/library/parts/12/file.flac" /></Media>
              </Track>
              <Track playQueueItemID="202" ratingKey="12"
                key="/library/metadata/12" duration="240000">
                <Media><Part key="/library/parts/12/file.flac" /></Media>
              </Track>
            </MediaContainer>
        """)

        items, current_index, version, _shuffled = self.bridge._parse_queue(
            root, "/library/metadata/12", "202"
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(current_index, 1)
        self.assertEqual(version, "9")

    def test_stop_resets_position_but_does_not_destroy_queue(self):
        original = self.play()

        stopped = self.bridge.playback_command("stop", {})

        self.assertEqual(stopped.state, "stopped")
        self.assertEqual(stopped.offset_ms, 0)
        self.assertEqual(stopped.items, original.items)

    def test_stop_from_later_track_never_restarts_renderer(self):
        session = self.play()
        later = replace(session, current_index=1, offset_ms=20000)
        self.bridge.store.save(later, self.bridge.store.token)
        started = len(self.renderer.started)
        stopped = self.bridge.playback_command("stop", {})
        self.assertEqual(len(self.renderer.started), started)
        self.assertEqual(stopped.current_index, 0)
        self.assertEqual(stopped.state, "stopped")
        self.assertEqual(self.renderer.applied[-1][2].state, "stopped")

    def test_renderer_stop_does_not_seek_before_stopping(self):
        session = self.play()
        before = replace(session, current_index=1, offset_ms=20000)
        after = replace(before, state="stopped", current_index=0, offset_ms=0)
        renderer = WiiMRendererClient()
        renderer.command = Mock()
        renderer.apply_session(ROOM, before, after)
        renderer.command.assert_called_once_with(ROOM, "setPlayerCmd:stop")
        renderer.command.reset_mock()
        renderer.apply_session(ROOM, replace(before, state="stopped"), after)
        renderer.command.assert_called_once_with(ROOM, "setPlayerCmd:stop")

    def test_inactive_room_endpoint_cannot_control_active_session(self):
        original = self.play()
        inactive_endpoint = self.bridge.endpoint_identifier(SITTING.id)

        with self.assertRaisesRegex(FocusError, "inactive room endpoint"):
            self.bridge.playback_command(
                "stop", {}, endpoint_identifier=inactive_endpoint
            )

        self.assertEqual(self.bridge.store.snapshot(), original)

    def test_retarget_grace_suppresses_delayed_controller_stop(self):
        original = self.play()
        self.bridge.retarget_stop_protect_until = time.monotonic() + 5

        with self.assertRaisesRegex(FocusError, "stale Plex stop"):
            self.bridge.playback_command(
                "stop",
                {},
                endpoint_identifier=self.bridge.endpoint_identifier(ROOM.id),
            )

        self.assertEqual(self.bridge.store.snapshot(), original)

    def test_physical_remote_state_reconciles_into_same_plex_session(self):
        original = self.play()
        next_source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(original, original.items[2])
        )

        class RemoteNextAndPause:
            def snapshot(self, device):
                self.device = device
                return RendererSnapshot(
                    state="paused",
                    playlist_index=1,
                    position_ms=32100,
                    source_url=next_source,
                    volume=62,
                )

        renderer = RemoteNextAndPause()
        self.bridge.renderer = renderer

        reconciled = self.bridge.reconcile_renderer()

        self.assertEqual(renderer.device, ROOM)
        self.assertEqual(reconciled.state, "paused")
        self.assertEqual(reconciled.current.rating_key, "13")
        self.assertEqual(reconciled.offset_ms, 32100)
        self.assertEqual(reconciled.volume, 62)
        self.assertEqual(reconciled.items, original.items)
        self.assertEqual(reconciled.play_queue_id, original.play_queue_id)

    def test_native_queue_index_overrides_stale_current_uri_after_remote_press(self):
        original = self.play()
        stale_source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(original, original.current)
        )

        class NativeRemoteNextAndPause:
            def snapshot(self, _device):
                return RendererSnapshot(
                    state="paused",
                    playlist_index=2,
                    position_ms=7000,
                    source_url=stale_source,
                    volume=48,
                    playlist_count=3,
                )

        self.bridge.renderer = NativeRemoteNextAndPause()
        reconciled = self.bridge.reconcile_renderer()

        self.assertEqual(reconciled.current.rating_key, "13")
        self.assertEqual(reconciled.state, "paused")
        self.assertEqual(reconciled.offset_ms, 7000)

    def test_native_remote_back_restarts_current_track_after_five_seconds(self):
        original = self.play()
        stale_source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(original, original.current)
        )

        class NativeRemoteBack:
            selected = []

            def snapshot(self, _device):
                return RendererSnapshot(
                    "playing", 0, 1000, stale_source, 48, playlist_count=3
                )

            def select_queue_index(self, device, index, state="playing"):
                self.selected.append((device, index, state))

        renderer = NativeRemoteBack()
        self.bridge.renderer = renderer
        reconciled = self.bridge.reconcile_renderer()

        self.assertEqual(renderer.selected, [(ROOM, 1, "playing")])
        self.assertEqual(reconciled.current.rating_key, "12")
        self.assertEqual(reconciled.offset_ms, 0)

    def test_native_remote_back_selects_previous_track_within_five_seconds(self):
        original = self.play()
        near_start = replace(original, offset_ms=3000)
        self.bridge.store.save(near_start, self.bridge.store.token)
        self.bridge.last_position_at = 0.0
        stale_source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(near_start, near_start.current)
        )

        class NativeRemoteBack:
            def snapshot(self, _device):
                return RendererSnapshot(
                    "playing", 0, 500, stale_source, 48, playlist_count=3
                )

        self.bridge.renderer = NativeRemoteBack()
        reconciled = self.bridge.reconcile_renderer()

        self.assertEqual(reconciled.current.rating_key, "11")
        self.assertEqual(reconciled.offset_ms, 500)

    def test_native_remote_track_change_preserves_paused_session(self):
        original = self.play()
        paused = replace(original, state="paused", offset_ms=12000)
        self.bridge.store.save(paused, self.bridge.store.token)
        self.bridge.last_position_at = 0.0
        next_source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(paused, paused.items[2])
        )

        class NativeRemoteForward:
            forced_states = []

            def snapshot(self, _device):
                return RendererSnapshot(
                    "playing", 2, 300, next_source, 48, playlist_count=0
                )

            def set_transport_state(self, device, state):
                self.forced_states.append((device, state))

        renderer = NativeRemoteForward()
        self.bridge.renderer = renderer
        reconciled = self.bridge.reconcile_renderer()

        self.assertEqual(renderer.forced_states, [(ROOM, "paused")])
        self.assertEqual(reconciled.current.rating_key, "13")
        self.assertEqual(reconciled.state, "paused")
        self.assertEqual(reconciled.offset_ms, 300)

    @patch("plex_bridge.playback.time.monotonic", return_value=101.0)
    def test_renderer_sampling_noise_does_not_jitter_playing_timeline(self, _clock):
        original = self.play()
        self.bridge.last_position_at = 100.0
        source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(original, original.current)
        )

        class CoarsePosition:
            def snapshot(self, _device):
                return RendererSnapshot(
                    state="playing",
                    playlist_index=0,
                    position_ms=45000,
                    source_url=source,
                    volume=50,
                )

        self.bridge.renderer = CoarsePosition()

        reconciled = self.bridge.reconcile_renderer()

        self.assertEqual(reconciled.offset_ms, 46678)

    @patch("plex_bridge.playback.time.monotonic", return_value=101.0)
    def test_renderer_large_position_change_is_treated_as_physical_seek(self, _clock):
        original = self.play()
        self.bridge.last_position_at = 100.0
        source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(original, original.current)
        )

        class PhysicalSeek:
            def snapshot(self, _device):
                return RendererSnapshot(
                    state="playing",
                    playlist_index=0,
                    position_ms=60000,
                    source_url=source,
                    volume=50,
                )

        self.bridge.renderer = PhysicalSeek()

        reconciled = self.bridge.reconcile_renderer()

        self.assertEqual(reconciled.offset_ms, 60000)

    @patch("plex_bridge.playback.time.monotonic", return_value=101.0)
    def test_natural_track_completion_advances_direct_renderer(self, _clock):
        original = self.play()
        near_end = replace(original, offset_ms=238000)
        self.bridge.store.update_runtime(near_end)
        self.bridge.last_position_at = 100.0
        source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(near_end, near_end.current)
        )

        class CompletedTrack(FakeRenderer):
            def snapshot(self, _device):
                return RendererSnapshot(
                    state="stopped",
                    playlist_index=0,
                    position_ms=0,
                    source_url=source,
                    volume=50,
                )

        renderer = CompletedTrack()
        self.bridge.renderer = renderer

        advanced = self.bridge.reconcile_renderer()

        self.assertEqual(advanced.current.rating_key, "13")
        self.assertEqual(advanced.offset_ms, 0)
        self.assertEqual(advanced.state, "playing")
        self.assertEqual(renderer.started[-1][1].current.rating_key, "13")

    @patch("plex_bridge.playback.time.monotonic", return_value=101.0)
    def test_mid_track_renderer_stop_does_not_auto_advance(self, _clock):
        original = self.play()
        self.bridge.last_position_at = 100.0
        source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(original, original.current)
        )

        class StoppedMidTrack(FakeRenderer):
            def snapshot(self, _device):
                return RendererSnapshot(
                    state="stopped",
                    playlist_index=0,
                    position_ms=60000,
                    source_url=source,
                    volume=50,
                )

        renderer = StoppedMidTrack()
        self.bridge.renderer = renderer

        reconciled = self.bridge.reconcile_renderer()

        self.assertEqual(reconciled.current.rating_key, original.current.rating_key)
        self.assertEqual(reconciled.state, "stopped")
        self.assertEqual(renderer.started, [])

    @patch("plex_bridge.playback.time.monotonic", return_value=101.0)
    def test_completion_refreshes_dynamic_plex_radio_queue_before_advancing(
        self, _clock
    ):
        original = self.play()
        last = replace(
            original,
            current_index=2,
            offset_ms=298000,
            repeat=0,
        )
        self.bridge.store.update_runtime(last)
        self.bridge.last_position_at = 100.0
        source = (
            "http://192.0.2.206:8794"
            + self.bridge.media_path(last, last.current)
        )
        self.server.play_queue = lambda *_: ET.fromstring("""
            <MediaContainer playQueueID="42" playQueueVersion="8"
              playQueueSelectedItemID="103" playQueueShuffled="0">
              <Track playQueueItemID="101" ratingKey="11"
                key="/library/metadata/11" duration="180000">
                <Media><Part key="/library/parts/11/file.flac" /></Media>
              </Track>
              <Track playQueueItemID="102" ratingKey="12"
                key="/library/metadata/12" duration="240000">
                <Media><Part key="/library/parts/12/file.flac" /></Media>
              </Track>
              <Track playQueueItemID="103" ratingKey="13"
                key="/library/metadata/13" duration="300000">
                <Media><Part key="/library/parts/13/file.flac" /></Media>
              </Track>
              <Track playQueueItemID="104" ratingKey="14"
                key="/library/metadata/14" duration="210000">
                <Media><Part key="/library/parts/14/file.flac" /></Media>
              </Track>
            </MediaContainer>
        """)

        class CompletedRadioTrack(FakeRenderer):
            def snapshot(self, _device):
                return RendererSnapshot(
                    state="stopped",
                    playlist_index=0,
                    position_ms=0,
                    source_url=source,
                    volume=50,
                )

        renderer = CompletedRadioTrack()
        self.bridge.renderer = renderer

        advanced = self.bridge.reconcile_renderer()

        self.assertEqual(advanced.current.rating_key, "14")
        self.assertEqual(advanced.play_queue_version, "8")
        self.assertEqual(len(advanced.items), 4)
        self.assertEqual(renderer.started[-1][1].current.rating_key, "14")

    def test_lost_renderer_stops_timeline_after_grace_without_device_commands(self):
        original = self.play()
        with patch.object(self.renderer, "snapshot", side_effect=FocusError("lost")):
            with patch("plex_bridge.playback.time.monotonic", return_value=100):
                self.bridge.monitor_once()
            self.assertEqual(self.bridge.store.snapshot().state, "playing")
            with patch("plex_bridge.playback.time.monotonic", return_value=104):
                self.bridge.monitor_once()
        stopped = self.bridge.store.snapshot()
        self.assertEqual(stopped.state, "stopped")
        self.assertEqual(stopped.offset_ms, original.offset_ms)
        self.assertEqual(stopped.current, original.current)
        self.assertEqual(self.server.timeline_calls[-1][0].state, "stopped")
        self.assertEqual(self.renderer.transport, [])
        self.assertEqual(len(self.renderer.started), 1)

    def test_renderer_recovery_resets_unavailable_grace(self):
        self.play()
        with patch.object(self.renderer, "snapshot", side_effect=FocusError("lost")):
            with patch("plex_bridge.playback.time.monotonic", return_value=100):
                self.bridge.monitor_once()
        self.bridge.monitor_once()
        self.assertIsNone(self.bridge.renderer_unavailable_since)
        with patch.object(self.renderer, "snapshot", side_effect=FocusError("lost")):
            with patch("plex_bridge.playback.time.monotonic", return_value=200):
                self.bridge.monitor_once()
        self.assertEqual(self.bridge.store.snapshot().state, "playing")

    def test_play_rebuilds_lost_stream_at_saved_position(self):
        original = self.play()
        with patch.object(self.renderer, "snapshot", return_value=
                          RendererSnapshot("stopped", 0, 0, "")):
            resumed = self.bridge.playback_command("play", {})
        self.assertEqual(resumed.offset_ms, original.offset_ms)
        self.assertEqual(resumed.current, original.current)
        self.assertNotEqual(resumed.renderer_session_id, original.renderer_session_id)
        self.assertEqual(len(self.renderer.started), 2)
        self.assertEqual(resumed.state, "playing")

    def test_unrelated_wiim_source_cannot_overwrite_plex_session(self):
        original = self.play()

        class UnrelatedSource:
            def snapshot(self, _device):
                return RendererSnapshot(
                    state="stopped",
                    playlist_index=0,
                    position_ms=0,
                    source_url="http://example.net/radio.mp3",
                )

        self.bridge.renderer = UnrelatedSource()

        with self.assertRaisesRegex(FocusError, "no longer rendering"):
            self.bridge.reconcile_renderer()

        self.assertEqual(self.bridge.store.snapshot(), original)

    def test_incomplete_queue_is_rejected_without_replacing_state(self):
        expected = self.play()
        self.server.play_queue = lambda *_: ET.fromstring(
            '<MediaContainer><Track playQueueItemID="1" key="/library/metadata/1" /></MediaContainer>'
        )

        with self.assertRaises(FocusError):
            self.play()

        self.assertEqual(self.bridge.store.snapshot(), expected)

    def test_truncated_queue_is_rejected_instead_of_claiming_exact_recovery(self):
        self.server.play_queue = lambda *_: ET.fromstring("""
            <MediaContainer totalSize="2" playQueueSelectedItemID="1">
              <Track playQueueItemID="1" ratingKey="12"
                key="/library/metadata/12" duration="240000">
                <Media><Part key="/library/parts/12/file.flac" /></Media>
              </Track>
            </MediaContainer>
        """)

        with self.assertRaisesRegex(FocusError, "only 1 of 2"):
            self.play()

    def test_unapproved_plex_server_is_rejected_before_network_access(self):
        params = {
            "protocol": ["http"],
            "address": ["192.0.2.99"],
            "port": ["32400"],
            "machineIdentifier": ["server-id"],
            "token": ["token"],
            "key": ["/library/metadata/12"],
            "containerKey": ["/playQueues/42"],
        }

        with self.assertRaisesRegex(FocusError, "unapproved"):
            self.bridge.play_media(params, "stable-client-id")

        self.assertEqual(self.server.calls, [])

    def test_secure_plex_direct_lan_alias_is_pinned_to_configured_server(self):
        params = {
            "protocol": ["https"],
            "address": ["192-0-2-206.example-certificate.plex.direct"],
            "port": ["32400"],
            "machineIdentifier": ["server-id"],
            "token": ["token"],
            "key": ["/library/metadata/12"],
            "containerKey": ["/playQueues/42"],
        }

        session = self.bridge.play_media(params, "stable-client-id")

        self.assertEqual(session.server_base, "http://192.0.2.206:32400")
        self.assertEqual(
            self.server.calls,
            [("http://192.0.2.206:32400", "42", "token")],
        )

    def test_renderer_start_failure_removes_unusable_new_session(self):
        def fail(*_args):
            raise FocusError("renderer unavailable")

        self.renderer.start_session = fail

        with self.assertRaisesRegex(FocusError, "renderer unavailable"):
            self.play()

        self.assertIsNone(self.bridge.store.snapshot())
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.secret_path.exists())


class WiiMRendererTests(unittest.TestCase):
    def test_snapshot_accepts_both_linkplay_loading_status_variants(self):
        class LoadingWiiM(WiiMRendererClient):
            status = "load"

            def current_track(self, _device):
                return "", -1

            def current_uri(self, _device):
                return ""

            def command(self, _device, command):
                if command == "getPlayerStatus":
                    return {
                        "status": self.status,
                        "plicurr": "1",
                        "curpos": "0",
                        "vol": "30",
                    }
                return {}

        renderer = LoadingWiiM()
        self.assertEqual(renderer.snapshot(ROOM).state, "buffering")
        renderer.status = "loading"
        self.assertEqual(renderer.snapshot(ROOM).state, "buffering")

    def test_snapshot_reads_remote_button_state_and_decodes_source_url(self):
        class FakeWiiM(WiiMRendererClient):
            def current_track(self, _device):
                return "", -1

            def current_uri(self, _device):
                return ""

            def command(self, _device, command):
                if command == "getPlayerStatus":
                    return {
                        "status": "pause",
                        "plicurr": "3",
                        "curpos": "9876",
                        "vol": "64",
                    }
                return {
                    "metaData": {
                        "url": base64.b64encode(b"http://192.0.2.206:8794/plex/media/session/103/audio").decode()
                    }
                }

        snapshot = FakeWiiM(playlist_index_base=1).snapshot(ROOM)

        self.assertEqual(snapshot.state, "paused")
        self.assertEqual(snapshot.playlist_index, 2)
        self.assertEqual(snapshot.position_ms, 9876)
        self.assertEqual(snapshot.volume, 64)
        self.assertEqual(
            snapshot.source_url,
            "http://192.0.2.206:8794/plex/media/session/103/audio",
        )

    def test_start_session_uses_native_playqueue_metadata_index_and_modes(self):
        commands = []
        av_calls = []
        queue_calls = []
        selected_url = [""]

        class RecordingWiiM(WiiMRendererClient):
            def command(self, _device, command):
                commands.append(command)
                return "OK"

            def av_transport(self, _device, action, arguments):
                av_calls.append((action, dict(arguments)))
                return ET.Element("response")

            def play_queue(self, _device, action, arguments):
                queue_calls.append((action, dict(arguments)))
                if action == "CreateQueue":
                    root = ET.fromstring(arguments[0][1])
                    selected_url[0] = root.findtext("./Tracks/Track2/URL") or ""
                return ET.Element("response")

            def snapshot(self, _device):
                return RendererSnapshot(
                    state="playing",
                    playlist_index=1,
                    position_ms=0,
                    source_url=selected_url[0],
                    volume=50,
                    playlist_count=3,
                )

            def verify_session(self, _device, _session):
                return None

        with tempfile.TemporaryDirectory() as directory:
            store = PlexSessionStore(
                Path(directory) / "session.json", Path(directory) / "token.json"
            )
            server = FakePlexServer()
            bridge = PlexBridge(
                store,
                server,
                "id",
                "http://192.0.2.206:32400",
                [ROOM],
                RecordingWiiM(),
                "http://192.0.2.206:8794",
                "secret",
            )
            bridge.play_media(
                {
                    "protocol": ["http"],
                    "address": ["192.0.2.206"],
                    "port": ["32400"],
                    "machineIdentifier": ["server"],
                    "token": ["token"],
                    "key": ["/library/metadata/12"],
                    "containerKey": ["/playQueues/42"],
                    "offset": ["45678"],
                    "repeat": ["2"],
                },
                "id",
            )

        self.assertEqual(av_calls, [])
        self.assertEqual(
            [call[0] for call in queue_calls],
            ["CreateQueue", "PlayQueueWithIndex", "SetQueueLoopMode"],
        )
        context = queue_calls[0][1]["QueueContext"]
        self.assertIn("<TotalNumber>3</TotalNumber>", context)
        self.assertIn("/101/", context)
        self.assertIn("/102/", context)
        self.assertIn("/103/", context)
        self.assertIn("Wanted Song", html.unescape(context))
        self.assertIn("Wanted Artist", html.unescape(context))
        self.assertEqual(queue_calls[1][1], {"QueueName": "CurrentQueue", "Index": 2})
        self.assertEqual(queue_calls[2][1], {"LoopMode": 2})
        self.assertEqual(
            commands,
            ["setPlayerCmd:seek:45"],
        )

    @patch("plex_bridge.playback.time.sleep")
    def test_initial_offset_waits_for_exact_queue_item_before_seek(self, _sleep):
        session = PlexSession(
            server_identifier="server",
            server_base="http://192.0.2.206:32400",
            play_queue_id="42",
            play_queue_version="1",
            items=(
                PlexQueueItem("101", "11", "/library/metadata/11", "/part/11", 180000),
                PlexQueueItem("102", "12", "/library/metadata/12", "/part/12", 240000),
            ),
            current_index=1,
            offset_ms=45000,
            state="playing",
            shuffle=False,
            repeat=0,
            target_device_id=ROOM.id,
            renderer_session_id="session",
            volume=50,
            renderer_start_index=1,
            playback_session_id="playback",
        )
        commands = []
        observations = iter(
            [
                RendererSnapshot("playing", 0, 1000, "/plex/media/session/101/a", 50, 2),
                RendererSnapshot("playing", 1, 0, "/plex/media/session/102/b", 50, 2),
                RendererSnapshot("playing", 1, 250, "/plex/media/session/102/b", 50, 2),
            ]
        )

        class SettlingWiiM(WiiMRendererClient):
            def snapshot(self, _device):
                return next(observations)

            def command(self, _device, command):
                commands.append(command)
                return "OK"

        SettlingWiiM().reinforce_initial_offset(ROOM, session)

        self.assertEqual(commands, ["setPlayerCmd:seek:45"])

    @patch("plex_bridge.playback.time.sleep")
    def test_zero_offset_start_retries_wrong_track_and_verifies_selection(self, _sleep):
        items = (PlexQueueItem("101", "11", "/metadata/11", "/part/11", 180000),
                 PlexQueueItem("102", "12", "/metadata/12", "/part/12", 240000))
        session = PlexSession("server", "http://nas:32400", "42", "1", items,
                              1, 0, "playing", False, 0, ROOM.id, "session", 50,
                              1, "playback")
        for count in (0, 2):
            with self.subTest(playlist_count=count):
                selected = [0]
                commands = []
                retries = []
                samples = []

                class SlowSelection(WiiMRendererClient):
                    def snapshot(self, _device):
                        index = selected[0]
                        samples.append(index)
                        return RendererSnapshot("playing", index, 200,
                            f"/plex/media/session/{items[index].play_queue_item_id}/signed",
                            50, count)

                    def play_queue(self, _device, action, arguments):
                        retries.append((action, dict(arguments)))
                        selected[0] = dict(arguments)["Index"] - 1

                    def command(self, _device, command):
                        commands.append(command)

                SlowSelection().reinforce_initial_offset(ROOM, session)
                self.assertEqual(retries, [("PlayQueueWithIndex",
                    {"QueueName": "CurrentQueue", "Index": 2})])
                self.assertEqual(samples[-2:], [1, 1])
                self.assertEqual(commands, [])  # A zero-offset start needs no seek.

    @patch("plex_bridge.playback.time.sleep")
    def test_zero_offset_start_rejects_persistent_wrong_track(self, _sleep):
        items = (PlexQueueItem("101", "11", "/metadata/11", "/part/11", 180000),
                 PlexQueueItem("102", "12", "/metadata/12", "/part/12", 240000))
        session = PlexSession("server", "http://nas:32400", "42", "1", items,
                              1, 0, "playing", False, 0, ROOM.id, "session", 50,
                              1, "playback")
        retries = []

        class IgnoredSelection(WiiMRendererClient):
            def snapshot(self, _device):
                return RendererSnapshot("playing", 0, 200,
                                        "/plex/media/session/101/signed", 50, 2)

            def play_queue(self, _device, action, arguments):
                retries.append(dict(arguments)["Index"])

        with self.assertRaisesRegex(FocusError, "did not settle"):
            IgnoredSelection().reinforce_initial_offset(ROOM, session)
        self.assertEqual(retries, [2, 2])

    def test_native_playqueue_failure_falls_back_to_direct_track(self):
        commands = []
        av_calls = []

        class RejectingQueueWiiM(WiiMRendererClient):
            def play_queue(self, _device, _action, _arguments):
                raise FocusError("not supported")

            def command(self, _device, command):
                commands.append(command)
                return "OK"

            def av_transport(self, _device, action, arguments):
                av_calls.append((action, dict(arguments)))
                return ET.Element("response")

        items = (
            PlexQueueItem("101", "11", "/library/metadata/11", "/part/11", 180000),
            PlexQueueItem("102", "12", "/library/metadata/12", "/part/12", 240000),
            PlexQueueItem("103", "13", "/library/metadata/13", "/part/13", 300000),
        )
        session = PlexSession(
            "server", "http://nas:32400", "42", "7", items, 1, 0,
            "playing", False, 2, ROOM.id, "renderer-session", 50, 1,
            "playback-session",
        )
        entries = [
            (
                item,
                f"http://nas/plex/media/session/{item.play_queue_item_id}/signed",
                "",
            )
            for item in session.items
        ]

        RejectingQueueWiiM().start_queue_session(ROOM, session, entries)

        self.assertEqual([call[0] for call in av_calls], ["SetAVTransportURI", "Play"])
        self.assertIn("/102/", av_calls[0][1]["CurrentURI"])
        self.assertEqual(commands, ["setPlayerCmd:loopmode:-1"])

    def test_loop_commands_use_documented_write_values(self):
        self.assertEqual(WiiMRendererClient.loop_mode(False, 0), 0)
        self.assertEqual(WiiMRendererClient.loop_mode(False, 1), 1)
        self.assertEqual(WiiMRendererClient.loop_mode(False, 2), -1)
        self.assertEqual(WiiMRendererClient.loop_mode(True, 0), 3)
        self.assertEqual(WiiMRendererClient.loop_mode(True, 2), 2)
        self.assertEqual(WiiMRendererClient.queue_loop_mode(False, 0), 4)
        self.assertEqual(WiiMRendererClient.queue_loop_mode(False, 2), 0)

    @patch("plex_bridge.playback.time.sleep")
    def test_verification_requires_expected_identity_item_and_progress(self, _sleep):
        observations = iter(
            [
                RendererSnapshot("playing", 0, 45678, "http://other/source"),
                RendererSnapshot(
                    "playing", 0, 46000, "http://nas/plex/media/session-marker/102/audio"
                ),
                RendererSnapshot(
                    "playing", 0, 47000, "http://nas/plex/media/session-marker/102/audio"
                ),
            ]
        )

        class ObservedWiiM(WiiMRendererClient):
            def snapshot(self, _device):
                return next(observations)

        with tempfile.TemporaryDirectory() as directory:
            store = PlexSessionStore(
                Path(directory) / "session.json", Path(directory) / "token.json"
            )
            bridge = PlexBridge(
                store,
                FakePlexServer(),
                "id",
                "http://192.0.2.206:32400",
                [ROOM],
                FakeRenderer(),
                "http://nas",
                "secret",
            )
            session = bridge.play_media(
                {
                    "protocol": ["http"],
                    "address": ["192.0.2.206"],
                    "port": ["32400"],
                    "machineIdentifier": ["server"],
                    "token": ["token"],
                    "key": ["/library/metadata/12"],
                    "containerKey": ["/playQueues/42"],
                },
                "id",
            )
            session = session.__class__(
                **{
                    **session.__dict__,
                    "renderer_session_id": "session-marker",
                }
            )

        verified = ObservedWiiM().verify_session(ROOM, session, attempts=3)

        self.assertEqual(verified.position_ms, 47000)

    @patch("plex_bridge.playback.time.sleep")
    def test_verification_retries_transient_status_timeout(self, _sleep):
        observations = iter(
            [
                FocusError("read operation timed out"),
                RendererSnapshot(
                    "playing", 0, 46000,
                    "http://nas/plex/media/session-marker/102/audio",
                ),
                RendererSnapshot(
                    "playing", 0, 47000,
                    "http://nas/plex/media/session-marker/102/audio",
                ),
            ]
        )

        class ObservedWiiM(WiiMRendererClient):
            def snapshot(self, _device):
                value = next(observations)
                if isinstance(value, Exception):
                    raise value
                return value

        session = PlexSession(
            "server", "http://nas:32400", "42", "7",
            (PlexQueueItem("102", "12", "/library/metadata/12", "/part/12", 240000),),
            0, 0, "playing", False, 0, ROOM.id, "session-marker", 50, 0,
            "playback-session",
        )

        verified = ObservedWiiM().verify_session(ROOM, session, attempts=3)

        self.assertEqual(verified.position_ms, 47000)


class FakePlexCloudHttp:
    def __init__(self):
        self.calls = []

    def request(self, url, method="GET", headers=None):
        self.calls.append((url, method, dict(headers or {})))
        if url.endswith("pins?strong=true"):
            return {"id": 77, "code": "ABCD1234"}
        if "/api/v2/pins/77" in url:
            return {"authToken": "cloud-device-token"}
        return {}


class PlexCloudTests(unittest.TestCase):
    def test_successful_non_json_publication_response_is_accepted(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"OK"

        with patch("plex_bridge.playback.urllib.request.urlopen", return_value=Response()):
            payload = PlexCloudHttp().request(
                "https://plex.tv/devices/id", "PUT", {}
            )

        self.assertEqual(payload, {})

    def test_room_pin_link_publishes_only_its_scoped_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            http = FakePlexCloudHttp()
            token_path = Path(directory) / "plex-cloud-token.json"
            cloud = PlexCloudClient(
                "stable-bridge-id",
                "http://192.0.2.206:8794",
                token_path,
                http,
                endpoint_names={
                    "stable-bridge-id": "Office",
                    "stable-bridge-id-study": "Study",
                },
            )

            code, auth_url = cloud.begin_link("stable-bridge-id-study")
            cloud.finish_link("stable-bridge-id-study")

            self.assertEqual(code, "ABCD1234")
            self.assertIn("clientID=stable-bridge-id-study", auth_url)
            self.assertTrue(cloud.authorized)
            self.assertFalse(cloud.linked("stable-bridge-id"))
            self.assertTrue(cloud.linked("stable-bridge-id-study"))
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            published = [call for call in http.calls if call[1] == "PUT"]
            self.assertEqual(len(published), 1)
            publish_url, method, headers = published[0]
            self.assertEqual(method, "PUT")
            self.assertIn("/devices/stable-bridge-id-study?", publish_url)
            self.assertIn("Connection%5B%5D%5Buri%5D=http", publish_url)
            self.assertNotIn("cloud-device-token", publish_url)
            self.assertEqual(
                headers["X-Plex-Client-Identifier"],
                "stable-bridge-id-study",
            )
            self.assertEqual(headers["X-Plex-Device-Name"], "Study")
            self.assertEqual(headers["X-Plex-Product"], "Plex Bridge")
            self.assertEqual(headers["X-Plex-Token"], "cloud-device-token")
            self.assertEqual(
                headers["X-Plex-Provides"], "client,player,pubsub-player"
            )
            with open(token_path, encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(
                stored,
                {
                    "tokens": {
                        "stable-bridge-id-study": "cloud-device-token"
                    }
                },
            )

    def test_legacy_bridge_token_migrates_without_relinking_default_room(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "plex-cloud-token.json"
            token_path.write_text(
                json.dumps({"token": "legacy-device-token"}), encoding="utf-8"
            )
            http = FakePlexCloudHttp()
            cloud = PlexCloudClient(
                "stable-bridge-id",
                "http://192.0.2.206:8794",
                token_path,
                http,
                endpoint_names={
                    "stable-bridge-id": "Office",
                    "stable-bridge-id-desk": "Studio",
                },
            )

            self.assertTrue(cloud.linked("stable-bridge-id"))
            self.assertFalse(cloud.linked("stable-bridge-id-desk"))
            cloud.publish()

            published = [call for call in http.calls if call[1] == "PUT"]
            self.assertEqual(len(published), 1)
            self.assertIn("/devices/stable-bridge-id?", published[0][0])
            self.assertEqual(
                published[0][2]["X-Plex-Token"], "legacy-device-token"
            )


if __name__ == "__main__":
    unittest.main()
