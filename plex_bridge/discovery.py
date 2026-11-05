"""Bounded, read-only IPv4 SSDP discovery. Never enroll or control a player."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import socket
import time
from urllib.parse import urlsplit
import urllib.request
import xml.etree.ElementTree as ET


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def location(packet, peer):
    """Only fetch a literal private address that actually answered discovery."""
    address = ipaddress.IPv4Address(peer)
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError("Not a LAN responder")
    lines = packet.decode("utf-8", "replace").split("\r\n")
    if not lines[0].startswith("HTTP/1.1 200"):
        raise ValueError("Not a discovery response")
    headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
    url = next(v.strip() for k, v in headers.items() if k.lower() == "location")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != peer or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.query
            or any(c.isspace() for c in url) or not 1 <= (parsed.port or 80) <= 65535):
        raise ValueError("Unsafe discovery location")
    return url


def describe(host, url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=2) as response:
            body = response.read(65537)
        if len(body) > 65536 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            return None
        root = ET.fromstring(body)
        for device in root.iter():
            if device.tag.rsplit("}", 1)[-1] != "device":
                continue
            fields = {n.tag.rsplit("}", 1)[-1]: (n.text or "").strip() for n in device}
            model = fields.get("modelName", "")
            vendor = fields.get("manufacturer", "")
            if "plex" in (model + " " + vendor).lower() and "MediaServer" in fields.get("deviceType", ""):
                origin = "http://" + host + ":" + str(urlsplit(url).port or 80)
                with opener.open(origin + "/identity", timeout=2) as response:
                    identity = ET.fromstring(response.read(65536))
                if identity.tag != "MediaContainer" or not identity.get("machineIdentifier"):
                    return None
                return {"kind": "plex", "host": host, "origin": origin,
                        "name": fields.get("friendlyName", "Plex server")[:128],
                        "model": "Plex Media Server"}
            if "wiim" not in (model + " " + vendor).lower():
                continue  # Do not label every Linkplay/UPnP device as a WiiM.
            if "MediaRenderer" not in fields.get("deviceType", ""):
                continue
            name = fields.get("friendlyName", "") or model
            if any(ord(c) < 32 for c in name + model) or not name or len(name) > 128:
                return None
            return {"kind": "wiim", "host": host, "name": name, "model": model[:128]}
    except (OSError, ValueError, ET.ParseError):
        return None


def discover_ssdp(bind_host):
    request = (b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
               b'MAN: "ssdp:discover"\r\nMX: 2\r\n'
               b'ST: ssdp:all\r\n\r\n')
    candidates = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((bind_host, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_host))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.sendto(request, ("239.255.255.250", 1900))
        deadline = time.monotonic() + 3
        while len(candidates) < 16 and time.monotonic() < deadline:
            sock.settimeout(max(.01, deadline - time.monotonic()))
            try:
                packet, peer = sock.recvfrom(8192)
                candidates.setdefault(peer[0], location(packet, peer[0]))
            except socket.timeout:
                break
            except (ValueError, StopIteration):
                continue
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda item: describe(*item), candidates.items()))
    return sorted((r for r in results if r), key=lambda r: (r["name"].casefold(), r["host"]))


def gdm_candidate(packet, peer):
    address = ipaddress.IPv4Address(peer)
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError("Not a LAN responder")
    lines = packet.decode("utf-8", "replace").splitlines()
    if not lines or "200 OK" not in lines[0]:
        raise ValueError("Not a discovery response")
    fields = {k.lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
    port = int(fields.get("port", "0"))
    if fields.get("content-type") != "plex/media-server" or not 1 <= port <= 65535:
        raise ValueError("Not a Plex server")
    return {"kind": "plex", "host": peer, "origin": "http://" + peer + ":" + str(port),
            "name": fields.get("name", "Plex server")[:128], "model": "Plex Media Server"}


def verify_plex(item):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(item["origin"] + "/identity", timeout=2) as response:
            body = response.read(65537)
        if len(body) > 65536 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            return None
        root = ET.fromstring(body)
        if root.tag == "MediaContainer" and root.get("machineIdentifier"):
            return item
    except (OSError, ValueError, ET.ParseError):
        pass
    return None


def discover_plex(bind_host):
    candidates = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((bind_host, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_host))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.sendto(b"M-SEARCH * HTTP/1.0", ("239.0.0.250", 32414))
        deadline = time.monotonic() + 3
        while len(candidates) < 16 and time.monotonic() < deadline:
            sock.settimeout(max(.01, deadline - time.monotonic()))
            try:
                packet, peer = sock.recvfrom(8192)
                item = gdm_candidate(packet, peer[0])
                candidates[item["origin"]] = item
            except socket.timeout:
                break
            except ValueError:
                continue
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [item for item in pool.map(verify_plex, candidates.values()) if item]


def discover(bind_host):
    # Plex's own discovery does not require its optional DLNA server.
    with ThreadPoolExecutor(max_workers=2) as pool:
        scans = [pool.submit(fn, bind_host) for fn in (discover_ssdp, discover_plex)]
        found = {}
        successes = 0
        for scan in scans:
            try:
                for item in scan.result():
                    found[(item["kind"], item.get("origin", item["host"]))] = item
                successes += 1
            except OSError:
                continue
        if not successes:
            raise OSError("LAN discovery unavailable")
    return sorted(found.values(), key=lambda r: (r["kind"], r["name"].casefold(), r["host"]))
