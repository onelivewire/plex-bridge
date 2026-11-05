import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plex_bridge import playback, topology
from plex_bridge.config import load_or_create_identity


class StartupTests(unittest.TestCase):
    def test_playback_consumes_config_and_persistent_endpoint_ids(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            raw = json.loads((Path(__file__).parents[1] / "examples/config.json").read_text())
            path.write_text(json.dumps(raw))
            env = {"CONFIG_PATH": str(path), "STATE_PATH": root,
                   "GROUP_API_BASE": "http://127.0.0.1:8793", "GROUP_API_TOKEN": "test",
                   "PLEX_MEDIA_SECRET": "test", "WEB_USERNAME": "operator",
                   "WEB_PASSWORD_HASH": "test", "WEB_SESSION_SECRET": "test"}
            with patch.dict(os.environ, env, clear=True), patch.object(playback, "Server") as server, \
                    patch.object(playback.threading, "Thread"):
                playback.main()
            app = server.call_args.args[1]
            identity = load_or_create_identity(Path(root) / "identity.json")
            self.assertEqual(app.plex.public_base, raw["advertised_origin"])
            self.assertEqual(app.plex.allowed_server_base, raw["plex_server_origin"])
            self.assertEqual(set(app.plex.endpoint_devices), {identity.endpoint_id("player-a")})
            self.assertEqual(app.plex.devices["player-a"].name, "Office")
            server.return_value.serve_forever.assert_called_once()

    def test_topology_consumes_policy_and_disables_trigger_by_default(self):
        with tempfile.TemporaryDirectory() as root:
            env = {"CONFIG_PATH": str(Path(__file__).parents[1] / "examples/config.json"),
                   "HISTORY_PATH": str(Path(root) / "history.json"),
                   "POLICY_PATH": str(Path(root) / "policy.json"),
                   "WEB_USERNAME": "operator", "WEB_PASSWORD_HASH": "test",
                   "WEB_SESSION_SECRET": "test", "SERVICE_API_TOKEN": "test"}
            with patch.dict(os.environ, env, clear=True), patch.object(topology, "Server"), \
                    patch.object(topology, "GroupManager") as manager, \
                    patch.object(topology, "TriggerStandbyMonitor") as trigger:
                topology.main()
            device = manager.call_args.args[0][0]
            self.assertEqual(device.id, "player-a")
            self.assertFalse(device.auto_join_target)
            trigger.assert_not_called()

    def test_unconfigured_services_do_not_start_workers(self):
        with tempfile.TemporaryDirectory() as root:
            for module in (playback, topology):
                with self.subTest(service=module.__name__), \
                        patch.dict(os.environ, {"CONFIG_PATH": str(Path(root) / "absent.json"),
                                               "STATE_PATH": root}, clear=True), \
                        patch.object(module.threading, "Thread") as worker:
                    with self.assertRaisesRegex(SystemExit, "Setup required"):
                        module.main()
                    worker.assert_not_called()
