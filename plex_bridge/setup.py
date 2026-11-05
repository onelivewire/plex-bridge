"""Run with python -m plex_bridge.setup; provisioning requires local setup code."""

import html
import logging
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
from urllib.parse import parse_qs, urlsplit

from plex_bridge.config import ConfigError, _origin
from plex_bridge.provisioning import Provisioner
from plex_bridge.runtime import publish_configured, configured_exports
from plex_bridge.diagnostics import SafeServerErrors, configure
from plex_bridge.discovery import discover


class SetupServer(SafeServerErrors, ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, provisioner, origin):
        self.provisioner = provisioner
        self.origin = origin.rstrip("/")
        parsed = urlsplit(self.origin)
        if parsed.scheme != "http" or parsed.path or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError("Setup origin must be the exact HTTP origin used by the browser")
        self.authority = parsed.netloc
        self.scan_lock = threading.Lock()
        if provisioner.state().get("discovery", {}).get("status") == "scanning":
            provisioner.discovery_finish([], "interrupted")
        super().__init__(address, Handler)

    def scan(self):
        try:
            results = discover(self.server_address[0])
            self.provisioner.discovery_finish(results)
        except Exception:
            self.provisioner.discovery_finish([], "failed")
        finally:
            self.scan_lock.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # Never record setup codes, form bodies, cookies, or player addresses.

    def token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie["bridge_setup"].value if "bridge_setup" in cookie else ""
        except CookieError:
            return ""

    def page(self, status, body, cookie=None):
        document = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width'><title>Plex bridge setup</title>"
                    "<style>body{font:17px system-ui;max-width:46rem;margin:2rem auto;padding:1rem;"
                    "background:#141820;color:#f3f5f8}label{display:block;margin:1rem 0}"
                    "input,textarea,button{box-sizing:border-box;width:100%;padding:.8rem;font:inherit}"
                    "button{margin:.6rem 0;background:#b4d8ff;color:#122438;border:0}"
                    "a{color:#b4d8ff}textarea{min-height:8rem}input[type=checkbox],input[type=radio]{width:auto}"
                    "fieldset,aside{padding:1rem;border:1px solid #65758b;margin:1rem 0}small{display:block}"
                    ".scan-status{padding:1rem;background:#29364a;border:1px solid #b4d8ff;border-radius:.6rem}"
                    ".scan-track{height:.4rem;overflow:hidden;background:#141820;border-radius:1rem;margin-top:.7rem}"
                    ".scan-track span{display:block;width:35%;height:100%;background:#b4d8ff;animation:scan 1.4s ease-in-out infinite alternate}"
                    "@keyframes scan{from{transform:translateX(0)}to{transform:translateX(185%)}}"
                    "@media(prefers-reduced-motion:reduce){.scan-track span{animation:none}}"
                    "</style><main><h1>Plex bridge setup</h1>"
                    + body + "</main></html>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(document)))
        self.send_header("Cache-Control", "no-store")
        # no-referrer makes browser form POST origins opaque ("null"), which
        # conflicts with the exact-origin CSRF check below. Suppress external
        # referrers while preserving same-origin form submissions.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"bridge_setup={cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600")
        self.end_headers()
        self.wfile.write(document)

    def valid_host(self):
        return self.headers.get("Host") == self.server.authority

    def do_GET(self):
        if self.valid_host() and self.path == "/health":
            self.page(200, "<p>Setup service is running.</p>")
            return
        if not self.valid_host() or self.path != "/":
            self.page(404, "<p>Page not found.</p>")
            return
        state = self.server.provisioner.state()
        if state["stage"] == "complete":
            self.render({"stage": "complete", "config": state["config"]})
            return
        try:
            view = self.server.provisioner.view(self.token())
        except ConfigError:
            self.page(200, "<p>Enter the setup code from the local setup-code.json file. "
                      "Use this setup page only on your trusted LAN.</p><form method='post' action='/claim'>"
                      "<label>Setup code<input name='code' type='password' required autocomplete='off'></label>"
                      "<button>Continue setup</button></form>")
            return
        self.render(view)

    def render(self, view, cookie=None):
        stage = view["stage"]
        if stage == "administrator":
            body = "<p>Step 1 of 3: Create your administrator account.</p><form method='post' action='/administrator'>" \
                   "<label>Username<input name='username' required autocomplete='username'></label>" \
                   "<label>Password (12 characters minimum)<input name='password' type='password' minlength='12' " \
                   "required autocomplete='new-password'></label><button>Save administrator</button></form>"
        elif stage in {"players", "review"}:
            config = view.get("config") or {}
            draft = view.get("draft") or {}
            discovery = view.get("discovery") or {}
            escape = html.escape
            rooms = "\n".join(f"{p['name']}, {p['host']}" for p in config.get("players", []))
            rooms = draft.get("rooms", rooms)
            body = "<h2>Step 2 of 3 · Choose your server and rooms</h2>" \
                   "<p>This page saves where the bridge, Plex server and WiiMs can be reached on your home network. " \
                   "Plex account linking and audio checks come after this page.</p>" \
                   "<ol><li><strong>Find devices:</strong> run the LAN search below.</li>" \
                   "<li><strong>Select:</strong> choose one Plex server and only the WiiM rooms you want to use.</li>" \
                   "<li><strong>Review addresses:</strong> confirm the bridge URL, then save and review your room names.</li></ol>"
            body += "<details><summary>Before you begin: keep network addresses stable</summary><p>In your router, reserve DHCP addresses for " \
                    "this bridge host, your Plex server, and each WiiM (using the interface each device actually uses). " \
                    "Reservations keep the same IP after a restart. Discovery cannot confirm router reservations, " \
                    "and saved addresses are not automatically repaired if DHCP changes them. Plex and the bridge " \
                    "may share a host or run on different hosts.</p></details>"
            if discovery.get("status") == "scanning":
                body += "<meta http-equiv='refresh' content='2;url=/'><div class='scan-status' role='status' aria-live='polite'>" \
                        "<strong>Searching for WiiMs and Plex servers…</strong><p>Please wait. Results appear automatically. " \
                        "Your entries are saved. No playback or grouping changes are made.</p>" \
                        "<div class='scan-track' aria-hidden='true'><span></span></div></div>"
                self.page(200, body, cookie)
                return
            if discovery:
                status = discovery.get("status")
                body += "<p role='status'>" + ({"complete": "Search complete. Select the devices you want below.",
                    "failed": "Search could not complete. Retry or use manual entry.",
                    "interrupted": "Search was interrupted by a restart. Your entries are saved; retry when ready."}.get(status, "")) + "</p>"
                if not discovery.get("results"):
                    body += "<p>No compatible devices found. Check the bridge and devices are on the same LAN, " \
                            "with multicast allowed and no guest/client isolation. Check Plex local discovery is enabled; " \
                            "manual entry remains available. Do not enable Internet port forwarding.</p>"
            body += "<form method='post' action='/players'>"
            body += "<h3>Find and select devices</h3><p>Power on your WiiMs and Plex server before searching. " \
                    "Search reads device information; it does not play music or change groups.</p>"
            body += "<button formaction='/discover'>Find WiiMs and Plex servers on my LAN</button>"
            results = discovery.get("results", [])
            if results:
                wiims = sum(item.get("kind") == "wiim" for item in results)
                servers = sum(item.get("kind") == "plex" for item in results)
                body += f"<p>Found {wiims} WiiM devices and {servers} Plex servers. Select only the rooms you want to set up.</p>"
                body += "<fieldset><legend>Discovered devices — choose what to enroll</legend>"
                for index, item in enumerate(results):
                    kind = item.get("kind")
                    field = "selected" if kind == "wiim" else "server"
                    input_type = "checkbox" if kind == "wiim" else "radio"
                    body += f"<label><input type='{input_type}' name='{field}' value='{index}'> " \
                            f"{escape(item['name'])}<small>{escape(item['model'])} · {escape(item.get('origin', item['host']))}</small></label>"
                body += "<p>Only selected WiiMs are added. A selected Plex server overrides the manual Plex URL below.</p></fieldset>"
            body += "<h3>Confirm the network addresses</h3>"
            for name, label in (("advertised_origin", "Bridge LAN URL, including port"),
                                ("plex_server_origin", "Plex server LAN URL, including port")):
                default = os.environ.get("EXPECTED_BRIDGE_ORIGIN", "") if name == "advertised_origin" else ""
                body += f"<label>{label}<input name='{name}' value='{escape(draft.get(name, config.get(name, default)), quote=True)}'></label>"
                if name == "advertised_origin":
                    body += "<small>The playback address of this bridge, not the setup page address. " \
                            "Use the prefilled value when provided. WiiMs and Plexamp must be able to reach it.</small>"
                else:
                    body += "<small>Selecting a discovered Plex server fills this setting when you save. " \
                            "For manual entry, use http:// followed by your Plex server's LAN IP and port " \
                            "(usually :32400), without /web. Plex may run on a different computer from this bridge.</small>"
            expanded = " open" if stage == "review" or rooms.strip() else ""
            body += f"<details{expanded}><summary>Manual room entry / saved rooms</summary><label>One Name, IP address per line. The first is the default. You can rename rooms here.<textarea name='rooms'>{escape(rooms)}</textarea></label></details>"
            body += "<button>Save and review players</button></form>"
            if stage == "review":
                body += "<aside><h2>Step 3 of 3 · Review and continue</h2><p>These are your saved settings:</p><ul>"
                body += f"<li>Bridge: {escape(config['advertised_origin'])}</li><li>Plex: {escape(config['plex_server_origin'])}</li>"
                for player in config.get("players", []):
                    body += f"<li>{escape(player['name'])} — {escape(player['host'])}</li>"
                body += "</ul><p>If you edit anything above, click <strong>Save and review players</strong> again before continuing. " \
                        "The first saved room is the default. Automatic joining remains disabled.</p>" \
                        "<p><strong>Next:</strong> finish local setup, sign in with the administrator account you created, " \
                        "then follow the room connection, Plex approval and audio-check steps. Saving these settings does not start playback.</p>" \
                        "<form method='post' action='/finish'><button>Finish local setup</button></form></aside>"
        else:
            body = "<p>Local setup is complete. Configuration and credentials are saved.</p>" \
                   "<p>Start the playback and topology services with these files, then sign in to the bridge " \
                   "dashboard to link Plex endpoints and perform a playback test. Plex authorization and " \
                   "device connectivity are still pending.</p>"
            if configured_exports():
                url = html.escape(view["config"]["advertised_origin"] + "/onboarding", quote=True)
                body = "<h2>Local setup is complete</h2><p>Your administrator account, room names and addresses are saved. " \
                       "Playback and grouping services will start automatically; allow a few seconds for startup.</p>" \
                       "<h3>Next: connect your rooms to Plex</h3><ol>" \
                       f"<li><a href='{url}'>Continue setup</a> on the bridge.</li>" \
                       "<li>Sign in with the <strong>same administrator username and password</strong> you created here.</li>" \
                       "<li>Follow the five numbered steps: check rooms, approve each room in Plex, check library access, " \
                       "find rooms in Plexamp, and test audio.</li></ol><p>Your Plex account login is separate from your bridge administrator login. " \
                       "No music starts until you choose to play it.</p>"
        self.page(200, body, cookie)

    def do_POST(self):
        if not self.valid_host() or self.headers.get("Origin") != self.server.origin:
            self.page(403, "<p>Open setup using its configured address.</p>")
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
            self.page(415, "<p>Unsupported form type.</p>")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 65536:
                raise ValueError
            data = parse_qs(self.rfile.read(size).decode("utf-8"), max_num_fields=40)
        except (ValueError, UnicodeError):
            self.page(400, "<p>Invalid form.</p>")
            return
        value = lambda name: data.get(name, [""])[0]
        service = self.server.provisioner
        token = self.token()
        cookie = None
        try:
            if self.path == "/claim":
                token = service.claim(value("code"))
                cookie = token
            elif self.path == "/administrator":
                service.administrator(token, value("username"), value("password"))
            elif self.path in {"/players", "/discover"}:
                view = service.view(token)
                if view["stage"] not in {"players", "review"}:
                    raise ConfigError("Complete the administrator step first")
                results = view.get("discovery", {}).get("results", [])
                rooms = value("rooms")
                known = {line.rsplit(",", 1)[-1].strip() for line in rooms.splitlines() if "," in line}
                for selected in data.get("selected", []):
                    if not selected.isdigit() or int(selected) >= len(results) or results[int(selected)].get("kind") != "wiim":
                        raise ConfigError("Device selection expired; search again")
                    item = results[int(selected)]
                    if item["host"] not in known:
                        rooms += "\n" + item["name"] + ", " + item["host"]
                        known.add(item["host"])
                plex_origin = value("plex_server_origin")
                if value("server"):
                    selected = value("server")
                    if not selected.isdigit() or int(selected) >= len(results) or results[int(selected)].get("kind") != "plex":
                        raise ConfigError("Server selection expired; search again")
                    plex_origin = results[int(selected)]["origin"]
                if self.path == "/discover":
                    if not self.server.scan_lock.acquire(blocking=False):
                        raise ConfigError("Search already running; reload to see progress")
                    try:
                        service.discovery_start(token, {"rooms": rooms, "advertised_origin": value("advertised_origin"),
                                                        "plex_server_origin": plex_origin})
                    except Exception:
                        self.server.scan_lock.release()
                        raise
                    threading.Thread(target=self.server.scan, daemon=True).start()
                    self.redirect_home()
                    return
                expected = os.environ.get("EXPECTED_BRIDGE_ORIGIN")
                if expected and _origin(value("advertised_origin"), "Bridge URL") != _origin(expected, "Deployment URL"):
                    raise ConfigError("Bridge URL must match this deployment's configured LAN address and port")
                service.configure(token, value("advertised_origin"), plex_origin, rooms)
            elif self.path == "/finish":
                service.finish(token)
                publish_configured(service)
            else:
                self.page(404, "<p>Page not found.</p>")
                return
            self.redirect_home(cookie)
        except ConfigError as exc:
            self.page(400, f"<p role='alert'>{html.escape(str(exc))}</p><a href='/'>Return to saved setup</a>")
        except OSError:
            self.page(503, "<p role='alert'>Could not save setup. Check storage availability, then retry the saved step.</p><a href='/'>Return to setup</a>")

    def redirect_home(self, cookie=None):
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        if cookie:
            self.send_header("Set-Cookie", f"bridge_setup={cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    configure("setup")
    root = os.environ.get("SETUP_PATH", "/config")
    host = os.environ.get("SETUP_HOST", "127.0.0.1")
    port = int(os.environ.get("SETUP_PORT", "8795"))
    origin = os.environ.get("SETUP_ORIGIN", f"http://{host}:{port}")
    provisioner = Provisioner(root)
    publish_configured(provisioner)  # Recover a restart between completion and export.
    server = SetupServer((host, port), provisioner, origin)
    logging.getLogger("plex_bridge").info("", extra={"event": "setup_ready"})
    server.serve_forever()


if __name__ == "__main__":
    main()
