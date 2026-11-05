"""Publish scoped runtime configuration and wait for complete provisioning."""

import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time

from plex_bridge.config import ConfigError, parse_config
from plex_bridge.provisioning import atomic_write, read_json
from plex_bridge.diagnostics import configure


SERVICES = ("playback", "topology")


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def publish(provisioner, exports, handoff):
    """Write all outputs before publishing a shared immutable readiness marker.

    Runtime mounts exports/<service> only; neither service can read setup state
    or the other service's credentials. Repeated publication must be identical.
    """
    exports, handoff = Path(exports), Path(handoff)
    with provisioner.locked():
        state = provisioner.state()
        if state["stage"] != "complete":
            return False
        parse_config(state["config"])
        payloads = {}
        for service in SERVICES:
            directory = exports / service
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            payloads[directory / "config.json"] = state["config"]
            payloads[directory / "credentials.json"] = state["credentials"][service]
        for path, value in payloads.items():
            if path.exists() and read_json(path) != value:
                raise ConfigError("Runtime output conflicts with completed setup; restore compatible files")
        for path, value in payloads.items():
            if not path.exists():
                atomic_write(path, value)
        manifest = {"schema_version": 1, "services": {
            service: {name: file_digest(exports / service / name)
                      for name in ("config.json", "credentials.json")}
            for service in SERVICES}}
        handoff.mkdir(parents=True, exist_ok=True, mode=0o700)
        ready = handoff / "ready.json"
        if ready.exists():
            if read_json(ready) != manifest:
                raise ConfigError("Runtime readiness marker differs; restore compatible files")
        else:
            atomic_write(ready, manifest)
        return True


def configured_exports():
    exports = os.environ.get("RUNTIME_EXPORTS")
    handoff = os.environ.get("HANDOFF_PATH")
    if bool(exports) != bool(handoff):
        raise ConfigError("Set both runtime export and handoff paths")
    return (exports, handoff) if exports else None


def publish_configured(provisioner):
    paths = configured_exports()
    return publish(provisioner, *paths) if paths else False


def is_ready(service, directory, handoff):
    if service not in SERVICES:
        raise ConfigError("Unknown runtime service")
    marker = Path(handoff) / "ready.json"
    if not marker.exists():
        return False
    manifest = read_json(marker)
    try:
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
            raise ValueError
        expected = manifest["services"][service]
        for name in ("config.json", "credentials.json"):
            if file_digest(Path(directory) / name) != expected[name]:
                raise ValueError
        parse_config(read_json(Path(directory) / "config.json"))
    except (KeyError, TypeError, ValueError, OSError):
        raise ConfigError("Runtime files do not match completed setup; restore compatible files") from None
    return True


def main():
    configure("runtime")
    service = os.environ.get("BRIDGE_SERVICE", "")
    if service not in SERVICES:
        raise SystemExit("BRIDGE_SERVICE must be playback or topology")
    directory = os.environ.get("RUNTIME_PATH", "/config")
    handoff = os.environ.get("HANDOFF_PATH", "/handoff")
    waiting = Path(os.environ.get("WAITING_PATH", "/tmp/bridge-waiting.json"))
    atomic_write(waiting, {"pid": os.getpid()})
    logging.getLogger("plex_bridge").info("", extra={"event": "setup_wait"})
    try:
        while not is_ready(service, directory, handoff):
            time.sleep(1)
    finally:
        waiting.unlink(missing_ok=True)
    os.environ["CONFIG_PATH"] = str(Path(directory) / "config.json")
    os.environ["CREDENTIALS_PATH"] = str(Path(directory) / "credentials.json")
    logging.getLogger("plex_bridge").info("", extra={"event": "setup_ready"})
    os.execv(sys.executable, [sys.executable, "-m", "plex_bridge." + service])


if __name__ == "__main__":
    main()
