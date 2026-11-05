import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.client import HTTPConnection
from unittest.mock import patch

from plex_bridge.topology import (
    App,
    Device,
    DeviceState,
    EventHistory,
    GroupManager,
    LeadershipPolicy,
    Server,
    TriggerStandbyMonitor,
    pbkdf2_password_hash,
    silent_wav,
    verify_password,
)


ROOM = Device("office", "Office", "192.0.2.33")
SITTING = Device("study", "Study", "192.0.2.143")
DESK = Device("desk", "Studio", "192.0.2.13")
TV = Device("tv-room", "TV Room", "192.0.2.78", False)


def state(
    device,
    playback="stop",
    role="standalone/leader",
    group_name=None,
    master_host="",
):
    return DeviceState(
        device=device,
        online=True,
        playback=playback,
        role=role,
        group_name=group_name or device.name,
        master_host=master_host,
    )


class FakeClient:
    def __init__(self, states):
        self.states = {item.device.id: item for item in states}
        self.commands = []
        self.trigger_states = {item.device.id: 1 for item in states}
        self.slave_lists = {item.device.id: set() for item in states}
        for item in states:
            if item.role == "follower" and item.master_host:
                leader = next(
                    (
                        candidate.device
                        for candidate in states
                        if candidate.device.host == item.master_host
                    ),
                    None,
                )
                if leader:
                    self.slave_lists[leader.id].add(item.device.host)

    def state(self, device):
        return self.states[device.id]

    def join(self, requester, leader):
        self.commands.append(("join", requester.id, leader.id))
        self.states[requester.id] = state(
            requester, "play", "follower", requester.name, leader.host
        )
        return "OK"

    def leave_follower(self, requester):
        self.commands.append(("leave", requester.id))
        self.states[requester.id] = state(requester)
        return "OK"

    def kick_follower(self, leader, follower):
        self.commands.append(("kick", leader.id, follower.id))
        self.slave_lists[leader.id].discard(follower.host)
        self.states[follower.id] = state(follower)
        return "OK"

    def follower_hosts(self, leader):
        return set(self.slave_lists.get(leader.id, set()))

    def dissolve(self, leader):
        self.commands.append(("dissolve", leader.id))
        self.states[leader.id] = state(leader)
        for device_id, item in list(self.states.items()):
            if item.role == "follower" and item.master_host == leader.host:
                self.states[device_id] = state(item.device)
        self.slave_lists[leader.id].clear()
        return "OK"

    def shutdown(self, device):
        self.commands.append(("shutdown", device.id))
        self.trigger_states[device.id] = 0
        return "OK"

    def trigger_off(self, device):
        self.commands.append(("trigger_off", device.id))
        self.trigger_states[device.id] = 0
        return {"status": "OK"}

    def trigger_status(self, device):
        return self.trigger_states.get(device.id)

    def resume(self, device):
        self.commands.append(("resume", device.id))
        self.states[device.id].playback = "play"
        return "OK"

    def stop(self, device):
        self.commands.append(("stop", device.id))
        self.states[device.id].playback = "stop"
        return "OK"


