import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plex_bridge.config import ConfigError
from plex_bridge.provisioning import Provisioner, atomic_write, credential_settings
from plex_bridge.runtime import is_ready, publish


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setup = Provisioner(self.root / "private")
        code = json.loads((self.root / "private/setup-code.json").read_text())["code"]
        self.token = self.setup.claim(code)
        self.exports = self.root / "exports"
        self.handoff = self.root / "handoff"

    def complete(self):
        self.setup.administrator(self.token, "operator", "a-test-password-long")
        self.setup.configure(self.token, "http://192.0.2.10:8794", "http://192.0.2.20:32400", "Office, 192.0.2.30")
        self.setup.finish(self.token)

    def test_waits_before_setup_and_publishes_only_scoped_credentials(self):
        self.assertFalse(publish(self.setup, self.exports, self.handoff))
        self.assertFalse(is_ready("playback", self.exports / "playback", self.handoff))
        self.complete()
        self.assertTrue(publish(self.setup, self.exports, self.handoff))
        for service in ("playback", "topology"):
            directory = self.exports / service
            self.assertEqual({p.name for p in directory.iterdir()}, {"config.json", "credentials.json"})
            self.assertTrue(is_ready(service, directory, self.handoff))
            with patch.dict(os.environ, {"CREDENTIALS_PATH": str(directory / "credentials.json")}, clear=True):
                credentials = credential_settings(service)
            if service == "topology":
                self.assertNotIn("PLEX_MEDIA_SECRET", credentials)
                self.assertNotIn("GROUP_API_TOKEN", credentials)
            else:
                self.assertNotIn("SERVICE_API_TOKEN", credentials)
        marker = (self.handoff / "ready.json").read_bytes()
        publish(self.setup, self.exports, self.handoff)
        self.assertEqual(marker, (self.handoff / "ready.json").read_bytes())

    def test_interrupted_export_never_releases_waiters_and_recovers(self):
        self.complete()
        def fail_midway(path, value):
            if Path(path).parent.name == "topology":
                raise OSError("simulated storage failure")
            atomic_write(path, value)
        with patch("plex_bridge.runtime.atomic_write", side_effect=fail_midway):
            with self.assertRaises(OSError):
                publish(self.setup, self.exports, self.handoff)
        self.assertFalse(is_ready("playback", self.exports / "playback", self.handoff))
        saved = (self.exports / "playback/credentials.json").read_bytes()
        publish(Provisioner(self.root / "private"), self.exports, self.handoff)
        self.assertEqual(saved, (self.exports / "playback/credentials.json").read_bytes())
        self.assertTrue(is_ready("topology", self.exports / "topology", self.handoff))

    def test_tampered_runtime_is_rejected_without_overwrite(self):
        self.complete()
        publish(self.setup, self.exports, self.handoff)
        path = self.exports / "playback/credentials.json"
        path.write_text('{}')
        with self.assertRaises(ConfigError):
            is_ready("playback", path.parent, self.handoff)
        with self.assertRaises(ConfigError):
            publish(self.setup, self.exports, self.handoff)
        self.assertEqual(path.read_text(), '{}')

    def test_runner_waits_then_executes_configured_service(self):
        from plex_bridge import runtime
        waiting = self.root / "waiting.json"
        real_path = Path
        def local_path(value):
            return waiting if str(value) == "/tmp/bridge-waiting.json" else real_path(value)
        env = {"BRIDGE_SERVICE": "playback", "RUNTIME_PATH": str(self.exports / "playback"),
               "HANDOFF_PATH": str(self.handoff)}
        with patch.dict(os.environ, env, clear=True), patch.object(runtime, "Path", side_effect=local_path), \
                patch.object(runtime, "is_ready", side_effect=[False, True]), \
                patch.object(runtime.time, "sleep") as sleep, patch.object(runtime.os, "execv") as execute:
            runtime.main()
            sleep.assert_called_once_with(1)
            self.assertEqual(os.environ["CREDENTIALS_PATH"], str(self.exports / "playback/credentials.json"))
            self.assertEqual(execute.call_args.args[1][-1], "plex_bridge.playback")
            self.assertFalse(waiting.exists())
