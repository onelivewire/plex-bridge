#!/usr/bin/env python3
"""Preset-driven, source-aware WiiM focus orchestration."""

from __future__ import annotations
from plex_bridge.config import load_config, load_or_create_identity, ConfigError
from plex_bridge.registration import RegistrationMaintainer
from plex_bridge.companion import CompanionRelay
from plex_bridge.symfonium_proxy import SymfoniumProxy
from plex_bridge.provisioning import credential_settings
from plex_bridge.onboarding import Onboarding
from plex_bridge.dashboard import Dashboard
from plex_bridge.diagnostics import Diagnostics, SafeServerErrors, configure, request_event, failure_reason, timed, traced, session_traced, media_traced

import base64
import contextvars
from plex_bridge.support import SupportChecks
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


LOGGER = logging.getLogger("wiim-focus-control")
SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state"


class FocusError(RuntimeError):
    pass


class PlexApprovalPending(FocusError):
    approval_pending = True


class PlexApprovalExpired(FocusError):
    approval_expired = True


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    host: str


@dataclass(frozen=True)
class EndpointAdvertisement:
    title: str
    visible: bool = True
    provides: str = "client,player,pubsub-player"


def load_devices(path: str) -> list[Device]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    devices = [Device(str(x["id"]), str(x["name"]), str(x["host"])) for x in raw]
    if not devices or len({x.id for x in devices}) != len(devices):
        raise ValueError("device inventory must contain unique ids")
    if len({x.host for x in devices}) != len(devices):
        raise ValueError("device inventory must contain unique hosts")
    return devices


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class EventHistory:
    def __init__(self, path: Path, limit: int = 200):
        self.path = path
        self.limit = limit
        self.lock = threading.Lock()
        self.events = []
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                self.events = [x for x in payload[-limit:] if isinstance(x, dict)]
        except (OSError, json.JSONDecodeError):
            pass

    def record(self, action: str, requester: str, outcome: str, detail: str):
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "requester": requester,
            "outcome": outcome,
            "detail": detail,
        }
        with self.lock:
            self.events.append(event)
            self.events = self.events[-self.limit :]
            try:
                atomic_json(self.path, self.events)
            except OSError:
                LOGGER.exception("could not persist event history")
        LOGGER.info("", extra={"event": "operation", "action": action, "outcome": outcome})
        return event

    def snapshot(self):
        with self.lock:
            return [dict(x) for x in reversed(self.events)]


