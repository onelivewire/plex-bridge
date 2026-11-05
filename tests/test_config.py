import concurrent.futures
import copy
import json
from pathlib import Path
import stat
import tempfile
import unittest

from plex_bridge.config import ConfigError, load_config, load_or_create_identity, parse_config


EXAMPLE = Path(__file__).parents[1] / "examples" / "config.json"


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(EXAMPLE.read_text())

    def test_portable_url_and_safe_policy_default(self):
        del self.raw["players"][0]["auto_join_target"]
        config = parse_config(self.raw)
        self.assertEqual(config.preset_url("focus"), "http://192.0.2.10:8794/preset/focus")
        self.assertFalse(config.players[0].auto_join_target)
        with self.assertRaises(ConfigError):
            config.preset_url("../login")

    def test_missing_file_enters_setup_but_invalid_file_does_not(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            self.assertIsNone(load_config(path))
            for content in ("", "{}", "null", "[]", "not JSON"):
                path.write_text(content)
                with self.assertRaises(ConfigError):
                    load_config(path)

    def test_schema_inventory_and_default_validation(self):
        for key, value in (("schema_version", True), ("schema_version", 2),
                           ("players", []), ("players", {}), ("default_player_id", "unknown")):
            with self.subTest(key=key, value=value):
                raw = copy.deepcopy(self.raw)
                raw[key] = value
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_duplicate_ids_and_addresses(self):
        for field in ("id", "host"):
            other = {"id": "player-b", "name": "Study", "host": "192.0.2.31"}
            other[field] = self.raw["players"][0][field]
            raw = copy.deepcopy(self.raw)
            raw["players"].append(other)
            with self.assertRaises(ConfigError):
                parse_config(raw)

    def test_string_false_is_rejected(self):
        self.raw["players"][0]["auto_join_target"] = "false"
        with self.assertRaises(ConfigError):
            parse_config(self.raw)

    def test_credential_and_malformed_urls_are_rejected_without_echo(self):
        for value in ("http://user:secret@192.0.2.10", "http://192.0.2.10/?token=secret",
                      "http://192.0.2.10/path", "http://192.0.2.10:0", "http://127.0.0.1",
                      "http://0.0.0.0", "http://[broken", "file:///secret"):
            self.raw["advertised_origin"] = value
            with self.assertRaises(ConfigError) as raised:
                parse_config(self.raw)
            self.assertNotIn("secret", str(raised.exception))

    def test_ipv6_canonicalization_detects_duplicates(self):
        self.raw["advertised_origin"] = "http://[2001:db8::10]:8794/"
        self.assertEqual(parse_config(self.raw).advertised_origin, "http://[2001:db8::10]:8794")
        self.raw["players"][0]["host"] = "2001:db8::30"
        self.raw["players"].append({"id": "player-b", "name": "Study", "host": "2001:0db8:0:0:0:0:0:30"})
        with self.assertRaises(ConfigError):
            parse_config(self.raw)


class IdentityTests(unittest.TestCase):
    def test_restart_rename_and_separate_installations(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "identity.json"
            original = load_or_create_identity(path)
            self.assertEqual(original, load_or_create_identity(path))
            raw = json.loads(EXAMPLE.read_text())
            before = original.endpoint_id(parse_config(raw).players[0].id)
            raw["players"][0].update(name="Renamed room", host="192.0.2.99")
            self.assertEqual(before, original.endpoint_id(parse_config(raw).players[0].id))
            other = load_or_create_identity(Path(root) / "other.json")
            self.assertNotEqual(before, other.endpoint_id("player-a"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_concurrent_creation_converges(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "identity.json"
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                identities = list(pool.map(lambda _: load_or_create_identity(path), range(24)))
            self.assertEqual(len(set(identities)), 1)
            self.assertEqual(list(Path(root).glob(".identity-*")), [])

    def test_invalid_identity_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "identity.json"
            path.write_text('{"schema_version":2}')
            with self.assertRaises(ConfigError):
                load_or_create_identity(path)
            self.assertEqual(path.read_text(), '{"schema_version":2}')
