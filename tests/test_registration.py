import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from plex_bridge.registration import RegistrationMaintainer


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.ads = {r: SimpleNamespace(title=r, provides='client,player,pubsub-player')
                    for r in ['first', 'second']}
        self.linked = set(self.ads)
        self.cloud = Mock()
        self.cloud.linked.side_effect = lambda identifier: identifier in self.linked
        self.worker = RegistrationMaintainer(self.cloud, lambda: self.ads,
            clock=lambda: self.now, refresh_interval=900)

    def test_failure_does_not_skip_later_room_and_retries_after_network_recovers(self):
        self.cloud.publish_endpoint.side_effect = [OSError('offline'), None, None]
        self.worker.poll_once()
        self.assertEqual([c.args[0] for c in self.cloud.publish_endpoint.call_args_list],
                         ['first', 'second'])
        self.now = 4
        self.worker.poll_once()
        self.assertEqual(self.cloud.publish_endpoint.call_count, 2)
        self.now = 5
        self.worker.poll_once()
        self.assertEqual(self.cloud.publish_endpoint.call_count, 3)
        self.assertFalse(self.worker.states['first']['failed'])
        self.assertEqual(self.worker.states['first']['retry'], 5)

    def test_repeated_failure_backs_off_to_cap_even_when_metadata_changes(self):
        self.cloud.publish_endpoint.side_effect = RuntimeError('outage')
        for delay in [5, 10, 20, 40, 80, 160, 300, 300]:
            self.worker.poll_once()
            self.assertEqual(self.worker.states['first']['due'], self.now + delay)
            count = self.cloud.publish_endpoint.call_count
            self.ads['first'].title += '!'
            self.now += delay - 1
            self.worker.poll_once()
            self.assertEqual(self.cloud.publish_endpoint.call_count, count)
            self.now += 1

    def test_healthy_rooms_refresh_periodically_and_metadata_updates_immediately(self):
        self.worker.poll_once()
        self.now = 899
        self.worker.poll_once()
        self.assertEqual(self.cloud.publish_endpoint.call_count, 2)
        self.now = 900
        self.worker.poll_once()
        self.assertEqual(self.cloud.publish_endpoint.call_count, 4)
        self.ads['first'].provides = 'client'
        self.worker.poll_once()
        self.assertEqual(self.cloud.publish_endpoint.call_args.kwargs['provides'], 'client')
        self.assertEqual(self.cloud.publish_endpoint.call_count, 5)

    def test_new_links_are_published_and_removed_links_are_not_renewed(self):
        self.linked.clear()
        self.worker.poll_once()
        self.cloud.publish_endpoint.assert_not_called()
        self.linked.add('first')
        self.worker.poll_once()
        self.assertEqual(self.cloud.publish_endpoint.call_count, 1)
        self.linked.clear()
        self.worker.poll_once()
        self.assertEqual(self.worker.states, {})

    def test_monitor_survives_unexpected_snapshot_failure(self):
        stop = threading.Event()
        def snapshot():
            stop.set()
            raise RuntimeError('temporary snapshot failure')
        self.worker.advertisements = snapshot
        self.worker.monitor_forever(stop)
        self.cloud.publish_endpoint.assert_not_called()
