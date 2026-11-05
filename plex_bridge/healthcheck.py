"""Container liveness; setup waiting is healthy, not playback readiness."""

import os
from pathlib import Path
import urllib.request

from plex_bridge.provisioning import read_json


def main():
    waiting = Path("/tmp/bridge-waiting.json")
    if waiting.exists():
        os.kill(read_json(waiting)["pid"], 0)
        return
    if os.environ.get("BRIDGE_SERVICE") == "setup":
        url = os.environ["SETUP_ORIGIN"] + "/health"
    else:
        url = "http://{}:{}/health".format(os.environ["LISTEN_HOST"], os.environ["LISTEN_PORT"])
    with urllib.request.urlopen(url, timeout=3) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
