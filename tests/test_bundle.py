import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from plex_bridge.bundle import SERVICES, environments, supervise
from plex_bridge import healthcheck


class BundleTests(unittest.TestCase):
    def test_paths_and_wait_markers_are_distinct_and_fixed(self):
        envs = environments({'BRIDGE_HOST': '192.0.2.10', 'CONFIG_PATH': '/wrong', 'STATE_PATH': '/wrong'})
        self.assertEqual(envs['setup']['SETUP_PATH'], '/data/setup')
        self.assertEqual(envs['playback']['STATE_PATH'], '/data/playback')
        self.assertEqual(envs['topology']['HISTORY_PATH'], '/data/topology/history.json')
        self.assertNotEqual(envs['topology']['WAITING_PATH'], envs['playback']['WAITING_PATH'])
        self.assertTrue(all('CONFIG_PATH' not in e for e in envs.values()))
        with self.assertRaises(ValueError): environments({'BRIDGE_HOST': 'http://wrong'})

    def test_child_exit_stops_and_reaps_remaining_children(self):
        real = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = real(*args, **kwargs); children.append(child); return child
        commands = {s: [sys.executable, '-c', 'import time; time.sleep(30)'] for s in SERVICES}
        commands['topology'] = [sys.executable, '-c', 'raise SystemExit(0)']
        with patch('plex_bridge.bundle.subprocess.Popen', side_effect=spawn):
            self.assertEqual(supervise(commands, {s: dict(os.environ) for s in SERVICES}, lambda: False), 1)
        self.assertEqual(len(children), 3)
        self.assertTrue(all(c.poll() is not None for c in children))

    def test_shutdown_kills_unresponsive_children_within_deadline(self):
        real = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = real(*args, **kwargs); children.append(child); return child
        commands = {s: [sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'] for s in SERVICES}
        start = time.monotonic()
        with patch('plex_bridge.bundle.subprocess.Popen', side_effect=spawn):
            result = supervise(commands, {s: dict(os.environ) for s in SERVICES},
                               lambda: time.monotonic()-start > 0.5, grace=0.2)
        self.assertEqual(result, 0)
        self.assertLess(time.monotonic()-start, 5)
        self.assertTrue(all(c.poll() is not None for c in children))

    def test_spawn_failure_still_stops_already_started_child(self):
        real = subprocess.Popen
        child = real([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        with patch('plex_bridge.bundle.subprocess.Popen', side_effect=[child, OSError('synthetic')]):
            with self.assertRaises(OSError):
                supervise({s: [] for s in SERVICES}, {s: {} for s in SERVICES}, lambda: False)
        self.assertIsNotNone(child.poll())

    def test_bundle_health_checks_each_service_and_propagates_failure(self):
        env = {'BRIDGE_HOST': '192.0.2.10', 'BRIDGE_SERVICE': 'bundle'}
        with patch.dict(os.environ, env, clear=True), patch.object(healthcheck, 'check') as check:
            healthcheck.main()
            self.assertEqual([c.args[0]['BRIDGE_SERVICE'] for c in check.call_args_list], list(SERVICES))
            check.side_effect = [None, OSError('synthetic')]
            with self.assertRaises(OSError): healthcheck.main()

    def test_dead_waiter_is_unhealthy_instead_of_masking_other_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp)/'wait.json'; marker.write_text('{"pid": 123}')
            with patch('plex_bridge.healthcheck.os.kill', side_effect=ProcessLookupError):
                with self.assertRaises(ProcessLookupError): healthcheck.check({'WAITING_PATH': str(marker)})
