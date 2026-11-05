"""Resumable local provisioning. This directory is private administrator data."""

import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import uuid
from contextlib import contextmanager

from plex_bridge.config import ConfigError, parse_config


def atomic_write(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".provision-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        raise ConfigError("Saved setup data is unreadable; restore its backup before continuing") from None


class Provisioner:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        with self.locked():
            path = self.root / "setup.json"
            if not path.exists():
                # Never claim an existing deployment whose setup record is missing.
                if any((self.root / name).exists() for name in
                       ("config.json", "playback-credentials.json", "topology-credentials.json")):
                    raise ConfigError("Existing service configuration found; restore setup state")
                code_path = self.root / "setup-code.json"
                if code_path.exists():
                    saved = read_json(code_path)
                    if not isinstance(saved, dict) or not isinstance(saved.get("code"), str) or len(saved["code"]) < 32:
                        raise ConfigError("Setup code file is invalid; restore its backup")
                    code = saved["code"]
                else:
                    code = secrets.token_urlsafe(32)
                    atomic_write(code_path, {"code": code})
                atomic_write(path, {"schema_version": 1, "stage": "administrator",
                                   "code_hash": self.digest(code)})
            self.state()

    @contextmanager
    def locked(self):
        fd = os.open(self.root / ".setup.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def state(self):
        state = read_json(self.root / "setup.json")
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                or state["schema_version"] != 1 or state.get("stage") not in
                {"administrator", "players", "review", "complete"}):
            raise ConfigError("Unsupported setup state; restore a compatible backup")
        return state

    def save(self, state):
        atomic_write(self.root / "setup.json", state)

    def claim(self, code):
        with self.locked():
            state = self.state()
            if state["stage"] == "complete" or not hmac.compare_digest(
                    state.get("code_hash", ""), self.digest(code)):
                raise ConfigError("Setup code is invalid or setup is already complete")
            token = secrets.token_urlsafe(32)
            state["session_hash"] = self.digest(token)
            state["session_expires"] = time.time() + 3600
            self.save(state)
            return token

    def authorize(self, state, token):
        if (not token or state.get("session_expires", 0) <= time.time()
                or not hmac.compare_digest(state.get("session_hash", ""), self.digest(token))):
            raise ConfigError("Setup session expired; enter the setup code again")

    def view(self, token):
        with self.locked():
            state = self.state()
            self.authorize(state, token)
            return {"stage": state["stage"], "config": state.get("config"),
                    "username": state.get("username", ""), "draft": state.get("draft", {}),
                    "discovery": state.get("discovery", {})}

    def discovery_start(self, token, draft):
        with self.locked():
            state = self.state()
            self.authorize(state, token)
            if state["stage"] not in {"players", "review"}:
                raise ConfigError("Complete the administrator step first")
            state.update(draft=draft, discovery={"status": "scanning", "results": []})
            self.save(state)

    def discovery_finish(self, results, status="complete"):
        with self.locked():
            state = self.state()
            state["discovery"] = {"status": status, "results": results}
            self.save(state)

    def administrator(self, token, username, password):
        with self.locked():
            state = self.state()
            self.authorize(state, token)
            if state["stage"] != "administrator":
                raise ConfigError("Administrator already saved; continue the current setup step")
            if not username.strip() or len(username) > 128 or any(ord(c) < 32 for c in username):
                raise ConfigError("Enter a valid administrator name")
            if not 12 <= len(password) <= 1024:
                raise ConfigError("Use an administrator password of 12–1024 characters")
            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000).hex()
            password_hash = f"pbkdf2-sha256:600000:{salt.hex()}:{digest}"
            shared_token = secrets.token_urlsafe(32)
            common = {"WEB_USERNAME": username.strip(), "WEB_PASSWORD_HASH": password_hash}
            state.update(username=username.strip(), stage="players", credentials={
                "playback": dict(common, WEB_SESSION_SECRET=secrets.token_urlsafe(32),
                                 GROUP_API_TOKEN=shared_token, PLEX_MEDIA_SECRET=secrets.token_urlsafe(32)),
                "topology": dict(common, WEB_SESSION_SECRET=secrets.token_urlsafe(32),
                                 SERVICE_API_TOKEN=shared_token)})
            self.save(state)

    def configure(self, token, advertised_origin, plex_server_origin, rooms):
        """rooms is user-entered Name, IP per line. First player is the default."""
        with self.locked():
            state = self.state()
            self.authorize(state, token)
            if state["stage"] not in {"players", "review"}:
                raise ConfigError("Complete the administrator step first")
            old = {p["host"]: p["id"] for p in state.get("config", {}).get("players", [])}
            players = []
            for line in rooms.splitlines():
                if not line.strip():
                    continue
                if "," not in line:
                    raise ConfigError("Enter each player as Name, IP address on its own line")
                name, host = (part.strip() for part in line.rsplit(",", 1))
                # Canonical address prevents accidental identity rotation on IPv6 spelling changes.
                from plex_bridge.config import _address
                host = _address(host, "player address")
                players.append({"id": old.get(host, "player-" + uuid.uuid4().hex),
                                "name": name, "host": host, "auto_join_target": False})
            raw = {"schema_version": 1, "advertised_origin": advertised_origin,
                   "plex_server_origin": plex_server_origin,
                   "default_player_id": players[0]["id"] if players else "", "players": players}
            config = parse_config(raw)
            raw.update(advertised_origin=config.advertised_origin,
                       plex_server_origin=config.plex_server_origin)
            state.update(stage="review", config=raw, draft={})
            self.save(state)

    def finish(self, token):
        with self.locked():
            state = self.state()
            self.authorize(state, token)
            if state["stage"] not in {"review", "complete"}:
                raise ConfigError("Review the player configuration before finishing")
            parse_config(state["config"])
            outputs = {"config.json": state["config"],
                       "playback-credentials.json": state["credentials"]["playback"],
                       "topology-credentials.json": state["credentials"]["topology"]}
            # Check every existing output before writing any. A interrupted commit
            # can resume, but unrelated edits cannot be silently overwritten.
            for name, value in outputs.items():
                path = self.root / name
                if path.exists() and read_json(path) != value:
                    raise ConfigError("Existing output differs from saved setup; restore or reconcile it first")
            for name, value in outputs.items():
                path = self.root / name
                if not path.exists():
                    atomic_write(path, value)
            state.update(stage="complete", code_hash="")
            self.save(state)
            # Saved code is now invalid, but retained as evidence until private
            # setup state is explicitly cleaned up by a later lifecycle operation.


def credential_settings(service):
    """A service reads only its own credential file; explicit env wins."""
    path = os.environ.get("CREDENTIALS_PATH")
    if path:
        setup_path = Path(path).parent / "setup.json"
        if setup_path.exists():
            saved = read_json(setup_path)
            if not isinstance(saved, dict) or saved.get("stage") != "complete":
                raise ConfigError("Finish local setup before starting services")
    values = read_json(path) if path else {}
    required = {"WEB_USERNAME", "WEB_PASSWORD_HASH", "WEB_SESSION_SECRET"}
    required |= {"GROUP_API_TOKEN", "PLEX_MEDIA_SECRET"} if service == "playback" else {"SERVICE_API_TOKEN"}
    if not isinstance(values, dict) or set(values) - required:
        raise ConfigError("Credential file contains unexpected fields")
    result = {key: os.environ.get(key, values.get(key)) for key in required}
    if any(not isinstance(value, str) or not value for value in result.values()):
        raise ConfigError("Complete setup or provide all required service credentials")
    return result
