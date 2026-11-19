import json
from pathlib import Path
import shlex
import tempfile
import unittest
import xml.etree.ElementTree as ET

from plex_bridge import __version__
from plex_bridge.config import ConfigError
from plex_bridge.provisioning import Provisioner
from plex_bridge.setup_code import pending_code


class InstallationTests(unittest.TestCase):
    def test_private_installer_code_resumes_and_cannot_reset_existing_state(self):
        code = "synthetic-private-install-code-123456789"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            p = Provisioner(root, setup_code=code)
            token = p.claim(code)
            p.administrator(token, "operator", "synthetic-test-password")
            before = (root / "setup.json").read_bytes()
            again = Provisioner(root, setup_code="invalid-new-value")
            self.assertEqual((root / "setup.json").read_bytes(), before)
            self.assertEqual(pending_code(root), code)
            with self.assertRaises(ConfigError):
                again.claim("invalid-new-value")
            again.configure(token, "http://192.0.2.10:8794", "http://192.0.2.20:32400",
                            "Study, 192.0.2.30")
            again.finish(token)
            with self.assertRaises(ValueError):
                pending_code(root)
            with self.assertRaises(ConfigError):
                Provisioner(root, setup_code=code).claim(code)

    def test_invalid_installer_code_is_rejected_without_echo_or_partial_claim(self):
        for code in ("short", "x" * 257, "x" * 32 + " ", "x" * 32 + "\n", "é" * 32):
            with self.subTest(code_length=len(code)), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ConfigError) as raised:
                    Provisioner(temp, setup_code=code)
                self.assertNotIn(code, str(raised.exception))
                self.assertFalse((Path(temp) / "setup.json").exists())
                self.assertFalse((Path(temp) / "setup-code.json").exists())

    def test_generated_code_is_retained_when_new_installer_setting_appears(self):
        with tempfile.TemporaryDirectory() as temp:
            original = pending_code_after_create(temp)
            Provisioner(temp, setup_code="another-private-install-code-123456789")
            self.assertEqual(pending_code(temp), original)
            Provisioner(temp).claim(original)

    def test_public_install_artifacts_agree_on_version_and_security(self):
        root = Path(__file__).resolve().parents[1]
        template = ET.parse(root / "unraid/plex-bridge.xml").getroot()
        image = "ghcr.io/onelivewire/plex-bridge:" + __version__
        self.assertEqual(template.findtext("Repository"), image)
        self.assertEqual(template.findtext("Network"), "host")
        self.assertEqual(template.findtext("Privileged"), "false")
        self.assertEqual(template.findtext("WebUI"), "http://[IP]:8795/")
        args = shlex.split(template.findtext("ExtraParams"))
        self.assertIn("--user=10001:10001", args)
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop=ALL", args)
        mount = args[args.index("--mount") + 1]
        self.assertEqual(mount, "type=volume,source=plex-bridge-data,target=/data")
        fields = {f.get("Target"): f for f in template.findall("Config")}
        self.assertEqual(fields["BRIDGE_SETUP_CODE"].get("Mask"), "true")
        self.assertEqual(fields["BRIDGE_SETUP_CODE"].get("Default"), "")
        self.assertEqual(fields["BRIDGE_HOST"].get("Required"), "true")
        compose = (root / "compose.yaml").read_text()
        self.assertIn("image: ghcr.io/onelivewire/plex-bridge:", compose)
        self.assertIn(__version__, compose)
        self.assertNotIn("    build:", compose)
        self.assertIn("BRIDGE_SETUP_CODE: ${BRIDGE_SETUP_CODE:-}", compose)
        self.assertIn("    build:", (root / "compose.source.yaml").read_text())
        self.assertIn("BRIDGE_VERSION=" + __version__, (root / "deployment.env.example").read_text())
        manifest = json.loads((root / "release-files.json").read_text())
        for name in ("unraid/plex-bridge.xml", "docs/quick-start.md", "compose.source.yaml",
                     "plex_bridge/setup_code.py", "tests/test_installation.py"):
            self.assertIn(name, manifest["files"])


def pending_code_after_create(root):
    Provisioner(root)
    return pending_code(root)
