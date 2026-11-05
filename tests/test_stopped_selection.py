import unittest
from dataclasses import replace
import test_playback

class StoppedSelectionTests(unittest.TestCase):
    setUp = test_playback.PlexBridgeTests.setUp
    play = test_playback.PlexBridgeTests.play
    wait_for_track_worker = test_playback.PlexBridgeTests.wait_for_track_worker

    def test_selection_from_stopped_queue_starts_requested_track(self):
        self.play()
        self.bridge.playback_command('stop', {})
        self.bridge.playback_command('skipTo', {'key': ['/library/metadata/13']})
        self.wait_for_track_worker()
        saved = self.bridge.store.snapshot()
        self.assertEqual(saved.state, 'playing')
        self.assertEqual(saved.current.rating_key, '13')
        self.assertEqual(self.renderer.started[-1][1].state, 'playing')
        self.assertEqual(self.renderer.started[-1][1].current.rating_key, '13')

    def test_selecting_same_stopped_item_rebuilds_renderer(self):
        self.play()
        self.bridge.playback_command('stop', {})
        count = len(self.renderer.started)
        self.bridge.playback_command('skipTo', {'key': ['/library/metadata/11']})
        self.assertEqual(len(self.renderer.started), count + 1)
        self.assertEqual(self.bridge.store.snapshot().state, 'playing')

    def test_explicit_stop_after_selection_is_honored(self):
        self.play()
        self.bridge.playback_command('stop', {})
        self.bridge.playback_command('skipTo', {'key': ['/library/metadata/13']})
        self.bridge.playback_command('stop', {})
        self.assertEqual(self.bridge.store.snapshot().state, 'stopped')

    def test_paused_selection_preserves_pause(self):
        self.play()
        self.bridge.playback_command('pause', {})
        self.bridge.playback_command('skipTo', {'key': ['/library/metadata/13']})
        self.wait_for_track_worker()
        self.assertEqual(self.bridge.store.snapshot().state, 'paused')

    def test_old_renderer_failure_cannot_stop_replacement_session(self):
        from plex_bridge.playback import FocusError
        original = self.play()
        self.bridge.renderer_unavailable_since = 0
        replacement = replace(original, renderer_session_id='replacement', state='buffering')
        def stale_observation():
            self.bridge.store.save(replacement, self.bridge.store.token)
            raise FocusError('old queue no longer loaded')
        self.bridge.reconcile_renderer = stale_observation
        self.bridge.monitor_once()
        self.assertEqual(self.bridge.store.snapshot().state, 'buffering')

    def test_pending_handoff_is_not_stopped_by_old_failure_timer(self):
        from unittest.mock import Mock
        from plex_bridge.playback import FocusError
        original = self.play()
        self.bridge.store.save(replace(original, state='buffering'), self.bridge.store.token)
        self.bridge.renderer_unavailable_since = 0
        worker = Mock()
        worker.is_alive.return_value = True
        self.bridge.last_track_worker = worker
        self.bridge.reconcile_renderer = Mock(side_effect=FocusError('transition'))
        self.bridge.monitor_once()
        self.assertEqual(self.bridge.store.snapshot().state, 'buffering')
        self.bridge.last_track_worker = None

    def test_starting_selection_resets_previous_failure_timer(self):
        self.play()
        self.bridge.playback_command('stop', {})
        self.bridge.renderer_unavailable_since = 0
        self.bridge.playback_command('skipTo', {'key': ['/library/metadata/13']})
        self.wait_for_track_worker()
        self.assertIsNone(self.bridge.renderer_unavailable_since)