class GroupManagerTests(unittest.TestCase):
    def manager(self, states, history_path=None):
        client = FakeClient(states)
        history = EventHistory(history_path)
        return GroupManager([ROOM, SITTING, DESK], client, history), client

    @patch("plex_bridge.topology.time.sleep")
    def test_joins_only_active_session(self, _sleep):
        manager, client = self.manager(
            [state(ROOM), state(SITTING, "play"), state(DESK)]
        )

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "joined")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(client.commands, [("join", ROOM.id, SITTING.id)])

    def test_no_active_player_is_safe_noop(self):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "no_active")
        self.assertEqual(client.commands, [])

    def test_multiple_independent_sessions_are_ambiguous(self):
        manager, client = self.manager(
            [state(ROOM), state(SITTING, "play"), state(DESK, "play")]
        )

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "ambiguous")
        self.assertEqual(client.commands, [])

    @patch("plex_bridge.topology.time.sleep")
    def test_tv_zone_is_not_an_automatic_leader(self, _sleep):
        states = [state(ROOM), state(SITTING), state(DESK, "play"), state(TV, "play")]
        client = FakeClient(states)
        manager = GroupManager(
            [ROOM, SITTING, DESK, TV], client, EventHistory(None)
        )

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "joined")
        self.assertEqual(event["leader"], DESK.name)
        self.assertEqual(client.commands, [("join", ROOM.id, DESK.id)])

    def test_tv_zone_alone_is_treated_as_no_active_session(self):
        states = [state(ROOM), state(SITTING), state(DESK), state(TV, "play")]
        client = FakeClient(states)
        manager = GroupManager(
            [ROOM, SITTING, DESK, TV], client, EventHistory(None)
        )

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "no_active")
        self.assertEqual(client.commands, [])

    @patch("plex_bridge.topology.time.sleep")
    def test_tv_zone_can_be_enabled_dynamically(self, _sleep):
        states = [state(ROOM), state(SITTING), state(DESK), state(TV, "play")]
        client = FakeClient(states)
        manager = GroupManager(
            [ROOM, SITTING, DESK, TV], client, EventHistory(None)
        )
        manager.set_smart_leadership(TV.id, True)

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "joined")
        self.assertEqual(event["leader"], TV.name)

    @patch("plex_bridge.topology.time.sleep")
    def test_multiple_active_members_of_one_group_resolve_to_leader(self, _sleep):
        manager, client = self.manager(
            [
                state(ROOM),
                state(SITTING, "play", group_name="Desk Group"),
                state(DESK, "play", "follower", "Desk", SITTING.host),
            ]
        )

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "joined")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(client.commands, [("join", ROOM.id, SITTING.id)])

    def test_existing_different_group_requires_leave_first(self):
        manager, client = self.manager(
            [
                state(
                    ROOM,
                    role="follower",
                    group_name="Old Group",
                    master_host=DESK.host,
                ),
                state(SITTING, "play"),
                state(DESK),
            ]
        )

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "conflict")
        self.assertEqual(client.commands, [])

    def test_auto_join_detects_existing_membership(self):
        manager, client = self.manager(
            [
                state(ROOM, "play", "follower", "Office", SITTING.host),
                state(SITTING, "play", group_name="Sitting Group"),
                state(DESK),
            ]
        )

        event = manager.join(ROOM.host)

        self.assertEqual(event["outcome"], "already_grouped")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(client.commands, [])

    @patch("plex_bridge.topology.time.sleep")
    def test_explicit_join_does_not_require_playback(self, _sleep):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])

        event = manager.join_device_to(ROOM.id, SITTING.id)

        self.assertEqual(event["outcome"], "joined")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(client.commands, [("join", ROOM.id, SITTING.id)])

    def test_explicit_join_resolves_a_follower_to_its_leader(self):
        manager, client = self.manager(
            [
                state(ROOM),
                state(SITTING, "play", group_name="Desk Group"),
                state(DESK, "play", "follower", "Desk", SITTING.host),
            ]
        )

        with patch("plex_bridge.topology.time.sleep"):
            event = manager.join_device_to(ROOM.id, DESK.id)

        self.assertEqual(event["outcome"], "joined")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(client.commands, [("join", ROOM.id, SITTING.id)])

    @patch("plex_bridge.topology.time.sleep")
    def test_follower_can_leave(self, _sleep):
        manager, client = self.manager(
            [
                state(ROOM, "play", "follower", "Office", SITTING.host),
                state(SITTING, "play", group_name="Sitting Group"),
                state(DESK),
            ]
        )

        event = manager.leave(ROOM.host)

        self.assertEqual(event["outcome"], "left")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(client.commands, [("kick", SITTING.id, ROOM.id)])

    @patch("plex_bridge.topology.time.sleep")
    def test_preset_leave_detaches_follower_then_sleeps(self, _sleep):
        manager, client = self.manager(
            [
                state(ROOM, "play", "follower", "Office", SITTING.host),
                state(SITTING, "play"),
                state(DESK),
            ]
        )

        event = manager.leave_and_sleep(ROOM.host)

        self.assertEqual(event["outcome"], "slept")
        self.assertEqual(
            client.commands,
            [("kick", SITTING.id, ROOM.id), ("stop", ROOM.id), ("trigger_off", ROOM.id)],
        )

    def test_preset_leave_sleeps_an_already_standalone_player(self):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])

        event = manager.leave_and_sleep(ROOM.host)

        self.assertEqual(event["outcome"], "slept")
        self.assertEqual(client.commands, [("stop", ROOM.id), ("trigger_off", ROOM.id)])

    @patch("plex_bridge.topology.time.sleep")
    def test_leader_leave_dissolves_group(self, _sleep):
        manager, client = self.manager(
            [
                state(ROOM, "play", group_name="Room Group"),
                state(SITTING, "play", "follower", "Study", ROOM.host),
                state(DESK),
            ]
        )

        event = manager.leave(ROOM.host)

        self.assertEqual(event["outcome"], "left")
        self.assertEqual(client.commands, [("dissolve", ROOM.id)])

    @patch("plex_bridge.topology.time.sleep")
    def test_disband_from_follower_dissolves_whole_group(self, _sleep):
        manager, client = self.manager(
            [
                state(ROOM, "play", "follower", "Office", SITTING.host),
                state(SITTING, "play", group_name="Sitting Group"),
                state(DESK),
            ]
        )

        event = manager.disband_device(ROOM.id)

        self.assertEqual(event["outcome"], "disbanded")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(client.commands, [("dissolve", SITTING.id)])
        self.assertNotEqual(client.states[ROOM.id].role, "follower")

    def test_sleep_refuses_group_member(self):
        manager, client = self.manager(
            [
                state(ROOM, "play", "follower", "Office", SITTING.host),
                state(SITTING, "play"),
                state(DESK),
            ]
        )

        event = manager.sleep_device(ROOM.id)

        self.assertEqual(event["outcome"], "conflict")
        self.assertEqual(client.commands, [])

    def test_sleep_standalone_player(self):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])

        event = manager.sleep_device(ROOM.id)

        self.assertEqual(event["outcome"], "slept")
        self.assertIn("trigger verified off", event["detail"])
        self.assertEqual(client.commands, [("stop", ROOM.id), ("trigger_off", ROOM.id)])

    @patch("plex_bridge.topology.time.sleep")
    def test_sleep_rejects_trigger_rebound(self, _sleep):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])
        from unittest.mock import Mock
        client.trigger_status = Mock(side_effect=[1, 0, 0, 1])
        event = manager.sleep_device(ROOM.id)
        self.assertEqual(event["outcome"], "failed")
        self.assertIn("turned back on", event["detail"])

    @patch("plex_bridge.topology.time.sleep")
    def test_sleep_fails_if_trigger_command_is_acknowledged_but_ignored(self, _sleep):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])
        client.trigger_off = lambda device: {"status": "OK"}
        event = manager.sleep_device(ROOM.id)
        self.assertEqual(event["outcome"], "failed")
        self.assertIn("trigger remained active", event["detail"])

    @patch("plex_bridge.topology.time.sleep")
    def test_sleep_does_not_cut_trigger_if_stop_cannot_be_verified(self, _sleep):
        manager, client = self.manager([state(ROOM, "play"), state(SITTING), state(DESK)])
        client.stop = lambda device: "OK"
        event = manager.sleep_device(ROOM.id)
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(client.trigger_states[ROOM.id], 1)
        self.assertEqual(client.commands, [])

    def test_sleep_without_trigger_requests_shutdown(self):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])
        client.trigger_states[ROOM.id] = None
        event = manager.sleep_device(ROOM.id)
        self.assertEqual(event["outcome"], "slept")
        self.assertEqual(client.commands, [("stop", ROOM.id), ("shutdown", ROOM.id)])

    def test_sleep_already_off_does_not_toggle_trigger(self):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])
        client.trigger_states[ROOM.id] = 0
        event = manager.sleep_device(ROOM.id)
        self.assertEqual(event["outcome"], "slept")
        self.assertEqual(client.commands, [("stop", ROOM.id)])

    def test_stop_session_resolves_follower_to_leader(self):
        manager, client = self.manager(
            [
                state(ROOM, "play", "follower", "Office", SITTING.host),
                state(SITTING, "play"),
                state(DESK),
            ]
        )

        event = manager.stop_session_device(ROOM.id)

        self.assertEqual(event["outcome"], "stopped")
        self.assertEqual(client.commands, [("stop", SITTING.id)])

    def test_unknown_source_is_rejected(self):
        manager, client = self.manager([state(ROOM), state(SITTING), state(DESK)])
        event = manager.join("192.0.2.250")
        self.assertEqual(event["outcome"], "rejected")
        self.assertEqual(client.commands, [])

    @patch("plex_bridge.topology.time.sleep")
    def test_trigger_standby_kicks_follower_and_resumes_leader(self, _sleep):
        manager, client = self.manager(
            [
                state(ROOM, "stop", "follower", "Office", SITTING.host),
                state(SITTING, "pause"),
                state(DESK),
            ]
        )
        client.trigger_states[ROOM.id] = 0

        event = manager.handle_trigger_standby(ROOM.id)

        self.assertEqual(event["outcome"], "restored")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(
            client.commands,
            [("kick", SITTING.id, ROOM.id), ("resume", SITTING.id)],
        )
        self.assertEqual(client.states[SITTING.id].playback, "play")

    def test_trigger_standby_ignores_group_wide_stop(self):
        manager, client = self.manager(
            [
                state(ROOM, "stop", "follower", "Office", SITTING.host),
                state(SITTING, "pause"),
                state(DESK),
            ]
        )
        client.trigger_states[ROOM.id] = 0
        client.trigger_states[SITTING.id] = 0

        event = manager.handle_trigger_standby(ROOM.id)

        self.assertEqual(event["outcome"], "ignored")
        self.assertEqual(client.commands, [])

    @patch("plex_bridge.topology.time.sleep")
    def test_trigger_standby_uses_leader_slave_list_during_transient(self, _sleep):
        manager, client = self.manager(
            [
                state(ROOM),
                state(SITTING, "pause"),
                state(DESK),
            ]
        )
        client.trigger_states[ROOM.id] = 0
        client.slave_lists[SITTING.id].add(ROOM.host)

        event = manager.handle_trigger_standby(ROOM.id)

        self.assertEqual(event["outcome"], "restored")
        self.assertEqual(event["leader"], SITTING.name)
        self.assertEqual(
            client.commands,
            [("kick", SITTING.id, ROOM.id), ("resume", SITTING.id)],
        )


