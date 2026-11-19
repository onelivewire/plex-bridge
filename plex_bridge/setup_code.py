"""Display a pending local claim code explicitly; never write it to service logs."""
import os
from pathlib import Path
import sys

from plex_bridge.provisioning import read_json


def pending_code(root):
    root = Path(root)
    if read_json(root / "setup.json").get("stage") == "complete":
        raise ValueError("Setup is complete; use your administrator login")
    code = read_json(root / "setup-code.json").get("code")
    if not isinstance(code, str) or not 32 <= len(code) <= 256:
        raise ValueError("No valid setup code; restore the private setup backup")
    return code


def main():
    root = os.environ.get("SETUP_PATH") or str(Path(os.environ.get("BRIDGE_DATA_PATH_INTERNAL", "/data")) / "setup")
    try:
        print(pending_code(root))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
