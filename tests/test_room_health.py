import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
from plex_bridge.room_health import RoomHealth


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.h = RoomHealth()
        self.d = NS(id='room')
        self.r = Mock()
        self.r.av_transport.return_value = ET.fromstring('<r><CurrentTransportState>STOPPED</CurrentTransportState></r>')
        self.clock = patch('plex_bridge.room_health.time.monotonic', return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def check(self, when, reachable=True):
        self.now.return_value = when
        return self.h.check(self.d, reachable, self.r)

    def test_repeat_failures_are_spaced_and_recovery_clears(self):
        self.r.av_transport.side_effect = RuntimeError('private error')
        self.assertFalse(self.check(100)['degraded'])
        self.assertFalse(self.check(101)['degraded'])
        self.assertEqual(self.r.av_transport.call_count, 1)
        self.assertTrue(self.check(110)['degraded'])
        self.r.av_transport.side_effect = None
        self.assertFalse(self.check(120)['degraded'])
        self.r.command.assert_not_called()

    def test_invalid_response_is_not_healthy(self):
        self.r.av_transport.return_value = ET.fromstring('<r/>')
        self.check(100)
        self.assertTrue(self.check(110)['degraded'])

    def test_restart_requires_recovery_and_blocks_duplicate(self):
        self.h.begin_restart('room')
        with self.assertRaises(ValueError): self.h.begin_restart('room')
        self.assertTrue(self.check(101, False)['restarting'])
        self.assertTrue(self.check(111)['restarting'])
        state = self.check(121)
        self.assertFalse(state['restarting'])
        self.assertIn('Restart verified', state['message'])
        self.assertFalse(state['restart_allowed'])

    def test_no_observed_reboot_is_not_claimed_verified(self):
        self.h.begin_restart('room')
        self.check(101)
        self.assertTrue(self.check(111)['restarting'])
        self.assertIn('not independently confirmed', self.check(121)['message'])

    def test_failed_recovery_suggests_power_cycle_without_retry(self):
        self.h.begin_restart('room')
        self.r.av_transport.side_effect = RuntimeError()
        state = self.check(221)
        self.assertFalse(state['restarting'])
        self.assertIn('Power-cycle', state['message'])
        self.r.command.assert_not_called()

    def test_offline_does_not_probe_or_suggest_network_restart(self):
        state = self.check(100, False)
        self.assertFalse(state['restart_allowed'])
        self.assertIn('power and network', state['message'])
        self.r.av_transport.assert_not_called()

    def test_later_failure_does_not_reenter_restart_state(self):
        self.h.begin_restart('room')
        self.check(101, False)
        self.check(111)
        self.check(121)
        self.r.av_transport.side_effect = RuntimeError()
        self.check(131)
        state = self.check(141)
        self.assertTrue(state['degraded'])
        self.assertFalse(state['restarting'])
        self.assertIn('may help', state['message'])

    def test_recovery_notice_expires_without_resetting_cooldown(self):
        for offline in (False, True):
            with self.subTest(observed_outage=offline):
                self.h = RoomHealth()
                self.now.return_value = 100
                self.h.begin_restart('room')
                self.check(101, not offline)
                self.check(111)
                self.assertIn('Both services', self.check(121)['message'])
                self.assertIn('Both services', self.check(150)['message'])
                state = self.check(151)
                self.assertEqual(state['message'], '')
                self.assertFalse(state['restarting'])
                self.assertFalse(state['restart_allowed'])
                self.assertEqual(self.check(170)['message'], '')
                self.assertTrue(self.check(221)['restart_allowed'])

    def test_failure_notice_does_not_expire(self):
        self.h.begin_restart('room')
        self.r.av_transport.side_effect = RuntimeError()
        self.assertIn('Power-cycle', self.check(221)['message'])
        self.assertIn('Power-cycle', self.check(260)['message'])