class TriggerStandbyMonitorTests(unittest.TestCase):
    def test_monitor_acts_only_on_falling_edge(self):
        client = FakeClient([state(ROOM), state(SITTING), state(DESK)])
        manager = GroupManager([ROOM, SITTING, DESK], client, EventHistory(None))
        monitor = TriggerStandbyMonitor(manager)
        calls = []
        manager.handle_trigger_standby = calls.append

        monitor.poll_once()
        monitor.poll_once()
        client.trigger_states[ROOM.id] = 0
        monitor.poll_once()
        monitor.poll_once()

        self.assertEqual(calls, [ROOM.id])


class PersistenceTests(unittest.TestCase):
    def test_history_survives_reload_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.json")
            history = EventHistory(path, limit=2)
            history.record("join", "one", "joined", "first")
            history.record("leave", "one", "left", "second")
            history.record("join", "two", "no_active", "third")
            loaded = EventHistory(path, limit=2).snapshot()
        self.assertEqual([event["detail"] for event in loaded], ["third", "second"])

    def test_password_hash_and_silent_audio(self):
        encoded = pbkdf2_password_hash("password", b"0123456789abcdef")
        self.assertTrue(verify_password("password", encoded))
        self.assertFalse(verify_password("wrong", encoded))
        self.assertTrue(silent_wav().startswith(b"RIFF"))

    def test_leadership_policy_persists_and_preserves_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "leadership.json")
            policy = LeadershipPolicy([ROOM, TV], path)
            self.assertTrue(policy.allowed(ROOM.id))
            self.assertFalse(policy.allowed(TV.id))
            policy.set_all({TV.id})
            loaded = LeadershipPolicy([ROOM, TV], path)
        self.assertFalse(loaded.allowed(ROOM.id))
        self.assertTrue(loaded.allowed(TV.id))


