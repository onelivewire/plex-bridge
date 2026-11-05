"""Synthetic acceptance URL validation helpers; no device commands."""
import ipaddress
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

def plex_location(value):
    """Validate an explicitly approved IPv4 origin without echoing its contents."""
    try:
        parsed = urllib.parse.urlsplit(value)
        address = ipaddress.IPv4Address(parsed.hostname)
        if (parsed.scheme not in ("http", "https") or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or "?" in value or "#" in value or any(c.isspace() for c in value)
                or address.is_loopback or address.is_unspecified or address.is_multicast
                or address.is_link_local):
            raise ValueError
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        origin = parsed.scheme + "://" + str(address) + ":" + str(port)
        return origin, {"protocol": parsed.scheme, "address": str(address), "port": str(port)}
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Plex origin must be an HTTP(S) IPv4 origin without credentials, path or query") from None


def approved_plex_location(session_origin, explicit_origin):
    origin, params = plex_location(explicit_origin)
    if plex_location(session_origin)[0] != origin:
        raise ValueError("Saved queue belongs to a different Plex origin; refresh the queue before testing")
    return origin, params


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def plex_identity(origin):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(origin + "/identity", timeout=10) as response:
        root = ET.fromstring(response.read(65536))
    if root.tag != "MediaContainer" or not root.get("machineIdentifier"):
        raise ValueError("Plex identity check did not return a server identity")
    return {"machineIdentifier": root.get("machineIdentifier"), "version": root.get("version", "")}

