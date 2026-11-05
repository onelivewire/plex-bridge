from http.client import HTTPConnection
import html
import io
import json
import logging
import re
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from plex_bridge.diagnostics import Diagnostics, SafeHandler, SafeServerErrors, request_event
from plex_bridge.playback import App, Server


PRIVATE = "Bearer secret-token Cookie=session-value http://user:password@192.0.2.9/signed-secret?token=secret-token /music/Private Album 01:23:45:67:89:ab Private Room server-identifier"


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.data = Diagnostics("playback")
        self.output = io.StringIO()
        self.logger = logging.Logger("private-logger-name")
        self.logger.addHandler(SafeHandler(self.data, self.output))

    def test_raw_messages_arguments_tracebacks_and_extra_fields_are_discarded(self):
        try:
            raise ValueError(PRIVATE)
        except ValueError:
            self.logger.exception(PRIVATE + " %s", PRIVATE, extra={"token": PRIVATE, "event": PRIVATE})
        self.logger.warning("received Plex playMedia target=%s offset=%s viewOffset=%s", PRIVATE, PRIVATE, PRIVATE)
        result = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(result[0]["error_class"], "ValueError")
        self.assertEqual(result[1]["event"], "play_media")
        self.assertNotIn("Traceback", self.output.getvalue())
        for word in PRIVATE.split():
            self.assertNotIn(word, self.output.getvalue())

    def test_unknown_exception_class_and_non_string_message_are_not_serialized(self):
        class SecretError(Exception):
            pass
        class Unsafe:
            def __str__(self):
                raise AssertionError("Must not stringify raw messages")
        try:
            raise SecretError(PRIVATE)
        except SecretError:
            self.logger.exception(Unsafe())
        self.assertEqual(json.loads(self.output.getvalue())["error_class"], "OtherError")

    def test_only_typed_allowlisted_fields_survive(self):
        self.logger.info("", extra={"event": "operation", "action": "join", "outcome": "joined", "status": 200})
        self.logger.info("", extra={"event": "request", "method": PRIVATE, "status": PRIVATE,
                                     "action": [PRIVATE], "outcome": PRIVATE})
        rows = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(rows[0]["action"], "join")
        self.assertEqual(rows[0]["status"], 200)
        self.assertNotIn("method", rows[1])
        self.assertNotIn("status", rows[1])
        self.assertNotIn("action", rows[1])

    def test_request_logging_never_records_paths_or_malformed_request_text(self):
        request_event(self.logger, "GET", (PRIVATE, "403", "-"))
        request_event(self.logger, PRIVATE, (PRIVATE, PRIVATE))
        self.assertNotIn("secret", self.output.getvalue())
        self.assertEqual(json.loads(self.output.getvalue().splitlines()[0])["status"], 403)

    def test_preview_download_is_exact_frozen_snapshot(self):
        self.logger.info("", extra={"event": "service_start"})
        incident, body = self.data.preview()
        self.logger.error(PRIVATE)
        self.assertEqual(body, self.data.download(incident))
        payload = json.loads(body)
        self.assertEqual(payload["incident_id"], incident)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["health"]["devices"], "not_checked")

    def test_bounded_events_and_preview_cache(self):
        for _ in range(400):
            self.logger.info("", extra={"event": "request"})
        first, body = self.data.preview()
        self.assertEqual(len(json.loads(body)["events"]), 250)
        for _ in range(8):
            self.data.preview()
        self.assertIsNone(self.data.download(first))
        self.assertEqual(len(self.data.bundles), 8)

    def test_retention_and_preview_expiry(self):
        with patch("plex_bridge.diagnostics.time.monotonic", return_value=0):
            self.logger.info("", extra={"event": "request"})
            incident, _ = self.data.preview()
        with patch("plex_bridge.diagnostics.time.monotonic", return_value=3601):
            self.assertIsNone(self.data.download(incident))
            self.assertEqual(json.loads(self.data.preview()[1])["events"], [])

    def test_logging_failure_does_not_dump_raw_record_to_stderr(self):
        broken = Mock()
        broken.write.side_effect = OSError(PRIVATE)
        self.logger.handlers = [SafeHandler(self.data, broken)]
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.logger.error(PRIVATE)
        self.assertEqual(stderr.getvalue(), "")

    def test_uncaught_errors_and_server_errors_use_safe_sink(self):
        code = ("from plex_bridge.diagnostics import configure, SafeServerErrors\n"
                "configure('playback')\n"
                "try: raise ValueError('private-exception-text')\n"
                "except ValueError: SafeServerErrors().handle_error(None, ('private-address', 1))\n"
                "raise RuntimeError('private-exception-text')\n")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        rows = [json.loads(line) for line in result.stderr.splitlines()]
        self.assertEqual([r["error_class"] for r in rows], ["ValueError", "RuntimeError"])
        self.assertNotIn("private-", result.stderr)


class DiagnosticsHttpTests(unittest.TestCase):
    def setUp(self):
        self.app = App(Mock(), "operator", "unused", "synthetic-session-secret")
        self.server = Server(("127.0.0.1", 0), self.app)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.addCleanup(self.close)
        self.cookie = "wiim_focus_session=" + self.app.session_token()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def get(self, path, authenticated=True):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("GET", path, headers={"Cookie": self.cookie} if authenticated else {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_preview_and_download_require_authentication(self):
        for path in ("/diagnostics", "/diagnostics/download/unknown"):
            status, headers, _ = self.get(path, False)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/login?next=diagnostics")
        self.assertEqual(len(self.app.diagnostics.bundles), 0)

    def test_preview_matches_download_without_dependency_calls(self):
        status, headers, body = self.get("/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        incident = re.search(rb"/diagnostics/download/([0-9a-f]{32})", body).group(1).decode()
        status, headers, download = self.get("/diagnostics/download/" + incident)
        self.assertEqual(status, 200)
        self.assertIn(incident, headers["Content-Disposition"])
        preview = html.unescape(re.search(r"<pre[^>]*>(.*?)</pre>", body.decode(), re.S).group(1))
        self.assertEqual(preview.encode(), download)
        self.assertEqual(self.app.coordinator.mock_calls, [])

    def test_unknown_download_and_login_return_target(self):
        self.assertEqual(self.get("/diagnostics/download/unknown")[0], 404)
        self.assertIn(b"value='diagnostics'", self.get("/login?next=diagnostics", False)[2])
        self.assertNotIn(b"value='http", self.get("/login?next=http://example.invalid", False)[2])
        self.assertEqual(self.get("/login?next=diagnostics")[1]["Location"], "/diagnostics")
        self.assertEqual(self.get("/login?next=http://example.invalid")[1]["Location"], "/dashboard")
