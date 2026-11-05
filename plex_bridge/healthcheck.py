"""Container liveness; setup waiting is healthy, not playback readiness."""

import os
from pathlib import Path
import urllib.request

from plex_bridge.provisioning import read_json


def check(environ):
    waiting = Path(environ.get("WAITING_PATH", "/tmp/bridge-waiting.json"))
    if waiting.exists():
        os.kill(read_json(waiting)["pid"], 0)
        return
    if environ.get("BRIDGE_SERVICE") == "setup":
        url = environ["SETUP_ORIGIN"] + "/health"
    else:
        url = "http://{}:{}/health".format(environ["LISTEN_HOST"], environ["LISTEN_PORT"])
    with urllib.request.urlopen(url, timeout=3) as response:
        if response.status != 200:
            raise SystemExit(1)


def main():
    if os.environ.get("BRIDGE_SERVICE") == "bundle":
        from plex_bridge.bundle import environments
        for env in environments().values():
            check(env)
    else:
        check(os.environ)


if __name__ == "__main__":
    main()
