"""Dedicated Plex connection for Symfonium controllers using bridge playback.

Only this URL namespace suppresses listen reports. Upstream credentials belong
 to the caller; the proxy never substitutes the bridge's Plex credentials.
"""
import http.client
import logging
import urllib.parse
from plex_bridge.diagnostics import failure_reason

LOGGER = logging.getLogger("wiim-focus-control")
PREFIX = "/symfonium"
REPORT_PATHS = {"/:/timeline", "/:/scrobble"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host"}
MAX_BODY = 2 * 1024 * 1024


class SymfoniumProxy:
    def __init__(self, origin, public_origin):
        self.origin = urllib.parse.urlsplit(origin)
        self.public = urllib.parse.urlsplit(public_origin)
        if self.origin.scheme not in {"http", "https"} or not self.origin.hostname:
            raise ValueError("Plex proxy requires a fixed HTTP origin")

    def target(self, raw_path):
        parsed = urllib.parse.urlsplit(raw_path)
        path = parsed.path[len(PREFIX):] or "/"
        decoded = urllib.parse.unquote(path)
        if (parsed.scheme or parsed.netloc or not parsed.path.startswith(PREFIX + "/")
                or decoded.startswith("//") or "\\" in decoded
                or any(p in {".", ".."} for p in decoded.split("/"))):
            raise ValueError("Invalid proxy path")
        # A controller may name its configured proxy as the media server. Route
        # that command back to the actual fixed PMS origin for bridge validation.
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        values = dict(query)
        if decoded == "/player/playback/playMedia" and values.get("address") == self.public.hostname:
            values.update(protocol=self.origin.scheme, address=self.origin.hostname,
                          port=str(self.origin.port or (443 if self.origin.scheme == "https" else 80)))
            query = [(k, values[k]) for k, _ in query]
        return path + ("?" + urllib.parse.urlencode(query) if query else ""), decoded

    def handle(self, handler):
        try:
            target, path = self.target(handler.path)
            if handler.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked request bodies are not supported")
            size = int(handler.headers.get("Content-Length", "0"))
            if size < 0 or size > MAX_BODY:
                raise ValueError("Request body too large")
        except (ValueError, TypeError):
            handler.close_connection = True
            handler.send_body(400, b"Invalid proxy request\n", "text/plain")
            return
        body = handler.rfile.read(size) if size else None
        query = urllib.parse.parse_qsl(urllib.parse.urlsplit(target).query, keep_blank_values=True)
        values = dict(query)
        token = handler.headers.get("X-Plex-Token", "") or values.get("X-Plex-Token", "")
        usable_token = bool(token and token.lower() not in {"null", "undefined", "none"})
        # PMS may trust the NAS address without authentication. Do not let that
        # shortcut leave a controller with a literal null token for cast commands.
        if path != "/identity" and not usable_token:
            LOGGER.info("", extra={"event": "proxy_auth_required", "status": 401})
            handler.send_body(401, b"Plex authentication required\n", "text/plain")
            return
        if path == "/player/playback/playMedia" and values.get("token", "").lower() in {"", "null", "undefined", "none"}:
            query = [(k, v) for k, v in query if k != "token"]
            query.append(("token", token))
            target = urllib.parse.urlsplit(target).path + "?" + urllib.parse.urlencode(query)
            LOGGER.info("", extra={"event": "proxy_media_token_repaired"})
        if path in REPORT_PATHS and handler.command in {"GET", "POST", "PUT"}:
            LOGGER.info("", extra={"event": "proxy_report_suppressed", "method": handler.command})
            handler.send_body(200, b'<MediaContainer size="0" />', "application/xml")
            return
        if handler.headers.get("Upgrade"):
            handler.send_body(426, b"Use HTTP polling\n", "text/plain")
            return
        if handler.command not in {"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"}:
            handler.send_body(405, b"Method not supported\n", "text/plain")
            return
        connection_headers = {s.strip().lower() for s in handler.headers.get("Connection", "").split(",")}
        headers = {k: v for k, v in handler.headers.items()
                   if k.lower() not in HOP_HEADERS | connection_headers | {"cookie", "content-length"}}
        headers["Connection"] = "close"
        connection_type = http.client.HTTPSConnection if self.origin.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(self.origin.hostname, self.origin.port, timeout=40)
        started = False
        try:
            connection.request(handler.command, target, body=body, headers=headers)
            response = connection.getresponse()
            if response.status >= 400:
                LOGGER.warning("", extra={"event": "proxy_forward_failed", "status": response.status,
                                         "reason": "http_error"})
            # Never follow upstream redirects with the caller's token. Relative
            # same-server redirects retain this namespace so reporting cannot bypass it.
            handler.send_response(response.status)
            started = True
            response_connection = {s.strip().lower() for s in (response.getheader("Connection") or "").split(",")}
            for name, value in response.getheaders():
                if name.lower() in HOP_HEADERS | response_connection | {"set-cookie"}:
                    continue
                if name.lower() == "location":
                    redirect = urllib.parse.urlsplit(value)
                    if not redirect.netloc and value.startswith("/") and not value.startswith("//"):
                        value = PREFIX + value
                    else:
                        # Do not redirect clients outside the fixed-origin proxy.
                        continue
                handler.send_header(name, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            if handler.command != "HEAD":
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        except (OSError, http.client.HTTPException) as exc:
            LOGGER.warning("", extra={"event": "proxy_forward_failed", "reason": failure_reason(exc)})
            if not started:
                handler.send_body(502, b"Plex upstream unavailable\n", "text/plain")
            handler.close_connection = True
        finally:
            connection.close()
