from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode, urlsplit, parse_qs

from plex_bridge.onboarding import Onboarding, check_server, NoRedirect
from plex_bridge.playback import App, Server, PlexCloudClient, PlexApprovalPending, PlexApprovalExpired, FocusError


class CloudHttp:
    def __init__(self):
        self.calls = []
        self.approved = False
        self.fail_publish = False

    def request(self, url, method, headers):
        self.calls.append((url, method, headers))
        if method == "POST":
            return {"id": 123, "code": "synthetic-pin"}
        if method == "GET":
            return {"authToken": "synthetic-token" if self.approved else None}
        if self.fail_publish:
            raise RuntimeError("upstream body with private data")
        return {}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.device = SimpleNamespace(id="player-a", name="Office")
        self.session = SimpleNamespace(target_device_id="player-a", state="playing",
                                       renderer_session_id="session-a", current=SimpleNamespace(play_queue_item_id="item-a"))
        self.renderer = Mock()
        self.bridge = SimpleNamespace(endpoint_devices={"endpoint-a": self.device},
                                      allowed_server_base="http://192.0.2.20:32400",
                                      public_base="http://192.0.2.10:8794",
                                      store=Mock(), renderer=self.renderer, media_url=Mock(return_value="signed-test-url"))
        self.bridge.store.snapshot.return_value = self.session
        self.http = CloudHttp()
        self.cloud = PlexCloudClient("installation", self.bridge.public_base,
                                     self.root / "cloud.json", self.http, {"endpoint-a": "Office"})
        self.groups = Mock()
        self.groups.players.return_value = [{"device": {"id": "player-a"}, "online": True}]
        self.server_check = Mock()
        self.flow = Onboarding(self.root / "onboarding.json", self.bridge, self.cloud, self.groups, self.server_check)

    def run_action(self, action, endpoint="endpoint-a"):
        self.assertTrue(self.flow.submit(action, endpoint))
        self.flow.worker.join(timeout=5)
        self.assertFalse(self.flow.worker.is_alive())
        return self.flow.snapshot()["operation"]

    def link(self):
        self.run_action("link")
        self.http.approved = True
        self.run_action("finish")

    def test_connection_and_server_checks_are_read_only_and_explicit(self):
        self.flow.page()
        self.groups.players.assert_not_called()
        self.server_check.assert_not_called()
        self.run_action("connections", "")
        self.assertEqual(self.flow.snapshot()["rooms"]["endpoint-a"]["connection"], "reachable")
        self.assertEqual(self.groups.method_calls, [("players", (), {})])
        self.assertEqual(self.run_action("server", "")["status"], "waiting")
        self.server_check.assert_not_called()
        self.link()
        self.run_action("server", "")
        self.server_check.assert_called_once_with(self.bridge.allowed_server_base, "synthetic-token")
        self.assertEqual(self.renderer.method_calls, [])

    def test_link_pending_survives_restart_without_new_pin(self):
        self.run_action("link")
        before = len([c for c in self.http.calls if c[1] == "POST"])
        cloud = PlexCloudClient("installation", self.bridge.public_base, self.root / "cloud.json",
                                self.http, {"endpoint-a": "Office"})
        self.assertIn("synthetic-pin", cloud.link_url("endpoint-a"))
        cloud.begin_link("endpoint-a")
        self.assertEqual(before, len([c for c in self.http.calls if c[1] == "POST"]))
        self.assertEqual(self.run_action("finish")["status"], "waiting")
        self.assertFalse(self.cloud.linked("endpoint-a"))
        self.http.approved = True
        self.run_action("finish")
        self.assertTrue(self.cloud.linked("endpoint-a"))
        saved = json.loads((self.root / "onboarding.json").read_text())
        self.assertNotIn("synthetic-token", json.dumps(saved))
        self.assertNotIn("synthetic-pin", json.dumps(saved))

    def test_publication_failure_keeps_token_and_retries_without_reauthorization(self):
        self.run_action("link")
        self.http.approved = True
        self.http.fail_publish = True
        result = self.run_action("finish")
        self.assertTrue(self.cloud.linked("endpoint-a"))
        self.assertIn("no new approval", result["message"])
        self.assertNotIn("private data", json.dumps(self.flow.snapshot()))
        self.http.fail_publish = False
        self.run_action("finish")
        self.assertEqual(self.flow.snapshot()["rooms"]["endpoint-a"]["registration"], "published")
        self.assertEqual(len([c for c in self.http.calls if c[1] == "POST"]), 1)

    def test_expired_pin_is_distinct_and_can_be_replaced(self):
        self.run_action("link")
        self.cloud.pending["endpoint-a"]["created"] = 0
        self.assertIn("expired", self.run_action("finish")["message"])
        self.run_action("link")
        self.assertEqual(len([c for c in self.http.calls if c[1] == "POST"]), 2)

    def test_discovery_is_user_confirmation_not_registration(self):
        self.link()
        self.assertFalse(self.flow.snapshot()["rooms"]["endpoint-a"].get("seen"))
        self.run_action("seen")
        self.assertTrue(self.flow.snapshot()["rooms"]["endpoint-a"]["seen"])

    def test_plex_authorization_returns_to_originating_page_and_room(self):
        self.run_action("link")
        for page, destination in (("onboarding", "/onboarding?step=accounts#link-endpoint-a"),
                                  ("dashboard", "/dashboard#plex-endpoints")):
            url = self.cloud.link_url("endpoint-a", return_to=page)
            query = parse_qs(urlsplit(url).fragment.removeprefix("?"))
            self.assertEqual(query["forwardUrl"], [self.bridge.public_base + destination])
        with self.assertRaises(FocusError):
            self.cloud.link_url("endpoint-a", return_to="https://example.invalid")
        page = self.flow.page("accounts")
        self.assertIn("id='link-endpoint-a'", page)
        self.assertNotIn("target='_blank'", page)

    def test_playback_requires_explicit_check_expected_source_and_advancement(self):
        self.link()
        self.assertEqual(self.run_action("verify")["status"], "waiting")
        self.renderer.snapshot.assert_not_called()
        self.run_action("arm")
        self.renderer.snapshot.assert_not_called()
        def snapshot(pos, source="signed-test-url"):
            return SimpleNamespace(state="playing", source_url=source, position_ms=pos)
        self.renderer.snapshot.side_effect = [snapshot(1000, "other-source"), snapshot(3000, "other-source")]
        with patch("plex_bridge.onboarding.time.sleep"):
            self.assertEqual(self.run_action("verify")["status"], "waiting")
        self.renderer.snapshot.side_effect = [snapshot(1000), snapshot(3000)]
        with patch("plex_bridge.onboarding.time.sleep"):
            self.assertEqual(self.run_action("verify")["status"], "success")
        self.assertFalse(self.flow.snapshot()["rooms"]["endpoint-a"]["test"]["heard"])
        self.run_action("heard")
        self.assertTrue(self.flow.snapshot()["rooms"]["endpoint-a"]["test"]["heard"])
        self.assertTrue(all(call[0] == "snapshot" for call in self.renderer.method_calls))

    def test_interrupted_progress_is_visible_after_restart(self):
        self.flow.data["operation"] = {"status": "running"}
        self.flow.save()
        recovered = Onboarding(self.flow.path, self.bridge, self.cloud, self.groups)
        self.assertEqual(recovered.snapshot()["operation"]["status"], "interrupted")

    def test_single_worker_and_per_room_error_persistence(self):
        release = threading.Event()
        self.groups.players.side_effect = lambda: release.wait(3) or []
        self.flow.submit("connections")
        self.assertFalse(self.flow.submit("link", "endpoint-a"))
        release.set()
        self.flow.worker.join(5)
        self.run_action("finish")
        last = self.flow.snapshot()["rooms"]["endpoint-a"]["last_result"]
        self.run_action("server", "")
        self.assertEqual(last, self.flow.snapshot()["rooms"]["endpoint-a"]["last_result"])

    def test_connection_progress_transitions_to_reachable_without_another_check(self):
        release = threading.Event()
        def check_rooms():
            release.wait(3)
            return [{"device": {"id": "player-a"}, "online": True}]
        self.groups.players.side_effect = check_rooms
        try:
            self.assertTrue(self.flow.submit("connections"))
            page = self.flow.page("connections")
            self.assertIn("data-busy='true'", page)
            self.assertIn("Checking room connections…", page)
            self.assertIn("class='setup-track'", page)
            self.assertIn("Not checked yet", page)
            release.set()
            self.flow.worker.join(5)

            page = self.flow.page("connections")
            self.assertIn("data-busy='false'", page)
            self.assertIn("class='setup-progress' hidden", page)
            self.assertIn("<strong>Office</strong>: Reachable", page)
            self.groups.players.assert_called_once()
            self.assertEqual(self.renderer.method_calls, [])
        finally:
            release.set()
            self.flow.worker.join(5)

    def test_approval_progress_identifies_room_and_action(self):
        self.flow.data["operation"] = {"status": "running", "action": "finish", "endpoint": "endpoint-a"}
        self.cloud.pending["endpoint-a"] = {"created": time.time(), "code": "synthetic-pin"}
        self.flow.data["rooms"]["endpoint-a"]["authorization_opened"] = self.cloud.pending["endpoint-a"]["created"]
        page = self.flow.page("accounts")
        self.assertIn("<strong>Checking Plex approval for Office…</strong>", page)
        self.assertIn("<button disabled>Checking Plex approval for Office…</button>", page)
        self.assertNotIn("<strong>Checking room connections…</strong>", page)

    def test_wizard_renders_one_step_and_preserves_explicit_step_after_completion(self):
        steps = ("connections", "accounts", "server", "players", "playback")
        for step in steps:
            page = self.flow.page(step)
            self.assertEqual(page.count("<section id="), 1)
            self.assertIn(f"<section id='{step}'>", page)
            self.assertIn(f"data-step='{step}'", page)
            self.assertIn("class='step-cards'", page)
            self.assertEqual(page.count("aria-current='step'"), 1)
        self.run_action("connections", "")
        self.assertIn("data-step='accounts'", self.flow.page())
        self.assertIn("data-step='connections'", self.flow.page("connections"))
        self.assertIn("<strong>Office</strong>: Reachable", self.flow.page("connections"))
        self.assertIn("data-step='accounts'", self.flow.page("https://example.invalid"))

    def test_authorization_card_promotes_open_link_then_recovers_expiry(self):
        page = self.flow.page("accounts")
        self.assertIn("action='/onboarding/link'", page)
        self.assertNotIn("action='/onboarding/finish'", page)
        self.run_action("link")
        page = self.flow.page("accounts")
        self.assertNotIn("action='/onboarding/link'", page)
        self.assertIn("class='primary-action'", page)
        self.assertIn("Open Plex authorization →", page)
        self.assertNotIn("action='/onboarding/finish'", page)
        url = self.flow.open_authorization("endpoint-a")
        self.assertEqual(url, self.cloud.link_url("endpoint-a"))
        page = self.flow.page("accounts")
        self.assertIn("action='/onboarding/finish'", page)
        self.assertNotIn(">Open Plex authorization →</button>", page)
        self.assertIn("<details><summary>Need to reopen authorization?</summary>", page)
        recovered = Onboarding(self.flow.path, self.bridge, self.cloud, self.groups)
        self.assertIn("action='/onboarding/finish'", recovered.page("accounts"))
        self.cloud.pending["endpoint-a"]["created"] = 0
        page = self.flow.page("accounts")
        self.assertIn("Get a new Plex authorization link", page)
        self.assertNotIn("action='/onboarding/finish'", page)
        with self.assertRaises(ValueError):
            self.flow.open_authorization("endpoint-a")
        self.run_action("link")
        self.assertIn(">Open Plex authorization →</button>", self.flow.page("accounts"))
        self.assertNotIn("action='/onboarding/finish'", self.flow.page("accounts"))

    def test_onboarding_http_authentication_and_origin(self):
        app = App(SimpleNamespace(), "operator", "unused", "test-secret", self.bridge, self.cloud)
        app.onboarding = self.flow
        server = Server(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.bridge.public_base = f"http://127.0.0.1:{server.server_port}"
        def request(path, method="GET", authenticated=False, origin=None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if authenticated:
                headers["Cookie"] = "wiim_focus_session=" + app.session_token()
            if origin:
                headers["Origin"] = origin
            connection.request(method, path, "endpoint=endpoint-a" if method == "POST" else None, headers)
            response = connection.getresponse()
            if response.getheader("Content-Type", "").startswith("text/html"):
                self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")
                if authenticated:
                    self.assertIn("SameSite=Lax", response.getheader("Set-Cookie"))
                    self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
            if path == "/login?next=onboarding" and authenticated:
                self.assertEqual(response.getheader("Location"), "/onboarding")
            if path == "/onboarding/open" and response.status == 303:
                self.assertEqual(response.getheader("Location"), self.cloud.link_url("endpoint-a"))
            result = response.status, response.read().decode()
            connection.close()
            return result
        try:
            self.assertEqual(request("/onboarding")[0], 303)
            self.assertIn("Finish setup", request("/onboarding", authenticated=True)[1])
            self.assertEqual(request("/login?next=onboarding", authenticated=True)[0], 303)
            self.assertEqual(request("/onboarding/link", "POST", True, "http://example.invalid")[0], 403)
            self.assertEqual(request("/onboarding/link", "POST", True, "null")[0], 403)
            self.assertEqual(len(self.http.calls), 0)
            self.assertEqual(request("/onboarding/link", "POST", True, self.bridge.public_base)[0], 303)
            self.flow.worker.join(5)
            self.assertEqual(request("/onboarding/open", "POST", True, "http://example.invalid")[0], 403)
            self.assertNotIn("authorization_opened", self.flow.snapshot()["rooms"]["endpoint-a"])
            self.assertEqual(request("/onboarding/open", "POST", True, self.bridge.public_base)[0], 303)
            self.assertIn("authorization_opened", self.flow.snapshot()["rooms"]["endpoint-a"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_server_probe_does_not_follow_redirects_or_expose_response(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))
        with patch("plex_bridge.onboarding.urllib.request.build_opener") as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b"<MediaContainer/>"
            check_server("http://192.0.2.20:32400", "synthetic-token")
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://192.0.2.20:32400/library/sections")
            self.assertNotIn("synthetic-token", request.full_url)
