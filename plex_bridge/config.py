"""Versioned non-secret configuration and persistent installation identity.

No discovery, network access, or player mutations happen in this module.
"""

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import uuid


class ConfigError(ValueError):
    """Safe for display: messages identify fields without echoing values."""


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ConfigError(f"{label}: expected an object with recognized fields")


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{label}: expected non-empty text without control characters")
    return value.strip()


def _address(value, label):
    value = _text(value, label)
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ConfigError(f"{label}: enter a literal LAN IP address") from None
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
        raise ConfigError(f"{label}: requires a reachable unicast LAN address")
    # Explicit enrollment authorizes the address. No DNS or reachability claims.
    return str(address)


def _origin(value, label):
    value = _text(value, label)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or "?" in value or "#" in value):
            raise ValueError
        host = _address(parsed.hostname, label)
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, ConfigError):
        raise ConfigError(f"{label}: expected an HTTP(S) LAN IP origin without credentials, path or query") from None
    host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    host: str
    auto_join_target: bool = False


@dataclass(frozen=True)
class Configuration:
    schema_version: int
    advertised_origin: str
    plex_server_origin: str
    default_player_id: str
    players: tuple[Player, ...]

    def preset_url(self, action):
        if action not in {"focus", "disband"}:
            raise ConfigError("preset action: unsupported action")
        return f"{self.advertised_origin}/preset/{action}"


def parse_config(raw):
    _object(raw, {"schema_version", "advertised_origin", "plex_server_origin",
                  "default_player_id", "players"}, "configuration")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ConfigError("schema_version: only version 1 is supported")
    inventory = raw.get("players")
    if not isinstance(inventory, list) or not inventory:
        raise ConfigError("players: enroll at least one player")
    players, ids, hosts = [], set(), set()
    for index, item in enumerate(inventory):
        label = f"players[{index}]"
        _object(item, {"id", "name", "host", "auto_join_target"}, label)
        player_id = _text(item.get("id"), label + ".id")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", player_id):
            raise ConfigError(label + ".id: use 1–64 lowercase letters, digits, underscores or hyphens")
        name = _text(item.get("name"), label + ".name")
        host = _address(item.get("host"), label + ".host")
        eligible = item.get("auto_join_target", False)
        if type(eligible) is not bool:
            raise ConfigError(label + ".auto_join_target: expected true or false")
        if player_id in ids or host in hosts:
            raise ConfigError(label + ": duplicate player ID or address")
        players.append(Player(player_id, name, host, eligible))
        ids.add(player_id)
        hosts.add(host)
    default = _text(raw.get("default_player_id"), "default_player_id")
    if default not in ids:
        raise ConfigError("default_player_id: must identify an enrolled player")
    return Configuration(1, _origin(raw.get("advertised_origin"), "advertised_origin"),
                         _origin(raw.get("plex_server_origin"), "plex_server_origin"),
                         default, tuple(players))


def load_config(path):
    """None means setup is needed; malformed existing config is an error."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ConfigError("configuration: could not read valid JSON") from None
    return parse_config(raw)


@dataclass(frozen=True)
class Identity:
    installation_id: str

    def endpoint_id(self, player_id):
        # Display names and addresses never participate in endpoint identity.
        return str(uuid.uuid5(uuid.UUID(self.installation_id), player_id))


def load_or_create_identity(path):
    """Publish a complete identity atomically; concurrent creators converge.

    Uses a same-directory hardlink for create-if-absent on the Linux target.
    An existing invalid identity is never silently replaced.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "installation_id": str(uuid.uuid4())}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        _object(raw, {"schema_version", "installation_id"}, "identity")
        if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
            raise ValueError
        identifier = str(uuid.UUID(raw["installation_id"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ConfigError("identity: unreadable or unsupported; restore the saved identity before continuing") from None
    return Identity(identifier)
