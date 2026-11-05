import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch

from plex_bridge.diagnostics import Diagnostics, SafeHandler, timed, utc
from plex_bridge.history import HistoryArchive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = HistoryArchive(Path(self.tmp.name)/'history', Diagnostics._restored_row)

    def row(self, event='stage_finished', age=0):
        return {'timestamp': (datetime.now(timezone.utc)-timedelta(days=age)).isoformat(timespec='seconds'),
                'event': event, 'severity': 'INFO', 'component': 'playback', 'version': '0.1.0'}

    def test_separate_retention_windows_and_allowlisted_restore(self):
        rows = [self.row(age=6), self.row(age=8), self.row('registration_failed', 29),
                self.row('registration_failed', 31)]
        rows[0]['password'] = 'must-not-persist'
        self.archive.append(rows)
        result = self.archive.read()
        self.assertEqual(len(result['events']), 2)
        self.assertNotIn('must-not-persist', ''.join(p.read_text() for p in self.archive.directory.iterdir()))
        self.assertEqual(self.archive.directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.archive.directory.iterdir()))

    def test_rotation_caps_each_lane_without_evicting_incidents(self):
        self.archive.SEGMENT_BYTES = 400
        self.archive.LIMITS = {'details': (7, 800), 'incidents': (30, 800)}
        self.archive.append([self.row('registration_failed')])
        self.archive.append([self.row() for _ in range(100)])
        for lane, info in self.archive.summary().items():
            self.assertLessEqual(info['bytes'], 800)
        self.assertTrue(any(r['event']=='registration_failed' for r in self.archive.read()['events']))
        self.assertGreater(len(self.archive.files('details')), 1)

    def test_restart_time_window_and_bounded_export_with_partial_tail(self):
        self.archive.append([self.row() for _ in range(600)])
        p=self.archive.files('details')[-1]
        with p.open('ab') as h:h.write(b'{broken-tail')
        restored=HistoryArchive(self.archive.directory, Diagnostics._restored_row)
        result=restored.read(limit=20)
        self.assertEqual(len(result['events']),20)
        self.assertEqual(result['matched'],600)
        self.assertTrue(result['truncated'])
        self.assertEqual(restored.read(start='2099-01-01')['events'],[])

    def test_expired_segments_are_pruned_even_without_new_events(self):
        self.archive.append([self.row()])
        self.archive.clock=lambda: datetime.now(timezone.utc).timestamp()+9*86400
        self.archive.append([])
        self.assertEqual(self.archive.files('details'),[])


class PollIsolationTests(unittest.TestCase):
    def test_background_success_is_omitted_but_failure_and_foreground_timings_survive(self):
        d=Diagnostics('playback')
        log=logging.Logger('history-test', logging.INFO)
        log.addHandler(SafeHandler(d,io.StringIO()))
        with patch('plex_bridge.diagnostics.logging.getLogger', return_value=log):
            @timed('wiim_http')
            def poll(fail=False):
                if fail:raise TimeoutError('secret')
            session=NS(renderer_session_id='session', target_device_id='room', current=NS(metadata_key='track'))
            with d.attempt('play','room','track'):
                d.bind_session(session)
                poll()
            before=len(d.incidents)
            with d.session_scope(session):
                for _ in range(2000):poll()
                self.assertEqual(len(d.incidents),before)
                with self.assertRaises(TimeoutError):poll(True)
            stages=[r for r in d.incidents if r['event']=='stage_finished']
            self.assertEqual(len(stages),2)
            self.assertIn('attempt_id',stages[0])
            self.assertNotIn('attempt_id',stages[1])
            self.assertEqual(stages[1]['reason'],'timeout')

    def test_disk_history_survives_recent_preview_eviction_and_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'diagnostics.json'
            d=Diagnostics('playback');d.enable_persistence(path)
            try:
                for i in range(650):
                    d._retain(dict(timestamp=utc(),severity='INFO',event='attempt_finished',
                                   version='0.1.0',attempt_id=f'{i:032x}',outcome='accepted'))
                d.flush()
            finally:d.close()
            restored=Diagnostics('playback');restored.enable_persistence(path)
            try:
                self.assertEqual(len(restored.incidents),500)
                result=restored.archive.read(limit=1000)
                self.assertEqual(result['matched'],650)
                self.assertEqual(len(result['events']),650)
            finally:restored.close()

    def test_history_preview_is_frozen_bounded_and_validates_window(self):
        with tempfile.TemporaryDirectory() as directory:
            d=Diagnostics('playback');d.enable_persistence(Path(directory)/'diagnostics.json')
            try:
                d._retain(dict(timestamp=utc(),severity='ERROR',event='registration_failed',version='0.1.0'))
                d.flush()
                start=(datetime.now(timezone.utc)-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
                end=(datetime.now(timezone.utc)+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
                incident, body=d.preview(start,end)
                result=json.loads(body)
                self.assertEqual(result['history_window']['matched'],1)
                self.assertFalse(result['history_window']['truncated'])
                self.assertEqual(d.download(incident),body)
                self.assertNotIn('salt',result)
                self.assertIn('Earlier activity',d.page())
                for a,b in [('',end),(end,start),('invalid',end)]:
                    with self.assertRaises(ValueError):d.preview(a,b)
            finally:d.close()

    def test_unchanged_position_between_successful_polls_does_not_flood_observations(self):
        d=Diagnostics('playback')
        log=logging.Logger('history-test', logging.INFO)
        log.addHandler(SafeHandler(d,io.StringIO()))
        session=NS(renderer_session_id='session',target_device_id='room',
                   current=NS(metadata_key='track',play_queue_item_id='item'))
        with patch('plex_bridge.diagnostics.logging.getLogger', return_value=log):
            with d.attempt('play','room','track'):d.bind_session(session)
            with d.session_scope(session):
                for position in [0,0,1000,1000,2000,2000]*100:
                    d.observe(session,NS(state='playing',position_ms=position),True)
                d.observe(session,NS(state='stopped',position_ms=2000),True)
        rows=[r for r in d.incidents if r['event']=='renderer_observed']
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[-1]['playback_state'],'stopped')