class ApiTests(unittest.TestCase):
    def setUp(self):
        client = FakeClient([state(ROOM), state(SITTING), state(DESK)])
        manager = GroupManager([ROOM, SITTING, DESK], client, EventHistory(None))
        app = App(
            manager,
            "operator",
            pbkdf2_password_hash("password", b"0123456789abcdef"),
            "session-secret",
            "api-token",
        )
        self.server = Server(("127.0.0.1", 0), app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def test_api_requires_bearer_and_returns_versioned_players(self):
        status, _ = self.request("GET", "/api/v1/players")
        self.assertEqual(status, 401)

        status, body = self.request(
            "GET", "/api/v1/players", headers={"Authorization": "Bearer api-token"}
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["api_version"], 1)
        self.assertEqual(len(payload["players"]), 3)

    def test_explicit_join_api_requires_leader(self):
        headers = {
            "Authorization": "Bearer api-token",
            "Content-Type": "application/json",
        }
        status, _ = self.request(
            "POST",
            "/api/v1/groups/join",
            body=json.dumps({"requester": ROOM.id}),
            headers=headers,
        )
        self.assertEqual(status, 400)

    def test_leadership_api_requires_authentication_and_validates_player_ids(self):
        headers = {
            "Authorization": "Bearer api-token",
            "Content-Type": "application/json",
        }
        status, _ = self.request(
            "POST",
            "/api/v1/policy/leadership",
            body=json.dumps({"enabled": [ROOM.id]}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)

        status, body = self.request(
            "POST",
            "/api/v1/policy/leadership",
            body=json.dumps({"enabled": ["missing"]}),
            headers=headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["players"], ["missing"])

        status, _ = self.request(
            "POST",
            "/api/v1/policy/leadership",
            body=json.dumps({"enabled": [SITTING.id]}),
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertFalse(self.server.app.manager.leadership.allowed(ROOM.id))
        self.assertTrue(self.server.app.manager.leadership.allowed(SITTING.id))

    def test_leave_and_sleep_api_uses_the_atomic_manager_action(self):
        headers = {
            "Authorization": "Bearer api-token",
            "Content-Type": "application/json",
        }
        with patch.object(
            self.server.app.manager,
            "leave_and_sleep_device",
            return_value={
                "outcome": "sleeping",
                "detail": "verified standalone and sleeping",
            },
        ) as action:
            status, _ = self.request(
                "POST",
                "/api/v1/groups/leave-and-sleep",
                body=json.dumps({"requester": ROOM.id}),
                headers=headers,
            )
        self.assertEqual(status, 200)
        action.assert_called_once_with(ROOM.id)

    def test_unsafe_leave_preset_is_disabled(self):
        status, _ = self.request("GET", "/preset/leave")
        self.assertEqual(status, 410)
        self.assertEqual(self.server.app.manager.client.commands, [])

    def test_dashboard_leave_is_authenticated_and_preserves_leader(self):
        client = self.server.app.manager.client
        client.states[ROOM.id] = state(
            ROOM, "stop", "follower", "Office", SITTING.host
        )
        client.states[SITTING.id] = state(SITTING, "play")
        body = urllib.parse.urlencode({"requester": ROOM.id})

        status, _ = self.request(
            "POST",
            "/dashboard/leave",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(client.commands, [])

        token = self.server.app.session_token()
        with patch("plex_bridge.topology.time.sleep"):
            status, _ = self.request(
                "POST",
                "/dashboard/leave",
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": f"wiim_session={token}",
                },
            )
        self.assertEqual(status, 303)
        self.assertEqual(client.commands, [("kick", SITTING.id, ROOM.id)])
        self.assertEqual(client.states[SITTING.id].playback, "play")


if __name__ == "__main__":
    unittest.main()
