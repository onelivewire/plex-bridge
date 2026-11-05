from http.client import HTTPConnection
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from plex_bridge.config import ConfigError, load_config
from plex_bridge import playback, topology
from plex_bridge.provisioning import Provisioner, credential_settings
from plex_bridge.setup import SetupServer


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setup = Provisioner(self.root)
        self.code = json.loads((self.root / "setup-code.json").read_text())["code"]
        self.token = self.setup.claim(self.code)

    def administrator(self):
        self.setup.administrator(self.token, "operator", "a-long-test-password")

    def configure(self):
        self.setup.configure(self.token, "http://192.0.2.10:8794", "http://192.0.2.20:32400",
                             "Office, 192.0.2.30\nStudy, 192.0.2.31")

    def test_resume_finish_credentials_and_code_invalidation(self):
        self.administrator()
        self.setup = Provisioner(self.root)
        self.assertEqual(self.setup.view(self.token)["stage"], "players")
        self.configure()
        original = self.setup.view(self.token)["config"]
        self.configure()
        self.assertEqual(original, self.setup.view(self.token)["config"])
        self.setup.finish(self.token)
        before = {p.name: p.read_bytes() for p in self.root.glob("*-credentials.json")}
        Provisioner(self.root).finish(self.token)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob("*-credentials.json")})
        self.assertEqual(len(load_config(self.root / "config.json").players), 2)
        with self.assertRaises(ConfigError):
            self.setup.claim(self.code)
        play = json.loads(before["playback-credentials.json"])
        group = json.loads(before["topology-credentials.json"])
        self.assertEqual(play["GROUP_API_TOKEN"], group["SERVICE_API_TOKEN"])
        self.assertNotEqual(play["WEB_SESSION_SECRET"], group["WEB_SESSION_SECRET"])
        self.assertTrue(playback.verify_password("a-long-test-password", play["WEB_PASSWORD_HASH"]))
        self.assertNotIn("a-long-test-password", (self.root / "setup.json").read_text())
        for name in ("setup.json", "setup-code.json", "config.json", *before):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600)

    def test_authorization_expiry_and_step_order(self):
        with self.assertRaises(ConfigError):
            self.setup.view("wrong")
        with self.assertRaises(ConfigError):
            self.configure()
        with patch("plex_bridge.provisioning.time.time", return_value=10**12):
            with self.assertRaises(ConfigError):
                self.setup.view(self.token)
        self.assertNotEqual(self.token, self.setup.claim(self.code))
        with self.assertRaises(ConfigError):
            self.setup.view(self.token)

    def test_validation_failure_preserves_saved_progress(self):
        self.administrator()
        before = (self.root / "setup.json").read_bytes()
        with self.assertRaises(ConfigError):
            self.setup.configure(self.token, "http://user:private@192.0.2.10", "invalid", "Room, 192.0.2.30")
        self.assertEqual(before, (self.root / "setup.json").read_bytes())
        self.assertFalse((self.root / "config.json").exists())

    def test_partial_commit_resumes_without_regenerating_credentials(self):
        self.administrator()
        self.configure()
        from plex_bridge.provisioning import atomic_write
        def fail_once(path, value):
            if Path(path).name == "topology-credentials.json":
                raise OSError("simulated disk failure")
            atomic_write(path, value)
        with patch("plex_bridge.provisioning.atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.setup.finish(self.token)
        before = (self.root / "playback-credentials.json").read_bytes()
        with patch.dict(os.environ, {"CREDENTIALS_PATH": str(self.root / "playback-credentials.json")}, clear=True):
            with self.assertRaises(ConfigError):
                credential_settings("playback")
        Provisioner(self.root).finish(self.token)
        self.assertEqual(before, (self.root / "playback-credentials.json").read_bytes())
        self.assertEqual(self.setup.view(self.token)["stage"], "complete")

    def test_conflicting_output_is_preserved(self):
        self.administrator()
        self.configure()
        (self.root / "config.json").write_text('{"different":true}')
        with self.assertRaises(ConfigError):
            self.setup.finish(self.token)
        self.assertFalse((self.root / "playback-credentials.json").exists())
        self.assertEqual((self.root / "config.json").read_text(), '{"different":true}')

    def test_generated_credentials_start_both_services(self):
        self.administrator()
        self.configure()
        self.setup.finish(self.token)
        for module, name in ((playback, "playback"), (topology, "topology")):
            with self.subTest(service=name), patch.dict(os.environ, {
                "CONFIG_PATH": str(self.root / "config.json"),
                "CREDENTIALS_PATH": str(self.root / f"{name}-credentials.json"),
                "STATE_PATH": str(self.root / "playback-state"),
                "HISTORY_PATH": str(self.root / "topology-history.json"),
                "POLICY_PATH": str(self.root / "topology-policy.json"),
                "GROUP_API_BASE": "http://127.0.0.1:8793"}, clear=True), \
                    patch.object(module, "Server") as server, patch.object(module.threading, "Thread"):
                module.main()
                server.return_value.serve_forever.assert_called_once()

    def test_wrong_service_credential_file_is_rejected(self):
        self.administrator()
        self.configure()
        self.setup.finish(self.token)
        with patch.dict(os.environ, {"CREDENTIALS_PATH": str(self.root / "topology-credentials.json")}, clear=True):
            with self.assertRaises(ConfigError):
                credential_settings("playback")


class SetupHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provisioner = Provisioner(self.temp.name)
        self.export_env = patch.dict(os.environ, {
            "RUNTIME_EXPORTS": str(Path(self.temp.name) / "exports"),
            "HANDOFF_PATH": str(Path(self.temp.name) / "handoff")})
        self.export_env.start()
        self.addCleanup(self.export_env.stop)
        self.server = SetupServer(("127.0.0.1", 0), self.provisioner, "http://127.0.0.1")
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.server.origin = self.origin
        self.server.authority = self.origin.removeprefix("http://")
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie = ""

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path="/", fields=None, origin=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            headers = {"Cookie": self.cookie}
            if fields is not None:
                headers.update({"Origin": origin or self.origin,
                                "Content-Type": "application/x-www-form-urlencoded"})
            conn.request("POST" if fields is not None else "GET", path,
                         urlencode(fields) if fields is not None else None, headers)
            response = conn.getresponse()
            self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")
            if response.getheader("Set-Cookie"):
                self.cookie = response.getheader("Set-Cookie").split(";")[0]
            if response.status == 303:
                self.assertEqual(response.getheader("Location"), "/")
                response.read()
                conn.close()
                return self.request()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def test_browser_flow_and_cross_origin_denial(self):
        code = json.loads((Path(self.temp.name) / "setup-code.json").read_text())["code"]
        self.assertEqual(self.request("/claim", {"code": code}, "http://example.invalid")[0], 403)
        self.assertEqual(self.request("/claim", {"code": code}, "null")[0], 403)
        self.assertEqual(self.request("/administrator", {"username": "operator", "password": "test"})[0], 400)
        self.assertEqual(self.request("/claim", {"code": code})[0], 200)
        self.assertEqual(self.request("/administrator", {"username": "operator", "password": "long-test-password"})[0], 200)
        status, body = self.request("/players", {"advertised_origin": "http://192.0.2.10:8794",
                                               "plex_server_origin": "http://192.0.2.20:32400",
                                               "rooms": "Office, 192.0.2.30"})
        self.assertEqual(status, 200)
        self.assertIn("Step 3", body)
        self.assertIn("Step 3", self.request()[1])
        status, body = self.request("/finish", {})
        self.assertEqual(status, 200)
        self.assertIn("Local setup is complete", body)
        from plex_bridge.runtime import is_ready
        self.assertTrue(is_ready("playback", Path(self.temp.name) / "exports/playback",
                                 Path(self.temp.name) / "handoff"))
        self.assertIn("start automatically", body)
        credentials = json.loads((Path(self.temp.name) / "playback-credentials.json").read_text())
        for key in ("GROUP_API_TOKEN", "PLEX_MEDIA_SECRET", "WEB_PASSWORD_HASH"):
            self.assertNotIn(credentials[key], body)
        self.assertNotIn(code, body)
        self.cookie = ""
        self.assertIn("Local setup is complete", self.request()[1])

    def test_discovery_selection_preserves_draft_and_requires_confirmation(self):
        import time
        code = json.loads((Path(self.temp.name) / "setup-code.json").read_text())["code"]
        self.request("/claim", {"code": code})
        self.request("/administrator", {"username": "operator", "password": "long-test-password"})
        found = [{"kind": "wiim", "host": "192.168.1.30", "name": "Office <speaker>", "model": "WiiM Pro"},
                 {"kind": "plex", "host": "192.168.1.20", "name": "Library", "model": "Plex Media Server", "origin": "http://192.168.1.20:32400"}]
        with patch("plex_bridge.setup.discover", return_value=found):
            status, _ = self.request("/discover", {"advertised_origin": "http://192.168.1.10:8794", "rooms": "Existing, 192.168.1.31"})
            self.assertEqual(status, 200)
            deadline = time.monotonic() + 3
            while self.provisioner.state().get("discovery", {}).get("status") == "scanning" and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertNotIn("config", self.provisioner.state())
        body = self.request()[1]
        self.assertIn("DHCP", body)
        self.assertIn("Office &lt;speaker&gt;", body)
        self.assertIn("Existing, 192.168.1.31", body)
        status, body = self.request("/players", {"advertised_origin": "http://192.168.1.10:8794", "selected": "0", "server": "1"})
        self.assertEqual(status, 200)
        config = self.provisioner.state()["config"]
        self.assertEqual(config["plex_server_origin"], "http://192.168.1.20:32400")
        self.assertEqual(config["players"][0]["host"], "192.168.1.30")
        self.assertFalse(config["players"][0]["auto_join_target"])
        self.assertEqual(self.provisioner.state()["stage"], "review")
