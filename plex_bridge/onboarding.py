"""Authenticated runtime onboarding; no player mutation occurs in checks."""

import copy
import html
import json
import logging
from pathlib import Path
import threading
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from plex_bridge.provisioning import atomic_write, read_json
from plex_bridge.config import ConfigError


PROGRESS_SCRIPT = """<script>
(() => {
  let timer, pending = false;
  const guide = () => document.querySelector('.setup-guide');
  function schedule() {
    clearTimeout(timer);
    if (guide()?.dataset.busy === 'true') timer = setTimeout(refresh, 1000);
  }
  function showProgress(message) {
    const root = guide();
    root.dataset.busy = 'true';
    root.setAttribute('aria-busy', 'true');
    const progress = root.querySelector('.setup-progress');
    progress.hidden = false;
    progress.querySelector('strong').textContent = message;
    root.querySelectorAll('button').forEach(button => button.disabled = true);
  }
  async function render(response) {
    if (!response.ok) throw new Error('request failed');
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const replacement = page.querySelector('.setup-guide');
    if (!replacement) {
      if (new URL(response.url).pathname === '/login') {
        location.assign('/login?next=onboarding');
        return;
      }
      throw new Error('unexpected page');
    }
    const active = document.activeElement;
    const focusedForm = active?.closest('form');
    const action = focusedForm?.getAttribute('action');
    const endpoint = focusedForm?.elements.endpoint?.value;
    guide().replaceWith(replacement);
    if (action) {
      const form = [...replacement.querySelectorAll('form')].find(form =>
        form.getAttribute('action') === action && form.elements.endpoint?.value === endpoint);
      const button = form?.querySelector('button:not(:disabled)');
      if (button) button.focus({preventScroll: true});
    }
  }
  async function refresh() {
    if (pending) return;
    pending = true;
    try {
      await render(await fetch('/onboarding?step=' + encodeURIComponent(guide().dataset.step), {credentials: 'same-origin', cache: 'no-store'}));
    } catch (_) {
      showProgress('Connection interrupted. Retrying status…');
    } finally { pending = false; schedule(); }
  }
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.closest('.setup-guide') || !form.getAttribute('action')?.startsWith('/onboarding/')) return;
    // Plex authorization navigates away; do not follow its cross-origin redirect with fetch.
    if (form.getAttribute('action') === '/onboarding/open') return;
    event.preventDefault();
    if (pending) return;
    clearTimeout(timer);
    const body = new URLSearchParams(new FormData(form));
    const message = form.dataset.progressMessage || 'Working…';
    showProgress(message);
    if (event.submitter) event.submitter.textContent = message;
    pending = true;
    try {
      await render(await fetch(form.action, {method: 'POST', body, credentials: 'same-origin', cache: 'no-store'}));
    } catch (_) {
      showProgress('Could not confirm the result. Checking saved status…');
    } finally { pending = false; schedule(); }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !pending) refresh();
  });
  schedule();
})();
</script>"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def check_server(origin, token):
    # Credentials are never forwarded through a redirect or environment proxy.
    request = urllib.request.Request(origin + "/library/sections", headers={"X-Plex-Token": token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=5) as response:
        root = ET.fromstring(response.read(1024 * 1024))
        if root.tag != "MediaContainer":
            raise ValueError("Unexpected Plex response")


class Onboarding:
    def __init__(self, path, bridge, cloud, groups, server_check=check_server):
        self.path = Path(path)
        self.bridge, self.cloud, self.groups = bridge, cloud, groups
        self.server_check = server_check
        self.lock = threading.RLock()
        self.worker = None
        if self.path.exists():
            self.data = read_json(self.path)
            if not isinstance(self.data, dict) or self.data.get("schema_version") != 1:
                raise ConfigError("Unsupported onboarding state")
        else:
            self.data = {"schema_version": 1, "rooms": {}, "server": "not_checked"}
        for endpoint in bridge.endpoint_devices:
            self.data["rooms"].setdefault(endpoint, {})
        if self.data.get("operation", {}).get("status") == "running":
            self.data["operation"].update(status="interrupted", message="Service restarted. Retry the interrupted step.")
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(self.path, self.data)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def open_authorization(self, endpoint):
        """Record opening this exact pending link before navigating to Plex."""
        if endpoint not in self.bridge.endpoint_devices:
            raise ConfigError("Unknown room")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ConfigError("Wait for the current check to finish, then open Plex authorization.")
            pending = self.cloud.pending.get(endpoint, {})
            if not pending or time.time() - float(pending.get("created", 0)) > 1800:
                raise ConfigError("The Plex link expired. Get a new authorization link for this room.")
            url = self.cloud.link_url(endpoint)
            self.data["rooms"][endpoint]["authorization_opened"] = pending["created"]
            message = "After approving in Plex, check Plex approval below to finish linking."
            self.data["rooms"][endpoint]["last_result"] = {"status": "success", "message": message}
            self.data["operation"] = {"action": "open", "endpoint": endpoint,
                                      "status": "success", "message": message, "finished": time.time()}
            self.save()
            return url

    def submit(self, action, endpoint=""):
        if action not in {"connections", "link", "finish", "seen", "arm", "verify", "heard", "server"}:
            raise ConfigError("Unknown setup action")
        if action not in {"connections", "server"} and endpoint not in self.bridge.endpoint_devices:
            raise ConfigError("Unknown room")
        with self.lock:
            if self.worker and self.worker.is_alive():
                return False
            self.data["operation"] = {"action": action, "endpoint": endpoint,
                                      "status": "running", "message": "Checking…", "started": time.time()}
            self.save()
            self.worker = threading.Thread(target=self._run, args=(action, endpoint), daemon=True)
            self.worker.start()
        return True

    def _run(self, action, endpoint):
        # No raw upstream exceptions or response bodies enter saved state or logs.
        status, message = "success", "Saved."
        try:
            if action == "connections":
                rows = self.groups.players()
                results = {row["device"]["id"]: row.get("online") is True for row in rows}
                with self.lock:
                    for identifier, device in self.bridge.endpoint_devices.items():
                        self.data["rooms"][identifier]["connection"] = "reachable" if results.get(device.id) else "unreachable"
                        self.data["rooms"][identifier]["checked_at"] = time.time()
                message = "Room checks finished. These read status only; no audio or grouping was changed."
            elif action == "server":
                token = next((self.cloud.tokens[e] for e in self.bridge.endpoint_devices if self.cloud.linked(e)), "")
                if not token:
                    raise ConfigError("Link at least one room before checking Plex server access.")
                self.server_check(self.bridge.allowed_server_base, token)
                with self.lock:
                    self.data["server"] = "accessible"
                    self.data["server_checked_at"] = time.time()
                message = "Plex server accepted the linked account's library request."
            elif action == "link":
                self.cloud.begin_link(endpoint)
                with self.lock:
                    self.data["rooms"][endpoint]["registration"] = "awaiting_approval"
                message = "Open Plex authorization for this room, approve it, then check approval here."
            elif action == "finish":
                if self.cloud.linked(endpoint) and not self.cloud.pending.get(endpoint):
                    self.cloud.publish(endpoint)
                else:
                    self.cloud.finish_link(endpoint)
                with self.lock:
                    self.data["rooms"][endpoint]["registration"] = "published"
                message = "Plex registration accepted. Now confirm this room appears in Plexamp."
            elif action == "seen":
                if not self.cloud.linked(endpoint):
                    raise ConfigError("Link this room before confirming it in Plexamp.")
                with self.lock:
                    self.data["rooms"][endpoint]["seen"] = True
                message = "Saved your confirmation that this endpoint appears in Plexamp."
            elif action == "arm":
                if not self.cloud.linked(endpoint):
                    raise ConfigError("Link this room before starting its playback check.")
                with self.lock:
                    self.data["rooms"][endpoint]["test"] = {"status": "waiting", "started": time.time()}
                message = "In Plexamp, select this room and play a track you choose, then check playback below."
            elif action == "verify":
                message = self.verify_playback(endpoint)
            elif action == "heard":
                with self.lock:
                    test = self.data["rooms"][endpoint].get("test", {})
                    if test.get("status") != "advancing" or time.time() - test.get("verified_at", 0) > 600:
                        raise ConfigError("Verify stream progress before confirming audible playback.")
                    test["heard"] = True
                message = "Playback check complete: stream progress verified and you confirmed audible audio."
        except ConfigError as exc:
            status, message = "waiting", str(exc)
        except Exception as exc:
            status = "failed"
            if action == "finish":
                if getattr(exc, "approval_pending", False):
                    status, message = "waiting", "Plex approval is still pending. Open the authorization link, approve, then check again."
                elif getattr(exc, "approval_expired", False):
                    status, message = "waiting", "The Plex link expired. Get a new authorization link for this room."
                elif self.cloud.linked(endpoint):
                    message = "Account linked, but registration was not confirmed. Retry registration; no new approval is needed."
                else:
                    message = "Approval is pending, expired, or Plex could not be reached. Approve the current link and retry; start a new link if it expired."
            elif action == "server":
                message = "Plex server access failed. Check its address, network connection and account access, then retry."
                with self.lock:
                    self.data["server"] = "failed"
            elif action == "connections":
                message = "The grouping service could not return room status. Check that it is running, then retry."
                with self.lock:
                    for room in self.data["rooms"].values():
                        room["connection"] = "unknown"
            else:
                message = "This check could not complete. Check the connection and retry."
        with self.lock:
            self.data["operation"].update(status=status, message=message, finished=time.time())
            if endpoint:
                self.data["rooms"][endpoint]["last_result"] = {"status": status, "message": message}
            try:
                self.save()
            except OSError:
                self.data["operation"].update(status="failed", message="Could not save progress. Check state storage before retrying.")
            logging.getLogger("plex_bridge").info("", extra={"event": "operation", "action": action,
                                                             "outcome": self.data["operation"]["status"]})

    def verify_playback(self, endpoint):
        with self.lock:
            test = self.data["rooms"][endpoint].get("test", {})
            if test:
                test.update(status="waiting", heard=False)
            if not test or time.time() - test.get("started", 0) > 600:
                raise ConfigError("Start a new playback check; each check lasts ten minutes.")
        device = self.bridge.endpoint_devices[endpoint]
        session = self.bridge.store.snapshot()
        if session is None or session.target_device_id != device.id or session.state != "playing":
            raise ConfigError("Waiting for bridge-owned Plex playback on this room. Select it in Plexamp and start music.")
        expected = self.bridge.media_url(session)
        first = self.bridge.renderer.snapshot(device)
        time.sleep(2)
        second = self.bridge.renderer.snapshot(device)
        after = self.bridge.store.snapshot()
        if (after is None or after.renderer_session_id != session.renderer_session_id
                or after.current.play_queue_item_id != session.current.play_queue_item_id):
            raise ConfigError("The track or room changed during the check. Retry while one track is playing.")
        if (first.source_url != expected or second.source_url != expected
                or first.state != "playing" or second.state != "playing"
                or not 250 <= second.position_ms - first.position_ms <= 7000):
            raise ConfigError("The expected stream did not show steady playback progress. Check playback and retry.")
        with self.lock:
            self.data["rooms"][endpoint]["test"] = dict(test, status="advancing", verified_at=time.time(), heard=False)
        return "The WiiM is playing the expected Plex stream and its position advanced. Confirm whether you hear audio."

    def completion_steps(self, state=None):
        state = self.snapshot() if state is None else state
        rooms = state["rooms"]
        devices = self.bridge.endpoint_devices
        connected = bool(devices) and all(rooms.get(e, {}).get("connection") == "reachable" for e in devices)
        registered = bool(devices) and all(self.cloud.linked(e) and rooms.get(e, {}).get("registration") == "published" for e in devices)
        seen = bool(devices) and all(rooms.get(e, {}).get("seen") for e in devices)
        heard = bool(devices) and all(rooms.get(e, {}).get("test", {}).get("heard") for e in devices)
        return [("connections", "Check room connections", connected),
                ("accounts", "Link each room to Plex", registered),
                ("server", "Check Plex library access", state.get("server") == "accessible"),
                ("players", "Find your rooms in Plexamp", seen),
                ("playback", "Test audio one room at a time", heard)]

    def is_complete(self):
        return all(done for _, _, done in self.completion_steps())

    def page(self, step=""):
        state = self.snapshot()
        escape = html.escape
        busy = state.get("operation", {}).get("status") == "running"
        op = state.get("operation", {})
        devices = self.bridge.endpoint_devices
        linked = {endpoint: self.cloud.linked(endpoint) for endpoint in devices}

        def progress_message(action, endpoint=""):
            message = {"connections": "Checking room connections", "server": "Checking Plex library access",
                       "link": "Preparing Plex authorization", "open": "Opening Plex authorization", "finish": "Checking Plex approval",
                       "verify": "Checking stream progress", "arm": "Preparing playback check",
                       "seen": "Saving Plexamp visibility confirmation", "heard": "Saving audible playback confirmation"}.get(action, "Working")
            device = devices.get(endpoint)
            return message + (" for " + device.name if device else "") + "…"

        def button(action, label, endpoint="", enabled=True):
            message = progress_message(action, endpoint)
            if busy and op.get("action") == action and op.get("endpoint", "") == endpoint:
                label = message
            attributes = "disabled" if busy or not enabled else ""
            if action == "open":
                attributes += " class='primary-action'"
            return (f"<form method='post' action='/onboarding/{action}' data-progress-message='{escape(message, quote=True)}'>"
                    f"<input type='hidden' name='endpoint' value='{escape(endpoint, quote=True)}'>"
                    f"<button {attributes}>{escape(label)}</button></form>")

        def room_card(endpoint, device, anchor=False):
            identifier = f" id='link-{escape(endpoint, quote=True)}'" if anchor else ""
            return f"<article class='setup-room'{identifier}><h3>{escape(device.name)}</h3>"

        rooms = state["rooms"]
        server_ok = state.get("server") == "accessible"
        stages = self.completion_steps(state)
        step_ids = [item[0] for item in stages]
        if step not in step_ids:
            step = next((item[0] for item in stages if not item[2]), "playback")
        position = step_ids.index(step)
        body = f"<div class='setup-guide' data-step='{step}' data-busy='{str(busy).lower()}' aria-busy='{str(busy).lower()}'><style>.setup-guide{{max-width:52rem;margin:auto;line-height:1.6}}" \
               ".setup-guide section{margin:1rem 0;padding:1.2rem}.setup-guide h2{margin-top:0}" \
               ".step-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:.5rem;margin:1rem 0}" \
               ".step-card{padding:.7rem;border:1px solid #566274;border-radius:.6rem;text-decoration:none;color:inherit}" \
               ".step-card strong,.step-card small{display:block}.step-card[aria-current=step]{border:2px solid #b4d8ff;background:#29364a}" \
               ".step-card.done small{color:#8be0b0}.step-footer{display:flex;justify-content:space-between;gap:1rem;margin:1.2rem 0}" \
               ".step-footer a{padding:.6rem 1rem;border:1px solid #65758b;border-radius:.4rem;text-decoration:none}" \
               "@media(max-width:600px){.step-cards{grid-template-columns:repeat(2,minmax(0,1fr))}}" \
               ".setup-guide .setup-room{border-top:1px solid #566274;margin-top:1.2rem;padding-top:.5rem}" \
               ".setup-guide li{margin:.5rem 0}.setup-guide button:disabled{opacity:.45;cursor:default}" \
               ".setup-guide .setup-status{font-weight:600;color:#b4d8ff}" \
               ".setup-guide .primary-action{display:inline-block;padding:.7rem 1rem;margin:.3rem;border:1px solid #b4d8ff;border-radius:.4rem;background:#b4d8ff;color:#122438;font-weight:650;text-decoration:none}" \
               ".setup-progress{position:sticky;top:1rem;z-index:2;padding:1rem;background:#29364a;border:1px solid #b4d8ff;border-radius:.6rem}" \
               ".setup-track{height:.4rem;overflow:hidden;background:#141820;border-radius:1rem;margin-top:.7rem}" \
               ".setup-track span{display:block;width:35%;height:100%;background:#b4d8ff;animation:setup-check 1.4s ease-in-out infinite alternate}" \
               "@keyframes setup-check{from{transform:translateX(0)}to{transform:translateX(185%)}}" \
               "@media(prefers-reduced-motion:reduce){.setup-track span{animation:none}}</style>"
        message = progress_message(op.get("action"), op.get("endpoint", ""))
        body += f"<div class='setup-progress' {'hidden' if not busy else ''} role='status' aria-live='polite'><strong>{escape(message)}</strong>" \
                "<small style='display:block'>Please wait. This page updates automatically when the check finishes.</small>" \
                "<div class='setup-track' aria-hidden='true'><span></span></div></div>"
        body += "<h1>Finish setup</h1><p>Connect your rooms to Plex, then verify playback. Your progress is saved.</p>"
        labels = ["Connections", "Plex approval", "Library access", "Plexamp", "Playback"]
        body += "<nav class='step-cards' aria-label='Setup progress'>"
        for number, (anchor, title, done) in enumerate(stages):
            current = " aria-current='step'" if anchor == step else ""
            status = "Complete" if done else "Current step" if anchor == step else "To do"
            body += f"<a class='step-card{' done' if done else ''}' href='/onboarding?step={anchor}'{current}><strong>{number + 1}. {labels[number]}</strong><small>{status}</small></a>"
        body += f"</nav><p>Step {position + 1} of 5 · {sum(bool(item[2]) for item in stages)} of 5 complete</p>"
        if all(item[2] for item in stages):
            body += "<p class='notice'>All setup checks are complete. <a href='/dashboard'>Open the playback dashboard</a>.</p>"
        operation_step = {"connections": "connections", "link": "accounts", "open": "accounts", "finish": "accounts",
                          "server": "server", "seen": "players", "arm": "playback",
                          "verify": "playback", "heard": "playback"}.get(op.get("action"))
        if op and (busy or operation_step == step):
            if not busy:
                device = devices.get(op.get("endpoint"))
                prefix = device.name + ": " if device else ""
                body += f"<p class='notice' role='status'>{escape(prefix + op.get('message', ''))}</p>"
            if busy:
                body += "<noscript><p>Enable JavaScript for automatic progress updates, or " \
                        "<a href='/onboarding'>refresh check status</a>.</p></noscript>"

        intro, sections, body = body, {}, ""
        body += "<section id='connections'><h2>1. Check room connections</h2>" \
                "<p>Turn on your WiiMs and connect them to the same LAN as the bridge. " \
                "Run this check to confirm the bridge can read each room's status.</p>"
        body += button("connections", "Check room connections") + "<ul>"
        for endpoint, device in devices.items():
            status = {"reachable": "Reachable", "unreachable": "Not reachable", "unknown": "Check unavailable"}.get(rooms.get(endpoint, {}).get("connection"), "Not checked yet")
            body += f"<li><strong>{escape(device.name)}</strong>: {status}</li>"
        body += "</ul><p>If a room is not reachable, check its power, Wi-Fi/Ethernet connection and saved IP address, then retry. " \
                "These results reflect your last check.</p></section>"

        sections["connections"], body = body, ""
        body += "<section id='accounts'><h2>2. Link each room to Plex</h2>" \
                "<p>Each room needs its own Plex approval so it can appear as a player. " \
                "Use the Plex account that has access to your music library. Repeat this sequence for every room below:</p>" \
                "<ol><li>Click <strong>Get Plex authorization</strong> to prepare a link.</li>" \
                "<li>Click <strong>Open Plex authorization</strong>, sign in to Plex and approve access.</li>" \
                "<li>Plex returns you to this room on this page. Click <strong>Check Plex approval</strong> and wait for confirmation.</li></ol>"
        for endpoint, device in devices.items():
            room = rooms.get(endpoint, {})
            pending = self.cloud.pending.get(endpoint, {})
            link_ready = bool(pending) and time.time() - float(pending.get("created", 0)) <= 1800
            opened = link_ready and room.get("authorization_opened") == pending.get("created")
            published = linked[endpoint] and room.get("registration") == "published" and not pending
            body += room_card(endpoint, device, anchor=True)
            body += f"<p class='setup-status'>{'Linked to Plex' if published else 'Plex authorization opened — ready to check approval' if opened else 'Authorization link ready — approval still required' if link_ready else 'Account linked; registration needs confirmation' if linked[endpoint] else 'Authorization link expired' if pending else 'Not linked yet'}</p>"
            if not published:
                if link_ready:
                    if opened:
                        body += "<p>Approved this room in Plex? Check the result below to finish linking.</p>"
                        body += button("finish", "Check Plex approval", endpoint)
                        body += "<details><summary>Need to reopen authorization?</summary><p>If you closed Plex before approving, reopen it here.</p>"
                        body += button("open", "Reopen Plex authorization →", endpoint) + "</details>"
                    else:
                        body += "<p><strong>Next: open Plex and approve this room.</strong> Preparing the link has not linked your account yet.</p>"
                        body += button("open", "Open Plex authorization →", endpoint)
                        body += "<p>After approving, Plex will return you here to check the approval.</p>"
                elif linked[endpoint]:
                    body += button("finish", "Retry registration", endpoint)
                else:
                    body += button("link", "Get a new Plex authorization link" if pending else "Get Plex authorization", endpoint)
                    body += "<p>This prepares the link. Your next action will be <strong>Open Plex authorization</strong>.</p>"
            if room.get("last_result") and (not opened or op.get("action") != "link"):
                body += f"<p>{escape(room['last_result']['message'])}</p>"
            body += "</article>"
        body += "<p>If approval is pending, complete it in the Plex tab and check again. If the link expires, get a new authorization link. " \
                "An already linked account can retry registration without signing in again.</p></section>"

        sections["accounts"], body = body, ""
        body += "<section id='server'><h2>3. Check Plex library access</h2>" \
                "<p>After linking at least one room, check that its Plex account can access the server you selected.</p>"
        body += f"<p>Saved Plex server: <strong>{escape(self.bridge.allowed_server_base)}</strong></p>"
        body += button("server", "Check Plex server access", enabled=any(linked.values()))
        body += f"<p class='setup-status'>{'Library access confirmed' if server_ok else 'Check failed — verify the server address and Plex account access, then retry' if state.get('server') == 'failed' else 'Not checked yet'}</p>"
        if not any(linked.values()):
            body += "<p>Complete a room's Plex approval in step 2 to enable this check.</p>"
        body += "</section>"

        sections["server"], body = body, ""
        body += "<section id='players'><h2>4. Find your rooms in Plexamp</h2>" \
                "<p>Open Plexamp using the same Plex account you approved above. Open its player/cast picker " \
                "and look for each exact room name below. Confirm a room only when you actually see it.</p>"
        for endpoint, device in devices.items():
            room = rooms.get(endpoint, {})
            body += room_card(endpoint, device)
            if room.get("seen"):
                body += "<p class='setup-status'>You confirmed this room in Plexamp.</p>"
            else:
                body += button("seen", "I see this room in Plexamp", endpoint, enabled=linked[endpoint])
                if not linked[endpoint]:
                    body += "<p>Link this room in step 2 first.</p>"
            body += "</article>"
        body += "<p>Missing a room? Close and reopen the player picker, allow time for its list to refresh, " \
                "and confirm you are using the correct Plex account. Successful account linking alone does not confirm player visibility.</p></section>"

        sections["players"], body = body, ""
        body += "<section id='playback'><h2>5. Test audio one room at a time</h2>" \
                "<p>One Plex queue plays on one room or native group at a time. Finish one room's test before moving to the next. " \
                "Playing a track can replace what is currently playing on that room or its native group.</p>" \
                "<ol><li>Click <strong>Start a playback check</strong> below. This opens a ten-minute check window; it does not start music.</li>" \
                "<li>In Plexamp, select that room and play a track from your library. Check your amplifier and volume if needed.</li>" \
                "<li>While the track plays, return here and click <strong>Check stream progress</strong>.</li>" \
                "<li>When progress is verified and you hear music, click <strong>I hear the audio</strong>.</li></ol>"
        for endpoint, device in devices.items():
            room = rooms.get(endpoint, {})
            test = room.get("test", {})
            body += room_card(endpoint, device)
            if test.get("heard"):
                body += "<p class='setup-status'>Complete — stream progress verified and you confirmed audible playback.</p>"
            body += button("arm", "Start a new playback check" if test else "Start a playback check", endpoint,
                           enabled=linked[endpoint] and server_ok and bool(room.get("seen")))
            if not (linked[endpoint] and server_ok and room.get("seen")):
                body += "<p>First link this room, confirm library access, and find it in Plexamp (steps 2–4).</p>"
            if test and not test.get("heard"):
                body += button("verify", "Check stream progress", endpoint)
                if test.get("status") == "advancing":
                    body += "<p class='setup-status'>Stream progress was verified. Can you hear the track in this room?</p>"
                    body += button("heard", "I hear the audio", endpoint)
                else:
                    body += "<p>Waiting for your track. If the ten-minute window expires, start a new playback check.</p>"
            body += "</article>"
        body += "<p>Stop music in Plexamp when you finish testing. Leaving this page does not stop playback.</p></section>"
        sections["playback"] = body
        footer = "<nav class='step-footer' aria-label='Step navigation'>"
        if position:
            footer += f"<a href='/onboarding?step={step_ids[position - 1]}'>← Back</a>"
        else:
            footer += "<span></span>"
        if position < len(stages) - 1:
            footer += f"<a href='/onboarding?step={step_ids[position + 1]}'>Next: {labels[position + 1]} →</a>"
        else:
            footer += "<a href='/dashboard'>Open dashboard →</a>"
        footer += "</nav><p><small>Move between steps using the cards above. Checks update automatically.</small></p></div>"
        return intro + sections[step] + footer + PROGRESS_SCRIPT
