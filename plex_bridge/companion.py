"""Authenticated PMS Companion relay for controllers using the server /clients list.

Each room keeps its existing identity. Commands are dispatched to the same bridge
methods as direct Companion HTTP requests; no caller-supplied URL is fetched here.
"""
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict

LOGGER = logging.getLogger("wiim-focus-control")
PARAMS = {"deviceClass": "speaker", "protocolVersion": "3",
          "protocolCapabilities": "timeline,playback,navigation,playqueues"}


class CompanionRelay:
    def __init__(self, plex, cloud, server_base):
        self.plex, self.cloud = plex, cloud
        self.server_base = server_base.rstrip("/")
        self.stop = threading.Event()
        self.workers = {}

    def request(self, endpoint, path, params=None, body=None, timeout=25):
        query = dict(PARAMS)
        query.update(params or {})
        headers = self.cloud.headers(include_token=True, client_identifier=endpoint)
        headers["Accept"] = "application/xml"
        headers["X-Plex-Platform-Version"] = "1"
        headers["X-Plex-Model"] = "standalone"
        headers["Content-Type"] = "application/xml; charset=utf-8"
        req = urllib.request.Request(self.server_base + path + "?" +
                                     urllib.parse.urlencode(query), data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Companion response too large")
        return data

    def dispatch(self, endpoint, command):
        path = "/" + command.get("path", "").lstrip("/")
        params = {}
        for key, value in command.attrib.items():
            if key.startswith("query") and len(key) > 5:
                params[key[5].lower() + key[6:]] = [value]
        command_id = command.get("commandID", "")
        controller = command.get("clientIdentifier", "")
        params["commandID"] = [command_id]
        # The receiving worker fixes the room; query/header fields cannot change it.
        self.plex.target_device(endpoint)
        if path in {"/player/timeline/subscribe", "/player/timeline/unsubscribe",
                    "/player/timeline/poll"}:
            return 200
        self.plex.note_command(endpoint, controller, command_id)
        if path == "/player/playback/playMedia":
            self.plex.play_media(params, endpoint)
        elif path.startswith("/player/playback/") and path.count("/") == 3:
            self.plex.playback_command(path.rsplit("/", 1)[1], params,
                                       endpoint_identifier=endpoint)
        else:
            return 404
        return 200

    def handle(self, endpoint, command, completed):
        command_id = command.get("commandID", "")
        if not command_id.isdigit():
            raise ValueError("Companion command ID missing")
        key = (command.get("clientIdentifier", ""), command_id)
        # Controllers may restart their command counter on reconnect. Keep only
        # the short retry window, rather than suppressing reused IDs indefinitely.
        now = time.monotonic()
        for previous in list(completed):
            if now - completed[previous][1] > 60:
                del completed[previous]
        if key not in completed:
            try:
                status = self.dispatch(endpoint, command)
            except Exception:
                LOGGER.warning("", extra={"event": "command_rejected"})
                status = 400
            completed[key] = (status, now)
            while len(completed) > 256:
                completed.popitem(last=False)
        status = completed[key][0]
        body = ET.tostring(ET.Element("Response", {"code": str(status),
                           "status": "OK" if status == 200 else "Rejected"}))
        self.request(endpoint, "/player/proxy/response", {"commandID": command_id}, body, 10)
        self.publish_timeline(endpoint, command_id)

    def publish_timeline(self, endpoint, command_id="0"):
        body = self.plex.timeline_xml(command_id, endpoint_identifier=endpoint)
        root = ET.fromstring(body)
        state = next((e.get("state") for e in root if e.get("type") == "music"), "stopped")
        self.request(endpoint, "/player/proxy/timeline",
                     {"commandID": command_id, "state": state}, body, 10)

    def poll_room(self, endpoint):
        completed = OrderedDict()
        retry = 1
        while not self.stop.is_set() and self.cloud.linked(endpoint):
            try:
                data = self.request(endpoint, "/player/proxy/poll", {"timeout": "1"})
                if data.strip():
                    root = ET.fromstring(data)
                    for command in root:
                        if command.tag != "Command":
                            raise ValueError("Unexpected Companion element")
                        self.handle(endpoint, command, completed)
                retry = 1
            except TimeoutError:
                # An idle long poll may exceed the HTTP read timeout.
                continue
            except Exception:
                LOGGER.warning("", extra={"event": "registration_failed"})
                self.stop.wait(retry)
                retry = min(retry * 2, 60)

    def monitor_forever(self):
        while not self.stop.is_set():
            for endpoint in self.plex.endpoint_devices:
                if not self.cloud.linked(endpoint):
                    continue
                worker = self.workers.get(endpoint)
                if worker is None or not worker.is_alive():
                    worker = threading.Thread(target=self.poll_room, args=(endpoint,), daemon=True)
                    self.workers[endpoint] = worker
                    worker.start()
                try:
                    self.publish_timeline(endpoint)
                except Exception:
                    LOGGER.warning("", extra={"event": "timeline_failed"})
            self.stop.wait(5)