class JsonHttpClient:
    def request(self, url: str, method="GET", headers=None, payload=None, timeout=8):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024)
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode(errors="replace")
            raise FocusError(f"HTTP {exc.code}: {detail[:300]}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise FocusError(str(exc)) from exc


class XmlHttpClient:
    def request(self, url: str, headers=None, timeout=8):
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/xml")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(8 * 1024 * 1024)
                return ET.fromstring(body) if body else ET.Element("MediaContainer")
        except urllib.error.HTTPError as exc:
            raise FocusError(f"Plex server returned HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, ET.ParseError) as exc:
            raise FocusError(f"Plex server request failed: {exc}") from exc


@dataclass(frozen=True)
class PlexQueueItem:
    play_queue_item_id: str
    rating_key: str
    metadata_key: str
    part_key: str
    duration_ms: int
    title: str = ""
    artist: str = ""
    album: str = ""
    art_key: str = ""


@dataclass(frozen=True)
class PlexSession:
    server_identifier: str
    server_base: str
    play_queue_id: str
    play_queue_version: str
    items: tuple[PlexQueueItem, ...]
    current_index: int
    offset_ms: int
    state: str
    shuffle: bool
    repeat: int
    target_device_id: str
    renderer_session_id: str
    volume: int
    renderer_start_index: int
    playback_session_id: str
    renderer_queue_version: str = ""

    def __post_init__(self):
        # Freeze the signing version for the lifetime of the loaded WiiM queue.
        if not self.renderer_queue_version:
            object.__setattr__(self, "renderer_queue_version", self.play_queue_version)

    @property
    def current(self):
        return self.items[self.current_index]

    def public_dict(self):
        return {
            "server_identifier": self.server_identifier,
            "server_base": self.server_base,
            "play_queue_id": self.play_queue_id,
            "play_queue_version": self.play_queue_version,
            "items": [item.__dict__ for item in self.items],
            "current_index": self.current_index,
            "offset_ms": self.offset_ms,
            "state": self.state,
            "shuffle": self.shuffle,
            "repeat": self.repeat,
            "target_device_id": self.target_device_id,
            "renderer_session_id": self.renderer_session_id,
            "volume": self.volume,
            "renderer_start_index": self.renderer_start_index,
            "playback_session_id": self.playback_session_id,
            "renderer_queue_version": self.renderer_queue_version,
        }


class PlexSessionStore:
    """Persist public queue state separately from the Plex access token."""

    def __init__(self, state_path: Path, secret_path: Path):
        self.state_path = state_path
        self.secret_path = secret_path
        self.lock = threading.Lock()
        self.session = None
        self.token = ""
        try:
            with open(state_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            items = tuple(PlexQueueItem(**item) for item in payload["items"])
            values = {key: value for key, value in payload.items() if key != "items"}
            values.setdefault("renderer_start_index", int(values.get("current_index", 0)))
            values.setdefault(
                "playback_session_id", str(values.get("renderer_session_id", ""))
            )
            self.session = PlexSession(items=items, **values)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            with open(secret_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.token = str(payload.get("token", ""))
        except (OSError, AttributeError, json.JSONDecodeError):
            pass

    def save(self, session: PlexSession, token: str):
        if not token:
            raise FocusError("Plex play command did not include an access token")
        with self.lock:
            atomic_json(self.state_path, session.public_dict())
            atomic_json(self.secret_path, {"token": token})
            self.session = session
            self.token = token

    def snapshot(self):
        with self.lock:
            return self.session

    def update_runtime(self, session: PlexSession):
        with self.lock:
            self.session = session

    def clear(self):
        with self.lock:
            for path in (self.state_path, self.secret_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self.session = None
            self.token = ""


class PlexServerClient:
    def __init__(self, http=None):
        self.http = http or XmlHttpClient()

    @timed("plex_queue")
    def play_queue(self, server_base: str, play_queue_id: str, token: str,
                   client_identifier: str):
        if not client_identifier:
            raise FocusError("Plex queue ownership requires a room client identifier")
        query = urllib.parse.urlencode(
            {"own": "1", "window": "200", "includeBefore": "1", "includeAfter": "1"}
        )
        return self.http.request(
            f"{server_base}/playQueues/{urllib.parse.quote(play_queue_id)}?{query}",
            {"X-Plex-Token": token, "X-Plex-Client-Identifier": client_identifier},
        )

    def report_timeline(
        self,
        session: PlexSession,
        token: str,
        client_identifier: str,
        device_name: str,
    ):
        item = session.current
        query = urllib.parse.urlencode(
            {
                "ratingKey": item.rating_key,
                "key": item.metadata_key,
                "state": session.state,
                "time": session.offset_ms,
                "playbackTime": session.offset_ms,
                "duration": item.duration_ms,
                "hasMDE": "1",
                "containerKey": f"/playQueues/{session.play_queue_id}",
                "playQueueID": session.play_queue_id,
                "playQueueItemID": item.play_queue_item_id,
                "playQueueVersion": session.play_queue_version,
            }
        )
        return self.http.request(
            f"{session.server_base}/:/timeline?{query}",
            {
                "X-Plex-Token": token,
                "X-Plex-Client-Identifier": client_identifier,
                "X-Plex-Session-Identifier": session.playback_session_id,
                "X-Plex-Product": "Plex Bridge",
                "X-Plex-Version": "1.0",
                "X-Plex-Device": "WiiM",
                "X-Plex-Device-Name": device_name,
                "X-Plex-Platform": "Linux",
                "X-Plex-Platform-Version": "NAS",
                "X-Plex-Model": "standalone",
                "X-Plex-Provides": "player",
            },
        )


class PlexCloudHttp:
    def request(self, url: str, method="GET", headers=None):
        request = urllib.request.Request(url, data=b"" if method != "GET" else None, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise FocusError(f"Plex account request returned HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise FocusError(f"Plex account request failed: {exc}") from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            # Device publication currently succeeds with a non-JSON body.
            # PIN calls validate their required JSON fields separately.
            return {}


class PlexCloudClient:
    PRODUCT = "Plex Bridge"

    def __init__(
        self,
        client_identifier: str,
        public_base: str,
        token_path: Path,
        http=None,
        endpoint_names=None,
    ):
        self.client_identifier = client_identifier
        self.endpoint_names = dict(endpoint_names or {})
        self.endpoint_names.setdefault(client_identifier, self.PRODUCT)
        self.public_base = public_base.rstrip("/")
        self.token_path = token_path
        self.http = http or PlexCloudHttp()
        self.pending = {}
        self.pending_path = token_path.with_name("plex-link-pending.json")
        try:
            with self.pending_path.open(encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                self.pending = {
                    key: value for key, value in saved.items()
                    if key in self.endpoint_names and isinstance(value, dict)
                    and isinstance(value.get("created"), (int, float))
                    and isinstance(value.get("id"), str) and isinstance(value.get("code"), str)
                    and 0 <= time.time() - value["created"] <= 1800
                }
        except (OSError, ValueError):
            pass
        self.tokens = {}
        try:
            with open(token_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            stored_tokens = payload.get("tokens", {})
            if isinstance(stored_tokens, dict):
                self.tokens = {
                    str(identifier): str(token)
                    for identifier, token in stored_tokens.items()
                    if identifier in self.endpoint_names and token
                }
            legacy_token = str(payload.get("token", ""))
            if legacy_token and client_identifier not in self.tokens:
                self.tokens[client_identifier] = legacy_token
        except (OSError, AttributeError, json.JSONDecodeError):
            pass

    @property
    def authorized(self):
        return bool(self.tokens)

    def linked(self, client_identifier):
        return bool(self.tokens.get(client_identifier, ""))

    def _validate_identifier(self, client_identifier):
        if client_identifier not in self.endpoint_names:
            raise FocusError("unknown Plex room endpoint")
        return client_identifier

    def headers(
        self,
        include_token=False,
        client_identifier="",
        device_name="",
        provides="",
    ):
        client_identifier = self._validate_identifier(
            client_identifier or self.client_identifier
        )
        headers = {
            "Accept": "application/json",
            "X-Plex-Product": self.PRODUCT,
            "X-Plex-Version": "1.0",
            "X-Plex-Client-Identifier": client_identifier,
            "X-Plex-Device": "NAS",
            "X-Plex-Device-Name": (
                device_name or self.endpoint_names[client_identifier]
            ),
            "X-Plex-Platform": "Linux",
            "X-Plex-Provides": provides or "client,player,pubsub-player",
        }
        if include_token:
            token = self.tokens.get(client_identifier, "")
            if not token:
                raise FocusError("Plex room endpoint has not been linked")
            headers["X-Plex-Token"] = token
        return headers

    def begin_link(self, client_identifier=""):
        client_identifier = self._validate_identifier(
            client_identifier or self.client_identifier
        )
        pending = self.pending.get(client_identifier, {})
        if pending and time.time() - pending.get("created", 0) <= 1800:
            return pending["code"], self.link_url(client_identifier)
        payload = self.http.request(
            "https://plex.tv/api/v2/pins?strong=true",
            "POST",
            self.headers(client_identifier=client_identifier),
        )
        pin_id = str(payload.get("id", ""))
        code = str(payload.get("code", ""))
        if not pin_id or not code:
            raise FocusError("Plex did not return a link code")
        self.pending[client_identifier] = {
            "id": pin_id,
            "code": code,
            "created": time.time(),
        }
        atomic_json(self.pending_path, self.pending)
        return code, self.link_url(client_identifier)

    def link_url(self, client_identifier, return_to="onboarding"):
        client_identifier = self._validate_identifier(client_identifier)
        if return_to not in {"onboarding", "dashboard"}:
            raise FocusError("Unsupported Plex return page")
        destination = "/onboarding?step=accounts#link-" + urllib.parse.quote(client_identifier, safe="") if return_to == "onboarding" else "/dashboard#plex-endpoints"
        code = self.pending[client_identifier]["code"]
        query = urllib.parse.urlencode(
            {
                "clientID": client_identifier,
                "code": code,
                "context[device][product]": self.PRODUCT,
                "forwardUrl": self.public_base + destination,
            }
        )
        return "https://app.plex.tv/auth#?" + query

    def finish_link(self, client_identifier=""):
        client_identifier = self._validate_identifier(
            client_identifier or self.client_identifier
        )
        pending = self.pending.get(client_identifier, {})
        pin_id = pending.get("id", "")
        if not pin_id or time.time() - float(pending.get("created", 0)) > 1800:
            raise PlexApprovalExpired("Plex link attempt is missing or expired")
        payload = self.http.request(
            f"https://plex.tv/api/v2/pins/{urllib.parse.quote(pin_id)}",
            "GET",
            self.headers(client_identifier=client_identifier),
        )
        token = str(payload.get("authToken", "") or "")
        if not token:
            raise PlexApprovalPending("Plex link has not been approved yet")
        self.tokens[client_identifier] = token
        atomic_json(self.token_path, {"tokens": self.tokens})
        self.pending.pop(client_identifier, None)
        atomic_json(self.pending_path, self.pending)
        self.publish(client_identifier)

    def publish(self, client_identifier=""):
        query = urllib.parse.urlencode(
            {"Connection[][uri]": self.public_base}
        )
        identifiers = (
            [self._validate_identifier(client_identifier)]
            if client_identifier
            else list(self.tokens)
        )
        for identifier in identifiers:
            self.publish_endpoint(identifier, query=query)

    def publish_endpoint(
        self,
        client_identifier,
        device_name="",
        provides="client,player,pubsub-player",
        query="",
    ):
        client_identifier = self._validate_identifier(client_identifier)
        query = query or urllib.parse.urlencode(
            {"Connection[][uri]": self.public_base}
        )
        self.http.request(
            "https://plex.tv/devices/"
            + urllib.parse.quote(client_identifier, safe="")
            + "?"
            + query,
            "PUT",
            self.headers(
                include_token=True,
                client_identifier=client_identifier,
                device_name=device_name,
                provides=provides,
            ),
        )


@dataclass(frozen=True)
class RendererSnapshot:
    state: str
    playlist_index: int
    position_ms: int
    source_url: str
    volume: int = 0
    playlist_count: int = 0


class WiiMRendererClient:
    """Read physical-button state without trusting an unrelated WiiM source."""

    def __init__(self, timeout=3, playlist_index_base=1):
        self.timeout = timeout
        self.playlist_index_base = playlist_index_base
        self.direct_sessions = {}
        self.ssl_context = ssl._create_unverified_context()

    @timed("wiim_http")
    def command(self, device: Device, command: str):
        encoded = urllib.parse.quote(command, safe=":,.[]{}")
        request = urllib.request.Request(
            f"https://{device.host}/httpapi.asp?command={encoded}"
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.ssl_context
            ) as response:
                body = response.read(1024 * 1024).decode(errors="replace")
        except (OSError, urllib.error.URLError) as exc:
            raise FocusError(f"WiiM renderer request failed: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body.strip()

    def rendering_control(self, device, action, arguments):
        return self._soap(device, action, arguments,
                          "urn:schemas-upnp-org:service:RenderingControl:1",
                          "rendercontrol1")

    def av_transport(self, device: Device, action: str, arguments):
        return self._soap(device, action, arguments,
                          "urn:schemas-upnp-org:service:AVTransport:1",
                          "rendertransport1")

    @timed("wiim_transport")
    def _soap(self, device, action, arguments, service, endpoint):
        envelope = ET.Element(
            "{http://schemas.xmlsoap.org/soap/envelope/}Envelope",
            {"{http://schemas.xmlsoap.org/soap/envelope/}encodingStyle": "http://schemas.xmlsoap.org/soap/encoding/"},
        )
        body = ET.SubElement(
            envelope, "{http://schemas.xmlsoap.org/soap/envelope/}Body"
        )
        operation = ET.SubElement(body, f"{{{service}}}{action}")
        for name, value in arguments:
            ET.SubElement(operation, name).text = str(value)
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
        request = urllib.request.Request(
            f"http://{device.host}:49152/upnp/control/{endpoint}",
            data=payload,
            method="POST",
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service}#{action}"',
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_body = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode(errors="replace")
            raise FocusError(f"WiiM AVTransport {action} returned HTTP {exc.code}: {detail[:300]}") from exc
        except (OSError, urllib.error.URLError) as exc:
            LOGGER.warning('renderer request failed', extra={'event': 'renderer_request_failed',
                           'operation': action, 'reason': failure_reason(exc)})
            raise FocusError(f"WiiM AVTransport {action} failed: {exc}") from exc
        if not response_body:
            return ET.Element("response")
        try:
            return ET.fromstring(response_body)
        except ET.ParseError as exc:
            raise FocusError(f"WiiM AVTransport {action} returned invalid XML") from exc

    @timed("wiim_queue")
    def play_queue(self, device: Device, action: str, arguments):
        service = "urn:schemas-wiimu-com:service:PlayQueue:1"
        envelope = ET.Element(
            "{http://schemas.xmlsoap.org/soap/envelope/}Envelope",
            {"{http://schemas.xmlsoap.org/soap/envelope/}encodingStyle": "http://schemas.xmlsoap.org/soap/encoding/"},
        )
        body = ET.SubElement(
            envelope, "{http://schemas.xmlsoap.org/soap/envelope/}Body"
        )
        operation = ET.SubElement(body, f"{{{service}}}{action}")
        for name, value in arguments:
            ET.SubElement(operation, name).text = str(value)
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
        request = urllib.request.Request(
            f"http://{device.host}:49152/upnp/control/PlayQueue1",
            data=payload,
            method="POST",
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service}#{action}"',
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_body = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode(errors="replace")
            raise FocusError(
                f"WiiM PlayQueue {action} returned HTTP {exc.code}: {detail[:300]}"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            LOGGER.warning('renderer request failed', extra={'event': 'renderer_request_failed',
                           'operation': action, 'reason': failure_reason(exc)})
            raise FocusError(f"WiiM PlayQueue {action} failed: {exc}") from exc
        if not response_body:
            return ET.Element("response")
        try:
            return ET.fromstring(response_body)
        except ET.ParseError as exc:
            raise FocusError(f"WiiM PlayQueue {action} returned invalid XML") from exc

    @staticmethod
    def didl_item_metadata(item: PlexQueueItem, media_url: str, artwork_url=""):
        duration_seconds, milliseconds = divmod(item.duration_ms, 1000)
        minutes, seconds = divmod(duration_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        duration = f"{hours}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
        artwork = (
            f"<upnp:albumArtURI>{html.escape(artwork_url)}</upnp:albumArtURI>"
            if artwork_url
            else ""
        )
        return (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="{html.escape(item.rating_key, quote=True)}" parentID="0" restricted="1">'
            f"<dc:title>{html.escape(item.title or 'Plex track')}</dc:title>"
            f"<dc:creator>{html.escape(item.artist)}</dc:creator>"
            f"<upnp:artist>{html.escape(item.artist)}</upnp:artist>"
            f"<upnp:album>{html.escape(item.album)}</upnp:album>"
            f"{artwork}"
            "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            f'<res protocolInfo="http-get:*:*:*" duration="{duration}">{html.escape(media_url)}</res>'
            "</item></DIDL-Lite>"
        )

    @classmethod
    def didl_metadata(cls, session: PlexSession, media_url: str, artwork_url=""):
        return cls.didl_item_metadata(session.current, media_url, artwork_url)

    @classmethod
    def queue_context(cls, entries, current_index=0):
        tracks = []
        for index, (item, media_url, artwork_url) in enumerate(entries, 1):
            metadata = html.escape(
                cls.didl_item_metadata(item, media_url, artwork_url), quote=True
            )
            tracks.append(
                f"<Track{index}>"
                f"<URL>{html.escape(media_url, quote=True)}</URL>"
                f"<Metadata>{metadata}</Metadata>"
                "<Source>OnlineMusic</Source>"
                f"<Key>{html.escape(item.rating_key)}</Key>"
                f"<Id>{html.escape(item.play_queue_item_id)}</Id>"
                f"</Track{index}>"
            )
        count = len(entries)
        return (
            '<?xml version="1.0"?>'
            "<PlayList><ListName>CurrentQueue</ListName><ListInfo>"
            "<QueueVersion>2.0</QueueVersion><Radio>0</Radio>"
            "<SourceName>Local</SourceName><PicUrl></PicUrl>"
            f"<TotalNumber>{count}</TotalNumber><TrackNumber>{count}</TrackNumber>"
            f"<LastPlayIndex>{current_index + 1}</LastPlayIndex></ListInfo><Tracks>"
            + "".join(tracks)
            + "</Tracks></PlayList>"
        )

    @staticmethod
    def queue_loop_mode(shuffle: bool, repeat: int):
        return {
            (False, 0): 4,
            (False, 1): 1,
            (False, 2): 0,
            (True, 0): 3,
            (True, 1): 2,
            (True, 2): 2,
        }[(shuffle, repeat)]

    def current_uri(self, device: Device):
        root = self.av_transport(device, "GetMediaInfo", [("InstanceID", 0)])
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] == "CurrentURI":
                return node.text or ""
        return ""

    def current_track(self, device: Device):
        root = self.av_transport(device, "GetPositionInfo", [("InstanceID", 0)])
        track_uri = ""
        track_index = -1
        for node in root.iter():
            name = node.tag.rsplit("}", 1)[-1]
            if name == "TrackURI":
                track_uri = node.text or ""
            elif name == "Track":
                try:
                    track_index = int(node.text or "0") - 1
                except ValueError:
                    track_index = -1
        return track_uri, track_index

    def current_track_uri(self, device: Device):
        return self.current_track(device)[0]

    @staticmethod
    def loop_mode(shuffle: bool, repeat: int):
        return {
            (False, 0): 0,
            (False, 1): 1,
            (False, 2): -1,
            (True, 0): 3,
            (True, 1): 5,
            (True, 2): 2,
        }[(shuffle, repeat)]

    def start_session(
        self, device: Device, session: PlexSession, media_url: str, artwork_url=""
    ):
        self.av_transport(
            device,
            "SetAVTransportURI",
            [
                ("InstanceID", 0),
                ("CurrentURI", media_url),
                (
                    "CurrentURIMetaData",
                    self.didl_metadata(session, media_url, artwork_url),
                ),
            ],
        )
        self.av_transport(device, "Play", [("InstanceID", 0), ("Speed", 1)])
        self.command(
            device,
            f"setPlayerCmd:loopmode:{self.loop_mode(session.shuffle, session.repeat)}",
        )
        if session.offset_ms:
            self.command(device, f"setPlayerCmd:seek:{session.offset_ms // 1000}")
        if session.state == "paused":
            self.command(device, "setPlayerCmd:pause")
        elif session.state == "stopped":
            self.command(device, "setPlayerCmd:stop")

    def start_queue_session(self, device: Device, session: PlexSession, entries):
        try:
            self.play_queue(
                device,
                "CreateQueue",
                [("QueueContext", self.queue_context(entries))],
            )
            self.play_queue(
                device,
                "PlayQueueWithIndex",
                [
                    ("QueueName", "CurrentQueue"),
                    ("Index", session.current_index + 1),
                ],
            )
            self.play_queue(
                device,
                "SetQueueLoopMode",
                [("LoopMode", self.queue_loop_mode(session.shuffle, session.repeat))],
            )
        except FocusError as exc:
            if failure_reason(exc) in {'timeout', 'connection_error'}:
                # Both APIs share the same playback service. A second request
                # after a transport failure adds delay and can duplicate work.
                raise
            LOGGER.warning(
                "WiiM native PlayQueue rejected the Plex queue; using direct track playback",
                exc_info=True,
            )
            self.direct_sessions[device.id] = session.renderer_session_id
            current = entries[session.current_index]
            return self.start_session(device, session, current[1], current[2])
        if session.offset_ms:
            self.command(device, f"setPlayerCmd:seek:{session.offset_ms // 1000}")
        if session.state == "paused":
            self.command(device, "setPlayerCmd:pause")
        elif session.state == "stopped":
            self.command(device, "setPlayerCmd:stop")

    def replace_queue_session(self, device: Device, session: PlexSession, entries):
        """Edit a rolling queue without CreateQueue (which can start playback)."""
        if self.direct_sessions.get(device.id) == session.renderer_session_id:
            return
        desired = [(item.play_queue_item_id, url) for item, url, _ in entries]

        def value(root, name):
            return next((n.text or "" for n in root.iter()
                         if n.tag.rsplit("}", 1)[-1] == name), "")

        def read():
            root = self.play_queue(device, "BrowseQueue", [("QueueName", "CurrentQueue")])
            context = value(root, "QueueContext")
            if not context:
                raise FocusError("native queue unavailable during refresh")
            tracks = ET.fromstring(context).find("Tracks")
            if tracks is None:
                raise FocusError("native queue has no tracks")
            return [(track.findtext("Id", ""), track.findtext("URL", "")) for track in tracks]

        def guard(expected_index):
            index = self.play_queue(device, "GetQueueIndex", [("QueueName", "CurrentQueue")])
            if value(index, "CurrentIndex") != str(expected_index + 1):
                raise FocusError("native queue advanced during refresh; retry")
            uri, _ = self.current_track(device)
            if uri != desired[session.current_index][1]:
                raise FocusError("native track changed during refresh; retry")

        native = read()
        current = desired[session.current_index]
        matches = [i for i, entry in enumerate(native) if entry == current]
        if len(matches) != 1:
            raise FocusError("current track is not unique in native queue")
        index = matches[0]
        # Rolling controllers discard played items. Retain any requested history;
        # arbitrary edits before the current track need a separate implementation.
        prune = index - session.current_index
        if prune < 0 or native[prune:index] != desired[:session.current_index]:
            raise FocusError("refresh changes played history; native edit unsupported")
        if native == desired:
            return
        guard(index)
        future = desired[session.current_index + 1:]
        retained = 0
        for old, new in zip(native[index + 1:], future):
            if old != new:
                break
            retained += 1
        cut = index + 1 + retained
        if len(native) > cut:
            guard(index)
            self.play_queue(device, "RemoveTracksInQueue", [
                ("QueueName", "CurrentQueue"), ("RangStart", cut + 1),
                ("RangEnd", len(native)), ("Action", 0),
            ])
            native = native[:cut]
            if read() != native:
                raise FocusError("native queue removal did not apply")
        tail = entries[session.current_index + 1 + retained:]
        if tail:
            guard(index)
            self.play_queue(device, "AppendTracksInQueueEx", [
                ("QueueContext", self.queue_context(tail)), ("Direction", 0),
                ("StartIndex", len(native)), ("Play", 0), ("Action", 0),
            ])
            native += future[retained:]
            if read() != native:
                raise FocusError("native queue append did not apply")
        if prune:
            guard(index)
            self.play_queue(device, "RemoveTracksInQueue", [
                ("QueueName", "CurrentQueue"), ("RangStart", 1),
                ("RangEnd", prune), ("Action", 0),
            ])
        if read() != desired:
            raise FocusError("native queue refresh verification failed")
        guard(session.current_index)

    def select_queue_index(self, device: Device, index: int, state="playing"):
        self.play_queue(
            device,
            "PlayQueueWithIndex",
            [("QueueName", "CurrentQueue"), ("Index", index + 1)],
        )
        if state == "paused":
            self.command(device, "setPlayerCmd:pause")

    def set_transport_state(self, device: Device, state: str):
        control = {
            "playing": "resume",
            "paused": "pause",
            "stopped": "stop",
        }.get(state)
        if control is None:
            raise FocusError("unsupported WiiM transport state")
        self.command(device, f"setPlayerCmd:{control}")

    def reinforce_initial_offset(
        self, device: Device, session: PlexSession, attempts=20, delay=0.25
    ):
        # LinkPlay can acknowledge PlayQueueWithIndex before selecting the
        # requested item, including starts at 0:00. Verify every initial start
        # while the bridge operation lock prevents reconciliation from adopting
        # a transient first-track observation. Seek only after selection settles.
        item_marker = (
            f"/plex/media/{session.renderer_session_id}/"
            f"{urllib.parse.quote(session.current.play_queue_item_id, safe='')}/"
        )
        settled = 0
        last_error = None
        for attempt in range(attempts):
            try:
                observed = self.snapshot(device)
            except FocusError as exc:
                last_error = exc
                settled = 0
            else:
                queue_index_matches = (
                    observed.playlist_count == 0
                    or observed.playlist_index == session.current_index
                )
                if (
                    item_marker in observed.source_url
                    and queue_index_matches
                    and observed.state in {"playing", "paused"}
                ):
                    settled += 1
                    if settled >= 2:
                        if session.offset_ms > 3000:
                            self.command(
                                device,
                                f"setPlayerCmd:seek:{session.offset_ms // 1000}",
                            )
                        return
                else:
                    settled = 0
                    # Retry only a positively identified item from our newly
                    # loaded queue, never an unrelated source or a read failure.
                    if (attempt in (4, 12)
                            and f"/plex/media/{session.renderer_session_id}/"
                            in observed.source_url):
                        self.select_queue_index(device, session.current_index,
                                                state=session.state)
            time.sleep(delay)
        detail = "WiiM did not settle on the requested Plex item"
        if last_error is not None:
            detail += f"; last transient status error: {last_error}"
        raise FocusError(detail)

    def apply_session(self, device: Device, before: PlexSession, after: PlexSession):
        if after.state == "stopped":
            # Stop must not seek or briefly start another queue item.
            self.command(device, "setPlayerCmd:stop")
            return
        if before.offset_ms != after.offset_ms:
            self.command(device, f"setPlayerCmd:seek:{after.offset_ms // 1000}")
        if (before.shuffle, before.repeat) != (after.shuffle, after.repeat):
            self.command(
                device,
                f"setPlayerCmd:loopmode:{self.loop_mode(after.shuffle, after.repeat)}",
            )
        if before.volume != after.volume:
            self.command(device, f"setPlayerCmd:vol:{after.volume}")
        if before.state != after.state:
            control = {
                "playing": "resume",
                "paused": "pause",
                "stopped": "stop",
            }.get(after.state)
            if control:
                self.command(device, f"setPlayerCmd:{control}")

    @staticmethod
    def _source_url(payload):
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key.lower() in {"url", "uri", "streamurl", "playbackurl"}:
                    candidate = str(value)
                    if candidate.startswith(("http://", "https://")):
                        return candidate
                    try:
                        decoded = base64.b64decode(candidate, validate=True).decode()
                    except (ValueError, UnicodeDecodeError):
                        decoded = ""
                    if decoded.startswith(("http://", "https://")):
                        return decoded
                nested = WiiMRendererClient._source_url(value)
                if nested:
                    return nested
        elif isinstance(payload, list):
            for value in payload:
                nested = WiiMRendererClient._source_url(value)
                if nested:
                    return nested
        return ""

    def snapshot(self, device: Device):
        track_uri, track_index = self.current_track(device)
        player = self.command(device, "getPlayerStatus")
        current_uri = "" if track_uri else self.current_uri(device)
        metadata = (
            {}
            if track_uri or current_uri
            else self.command(device, "getMetaInfo")
        )
        if not isinstance(player, dict):
            raise FocusError("WiiM renderer returned an unexpected status response")
        state = {
            "play": "playing",
            "pause": "paused",
            "stop": "stopped",
            "load": "buffering",
            "loading": "buffering",
        }.get(str(player.get("status", "")).lower())
        if state is None:
            raise FocusError("WiiM reported an unknown playback state")
        try:
            playlist_index = int(player.get("plicurr")) - self.playlist_index_base
            playlist_count = max(0, int(player.get("plicount", "0") or 0))
            if playlist_count == 0 and track_index >= 0:
                playlist_index = track_index
            position_ms = max(0, int(player.get("curpos")))
            volume = max(0, min(100, int(player.get("vol"))))
        except (TypeError, ValueError) as exc:
            raise FocusError("WiiM omitted its playlist position or volume") from exc
        return RendererSnapshot(
            state=state,
            playlist_index=playlist_index,
            position_ms=position_ms,
            # TrackURI follows the active item inside a native PlayQueue, while
            # GetMediaInfo.CurrentURI may remain queue-level or point at the
            # item that originally opened the queue.
            source_url=(
                track_uri
                or current_uri
                or self._source_url(player)
                or self._source_url(metadata)
            ),
            volume=volume,
            playlist_count=playlist_count,
        )

    def verify_session(self, device: Device, session: PlexSession, attempts=10):
        markers = (
            f"/plex/media/{session.renderer_session_id}/",
            f"/plex/playlist/{session.renderer_session_id}/",
        )
        previous_position = None
        last_transient_error = None
        for _ in range(attempts):
            try:
                observed = self.snapshot(device)
            except FocusError as exc:
                # LinkPlay's status endpoint can briefly disappear while a
                # player changes topology. The media request may already have
                # succeeded, so one failed sample is not a failed transfer.
                last_transient_error = exc
                time.sleep(1)
                continue
            identity_matches = any(marker in observed.source_url for marker in markers)
            if session.state == "playing":
                if (
                    identity_matches
                    and observed.state == "playing"
                    and previous_position is not None
                    and observed.position_ms > previous_position
                ):
                    return observed
                if identity_matches and observed.state == "playing":
                    previous_position = observed.position_ms
            elif identity_matches and observed.state == session.state:
                return observed
            time.sleep(1)
        detail = "WiiM did not verify the expected advancing Plex session"
        if last_transient_error is not None:
            detail += f"; last transient status error: {last_transient_error}"
        raise FocusError(detail)


class PlexBridge:
    """Canonical Plex queue owner; physical WiiMs are replaceable renderers."""

    def __init__(
        self,
        store: PlexSessionStore,
        server=None,
        client_identifier="wiim-focus-control",
        allowed_server_base="",
        devices=(),
        renderer=None,
        public_base="",
        media_secret="",
        default_device_id=None,
        endpoint_ids=None,
    ):
        self.store = store
        self.server = server or PlexServerClient()
        self.client_identifier = client_identifier
        self.allowed_server_base = allowed_server_base.rstrip("/")
        self.devices = {device.id: device for device in devices}
        if default_device_id is None and devices:
            default_device_id = devices[0].id
        self.endpoint_devices = {
            (
                endpoint_ids[device.id] if endpoint_ids is not None else (
                    client_identifier if device.id == default_device_id
                    else f"{client_identifier}-{device.id}"
                )
            ): device
            for device in devices
        }
        self.advertisement_lock = threading.Lock()
        self.endpoint_advertisements = self.individual_advertisements(
            resource_prefix=True
        )
        self.renderer = renderer or WiiMRendererClient()
        self.public_base = public_base.rstrip("/")
        self.media_secret = media_secret.encode()
        self.renderer_unavailable_since = None
        self.last_verified = 0.0
        self.last_server_report = 0.0
        self.last_position_at = 0.0
        self.last_session_persist = time.monotonic()
        self.operation_lock = threading.RLock()
        self.volume_lock = threading.Lock()
        self.pending_volume = None
        self.volume_worker_active = False
        self.subscription_lock = threading.Lock()
        self.subscribers = {}
        self.timeline_condition = threading.Condition()
        self.timeline_generation = 0
        self.last_track_worker = None
        self.retarget_stop_protect_until = 0.0

    @property
    def active(self):
        return self.store.snapshot() is not None

    @staticmethod
    def _play_queue_id(container_key: str):
        path = urllib.parse.urlsplit(container_key).path
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[-2] == "playQueues":
            return parts[-1]
        raise FocusError("Plex command did not identify a play queue")

    def target_device(self, endpoint_identifier: str):
        device = self.endpoint_devices.get(endpoint_identifier)
        if device is None:
            raise FocusError("Plex command targeted an unknown room endpoint")
        return device

    def endpoint_identifier(self, device_id: str):
        for identifier, device in self.endpoint_devices.items():
            if device.id == device_id:
                return identifier
        raise FocusError("Plex session targets an unknown room endpoint")

    def endpoint_group_states(self, players):
        """Describe live native topology for each Plex room endpoint."""
        by_id = {
            str(item.get("device", {}).get("id", "")): item
            for item in players
            if isinstance(item, dict) and isinstance(item.get("device"), dict)
        }
        by_host = {
            str(item.get("device", {}).get("host", "")): item
            for item in by_id.values()
            if item.get("device", {}).get("host")
        }
        labels = {}
        for identifier, device in self.endpoint_devices.items():
            state = by_id.get(device.id)
            if not state or not state.get("online"):
                labels[identifier] = "Unavailable"
                continue
            if state.get("role") == "follower":
                master_host = str(state.get("master_host", ""))
                leader = by_host.get(master_host)
                leader_name = (
                    str(leader.get("device", {}).get("name", ""))
                    if leader
                    else master_host
                )
                labels[identifier] = (
                    f"Follower of {leader_name}"
                    if leader_name
                    else "Follower (leader unknown)"
                )
                continue
            followers = sorted(
                str(item.get("device", {}).get("name", ""))
                for item in by_id.values()
                if item.get("online")
                and item.get("role") == "follower"
                and item.get("master_host") == device.host
                and item.get("device", {}).get("name")
            )
            labels[identifier] = (
                "Leader with " + ", ".join(followers)
                if followers
                else "Standalone"
            )
        return labels

    def individual_advertisements(self, resource_prefix=False):
        return {
            identifier: EndpointAdvertisement(
                ("Plex – " if resource_prefix else "") + device.name
            )
            for identifier, device in self.endpoint_devices.items()
        }

    def build_group_advertisements(self, players):
        """Build a complete topology view; callers supply fail-open policy."""
        states = {
            str(item.get("device", {}).get("id", "")): item
            for item in players
            if isinstance(item, dict) and isinstance(item.get("device"), dict)
        }
        if any(device.id not in states for device in self.endpoint_devices.values()):
            raise FocusError("group topology omitted a configured Plex room")
        if any(not states[device.id].get("online") for device in self.endpoint_devices.values()):
            raise FocusError("group topology contains an unavailable Plex room")
        by_host = {
            str(item["device"].get("host", "")): item
            for item in states.values()
            if item["device"].get("host")
        }
        followers = {}
        for device in self.endpoint_devices.values():
            state = states[device.id]
            role = state.get("role")
            if role not in {"follower", "standalone/leader"}:
                raise FocusError("group topology contains an unknown player role")
            if role != "follower":
                continue
            leader = by_host.get(str(state.get("master_host", "")))
            if not leader or leader.get("role") == "follower":
                raise FocusError("group topology has an unresolved leader")
            leader_id = str(leader["device"].get("id", ""))
            if leader_id not in states:
                raise FocusError("group topology leader is not a configured Plex room")
            followers.setdefault(leader_id, []).append(device)

        session = self.store.snapshot()
        if session and states[session.target_device_id].get("role") == "follower":
            # A native UI made the active Plex renderer follow another source.
            # Do not hide or retarget it automatically: doing so could replace
            # unrelated playback on the new leader.
            raise FocusError("active Plex renderer is no longer the group leader")

        advertisements = self.individual_advertisements()
        for identifier, device in self.endpoint_devices.items():
            members = followers.get(device.id, [])
            if members:
                title = (
                    f"{device.name} + {members[0].name}"
                    if len(members) == 1
                    else f"{device.name} + {len(members)} rooms"
                )
                advertisements[identifier] = EndpointAdvertisement(title)
            elif states[device.id].get("role") == "follower":
                advertisements[identifier] = EndpointAdvertisement(
                    device.name,
                    visible=False,
                    provides="client",
                )
        return advertisements

    def set_endpoint_advertisements(self, advertisements):
        if set(advertisements) != set(self.endpoint_devices):
            raise FocusError("Plex endpoint advertisement set is incomplete")
        with self.advertisement_lock:
            self.endpoint_advertisements = dict(advertisements)

    def advertisement_snapshot(self):
        with self.advertisement_lock:
            return dict(self.endpoint_advertisements)

    @staticmethod
    def _parse_queue(root, current_key: str, current_item_id=""):
        items = []
        for node in root.findall("./Track") + root.findall("./Video"):
            part = node.find("./Media/Part")
            item = PlexQueueItem(
                play_queue_item_id=str(node.get("playQueueItemID", "")),
                rating_key=str(node.get("ratingKey", "")),
                metadata_key=str(node.get("key", "")),
                part_key=str(part.get("key", "")) if part is not None else "",
                duration_ms=int(node.get("duration", "0") or 0),
                title=str(node.get("title", "")),
                artist=str(
                    node.get("grandparentTitle", node.get("originalTitle", ""))
                ),
                album=str(node.get("parentTitle", "")),
                art_key=str(
                    node.get("thumb", node.get("parentThumb", node.get("grandparentThumb", "")))
                ),
            )
            part_url = urllib.parse.urlsplit(item.part_key)
            if (
                not item.play_queue_item_id
                or not item.metadata_key
                or not item.part_key.startswith("/")
                or part_url.scheme
                or part_url.netloc
            ):
                raise FocusError("Plex queue contains an incomplete media item")
            items.append(item)
        if not items:
            raise FocusError("Plex play queue is empty")
        total_size = int(
            root.get("totalSize", root.get("size", str(len(items)))) or 0
        )
        if total_size != len(items):
            raise FocusError(
                f"Plex returned only {len(items)} of {total_size} queue items"
            )
        if current_item_id:
            matches = [
                index
                for index, item in enumerate(items)
                if item.play_queue_item_id == current_item_id
            ]
        else:
            matches = [
                index
                for index, item in enumerate(items)
                if item.metadata_key == current_key
                or item.rating_key == current_key.rsplit("/", 1)[-1]
            ]
        if len(matches) != 1:
            raise FocusError("current Plex item is not unique in its play queue")
        selected_id = str(root.get("playQueueSelectedItemID", ""))
        if (
            current_item_id
            and selected_id
            and items[matches[0]].play_queue_item_id != selected_id
        ):
            raise FocusError("Plex queue selection disagrees with the requested item")
        return (
            tuple(items),
            matches[0],
            str(root.get("playQueueVersion", "")),
            str(root.get("playQueueShuffled", "0")) == "1",
        )

    @traced("play_media")
    def play_media(self, params, endpoint_identifier=""):
        with self.operation_lock:
            return self._play_media(params, endpoint_identifier)

    def _play_media(self, params, endpoint_identifier=""):
        def required(name):
            value = params.get(name, [""])[0]
            if not value:
                raise FocusError(f"Plex play command is missing {name}")
            return value

        protocol = required("protocol")
        address = required("address")
        port = required("port")
        if protocol not in {"http", "https"} or not port.isdigit():
            raise FocusError("Plex server address is invalid")
        requested_server_base = f"{protocol}://{address}:{port}"
        server_base = requested_server_base
        if self.allowed_server_base and requested_server_base != self.allowed_server_base:
            allowed = urllib.parse.urlsplit(self.allowed_server_base)
            allowed_port = allowed.port or (443 if allowed.scheme == "https" else 80)
            plex_direct_prefix = (allowed.hostname or "").replace(".", "-") + "."
            secure_lan_alias = (
                protocol == "https"
                and int(port) == allowed_port
                and address.startswith(plex_direct_prefix)
                and address.endswith(".plex.direct")
            )
            if not secure_lan_alias:
                raise FocusError("Plex command named an unapproved server address")
            # Plexamp addresses the same LAN server through its certificate-bearing
            # plex.direct name. Keep all NAS-to-Plex traffic pinned to the configured
            # local origin after validating the encoded IP and port above.
            server_base = self.allowed_server_base
        server_identifier = required("machineIdentifier")
        target = self.target_device(endpoint_identifier)
        token = required("token")
        current_key = required("key")
        queue_id = self._play_queue_id(required("containerKey"))
        requested_offset = params.get("offset", [""])[0]
        requested_view_offset = params.get("viewOffset", [""])[0]
        LOGGER.info(
            "received Plex playMedia target=%s offset=%s viewOffset=%s",
            target.id,
            requested_offset or "absent",
            requested_view_offset or "absent",
        )
        root = self.server.play_queue(
            server_base, queue_id, token, self.endpoint_identifier(target.id)
        )
        previous = self.store.snapshot()
        requested_item = params.get("playQueueItemID", [""])[0]
        new_queue = (previous is None or previous.play_queue_id != queue_id
                     or previous.server_base != server_base)
        tracks = root.findall("./Track") + root.findall("./Video")
        # Plexamp can send the album's first key when handing off a NEW queue,
        # although Plex created that queue with the user's later track selected.
        # Only in this queue-head handoff case prefer Plex's selected item.
        # Explicit item IDs and same-queue/direct non-head selections stay intact.
        if (new_queue and not requested_item and tracks
                and current_key in {tracks[0].get("key"), tracks[0].get("ratingKey")}
                and root.get("playQueueSelectedItemID")):
            requested_item = root.get("playQueueSelectedItemID")
        items, current_index, queue_version, shuffled = self._parse_queue(
            root, current_key, requested_item,
        )
        session = PlexSession(
            server_identifier=server_identifier,
            server_base=server_base,
            play_queue_id=queue_id,
            play_queue_version=queue_version,
            items=items,
            current_index=current_index,
            offset_ms=max(0, int(params.get("offset", ["0"])[0] or 0)),
            state="playing",
            shuffle=shuffled,
            repeat=max(0, min(2, int(params.get("repeat", ["0"])[0] or 0))),
            target_device_id=target.id,
            renderer_session_id=secrets.token_urlsafe(18),
            volume=max(0, min(100, int(params.get("volume", ["50"])[0] or 50))),
            renderer_start_index=current_index,
            playback_session_id=secrets.token_urlsafe(18),
        )
        previous = self.store.snapshot()
        previous_token = self.store.token
        if previous is not None:
            self.report_server_timeline(replace(previous, state="stopped"), force=True)
        self.store.save(session, token)
        try:
            if not self.public_base:
                raise FocusError("Plex media public address is not configured")
            # Do not seek while LinkPlay is still selecting the requested queue
            # item. reinforce_initial_offset verifies the exact item first.
            initial_session = (
                replace(session, offset_ms=0) if session.offset_ms > 3000 else session
            )
            self._start_renderer_session(target, initial_session)
            if hasattr(self.renderer, "reinforce_initial_offset"):
                self.renderer.reinforce_initial_offset(target, session)
        except Exception:
            if previous is None:
                self.store.clear()
            else:
                self.store.save(previous, previous_token)
            raise
        if previous is not None and previous.target_device_id != target.id:
            previous_device = self.devices.get(previous.target_device_id)
            if previous_device is not None:
                try:
                    self.renderer.set_transport_state(previous_device, "stopped")
                except Exception:
                    # The new target already owns the canonical Plex session.
                    # A stale former renderer is cleanup work, not grounds to
                    # roll the successful handoff back onto the old room.
                    LOGGER.warning(
                        "could not stop former Plex room renderer %s",
                        previous_device.id,
                        exc_info=True,
                    )
        self.renderer_unavailable_since = None
        self.last_position_at = time.monotonic()
        self.notify_subscribers()
        self.report_server_timeline(session, force=True)
        LOGGER.info(
            "accepted Plex playMedia queue=%s item=%s canonical_index=%s renderer_index=0",
            session.play_queue_id,
            session.current.play_queue_item_id,
            session.current_index,
        )
        return session

    def _playlist_signature(self, session: PlexSession):
        if not self.media_secret:
            raise FocusError("Plex media signing is not configured")
        value = ":".join(
            [
                "playlist",
                session.renderer_session_id,
                session.play_queue_id,
                session.renderer_queue_version,
            ]
        )
        return hmac.new(self.media_secret, value.encode(), hashlib.sha256).hexdigest()

    def playlist_path(self, session: PlexSession):
        return (
            f"/plex/playlist/{session.renderer_session_id}/"
            f"{self._playlist_signature(session)}.m3u"
        )

    def resolve_playlist(self, path: str):
        session = self.store.snapshot()
        if session is None:
            raise FocusError("no active Plex session")
        parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
        if len(parts) != 4 or parts[:2] != ["plex", "playlist"]:
            raise FocusError("invalid Plex playlist path")
        _, _, session_id, supplied = parts
        supplied_signature = supplied.removesuffix(".m3u")
        if supplied_signature == supplied:
            raise FocusError("invalid Plex playlist path")
        if not hmac.compare_digest(session_id, session.renderer_session_id):
            raise FocusError("expired Plex renderer session")
        if not hmac.compare_digest(supplied_signature, self._playlist_signature(session)):
            raise FocusError("invalid Plex playlist signature")
        return session

    def _media_signature(self, session: PlexSession, item: PlexQueueItem):
        if not self.media_secret:
            raise FocusError("Plex media signing is not configured")
        value = ":".join(
            [
                session.renderer_session_id,
                session.play_queue_id,
                session.renderer_queue_version,
                item.play_queue_item_id,
            ]
        )
        return hmac.new(self.media_secret, value.encode(), hashlib.sha256).hexdigest()

    def media_path(self, session: PlexSession, item: PlexQueueItem):
        signature = self._media_signature(session, item)
        return (
            f"/plex/media/{session.renderer_session_id}/"
            f"{urllib.parse.quote(item.play_queue_item_id, safe='')}/{signature}"
        )

    def media_url(self, session: PlexSession):
        if not self.public_base:
            raise FocusError("Plex media public address is not configured")
        return self.public_base + self.media_path(session, session.current)

    def _art_signature(self, session: PlexSession, item: PlexQueueItem):
        value = ":".join(
            [
                "art",
                session.renderer_session_id,
                item.play_queue_item_id,
                item.art_key,
            ]
        )
        return hmac.new(self.media_secret, value.encode(), hashlib.sha256).hexdigest()

    def art_path(self, session: PlexSession, item: PlexQueueItem):
        if not item.art_key:
            return ""
        return (
            f"/plex/art/{session.renderer_session_id}/"
            f"{urllib.parse.quote(item.play_queue_item_id, safe='')}/"
            f"{self._art_signature(session, item)}"
        )

    def artwork_url(self, session: PlexSession):
        path = self.art_path(session, session.current)
        return self.public_base + path if path else ""

    @timed("renderer_start")
    def _start_renderer_session(self, device: Device, session: PlexSession):
        if getattr(self, 'diagnostics', None) is not None:
            self.diagnostics.bind_session(session)
        if hasattr(self.renderer, "start_queue_session"):
            return self.renderer.start_queue_session(device, session, self._renderer_entries(session))
        self.renderer.start_session(
            device, session, self.media_url(session), self.artwork_url(session)
        )

    def _renderer_entries(self, session):
        entries = []
        for item in session.items:
            art_path = self.art_path(session, item)
            entries.append((item, self.public_base + self.media_path(session, item),
                            self.public_base + art_path if art_path else ""))
        return entries

    def resolve_art(self, path: str):
        session = self.store.snapshot()
        if session is None:
            raise FocusError("no active Plex session")
        parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
        if len(parts) != 5 or parts[:2] != ["plex", "art"]:
            raise FocusError("invalid Plex artwork path")
        _, _, session_id, item_id, supplied_signature = parts
        if not hmac.compare_digest(session_id, session.renderer_session_id):
            raise FocusError("expired Plex renderer session")
        matches = [item for item in session.items if item.play_queue_item_id == item_id]
        if len(matches) != 1 or not matches[0].art_key:
            raise FocusError("Plex artwork is absent from the active queue")
        item = matches[0]
        if not hmac.compare_digest(supplied_signature, self._art_signature(session, item)):
            raise FocusError("invalid Plex artwork signature")
        return session.server_base + item.art_key, self.store.token

    def art_request(self, path: str):
        url, token = self.resolve_art(path)
        request = urllib.request.Request(url, method="GET")
        request.add_header("X-Plex-Token", token)
        return request

    def playlist_m3u(self, path: str):
        session = self.resolve_playlist(path)
        if not self.public_base:
            raise FocusError("Plex media public address is not configured")
        lines = ["#EXTM3U"]
        for item in session.items[session.renderer_start_index :]:
            lines.append(
                f"#EXTINF:{max(0, item.duration_ms // 1000)},"
                f"Plex queue item {item.play_queue_item_id}"
            )
            lines.append(self.public_base + self.media_path(session, item))
        return ("\n".join(lines) + "\n").encode()

    def resolve_media(self, path: str):
        session = self.store.snapshot()
        if session is None:
            raise FocusError("no active Plex session")
        parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
        if len(parts) != 5 or parts[:2] != ["plex", "media"]:
            raise FocusError("invalid Plex media path")
        _, _, session_id, item_id, supplied_signature = parts
        if not hmac.compare_digest(session_id, session.renderer_session_id):
            raise FocusError("expired Plex renderer session")
        matches = [
            item for item in session.items if item.play_queue_item_id == item_id
        ]
        if len(matches) != 1:
            raise FocusError("Plex media item is absent from the active queue")
        item = matches[0]
        expected = self._media_signature(session, item)
        if not hmac.compare_digest(supplied_signature, expected):
            raise FocusError("invalid Plex media signature")
        return session.server_base + item.part_key, self.store.token

    def media_request(self, path: str, range_header=""):
        url, token = self.resolve_media(path)
        request = urllib.request.Request(url, method="GET")
        request.add_header("X-Plex-Token", token)
        if range_header:
            request.add_header("Range", range_header)
        return request

    def reconcile_renderer(self):
        with self.operation_lock:
            return self._reconcile_renderer()

    def _reconcile_renderer(self):
        session = self.store.snapshot()
        if session is None:
            raise FocusError("no active Plex session")
        device = self.devices.get(session.target_device_id)
        if device is None:
            raise FocusError("active Plex renderer is no longer configured")
        observed = self.renderer.snapshot(device)
        markers = (
            f"/plex/media/{session.renderer_session_id}/",
            f"/plex/playlist/{session.renderer_session_id}/",
        )
        matches = any(marker in observed.source_url for marker in markers)
        if getattr(self, 'diagnostics', None) is not None:
            self.diagnostics.observe(session, observed, matches)
        if not matches:
            raise FocusError("WiiM is no longer rendering the active Plex session")
        identity_matches = any(marker in observed.source_url for marker in markers)
        if (
            identity_matches
            and observed.playlist_count == len(session.items)
            and 0 <= observed.playlist_index < len(session.items)
        ):
            # Native PlayQueue exposes its current item as a one-based plicurr.
            # Prefer that authoritative index because CurrentURI can lag behind
            # physical remote Forward/Back presses.
            canonical_index = observed.playlist_index
        else:
            matches = [
                index
                for index, item in enumerate(session.items)
                if self.media_path(session, item) in observed.source_url
            ]
            if len(matches) != 1:
                raise FocusError("WiiM reported an unknown Plex media URI")
            canonical_index = matches[0]
        projected_offset = self.effective_offset(session)
        previous_index = session.current_index - 1
        if session.repeat == 2 and session.current_index == 0:
            previous_index = len(session.items) - 1
        native_remote_previous = (
            canonical_index == previous_index
        )
        if native_remote_previous and projected_offset > 5000:
            # WiiM's native queue always moves to the previous item. Plex's
            # Back UX restarts the current item once playback is beyond the
            # opening five-second window, so correct the physical action.
            if not hasattr(self.renderer, "select_queue_index"):
                raise FocusError("WiiM renderer cannot correct native Back behavior")
            self.renderer.select_queue_index(
                device, session.current_index, state=session.state
            )
            canonical_index = session.current_index
            observed = replace(
                observed,
                state=session.state,
                playlist_index=session.current_index,
                position_ms=0,
            )
        item_changed = canonical_index != session.current_index
        if item_changed and session.state == "paused" and observed.state == "playing":
            # WiiM PlayQueue Forward/Back always starts the selected item. Keep
            # the Plex transport paused when the physical command originated
            # from an already-paused session.
            if not hasattr(self.renderer, "set_transport_state"):
                raise FocusError("WiiM renderer cannot preserve paused queue changes")
            self.renderer.set_transport_state(device, "paused")
            observed = replace(observed, state="paused")
        duration = session.items[canonical_index].duration_ms
        observed_offset = min(observed.position_ms, duration)
        if (
            canonical_index == session.current_index
            and session.state == "playing"
            and observed.state == "stopped"
            and max(observed_offset, projected_offset) >= max(0, duration - 5000)
        ):
            return self._advance_completed_track(session)
        # LinkPlay exposes position at a much coarser cadence than Plexamp's
        # local playhead.  Re-anchoring Plex to every sample makes an otherwise
        # smooth stream appear to jump backward and forward.  Keep the
        # monotonic Plex clock for small same-track discrepancies, but accept
        # larger jumps (a physical seek), state changes, and track changes.
        if (
            canonical_index == session.current_index
            and observed.state == "playing"
            and session.state == "playing"
        ):
            if abs(observed_offset - projected_offset) <= 5000:
                observed_offset = projected_offset
        if item_changed:
            self.report_server_timeline(
                replace(
                    session,
                    state="stopped",
                    offset_ms=min(projected_offset, session.current.duration_ms),
                ),
                force=True,
            )
        changed = replace(
            session,
            state=observed.state,
            current_index=canonical_index,
            offset_ms=observed_offset,
            volume=observed.volume,
        )
        now = time.monotonic()
        if now - self.last_session_persist >= 10:
            self.store.save(changed, self.store.token)
            self.last_session_persist = now
        else:
            self.store.update_runtime(changed)
        self.renderer_unavailable_since = None
        self.last_verified = now
        self.last_position_at = now
        self.notify_subscribers()
        self.report_server_timeline(changed, force=item_changed)
        return changed

    def _advance_completed_track(self, session: PlexSession):
        completed_item = replace(
            session,
            state="stopped",
            offset_ms=session.current.duration_ms,
        )
        self.report_server_timeline(completed_item, force=True)
        # Plexamp Radio, DJs, and queue edits can extend or reorder the active
        # play queue after playMedia. Refresh at the track boundary so Plex,
        # not this renderer, remains the authority for what comes next.
        try:
            root = self.server.play_queue(
                session.server_base, session.play_queue_id, self.store.token,
                self.endpoint_identifier(session.target_device_id),
            )
            items, current_index, queue_version, shuffled = self._parse_queue(
                root,
                session.current.metadata_key,
                session.current.play_queue_item_id,
            )
            session = replace(
                session,
                items=items,
                current_index=current_index,
                play_queue_version=queue_version,
                shuffle=shuffled,
            )
            self.store.update_runtime(session)
        except Exception:
            LOGGER.warning(
                "Plex queue refresh at track completion failed; using cached queue",
                exc_info=True,
            )
        if session.repeat == 1:
            target_index = session.current_index
        elif session.current_index + 1 < len(session.items):
            target_index = session.current_index + 1
        elif session.repeat == 2:
            target_index = 0
        else:
            completed = replace(
                session,
                state="stopped",
                offset_ms=session.current.duration_ms,
            )
            self.store.save(completed, self.store.token)
            self.last_session_persist = time.monotonic()
            self.last_position_at = 0.0
            self.notify_subscribers()
            self.report_server_timeline(completed, force=True)
            return completed

        return self._apply_change(
            current_index=target_index,
            offset_ms=0,
            state="playing",
            restart_renderer=True,
        )

    def report_server_timeline(self, session=None, force=False):
        session = session or self.store.snapshot()
        if session is None:
            return
        now = time.monotonic()
        if not force and now - self.last_server_report < 5:
            return
        device = self.devices.get(session.target_device_id)
        if device is None:
            return
        try:
            reported = replace(session, offset_ms=self.effective_offset(session))
            response = self.server.report_timeline(
                reported,
                self.store.token,
                self.endpoint_identifier(session.target_device_id),
                device.name,
            )
            playback_state = response.get("playbackState", "") if response is not None else ""
            if playback_state == "ignore":
                LOGGER.warning(
                    "Plex Media Server ignored timeline session=%s item=%s",
                    session.playback_session_id,
                    session.current.play_queue_item_id,
                )
            self.last_server_report = now
        except Exception:
            LOGGER.warning("Plex Media Server timeline report failed", exc_info=True)

    def effective_offset(self, session: PlexSession):
        offset = session.offset_ms
        if session.state == "playing" and self.last_position_at:
            offset += int((time.monotonic() - self.last_position_at) * 1000)
        return min(max(0, offset), session.current.duration_ms)

    def recoverable(self, maximum_age=5):
        return self.active and time.monotonic() - self.last_verified <= maximum_age

    def retarget(self, device: Device):
        with self.operation_lock:
            return self._retarget(device)

    def _retarget(self, device: Device):
        original = self.store.snapshot()
        if original is None:
            raise FocusError("no active Plex session")
        candidate = replace(
            original,
            target_device_id=device.id,
            renderer_session_id=secrets.token_urlsafe(18),
            renderer_start_index=original.current_index,
            offset_ms=self.effective_offset(original),
        )
        self.store.save(candidate, self.store.token)
        try:
            self._start_renderer_session(device, candidate)
            observed = self.renderer.verify_session(device, candidate)
            if observed is not None:
                if (
                    candidate.offset_ms > 3000
                    and abs(observed.position_ms - candidate.offset_ms) > 3000
                ):
                    # A seek issued while PlayQueue is still loading can be
                    # ignored. Reapply it only after the new renderer has
                    # proven that the intended stream is advancing.
                    self.renderer.command(
                        device, f"setPlayerCmd:seek:{candidate.offset_ms // 1000}"
                    )
                candidate = replace(candidate, volume=observed.volume)
                self.store.save(candidate, self.store.token)
            self.last_verified = time.monotonic()
            # Plexamp can tear down the former endpoint a moment after its
            # timeline changes to stopped. Do not let that stale stop destroy
            # the verified session that was just moved to another room.
            self.retarget_stop_protect_until = time.monotonic() + 5
            self.notify_subscribers()
            self.report_server_timeline(candidate, force=True)
            return candidate
        except Exception:
            self.store.save(original, self.store.token)
            raise

    def restore(self, session: PlexSession):
        with self.operation_lock:
            return self._restore(session)

    def _restore(self, session: PlexSession):
        device = self.devices.get(session.target_device_id)
        if device is None:
            raise FocusError("former Plex renderer is no longer configured")
        session = replace(
            session,
            renderer_session_id=secrets.token_urlsafe(18),
            renderer_start_index=session.current_index,
        )
        self.store.save(session, self.store.token)
        self._start_renderer_session(device, session)
        self.renderer.verify_session(device, session)
        self.last_verified = time.monotonic()
        self.notify_subscribers()
        self.report_server_timeline(session, force=True)

    @session_traced
    def monitor_once(self):
        observed_session = self.store.snapshot()
        try:
            self.reconcile_renderer()
            self.renderer_unavailable_since = None
        except FocusError:
            # A missing URI during a track transition is normal. A sustained
            # lost renderer must not leave Plexamp's optimistic clock running.
            with self.operation_lock:
                session = self.store.snapshot()
                if (session is None or observed_session is None
                        or session.renderer_session_id != observed_session.renderer_session_id):
                    return  # A failed observation of an old queue cannot stop its replacement.
                worker = self.last_track_worker
                if session.state == "buffering" and worker is not None and worker.is_alive():
                    return  # The deferred handoff still owns this transition.
                now = time.monotonic()
                if self.renderer_unavailable_since is None:
                    self.renderer_unavailable_since = now
                if (session is not None and session.state != "stopped"
                        and now - self.renderer_unavailable_since >= 3):
                    stopped = replace(session, state="stopped")
                    self.store.save(stopped, self.store.token)
                    self.last_position_at = 0.0
                    self.notify_subscribers()
                    self.report_server_timeline(stopped, force=True)
                else:
                    self.report_server_timeline()
        except Exception:
            LOGGER.exception("Plex renderer monitor recovered from an unexpected error")

    def monitor_forever(self, interval=0.2):
        while True:
            if self.active:
                self.monitor_once()
            time.sleep(interval)

    def _finish_deferred_track_change(
        self, before, after, device, previous_position_at
    ):
        with self.operation_lock:
            active = self.store.snapshot()
            if (
                active is None
                or active.state == "stopped"
                or active.renderer_session_id != after.renderer_session_id
                or active.current.play_queue_item_id
                != after.current.play_queue_item_id
            ):
                return
            try:
                self._start_renderer_session(device, after)
            except Exception:
                LOGGER.warning(
                    "deferred WiiM track change failed; restoring prior timeline",
                    exc_info=True,
                )
                self.store.save(before, self.store.token)
                self.last_position_at = previous_position_at
                self.notify_subscribers()
                self.report_server_timeline(before, force=True)
                return
            self.store.save(after, self.store.token)
            self.last_session_persist = time.monotonic()
            self.last_position_at = time.monotonic()
            self.notify_subscribers()
            self.report_server_timeline(after, force=True)

    def _apply_change(
        self, restart_renderer=False, defer_renderer=False, **values
    ):
        before = self.store.snapshot()
        if before is None:
            raise FocusError("no active Plex session")
        after = replace(before, **values)
        device = self.devices.get(before.target_device_id)
        if device is None:
            raise FocusError("active Plex renderer is no longer configured")
        if after.state != "stopped" and (restart_renderer or after.current_index != before.current_index):
            after = replace(
                after,
                renderer_start_index=after.current_index,
                renderer_session_id=secrets.token_urlsafe(18),
            )
            previous_position_at = self.last_position_at
            transition = replace(after, state="buffering", offset_ms=0)
            self.renderer_unavailable_since = None
            self.last_position_at = 0.0
            self.store.save(transition, self.store.token)
            # Deliver the new-item/zero-position transition before acknowledging
            # the controller command.  An asynchronous callback can lose the
            # race with the HTTP command response, causing Plexamp to display
            # the new track with the previous track's playhead temporarily.
            self.notify_subscribers(synchronous=True)
            if defer_renderer:
                worker = threading.Thread(
                    target=contextvars.copy_context().run,
                    args=(self._finish_deferred_track_change, before, after, device, previous_position_at),
                    daemon=True,
                )
                self.last_track_worker = worker
                worker.start()
                return after
            try:
                self._start_renderer_session(device, after)
            except Exception:
                self.store.save(before, self.store.token)
                self.last_position_at = previous_position_at
                self.notify_subscribers()
                raise
        else:
            self.renderer.apply_session(device, before, after)
        self.store.save(after, self.store.token)
        self.last_session_persist = time.monotonic()
        self.renderer_unavailable_since = None
        self.last_position_at = time.monotonic()
        self.notify_subscribers()
        self.report_server_timeline(after, force=True)
        return after

    @traced("command")
    def playback_command(self, command: str, params, endpoint_identifier=""):
        prior_worker = self.last_track_worker
        if (
            prior_worker is not None
            and prior_worker is not threading.current_thread()
            and prior_worker.is_alive()
        ):
            prior_worker.join(timeout=10)
            if prior_worker.is_alive():
                raise FocusError("previous WiiM track change is still pending")
        with self.operation_lock:
            session = self.store.snapshot()
            if endpoint_identifier:
                if session is None:
                    raise FocusError("no active Plex session")
                active_endpoint = self.endpoint_identifier(session.target_device_id)
                if endpoint_identifier != active_endpoint:
                    raise FocusError("Plex command targeted an inactive room endpoint")
            if command == "stop" and time.monotonic() < self.retarget_stop_protect_until:
                raise FocusError("stale Plex stop suppressed during room handoff")
            if (
                command == "setParameters"
                and "volume" in params
                and not any(name in params for name in ("shuffle", "repeat"))
            ):
                return self.queue_volume(params["volume"][0])
            return self._playback_command(command, params)

    def queue_volume(self, raw_volume):
        volume = max(0, min(100, int(raw_volume or 0)))
        session = self.store.snapshot()
        if session is None:
            raise FocusError("no active Plex session")
        changed = replace(session, volume=volume)
        self.store.update_runtime(changed)
        self.notify_subscribers()
        with self.volume_lock:
            self.pending_volume = volume
            if not self.volume_worker_active:
                self.volume_worker_active = True
                threading.Thread(target=self._volume_worker, daemon=True).start()
        return changed

    def _volume_worker(self):
        while True:
            # Let rapid slider events collapse before touching the WiiM.
            time.sleep(0.12)
            with self.volume_lock:
                volume = self.pending_volume
                self.pending_volume = None
            session = self.store.snapshot()
            if session is not None and volume is not None:
                device = self.devices.get(session.target_device_id)
                if device is not None:
                    try:
                        self.renderer.command(device, f"setPlayerCmd:vol:{volume}")
                    except FocusError:
                        LOGGER.warning("coalesced WiiM volume update failed", exc_info=True)
            with self.volume_lock:
                if self.pending_volume is None:
                    self.volume_worker_active = False
                    break
        session = self.store.snapshot()
        if session is not None:
            self.store.save(session, self.store.token)
            self.last_session_persist = time.monotonic()
            self.report_server_timeline(session, force=True)

    def _playback_command(self, command: str, params):
        session = self.store.snapshot()
        if session is None:
            raise FocusError("no active Plex session")
        if command == "refreshPlayQueue":
            requested = params.get("playQueueID", [""])[0]
            if requested != session.play_queue_id:
                raise FocusError("refresh named a different Plex play queue")
            root = self.server.play_queue(
                session.server_base, session.play_queue_id, self.store.token,
                self.endpoint_identifier(session.target_device_id),
            )
            items, current_index, queue_version, shuffled = self._parse_queue(
                root,
                session.current.metadata_key,
                session.current.play_queue_item_id,
            )
            candidate = replace(
                session,
                items=items,
                current_index=current_index,
                play_queue_version=queue_version,
                shuffle=shuffled,
            )
            # Controllers such as Symfonium publish a rolling current+next
            # queue. Update the physical queue too, without restarting audio.
            # Publish signed media paths before the WiiM can preload new items.
            self.store.save(candidate, self.store.token)
            if session.state != "stopped" and hasattr(self.renderer, "replace_queue_session"):
                device = self.devices[session.target_device_id]
                # Keep the controller's desired state if an edit fails midway:
                # it authorizes newly appended URLs and lets an identical retry
                # reconcile the observed queue instead of duplicating changes.
                self.renderer.replace_queue_session(device, candidate, self._renderer_entries(candidate))
            self.notify_subscribers()
            self.report_server_timeline(candidate, force=True)
            return candidate
        if command == "play":
            device = self.devices[session.target_device_id]
            try:
                observed = self.renderer.snapshot(device)
                owned = any(marker in observed.source_url for marker in (
                    f"/plex/media/{session.renderer_session_id}/",
                    f"/plex/playlist/{session.renderer_session_id}/",
                ))
                restart = not owned or observed.state == "stopped"
            except FocusError:
                restart = True
            return self._apply_change(state="playing", restart_renderer=restart)
        if command == "pause":
            return self._apply_change(state="paused")
        if command == "stop":
            return self._apply_change(state="stopped", current_index=0, offset_ms=0)
        if command == "skipNext":
            if session.repeat == 1:
                return self._apply_change(offset_ms=0, state="playing")
            if session.current_index + 1 < len(session.items):
                return self._apply_change(
                    current_index=session.current_index + 1,
                    offset_ms=0,
                    state="playing",
                    defer_renderer=True,
                )
            if session.repeat == 2:
                return self._apply_change(
                    current_index=0,
                    offset_ms=0,
                    state="playing",
                    defer_renderer=True,
                )
            return self._apply_change(state="stopped", current_index=0, offset_ms=0)
        if command == "skipPrevious":
            if session.offset_ms > 5000:
                return self._apply_change(offset_ms=0, state="playing")
            if session.current_index > 0:
                return self._apply_change(
                    current_index=session.current_index - 1,
                    offset_ms=0,
                    state="playing",
                    defer_renderer=True,
                )
            if session.repeat == 2:
                return self._apply_change(
                    current_index=len(session.items) - 1,
                    offset_ms=0,
                    state="playing",
                    defer_renderer=True,
                )
            return self._apply_change(offset_ms=0, state="playing")
        if command == "stepBack":
            return self._apply_change(offset_ms=max(0, session.offset_ms - 15000))
        if command == "stepForward":
            return self._apply_change(
                offset_ms=min(
                    session.current.duration_ms, session.offset_ms + 30000
                )
            )
        if command == "seekTo":
            offset = int(params.get("offset", ["0"])[0] or 0)
            return self._apply_change(
                offset_ms=max(0, min(offset, session.current.duration_ms))
            )
        if command == "skipTo":
            wanted = params.get("key", [""])[0]
            matches = [
                index
                for index, item in enumerate(session.items)
                if wanted
                in {item.metadata_key, item.rating_key, item.play_queue_item_id}
            ]
            if len(matches) != 1:
                raise FocusError("requested Plex queue item is not uniquely available")
            return self._apply_change(
                current_index=matches[0],
                offset_ms=0,
                state="playing" if session.state == "stopped" else session.state,
                restart_renderer=session.state == "stopped",
                defer_renderer=matches[0] != session.current_index,
            )
        if command == "setParameters":
            values = {}
            if "shuffle" in params:
                values["shuffle"] = params["shuffle"][0] == "1"
            if "repeat" in params:
                values["repeat"] = max(
                    0, min(2, int(params["repeat"][0] or 0))
                )
            if "volume" in params:
                values["volume"] = max(
                    0, min(100, int(params["volume"][0] or 0))
                )
            if not values:
                raise FocusError("no supported Plex playback parameter was supplied")
            return self._apply_change(**values)
        raise FocusError("unsupported Plex playback command")

    def resources_xml(self, endpoint_identifier=""):
        public = urllib.parse.urlsplit(self.public_base)
        address = public.hostname or ""
        port = str(public.port or (443 if public.scheme == "https" else 80))
        advertisements = self.advertisement_snapshot()
        if endpoint_identifier:
            device = self.target_device(endpoint_identifier)
            visible = (
                [(endpoint_identifier, device)]
                if advertisements[endpoint_identifier].visible
                else []
            )
        else:
            visible = [
                (identifier, device)
                for identifier, device in self.endpoint_devices.items()
                if advertisements[identifier].visible
            ]
        root = ET.Element("MediaContainer", {"size": str(len(visible))})
        session = self.store.snapshot()
        for endpoint_identifier, device in visible:
            state = (
                session.state
                if session and session.target_device_id == device.id
                else "stopped"
            )
            player = ET.SubElement(root, "Player", {
                "title": advertisements[endpoint_identifier].title,
                "machineIdentifier": endpoint_identifier,
                "product": "WiiM Plex Bridge",
                "platform": "Linux",
                "platformVersion": "1",
                "protocol": "plex",
                "protocolVersion": "1",
                "protocolCapabilities": "timeline,playback,navigation,playqueues",
                "deviceClass": "speaker",
                "state": state,
                "address": address,
                "port": port,
                "version": "1.0",
                "provides": "client,player,pubsub-player",
            })
            ET.SubElement(player, "Connection", {
                "protocol": public.scheme or "http",
                "address": address,
                "port": port,
                "uri": self.public_base,
                "local": "1",
            })
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def root_xml(self, endpoint_identifier=""):
        endpoint_identifier = endpoint_identifier or self.client_identifier
        device = self.target_device(endpoint_identifier)
        advertisement = self.advertisement_snapshot()[endpoint_identifier]
        root = ET.Element("MediaContainer", {
            "machineIdentifier": endpoint_identifier,
            "version": "1.0",
        })
        ET.SubElement(root, "Player", {
            "title": advertisement.title,
            "machineIdentifier": endpoint_identifier,
        })
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def subscribe_timeline(
        self, endpoint, controller_id, host, protocol, port, command_id
    ):
        if protocol not in {"http", "https"}:
            raise FocusError("unsupported Plex timeline protocol")
        try:
            port_number = int(port)
        except (TypeError, ValueError) as exc:
            raise FocusError("invalid Plex timeline port") from exc
        if not 1 <= port_number <= 65535:
            raise FocusError("invalid Plex timeline port")
        self.target_device(endpoint)
        subscriber = {
            "endpoint": endpoint,
            "controller": controller_id,
            "url": f"{protocol}://{host}:{port_number}/:/timeline",
            "command_id": str(command_id),
            "expires": time.monotonic() + 90,
        }
        with self.subscription_lock:
            self.subscribers[(controller_id, endpoint)] = subscriber
        threading.Thread(
            target=self._post_timeline, args=(subscriber,), daemon=True
        ).start()

    def unsubscribe_timeline(self, endpoint, controller_id):
        with self.subscription_lock:
            self.subscribers.pop((controller_id, endpoint), None)

    def note_command(self, endpoint, controller_id, command_id):
        if not command_id:
            return
        with self.subscription_lock:
            subscriber = self.subscribers.get((controller_id, endpoint))
            if subscriber:
                subscriber["command_id"] = str(command_id)

    def _post_timeline(self, subscriber, data=None):
        request = urllib.request.Request(
            subscriber["url"],
            data=data or self.timeline_xml(
                subscriber["command_id"], subscriber["endpoint"]
            ),
            method="POST",
            headers={
                "Content-Type": "application/xml",
                "X-Plex-Client-Identifier": subscriber["endpoint"],
            },
        )
        try:
            context = (
                ssl._create_unverified_context()
                if subscriber["url"].startswith("https://")
                else None
            )
            with urllib.request.urlopen(request, timeout=3, context=context):
                pass
        except (OSError, urllib.error.URLError):
            LOGGER.debug(
                "Plex timeline delivery failed for controller %s",
                subscriber["controller"],
            )

    def notify_subscribers(self, synchronous=False):
        with self.timeline_condition:
            self.timeline_generation += 1
            self.timeline_condition.notify_all()
        now = time.monotonic()
        with self.subscription_lock:
            expired = [key for key, item in self.subscribers.items() if item["expires"] < now]
            for key in expired:
                self.subscribers.pop(key, None)
            subscribers = [dict(item) for item in self.subscribers.values()]
        for subscriber in subscribers:
            # Capture the exact state that caused the notification. Otherwise
            # the delivery thread can race a rapid buffering -> playing track
            # handoff and omit the zero-position transition entirely.
            data = self.timeline_xml(
                subscriber["command_id"], subscriber["endpoint"]
            )
            if synchronous:
                self._post_timeline(subscriber, data)
            else:
                threading.Thread(
                    target=self._post_timeline, args=(subscriber, data), daemon=True
                ).start()

    def timeline_poll(
        self, command_id="", wait=False, timeout=10, endpoint_identifier=""
    ):
        if wait:
            deadline = time.monotonic() + timeout
            with self.timeline_condition:
                generation = self.timeline_generation
                initial = self.timeline_xml(command_id, endpoint_identifier)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    changed = self.timeline_condition.wait_for(
                        lambda: self.timeline_generation != generation,
                        timeout=remaining,
                    )
                    if not changed:
                        break
                    generation = self.timeline_generation
                    current = self.timeline_xml(command_id, endpoint_identifier)
                    if current != initial:
                        return current
        return self.timeline_xml(command_id, endpoint_identifier)

    def timeline_xml(self, command_id="", endpoint_identifier=""):
        session = self.store.snapshot()
        if endpoint_identifier:
            device = self.target_device(endpoint_identifier)
            if session and session.target_device_id != device.id:
                session = None
        attributes = {
            "location": "fullScreenMusic" if session else "navigation",
        }
        if command_id:
            attributes["commandID"] = command_id
        root = ET.Element("MediaContainer", attributes)
        if session:
            item = session.current
            server = urllib.parse.urlsplit(session.server_base)
            ET.SubElement(root, "Timeline", {
                "type": "music",
                "state": session.state,
                "time": str(self.effective_offset(session)),
                "duration": str(item.duration_ms),
                "key": item.metadata_key,
                "ratingKey": item.rating_key,
                "machineIdentifier": session.server_identifier,
                "containerKey": f"/playQueues/{session.play_queue_id}",
                "playQueueID": session.play_queue_id,
                "playQueueItemID": item.play_queue_item_id,
                "playQueueVersion": session.play_queue_version,
                "shuffle": "1" if session.shuffle else "0",
                "repeat": str(session.repeat),
                "protocol": server.scheme,
                "address": server.hostname or "",
                "port": str(server.port or (443 if server.scheme == "https" else 80)),
                "token": self.store.token,
                "volume": str(session.volume),
                "controllable": "playPause,stop,skipPrevious,skipNext,seekTo,stepBack,stepForward,volume,shuffle,repeat",
                "seekRange": f"0-{item.duration_ms}",
            })
        else:
            ET.SubElement(root, "Timeline", {
                "type": "music",
                "state": "stopped",
                "time": "0",
                "volume": "100",
                "shuffle": "0",
                "repeat": "0",
                "controllable": "playPause,stop,skipPrevious,skipNext,seekTo,stepBack,stepForward,volume,shuffle,repeat",
            })
        ET.SubElement(root, "Timeline", {"type": "video", "state": "stopped"})
        ET.SubElement(root, "Timeline", {"type": "photo", "state": "stopped"})
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class GroupClient:
    def __init__(self, base: str, token: str, http=None):
        self.base = base.rstrip("/")
        self.token = token
        self.http = http or JsonHttpClient()

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def players(self):
        payload = self.http.request(f"{self.base}/api/v1/players", headers=self.headers)
        return payload["players"]

    def events(self):
        payload = self.http.request(f"{self.base}/api/v1/events", headers=self.headers)
        return payload["events"]

    def action(self, path: str, requester: str, **extra):
        body = {"requester": requester, **extra}
        payload = self.http.request(
            f"{self.base}{path}", "POST", self.headers, body, timeout=30
        )
        event = payload["event"]
        if event["outcome"] in {"failed", "rejected", "conflict", "ambiguous"}:
            raise FocusError(event["detail"])
        return event

    def disband(self, requester: str):
        return self.action("/api/v1/groups/disband", requester)

    def auto_join(self, requester: str):
        return self.action("/api/v1/groups/auto-join", requester)

    def sleep(self, requester: str):
        return self.action("/api/v1/players/sleep", requester)

    def join(self, requester: str, leader: str):
        return self.action("/api/v1/groups/join", requester, leader=leader)

    def leave(self, requester: str):
        return self.action("/api/v1/groups/leave", requester)

    def leave_and_sleep(self, requester: str):
        return self.action("/api/v1/groups/leave-and-sleep", requester)

    def stop(self, requester: str):
        return self.action("/api/v1/players/stop-session", requester)

    def set_leadership(self, enabled: list[str]):
        payload = self.http.request(
            f"{self.base}/api/v1/policy/leadership",
            "POST",
            self.headers,
            {"enabled": enabled},
            timeout=30,
        )
        event = payload["event"]
        if event["outcome"] in {"failed", "rejected"}:
            raise FocusError(event["detail"])
        return event


class PlexEndpointPublisher:
    """Stabilize native topology before changing Plex endpoint metadata."""

    def __init__(self, plex, groups, cloud=None, mode="off", interval=3):
        if mode not in {"off", "local", "cloud"}:
            raise ValueError("PLEX_GROUP_ENDPOINT_MODE must be off, local, or cloud")
        self.plex = plex
        self.groups = groups
        self.cloud = cloud
        self.mode = mode
        self.interval = interval
        self.pending = None
        self.pending_count = 0
        self.applied = None
        self.published = {}
        self.registration = RegistrationMaintainer(cloud, self.plex.advertisement_snapshot)

    @staticmethod
    def signature(advertisements):
        return tuple(
            (identifier, item.title, item.visible, item.provides)
            for identifier, item in advertisements.items()
        )

    def observe(self):
        try:
            return self.plex.build_group_advertisements(self.groups.players())
        except (FocusError, KeyError, TypeError, ValueError):
            LOGGER.warning(
                "using fail-open individual Plex endpoints; topology unavailable",
                exc_info=True,
            )
            return self.plex.individual_advertisements()

    def poll_once(self):
        if self.mode == "off":
            return False
        advertisements = self.observe()
        signature = self.signature(advertisements)
        if signature == self.pending:
            self.pending_count += 1
        else:
            self.pending = signature
            self.pending_count = 1
        if self.pending_count < 2:
            return False
        if signature != self.applied:
            self.plex.set_endpoint_advertisements(advertisements)
            self.applied = signature
        if self.mode == "cloud" and self.cloud:
            self.registration.poll_once()
        return True

    def monitor_forever(self):
        while True:
            try:
                self.poll_once()
            except Exception:
                LOGGER.exception("dynamic Plex endpoint monitor failed")
            time.sleep(self.interval)


class SpotifyClient:
    def __init__(self, client_id: str, redirect_uri: str, token_path: Path, http=None):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_path = token_path
        self.http = http or JsonHttpClient()
        self.lock = threading.Lock()
        self.token = {}
        self.pending = {}
        try:
            with open(token_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                self.token = payload
        except (OSError, json.JSONDecodeError):
            pass

    @property
    def configured(self):
        return bool(self.client_id)

    @property
    def authorized(self):
        return bool(self.token.get("refresh_token") or self.token.get("access_token"))

    def begin_authorization(self):
        if not self.configured:
            raise FocusError("Spotify client id is not configured")
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        state = secrets.token_urlsafe(24)
        self.pending = {"state": state, "verifier": verifier, "created": time.time()}
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": SPOTIFY_SCOPES,
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        return f"https://accounts.spotify.com/authorize?{query}"

    def finish_authorization(self, callback_url: str):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(callback_url).query)
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        if not code or not state or state != self.pending.get("state"):
            raise FocusError("invalid or expired Spotify callback")
        if time.time() - float(self.pending.get("created", 0)) > 600:
            raise FocusError("Spotify authorization attempt expired")
        payload = self._token_request(
            {
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": self.pending["verifier"],
            }
        )
        self._save_token(payload)
        self.pending = {}

    def _token_request(self, values):
        request = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=urllib.parse.urlencode(values).encode(),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise FocusError(f"Spotify token exchange failed: HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise FocusError(f"Spotify token exchange failed: {exc}") from exc

    def _save_token(self, payload):
        merged = dict(self.token)
        merged.update(payload)
        merged["expires_at"] = time.time() + int(payload.get("expires_in", 3600)) - 30
        self.token = merged
        atomic_json(self.token_path, merged)

    def access_token(self):
        with self.lock:
            if not self.authorized:
                raise FocusError("Spotify has not been authorized")
            if time.time() < float(self.token.get("expires_at", 0)):
                return self.token["access_token"]
            refresh = self.token.get("refresh_token")
            if not refresh:
                raise FocusError("Spotify authorization cannot be refreshed")
            payload = self._token_request(
                {
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                }
            )
            self._save_token(payload)
            return self.token["access_token"]

    def api(self, path: str, method="GET", payload=None):
        return self.http.request(
            f"https://api.spotify.com/v1{path}",
            method,
            {"Authorization": f"Bearer {self.access_token()}"},
            payload,
        )

    def playback(self):
        return self.api("/me/player")

    def devices(self):
        payload = self.api("/me/player/devices")
        return payload.get("devices", []) if payload else []

    def transfer(self, device_id: str):
        self.api("/me/player", "PUT", {"device_ids": [device_id], "play": True})

    def reconstruct(self, device_id: str, snapshot):
        item = (snapshot or {}).get("item") or {}
        item_uri = item.get("uri")
        if not item_uri:
            raise FocusError("Spotify snapshot has no playable item")
        body = {"position_ms": int(snapshot.get("progress_ms") or 0)}
        context_uri = ((snapshot.get("context") or {}).get("uri"))
        if context_uri:
            body.update({"context_uri": context_uri, "offset": {"uri": item_uri}})
        else:
            body["uris"] = [item_uri]
        query = urllib.parse.urlencode({"device_id": device_id})
        self.api(f"/me/player/play?{query}", "PUT", body)


def normalized(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


class Coordinator:
    def __init__(self, devices, groups, spotify, history, plex=None):
        self.devices = devices
        self.by_id = {x.id: x for x in devices}
        self.by_host = {x.host: x for x in devices}
        self.groups = groups
        self.spotify = spotify
        self.history = history
        self.plex = plex
        self.operation_lock = threading.Lock()
        self.trigger_lock = threading.Lock()
        self.trigger_cooldowns = {}
        self.trigger_cooldown_seconds = 30

    def schedule(self, action: str, requester_host: str):
        requester = self.by_host.get(requester_host)
        if requester is None:
            self.history.record(action, requester_host, "rejected", "unknown source address")
            return False
        key = (action, requester.id)
        now = time.monotonic()
        with self.trigger_lock:
            if now < self.trigger_cooldowns.get(key, 0):
                self.history.record(
                    action,
                    requester.name,
                    "debounced",
                    "duplicate preset stream request suppressed",
                )
                return False
            self.trigger_cooldowns[key] = now + self.trigger_cooldown_seconds
            if not self.operation_lock.acquire(blocking=False):
                self.history.record(action, requester.name, "busy", "another action is running")
                return False
        targets = {
            "focus": self.focus,
            "disband": self.disband,
        }
        target = targets.get(action)
        if target is None:
            self.operation_lock.release()
            self.history.record(action, requester.name, "rejected", "unknown action")
            return False

        def run():
            try:
                target(requester)
            except Exception as exc:
                self.history.record(action, requester.name, "failed", str(exc))
                try:
                    # A WiiM preset is an audio stream. Stop that stream after
                    # a failed action so firmware repeat behavior cannot keep
                    # reopening the command URL indefinitely.
                    self.groups.stop(requester.id)
                except Exception:
                    LOGGER.exception("could not stop failed preset stream")
            finally:
                self.operation_lock.release()

        threading.Thread(target=run, daemon=True).start()
        return True

    def topology(self, requester: Device, players=None):
        players = players or self.groups.players()
        by_id = {x["device"]["id"]: x for x in players}
        current = by_id.get(requester.id)
        if not current or not current.get("online"):
            raise FocusError("requesting WiiM state is unavailable")
        if current.get("role") == "follower":
            leader = next(
                (x for x in players if x["device"]["host"] == current.get("master_host")),
                None,
            )
            if leader is None:
                raise FocusError("group leader cannot be resolved")
        else:
            leader = current
        members = [
            x
            for x in players
            if x["device"]["id"] == leader["device"]["id"]
            or (
                x.get("role") == "follower"
                and x.get("master_host") == leader["device"]["host"]
            )
        ]
        return current, leader, members

    def _spotify_device(self, requester: Device, attempts=10):
        wanted = normalized(requester.name)
        for _ in range(attempts):
            devices = self.spotify.devices()
            exact = [x for x in devices if normalized(str(x.get("name", ""))) == wanted]
            if len(exact) == 1 and exact[0].get("id") and not exact[0].get("is_restricted"):
                return exact[0]
            time.sleep(1)
        raise FocusError(f"Spotify Connect device {requester.name!r} was not uniquely available")

    def _verify_spotify(self, device_id: str, item_uri: str):
        previous = -1
        for _ in range(10):
            time.sleep(1)
            state = self.spotify.playback() or {}
            item = state.get("item") or {}
            progress = int(state.get("progress_ms") or 0)
            if (
                (state.get("device") or {}).get("id") == device_id
                and state.get("is_playing") is True
                and item.get("uri") == item_uri
            ):
                if previous >= 0 and progress > previous:
                    return True
                previous = progress
        return False

    def focus(self, requester: Device):
        if self.plex and self.plex.recoverable():
            self._focus_plex_request(requester)
            return
        players = self.groups.players()
        _, leader, members = self.topology(requester, players)
        if not self.spotify.authorized:
            self.groups.stop(requester.id)
            raise FocusError("Spotify is not authorized; Plex adapter is not installed yet")
        snapshot = self.spotify.playback() or {}
        item_uri = ((snapshot.get("item") or {}).get("uri"))
        if not item_uri:
            self.groups.stop(requester.id)
            raise FocusError("no recoverable Spotify session; Plex adapter is not installed yet")
        member_ids = [x["device"]["id"] for x in members]
        leader_id = leader["device"]["id"]
        self.groups.disband(requester.id)
        try:
            target = self._spotify_device(requester)
            try:
                self.spotify.transfer(target["id"])
            except FocusError:
                self.spotify.reconstruct(target["id"], snapshot)
            if not self._verify_spotify(target["id"], item_uri):
                self.spotify.reconstruct(target["id"], snapshot)
                if not self._verify_spotify(target["id"], item_uri):
                    raise FocusError("Spotify playback did not verify on the selected room")
            sleep_failures = []
            for device_id in member_ids:
                if device_id == requester.id:
                    continue
                try:
                    self.groups.sleep(device_id)
                except FocusError as exc:
                    sleep_failures.append(f"{device_id}: {exc}")
            if sleep_failures:
                self.history.record(
                    "focus",
                    requester.name,
                    "partial",
                    "Spotify playback verified, but former rooms did not all sleep: "
                    + "; ".join(sleep_failures),
                )
                return
            self.history.record(
                "focus",
                requester.name,
                "focused",
                f"Spotify playback verified; slept {len(member_ids) - 1} former member(s)",
            )
        except Exception as exc:
            self._rollback_spotify(snapshot, leader_id, member_ids)
            raise FocusError(f"{exc}; attempted restoration to {leader['device']['name']}") from exc

    def _focus_plex_request(self, requester: Device):
        players = self.groups.players()
        _, leader, members = self.topology(requester, players)
        snapshot = self.plex.store.snapshot()
        source = self.by_id.get(getattr(snapshot, "target_device_id", ""))
        if source is not None:
            _, leader, source_members = self.topology(source, players)
            members = list(
                {
                    item["device"]["id"]: item
                    for item in [*members, *source_members]
                }.values()
            )
        self._focus_plex(requester, leader, members)

    def _focus_plex(self, requester: Device, leader, members):
        # Freeze renderer reconciliation and Companion playback commands across
        # the handoff. Otherwise the monitor can publish the old renderer's
        # transient stopped state and controllers may stop the new renderer.
        with self.plex.operation_lock:
            return self._focus_plex_transaction(requester, leader, members)

    def _focus_plex_transaction(self, requester: Device, leader, members):
        snapshot = self.plex.store.snapshot()
        member_ids = [item["device"]["id"] for item in members]
        restore_leader_id = getattr(snapshot, "target_device_id", "")
        if restore_leader_id not in member_ids:
            restore_leader_id = leader["device"]["id"]
        # Selecting a network preset can reverse leader/follower roles before
        # the callback is processed. Isolate the room that actually requested
        # Focus instead of acting on a topology leader observed earlier. A
        # second call confirms the durable standalone state after LinkPlay's
        # asynchronous role transition has had time to settle.
        self.groups.leave(requester.id)
        time.sleep(0.5)
        self.groups.leave(requester.id)
        try:
            # The preset URL is itself the requester's active audio stream.
            # End it before installing the Plex queue or firmware EOF handling
            # can overwrite the newly selected renderer session.
            self.groups.stop(requester.id)
            self.plex.retarget(requester)
            failures = []
            for device_id in member_ids:
                if device_id == requester.id:
                    continue
                try:
                    # Selecting a network preset can make the requester leader
                    # before the HTTP callback arrives. Explicitly eject each
                    # former room from that new group, then sleep it.
                    self.groups.leave(device_id)
                    self.groups.sleep(device_id)
                except FocusError as exc:
                    failures.append(f"{device_id}: {exc}")
            if failures:
                self.history.record(
                    "focus",
                    requester.name,
                    "partial",
                    "Plex playback verified, but former rooms did not all sleep: "
                    + "; ".join(failures),
                )
                return
            self.history.record(
                "focus",
                requester.name,
                "focused",
                f"Plex playback verified; slept {len(member_ids) - 1} former member(s)",
            )
        except Exception as exc:
            try:
                self.plex.restore(snapshot)
                for member_id in member_ids:
                    if member_id != restore_leader_id:
                        self.groups.join(member_id, restore_leader_id)
            except Exception:
                LOGGER.exception("Plex focus rollback failed")
            raise FocusError(
                f"{exc}; attempted Plex restoration to "
                f"{self.by_id[restore_leader_id].name}"
            ) from exc

    def _rollback_spotify(self, snapshot, leader_id: str, member_ids: list[str]):
        leader = self.by_id[leader_id]
        try:
            device = self._spotify_device(leader, attempts=5)
            self.spotify.reconstruct(device["id"], snapshot)
            for member_id in member_ids:
                if member_id != leader_id:
                    self.groups.join(member_id, leader_id)
        except Exception:
            LOGGER.exception("focus rollback failed")

    def sleep(self, requester: Device):
        # Serialize with track completion and pending queue changes. Only stop
        # Plex when this physical room owns it; a follower must leave quietly.
        with self.plex.operation_lock:
            session = self.plex.store.snapshot()
            if session is not None and session.target_device_id == requester.id:
                self.plex._apply_change(state="stopped")
            return self.groups.leave_and_sleep(requester.id)

    def disband(self, requester: Device):
        _, _, members = self.topology(requester)
        member_ids = [x["device"]["id"] for x in members]
        self.groups.disband(requester.id)
        failures = []
        for device_id in member_ids:
            try:
                self.groups.sleep(device_id)
            except FocusError as exc:
                failures.append(f"{device_id}: {exc}")
        if failures:
            raise FocusError("group dissolved, but shutdown failed: " + "; ".join(failures))
        self.history.record(
            "disband",
            requester.name,
            "disbanded",
            f"dissolved and slept {len(member_ids)} player(s)",
        )


def pbkdf2_password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2-sha256:600000:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected = encoded.split(":", 3)
        if algorithm != "pbkdf2-sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


class App:
    def __init__(
        self, coordinator, username, password_hash, secret, plex=None, plex_cloud=None
    ):
        self.coordinator = coordinator
        self.username = username
        self.password_hash = password_hash
        self.secret = secret.encode()
        self.plex = plex
        self.plex_cloud = plex_cloud
        self.onboarding = None
        self.dashboard_ui = Dashboard(self)
        self.diagnostics = Diagnostics("playback")
        if self.plex is not None:
            self.plex.diagnostics = self.diagnostics
        self.support_checks = SupportChecks(self)

    def session_token(self):
        nonce = secrets.token_urlsafe(24)
        signature = hmac.new(self.secret, nonce.encode(), hashlib.sha256).hexdigest()
        return f"{nonce}.{signature}"

    def valid_session(self, token):
        try:
            nonce, signature = token.rsplit(".", 1)
        except ValueError:
            return False
        expected = hmac.new(self.secret, nonce.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


class Server(SafeServerErrors, ThreadingHTTPServer):
    daemon_threads = True
    # Plexamp fans out one discovery and long-poll connection per linked room.
    # TCPServer's default backlog of five can refuse part of a six-room refresh
    # even while the health check succeeds on a neighboring connection.
    request_queue_size = 64

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "WiiMPlexBridge/1"

    @property
    def app(self):
        return self.server.app

    def proxy_symfonium(self):
        proxy = getattr(self.app, "symfonium_proxy", None)
        if proxy and self.path.startswith("/symfonium/"):
            proxy.handle(self)
            return True
        return False

    def do_HEAD(self):
        if not self.proxy_symfonium():
            self.send_body(405, b"", "text/plain")

    do_PUT = do_HEAD
    do_DELETE = do_HEAD

    def do_OPTIONS(self):
        if self.proxy_symfonium():
            return
        path = urllib.parse.urlsplit(self.path).path
        if self.app.plex and (
            path == "/" or path == "/resources" or path.startswith("/player/")
        ):
            self.send_body(
                HTTPStatus.OK,
                b"",
                "text/plain",
                {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                },
            )
        else:
            self.send_body(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

    def do_GET(self):
        if self.proxy_symfonium():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if (
            self.app.plex
            and path == "/"
            and self.headers.get("X-Plex-Client-Identifier", "")
        ):
            endpoint = self.headers.get("X-Plex-Target-Client-Identifier", "")
            self.send_body(
                HTTPStatus.OK,
                self.app.plex.root_xml(endpoint),
                "text/xml",
                {"Access-Control-Allow-Origin": "*"},
            )
        elif path == "/health":
            self.send_body(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        elif self.app.plex and path.startswith("/plex/playlist/"):
            try:
                self.require_wiim_source()
                self.send_body(
                    HTTPStatus.OK,
                    self.app.plex.playlist_m3u(path),
                    "audio/x-mpegurl",
                )
            except FocusError as exc:
                self.send_body(
                    HTTPStatus.FORBIDDEN,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif self.app.plex and path.startswith("/plex/media/"):
            try:
                self.require_wiim_source()
                self.proxy_plex_media(path)
            except FocusError as exc:
                self.send_body(
                    HTTPStatus.FORBIDDEN,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif self.app.plex and path.startswith("/plex/art/"):
            try:
                self.proxy_plex_art(path)
            except FocusError as exc:
                self.send_body(
                    HTTPStatus.FORBIDDEN,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif self.app.plex and path == "/resources":
            try:
                endpoint = self.headers.get(
                    "X-Plex-Target-Client-Identifier", ""
                )
                self.send_body(
                    HTTPStatus.OK,
                    self.app.plex.resources_xml(endpoint),
                    "text/xml",
                    {"Access-Control-Allow-Origin": "*"},
                )
            except FocusError as exc:
                self.send_body(
                    HTTPStatus.BAD_REQUEST,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif self.app.plex and path == "/player/timeline/poll":
            try:
                endpoint = self.require_plex_request()
                params = urllib.parse.parse_qs(parsed.query)
                self.send_body(
                    HTTPStatus.OK,
                    self.app.plex.timeline_poll(
                        params.get("commandID", [""])[0],
                        params.get("wait", ["0"])[0] == "1",
                        endpoint_identifier=endpoint,
                    ),
                    "application/xml",
                    {
                        "X-Plex-Client-Identifier": endpoint,
                        "Access-Control-Expose-Headers": "X-Plex-Client-Identifier",
                        "Access-Control-Allow-Origin": "*",
                    },
                )
            except FocusError as exc:
                self.send_body(
                    HTTPStatus.BAD_REQUEST,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif self.app.plex and path == "/player/timeline/subscribe":
            try:
                endpoint = self.require_plex_request()
                controller = self.headers.get("X-Plex-Client-Identifier", "")
                params = urllib.parse.parse_qs(parsed.query)
                self.app.plex.subscribe_timeline(
                    endpoint,
                    controller,
                    self.client_address[0],
                    params.get("protocol", [""])[0],
                    params.get("port", [""])[0],
                    params.get("commandID", [""])[0],
                )
                self.send_body(
                    HTTPStatus.OK,
                    b"",
                    "text/plain",
                    {"X-Plex-Client-Identifier": endpoint},
                )
            except FocusError as exc:
                self.send_body(
                    HTTPStatus.BAD_REQUEST,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif self.app.plex and path == "/player/timeline/unsubscribe":
            try:
                endpoint = self.require_plex_request()
                controller = self.headers.get("X-Plex-Client-Identifier", "")
                self.app.plex.unsubscribe_timeline(endpoint, controller)
                self.send_body(HTTPStatus.OK, b"", "text/plain")
            except FocusError as exc:
                self.send_body(
                    HTTPStatus.BAD_REQUEST,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif self.app.plex and path == "/player/playback/playMedia":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                endpoint = self.require_plex_request()
                self.app.plex.note_command(
                    endpoint,
                    self.headers.get("X-Plex-Client-Identifier", ""),
                    params.get("commandID", [""])[0],
                )
                self.app.plex.play_media(params, endpoint)
                self.send_body(HTTPStatus.OK, b"", "text/plain")
            except FocusError as exc:
                LOGGER.warning(
                    "Plex playMedia rejected: %s fields=%s",
                    exc,
                    ",".join(sorted(params)),
                )
                self.send_body(
                    HTTPStatus.BAD_REQUEST,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif self.app.plex and path.startswith("/player/playback/"):
            try:
                endpoint = self.require_plex_request(allow_active=True)
                command = path.rsplit("/", 1)[1]
                params = urllib.parse.parse_qs(parsed.query)
                self.app.plex.note_command(
                    endpoint,
                    self.headers.get("X-Plex-Client-Identifier", ""),
                    params.get("commandID", [""])[0],
                )
                self.app.plex.playback_command(
                    command, params, endpoint_identifier=endpoint
                )
                self.send_body(HTTPStatus.OK, b"", "text/plain")
            except (FocusError, ValueError) as exc:
                LOGGER.warning(
                    "Plex playback command rejected: %s path=%s endpoint=%s",
                    exc,
                    path,
                    locals().get("endpoint", "unknown"),
                )
                self.send_body(
                    HTTPStatus.BAD_REQUEST,
                    str(exc).encode(),
                    "text/plain; charset=utf-8",
                )
        elif path in {"/preset/focus", "/preset/disband"}:
            action = path.rsplit("/", 1)[1]
            self.app.coordinator.schedule(action, self.client_address[0])
            self.send_body(HTTPStatus.OK, silent_wav(), "audio/wav")
        elif path == "/oauth/callback":
            callback = self.app.coordinator.spotify.redirect_uri.split("?", 1)[0]
            callback += "?" + urllib.parse.urlsplit(self.path).query
            try:
                self.app.coordinator.spotify.finish_authorization(callback)
                self.send_html(HTTPStatus.OK, "<h1>Spotify authorized</h1><p>You may close this page.</p>")
            except FocusError as exc:
                self.send_html(HTTPStatus.BAD_REQUEST, f"<h1>Authorization failed</h1><p>{html.escape(str(exc))}</p>")
        elif path == "/login":
            if self.authenticated():
                next_page = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("next", [""])[0]
                self.redirect("/" + next_page if next_page in {"onboarding", "diagnostics"} else "/dashboard")
            else:
                self.login_page()
        elif path == "/diagnostics" or path.startswith("/diagnostics/download/"):
            if not self.authenticated():
                self.redirect("/login?next=diagnostics")
            elif path == "/diagnostics":
                window = urllib.parse.parse_qs(parsed.query)
                self.send_html(HTTPStatus.OK, self.app.diagnostics.page(
                    window.get('start', [''])[0], window.get('end', [''])[0]), {
                    "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
                    "X-Content-Type-Options": "nosniff"})
            else:
                incident = path.removeprefix("/diagnostics/download/")
                body = self.app.diagnostics.download(incident)
                if body is None:
                    self.send_html(HTTPStatus.NOT_FOUND, "<p>Preview expired or unavailable.</p><a href='/diagnostics'>Create a new preview</a>")
                else:
                    self.send_body(HTTPStatus.OK, body, "application/json", {
                        "Content-Disposition": f'attachment; filename="plex-bridge-{incident}.json"',
                        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
        elif path == "/onboarding":
            if not self.authenticated():
                self.redirect("/login?next=onboarding")
            elif self.app.onboarding:
                step = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("step", [""])[0]
                self.send_html(HTTPStatus.OK, self.app.onboarding.page(step))
            else:
                self.send_body(HTTPStatus.SERVICE_UNAVAILABLE, b"Setup status unavailable", "text/plain")
        elif path in {"/", "/dashboard"}:
            if not self.authenticated():
                self.redirect("/login")
            else:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.dashboard(query.get("notice", [""])[0])
        else:
            self.send_body(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

    def do_POST(self):
        if self.proxy_symfonium():
            return
        path = urllib.parse.urlsplit(self.path).path
        form = self.read_form()
        if path == "/login":
            if form.get("username", [""])[0] == self.app.username and verify_password(
                form.get("password", [""])[0], self.app.password_hash
            ):
                self.send_response(HTTPStatus.SEE_OTHER)
                next_page = form.get("next", [""])[0]
                self.send_header("Location", "/" + next_page if next_page in {"onboarding", "diagnostics"} else "/dashboard")
                self.send_header("Set-Cookie", f"wiim_focus_session={self.app.session_token()}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
            else:
                self.login_page("Invalid username or password.", HTTPStatus.UNAUTHORIZED)
            return
        if not self.authenticated():
            self.redirect("/login")
            return
        if path == "/diagnostics/run":
            if self.app.plex is None or self.headers.get("Origin") != self.app.plex.public_base:
                self.send_body(HTTPStatus.FORBIDDEN, b"Open the configured bridge URL", "text/plain")
                return
            try:
                self.app.support_checks.run(form.get("requester", [""])[0])
            except ValueError as exc:
                self.send_html(HTTPStatus.CONFLICT, "<p>" + html.escape(str(exc)) + "</p><a href='/diagnostics'>View diagnostics</a>")
                return
            self.redirect("/diagnostics")
            return
        if path == "/dashboard/action":
            if self.headers.get("Origin") != self.app.plex.public_base:
                self.send_body(HTTPStatus.FORBIDDEN, b"Open the configured bridge URL", "text/plain")
                return
            try:
                message = self.app.dashboard_ui.act(form)
                status = HTTPStatus.OK
            except (FocusError, ValueError, KeyError, TypeError, OSError) as exc:
                message = f"Action failed: {exc}"
                LOGGER.warning('dashboard action rejected', exc_info=True,
                               extra={'event': 'action_rejected',
                                      'action': form.get('action', [''])[0],
                                      'reason': {
                                          'Another room action is running; wait for it to finish': 'busy',
                                          'Confirm restarting this WiiM; playback and any group it leads may be interrupted': 'confirmation_required',
                                          'A restart was already requested. Wait for the recovery check before retrying': 'restart_cooldown',
                                          'WiiM did not acknowledge the restart; check room status': 'restart_rejected',
                                      }.get(str(exc), failure_reason(exc))})
                status = HTTPStatus.CONFLICT
            if self.headers.get("Accept") == "application/json":
                self.send_body(status, json.dumps({"message": message}).encode(), "application/json")
            else:
                self.redirect("/dashboard?" + urllib.parse.urlencode({"notice": message}))
            return
        if path.startswith("/onboarding/"):
            if self.headers.get("Origin") != self.app.plex.public_base:
                self.send_body(HTTPStatus.FORBIDDEN, b"Open setup using the configured bridge URL", "text/plain")
                return
            try:
                if not self.app.onboarding:
                    raise ValueError("Setup status unavailable")
                action = path.rsplit("/", 1)[-1]
                endpoint = form.get("endpoint", [""])[0]
                if action == "open":
                    self.redirect(self.app.onboarding.open_authorization(endpoint))
                    return
                self.app.onboarding.submit(action, endpoint)
                anchor = "link-" + urllib.parse.quote(endpoint, safe="") if action in {"link", "finish"} else {"connections": "connections", "server": "server", "seen": "players"}.get(action, "playback")
                step = {"link": "accounts", "finish": "accounts", "connections": "connections", "server": "server", "seen": "players"}.get(action, "playback")
                self.redirect("/onboarding?step=" + step + "#" + anchor)
            except (ValueError, OSError):
                self.send_html(HTTPStatus.BAD_REQUEST, "<p>Could not save this setup action. Check state storage and retry.</p><a href='/onboarding'>Return to setup</a>")
        elif path == "/spotify/start":
            try:
                url = self.app.coordinator.spotify.begin_authorization()
                body = f"<h1>Authorize Spotify</h1><p><a href=\"{html.escape(url, quote=True)}\">Open Spotify authorization</a>.</p><p>If the localhost callback cannot load, copy its complete URL from the browser and paste it below.</p><form method=\"post\" action=\"/spotify/finish\"><input name=\"callback\" required><button>Finish authorization</button></form>"
                self.send_html(HTTPStatus.OK, body)
            except FocusError as exc:
                self.send_html(HTTPStatus.CONFLICT, f"<h1>Spotify unavailable</h1><p>{html.escape(str(exc))}</p>")
        elif path == "/spotify/finish":
            try:
                self.app.coordinator.spotify.finish_authorization(form.get("callback", [""])[0])
                self.redirect("/dashboard")
            except FocusError as exc:
                self.send_html(HTTPStatus.BAD_REQUEST, f"<h1>Authorization failed</h1><p>{html.escape(str(exc))}</p>")
        elif path == "/plex/start":
            try:
                endpoint = form.get("endpoint", [""])[0]
                code, url = self.app.plex_cloud.begin_link(endpoint)
                url = self.app.plex_cloud.link_url(endpoint, return_to="dashboard")
                endpoint_name = self.app.plex_cloud.endpoint_names[endpoint]
                body = (
                    f"<h1>Link {html.escape(endpoint_name)}</h1>"
                    f"<p>Code: <strong>{html.escape(code)}</strong></p>"
                    f"<p><a href='{html.escape(url, quote=True)}' target='_blank'>"
                    "Open Plex authorization</a>, approve this room, then return here.</p>"
                    "<form method='post' action='/plex/finish'>"
                    f"<input type='hidden' name='endpoint' value='{html.escape(endpoint, quote=True)}'>"
                    "<button>Finish Plex link</button></form>"
                )
                self.send_html(HTTPStatus.OK, body)
            except FocusError as exc:
                self.send_html(
                    HTTPStatus.CONFLICT,
                    f"<h1>Plex link failed</h1><p>{html.escape(str(exc))}</p>",
                )
        elif path == "/plex/finish":
            try:
                self.app.plex_cloud.finish_link(form.get("endpoint", [""])[0])
                self.redirect("/dashboard")
            except FocusError as exc:
                self.send_html(
                    HTTPStatus.CONFLICT,
                    f"<h1>Plex link incomplete</h1><p>{html.escape(str(exc))}</p>",
                )
        elif path in {"/dashboard/focus", "/dashboard/disband"}:
            requester_id = form.get("requester", [""])[0]
            requester = self.app.coordinator.by_id.get(requester_id)
            if requester:
                action = path.rsplit("/", 1)[1]
                self.app.coordinator.schedule(action, requester.host)
            self.redirect("/dashboard")
        elif path in {
            "/dashboard/group-auto-join",
            "/dashboard/group-leave",
            "/dashboard/group-leave-sleep",
            "/dashboard/group-disband",
            "/dashboard/leadership",
        }:
            try:
                if path == "/dashboard/leadership":
                    event = self.app.coordinator.groups.set_leadership(
                        form.get("enabled", [])
                    )
                else:
                    requester = form.get("requester", [""])[0]
                    action = {
                        "/dashboard/group-auto-join": self.app.coordinator.groups.auto_join,
                        "/dashboard/group-leave": self.app.coordinator.groups.leave,
                        "/dashboard/group-leave-sleep": self.app.coordinator.groups.leave_and_sleep,
                        "/dashboard/group-disband": self.app.coordinator.groups.disband,
                    }[path]
                    event = action(requester)
                notice = event.get("detail", event.get("outcome", "Action complete"))
            except (FocusError, KeyError, TypeError, ValueError) as exc:
                LOGGER.warning("dashboard group action failed: %s", exc)
                notice = f"Action failed: {exc}"
            self.redirect("/dashboard?" + urllib.parse.urlencode({"notice": notice}))
        else:
            self.send_body(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

    def dashboard(self, message=""):
        self.send_html(HTTPStatus.OK, self.app.dashboard_ui.page(message))

    def login_page(self, message="", status=HTTPStatus.OK):
        notice = f"<p>{html.escape(message)}</p>" if message else ""
        requested = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("next", [""])[0]
        next_page = requested if requested in {"onboarding", "diagnostics"} else "dashboard"
        self.send_html(status, f"<h1>WiiM Plex bridge</h1>{notice}<form method='post' action='/login'><input type='hidden' name='next' value='{next_page}'><label>Username<input name='username' required></label><label>Password<input type='password' name='password' required></label><button>Sign in</button></form>")

    def authenticated(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        token = cookie.get("wiim_focus_session")
        return bool(token and self.app.valid_session(token.value))

    def require_plex_target(self):
        target = self.headers.get("X-Plex-Target-Client-Identifier", "")
        self.app.plex.target_device(target)
        return target

    def require_plex_request(self, allow_active=False):
        if not self.headers.get("X-Plex-Client-Identifier", ""):
            raise FocusError("Plex controller identifier is missing")
        if allow_active and not self.headers.get(
            "X-Plex-Target-Client-Identifier", ""
        ):
            session = self.app.plex.store.snapshot()
            if session is None:
                raise FocusError("Plex command has no active room endpoint")
            return self.app.plex.endpoint_identifier(session.target_device_id)
        return self.require_plex_target()

    def require_wiim_source(self):
        allowed = {device.host for device in self.app.plex.devices.values()}
        if self.client_address[0] not in allowed:
            raise FocusError("Plex media is available only to configured WiiMs")

    @media_traced
    def proxy_plex_media(self, path):
        request = self.app.plex.media_request(
            path, self.headers.get("Range", "")
        )
        started = time.monotonic()
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as exc:
            LOGGER.warning('', extra={'event':'stage_finished', 'stage':'plex_media',
                           'outcome':'failed', 'reason':'http_error', 'status':exc.code,
                           'elapsed_ms':int((time.monotonic()-started)*1000)})
            self.send_body(
                exc.code,
                b"Plex media request failed\n",
                "text/plain; charset=utf-8",
            )
            return
        except (OSError, urllib.error.URLError) as exc:
            LOGGER.warning('', extra={'event':'stage_finished', 'stage':'plex_media',
                           'outcome':'failed', 'reason':failure_reason(exc),
                           'elapsed_ms':int((time.monotonic()-started)*1000)})
            raise FocusError(f"Plex media request failed: {exc}") from exc
        LOGGER.info('', extra={'event':'stage_finished', 'stage':'plex_media',
                    'outcome':'success', 'status':response.status,
                    'elapsed_ms':int((time.monotonic()-started)*1000)})
        with response:
            self.send_response(response.status)
            for name in (
                "Content-Type",
                "Content-Length",
                "Content-Range",
                "Accept-Ranges",
                "ETag",
                "Last-Modified",
            ):
                value = response.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            try:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def proxy_plex_art(self, path):
        request = self.app.plex.art_request(path)
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as exc:
            self.send_body(
                exc.code,
                b"Plex artwork request failed\n",
                "text/plain; charset=utf-8",
            )
            return
        except (OSError, urllib.error.URLError) as exc:
            raise FocusError(f"Plex artwork request failed: {exc}") from exc
        with response:
            self.send_response(response.status)
            for name in ("Content-Type", "Content-Length", "Cache-Control"):
                value = response.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.end_headers()
            try:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def read_form(self):
        length = min(int(self.headers.get("Content-Length", "0")), 16384)
        return urllib.parse.parse_qs(self.rfile.read(length).decode())

    def redirect(self, location):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def send_html(self, status, body, headers=None):
        headers = dict(headers or {})
        if self.authenticated():
            # Upgrade existing Strict cookies before leaving for Plex. Lax permits
            # top-level return GETs while still excluding cross-site POSTs.
            token = SimpleCookie(self.headers.get("Cookie", ""))["wiim_focus_session"].value
            headers["Set-Cookie"] = f"wiim_focus_session={token}; Path=/; HttpOnly; SameSite=Lax"
        style = "<style>:root{color-scheme:dark;font-family:system-ui;background:#111;color:#eee}body{max-width:92rem;margin:4vh auto;padding:1.5rem;background:#1d1d1d}table{width:100%;border-collapse:collapse}th,td{padding:.6rem;border-bottom:1px solid #555;text-align:left;vertical-align:top}input,button{padding:.6rem;margin:.3rem;border:1px solid #666;background:#222;color:#fff;border-radius:.4rem}form{display:inline}.actions{display:flex;flex-wrap:wrap}.policy{display:block}.check{display:inline-block;margin:.5rem 1rem .5rem 0}.notice{padding:.7rem;background:#29364a;border-radius:.4rem}</style>"
        style += "<style>a{color:#a8d5ff}section{border:1px solid #566274;border-radius:.6rem;margin:1rem 0;padding:1rem}button{cursor:pointer}input,button{max-width:100%}</style>"
        self.send_body(status, f"<!doctype html><meta name='viewport' content='width=device-width'><title>WiiM Plex bridge</title>{style}{body}".encode(), "text/html; charset=utf-8", {"Referrer-Policy": "same-origin", **(headers or {})})

    def send_body(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        request_event(LOGGER, getattr(self, "command", ""), args)


def silent_wav():
    # Give the asynchronous topology/playback transaction time to replace or
    # stop this command stream before the WiiM reaches end-of-file.
    samples = b"\x00\x00" * (8000 * 5)
    return b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16) + b"data" + struct.pack("<I", len(samples)) + samples


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def main():
    diagnostics = configure("playback")
    state_path = Path(os.environ.get("STATE_PATH", "/state"))
    config = load_config(required_env("CONFIG_PATH"))
    if config is None:
        raise SystemExit("Setup required: create configuration before starting playback")
    credentials = credential_settings("playback")
    devices = [Device(p.id, p.name, p.host) for p in config.players]
    identity = load_or_create_identity(state_path / "identity.json")
    groups = GroupClient(required_env("GROUP_API_BASE"), credentials["GROUP_API_TOKEN"])
    spotify = SpotifyClient(
        os.environ.get("SPOTIFY_CLIENT_ID", ""),
        os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8794/oauth/callback"),
        state_path / "spotify-token.json",
    )
    plex_client_identifier = identity.installation_id
    plex_public_base = config.advertised_origin
    plex = PlexBridge(
        PlexSessionStore(state_path / "plex-session.json", state_path / "plex-token.json"),
        client_identifier=plex_client_identifier,
        allowed_server_base=config.plex_server_origin,
        devices=devices,
        public_base=plex_public_base,
        media_secret=credentials["PLEX_MEDIA_SECRET"],
        default_device_id=config.default_player_id,
        endpoint_ids={p.id: identity.endpoint_id(p.id) for p in config.players},
    )
    plex_cloud = PlexCloudClient(
        plex_client_identifier,
        plex_public_base,
        state_path / "plex-cloud-token.json",
        endpoint_names={
            identifier: device.name
            for identifier, device in plex.endpoint_devices.items()
        },
    )
    coordinator = Coordinator(
        devices,
        groups,
        spotify,
        EventHistory(state_path / "history.json"),
        plex,
    )
    endpoint_publisher = PlexEndpointPublisher(
        plex,
        groups,
        plex_cloud,
        mode=os.environ.get("PLEX_GROUP_ENDPOINT_MODE", "off").strip().lower(),
    )
    app = App(
        coordinator,
        credentials["WEB_USERNAME"],
        credentials["WEB_PASSWORD_HASH"],
        credentials["WEB_SESSION_SECRET"],
        plex,
        plex_cloud,
    )
    app.onboarding = Onboarding(state_path / "onboarding.json", plex, plex_cloud, groups)
    app.diagnostics = diagnostics
    plex.diagnostics = diagnostics
    diagnostics.enable_persistence(state_path / "diagnostics.json")
    address = (os.environ.get("LISTEN_HOST", "127.0.0.1"), int(os.environ.get("LISTEN_PORT", "8794")))
    threading.Thread(target=app.dashboard_ui.monitor_forever, daemon=True).start()
    threading.Thread(target=plex.monitor_forever, daemon=True).start()
    if endpoint_publisher.mode != "off":
        threading.Thread(
            target=endpoint_publisher.monitor_forever, daemon=True
        ).start()
    if endpoint_publisher.mode != "cloud":
        registration = RegistrationMaintainer(plex_cloud, plex.individual_advertisements)
        threading.Thread(target=registration.monitor_forever, daemon=True).start()
    LOGGER.info("listening on %s:%s", *address)
    companion = CompanionRelay(plex, plex_cloud, config.plex_server_origin)
    threading.Thread(target=companion.monitor_forever, daemon=True).start()
    app.symfonium_proxy = SymfoniumProxy(config.plex_server_origin, plex.public_base)
    Server(address, app).serve_forever()


if __name__ == "__main__":
    main()
