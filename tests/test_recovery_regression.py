import json
import logging
import unittest
import urllib.error
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from plex_bridge.diagnostics import Diagnostics, failure_reason
from plex_bridge.playback import WiiMRendererClient, FocusError
import test_dashboard

class RestartRegression(test_dashboard.DashboardTests):
    def test_unknown_command_uses_alternate_once(self):
        self.renderer.command.side_effect = ['unknown command', 'OK']
        self.act('restart', confirm='yes')
        self.assertEqual([c.args[1] for c in self.renderer.command.call_args_list], ['reboot', 'StartRebootTime:1'])

    def test_restart_bypasses_broken_dashboard_snapshot(self):
        self.ui.snapshot.side_effect = RuntimeError('topology timeout')
        self.act('restart', confirm='yes')
        self.ui.snapshot.assert_not_called()
        self.renderer.command.assert_called_once_with(self.devices[1], 'reboot')

    def test_alternate_disconnect_does_not_retry(self):
        self.renderer.command.side_effect = ['unknown command', TimeoutError()]
        self.act('restart', confirm='yes')
        self.assertEqual(self.renderer.command.call_count, 2)
        with self.assertRaises(ValueError): self.act('restart', confirm='yes')
        self.assertEqual(self.renderer.command.call_count, 2)

    def test_monitor_works_without_snapshot_or_topology(self):
        self.app.diagnostics = Diagnostics('playback')
        self.renderer.command.return_value = {'status': 'stop'}
        self.renderer.av_transport.side_effect = RuntimeError('timeout')
        with patch('plex_bridge.room_health.time.monotonic', return_value=100): self.ui.monitor_once()
        with patch('plex_bridge.room_health.time.monotonic', return_value=111): self.ui.monitor_once()
        self.ui.snapshot.assert_not_called()
        self.c.groups.players.assert_not_called()
        self.assertEqual(self.ui.health.rooms['follower']['failures'], 2)
        self.assertTrue(all(c.args[1] == 'getPlayerStatus' for c in self.renderer.command.call_args_list))

class TransportRegression(unittest.TestCase):
    def test_timeout_does_not_fall_back_to_same_dead_service(self):
        renderer = WiiMRendererClient()
        renderer.start_session = Mock()
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError(TimeoutError())):
            with self.assertRaises(FocusError) as caught:
                renderer.start_queue_session(NS(host='192.0.2.1'), NS(current_index=0), [])
        self.assertEqual(failure_reason(caught.exception), 'timeout')
        renderer.start_session.assert_not_called()

    def test_rejection_still_allows_direct_fallback(self):
        renderer = WiiMRendererClient()
        renderer.play_queue = Mock(side_effect=FocusError('unsupported'))
        renderer.queue_context = Mock(return_value='context')
        renderer.start_session = Mock()
        renderer.start_queue_session(NS(), NS(current_index=0), [('item', 'url', 'metadata')])
        renderer.start_session.assert_called_once()

    def test_safe_diagnostic_reason_and_operation(self):
        data = Diagnostics('playback')
        record = logging.LogRecord('test', logging.WARNING, '', 0, 'secret', (), None)
        record.event = 'renderer_request_failed'
        record.reason = 'timeout'
        record.operation = 'CreateQueue'
        row = json.loads(data.record(record))
        self.assertEqual(row['reason'], 'timeout')
        self.assertEqual(row['operation'], 'CreateQueue')
        record.reason = record.operation = 'private-token'
        serialized = data.record(record)
        self.assertNotIn('private-token', serialized)
        self.assertNotIn('secret', serialized)
