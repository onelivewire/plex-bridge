import contextvars
import io
import json
import logging
from pathlib import Path
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from plex_bridge.diagnostics import Diagnostics, SafeHandler, TRACE, timed, utc
from plex_bridge.support import SupportChecks, version
SECRET='Bearer private-token http://user:password@192.0.2.1/private?token=secret Private Room'

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'diagnostics.json'
        self.d=Diagnostics('playback');self.d.enable_persistence(self.path);self.addCleanup(self.d.close)
        self.log=logging.getLogger('wiim-focus-control')
        old=self.log.handlers[:],self.log.level,self.log.propagate
        self.addCleanup(lambda:self.restore(old))
        self.log.handlers=[SafeHandler(self.d,io.StringIO())]
        self.log.setLevel(logging.INFO);self.log.propagate=False
    def restore(self,old):self.log.handlers,self.log.level,self.log.propagate=old
    def test_persistence_and_poll_isolation(self):
        with self.d.attempt('skipTo',SECRET,SECRET):
            self.log.warning(SECRET,extra={'event':'renderer_request_failed','reason':'timeout'})
        for _ in range(1000):self.log.info(SECRET,extra={'event':'request','status':200})
        self.d.flush()
        restored=Diagnostics('playback');restored.enable_persistence(self.path);self.addCleanup(restored.close)
        data=json.loads(restored.preview()[1])
        self.assertEqual(len(data['incidents']),3)
        self.assertEqual(data['persistence'],'enabled')
        self.assertEqual(restored.opaque(SECRET),self.d.opaque(SECRET))
        self.assertNotIn(SECRET,json.dumps(data));self.assertNotIn('salt',data)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
    def test_thread_correlation_timing_and_context_cleanup(self):
        @timed('wiim_transport')
        def operation(*args):raise TimeoutError(SECRET)
        def worker():
            try:operation(None,None,'GetTransportInfo')
            except TimeoutError:pass
        with self.d.attempt('play',SECRET,SECRET):
            ctx=contextvars.copy_context();t=threading.Thread(target=ctx.run,args=(worker,));t.start();t.join()
        rows=list(self.d.incidents);stage=next(r for r in rows if r['event']=='stage_finished')
        self.assertEqual(stage['attempt_id'],rows[0]['attempt_id'])
        self.assertEqual(stage['room_id'],rows[0]['room_id'])
        self.assertEqual(stage['reason'],'timeout');self.assertGreaterEqual(stage['elapsed_ms'],0)
        self.assertEqual(TRACE.get(),{})
    def test_warning_coalescing(self):
        for _ in range(200):self.log.warning('',extra={'event':'timeline_ignored'})
        self.assertEqual(len(self.d.incidents),1);self.assertEqual(self.d.incidents[0]['repeat_count'],200)
    def test_untrusted_stored_fields_and_expiry(self):
        self.path.write_text(json.dumps({'salt':'a'*64,'incidents':[
            {'timestamp':utc(),'event':'renderer_request_failed','room_id':SECRET,'reason':SECRET,'message':SECRET,'token':SECRET},
            {'timestamp':'2000-01-01T00:00:00+00:00','event':'play_media'}]}))
        self.d.enable_persistence(self.path)
        self.assertNotIn(SECRET,self.d.preview()[1].decode());self.assertEqual(len(self.d.incidents),1)
    def test_corrupt_and_unwritable_storage_do_not_break_logging(self):
        self.path.write_text('broken secret');self.d.enable_persistence(self.path)
        self.assertEqual(self.d.persistence_status,'unavailable')
        self.d.persistence_path=Path(self.tmp.name)/'missing'/'file'
        with self.d.attempt('play',SECRET):pass
        self.d.flush()
        self.assertEqual(self.d.persistence_status,'unavailable');self.assertTrue(self.d.incidents)
        self.assertNotIn('broken secret',self.d.preview()[1].decode())
    def test_advancement_observations_do_not_flood_history(self):
        session=NS(renderer_session_id='private-session',target_device_id='private-room',current=NS(play_queue_item_id='private-item',metadata_key='private-key'))
        with self.d.attempt('play',SECRET,SECRET):self.d.bind_session(session)
        with self.d.session_scope(session):
            for position in range(20):self.d.observe(session,NS(state='playing',position_ms=position),True)
        observations=[r for r in self.d.incidents if r['event']=='renderer_observed']
        self.assertEqual(len(observations),2);self.assertEqual(observations[-1]['outcome'],'success')
        self.assertNotIn('private-',self.d.preview()[1].decode())

class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.renderer=Mock();self.device=NS(id='room',host='192.0.2.1',name=SECRET)
        self.renderer.command.side_effect=lambda d,c: {'project':'WiiM_Pro_with_gc4a','firmware':'4.8.123456','DeviceName':SECRET} if c=='getStatusEx' else {'status':'1','secret':SECRET}
        self.renderer.av_transport.return_value=ET.fromstring('<r><CurrentTransportState>STOPPED</CurrentTransportState></r>')
        self.plex=NS(renderer=self.renderer,allowed_server_base='http://192.0.2.2:32400',store=Mock())
        self.plex.store.snapshot.return_value=None
        self.app=NS(diagnostics=Diagnostics('playback'),coordinator=NS(by_id={'room':self.device},plex=self.plex,groups=NS(base='http://192.0.2.3:8793',headers={'Authorization':SECRET})))
        self.runner=SupportChecks(self.app)
    def response(self,request,**kwargs):
        r=Mock();r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
        target=request if isinstance(request,str) else request.full_url
        r.read.return_value=(b'<MediaContainer version="1.42.1.1000-abcd12345" machineIdentifier="private"/>' if target.endswith('/identity') else json.dumps({'players':[{'device':{'id':'room'},'role':'standalone/leader','token':SECRET}]}).encode())
        return r
    def test_read_only_allowlisted_partial_snapshot_and_cooldown(self):
        with patch('urllib.request.urlopen',side_effect=self.response):result=self.runner.run('room')
        self.assertNotIn(SECRET,json.dumps(result));self.assertEqual(result['device']['model'],'WiiM Pro')
        self.assertEqual(result['checks']['playback_control']['state'],'STOPPED')
        self.assertEqual(result['checks']['plex_audio_read']['outcome'],'not_checked')
        self.assertEqual([c.args[1] for c in self.renderer.command.call_args_list],['getStatusEx','getTriggeroutStatus'])
        self.renderer.av_transport.assert_called_once_with(self.device,'GetTransportInfo',[('InstanceID',0)])
        with self.assertRaises(ValueError):self.runner.run('room')
    def test_timeout_keeps_other_checks_and_hides_raw_error(self):
        self.renderer.av_transport.side_effect=TimeoutError(SECRET)
        with patch('urllib.request.urlopen',side_effect=self.response):result=self.runner.run('room')
        self.assertEqual(result['checks']['playback_control']['reason'],'timeout')
        self.assertEqual(result['checks']['plex_server']['outcome'],'success')
        self.assertNotIn(SECRET,json.dumps(result))
    def test_saved_queue_checks_progress_and_reads_bounded_audio(self):
        session=NS(target_device_id='room',server_base=self.plex.allowed_server_base,
                   current=NS(metadata_key=SECRET))
        self.plex.store.snapshot.return_value=session
        self.plex.media_path=Mock(return_value='/private-audio')
        self.plex.media_request=Mock(return_value='http://192.0.2.2:32400/private-audio')
        self.renderer.snapshot.side_effect=[NS(position_ms=100,state='playing',source_url='/private-audio'),
                                            NS(position_ms=1100,state='playing',source_url='/private-audio')]
        original=self.response
        audio=Mock();audio.__enter__=Mock(return_value=audio);audio.__exit__=Mock(return_value=False)
        audio.status=206;audio.read.return_value=b'audio-private'
        def response(request,**kwargs):
            return audio if isinstance(request,str) and request.endswith('/private-audio') else original(request,**kwargs)
        with patch('urllib.request.urlopen',side_effect=response), patch('plex_bridge.support.time.sleep'):
            result=self.runner.run('room')
        self.assertTrue(result['checks']['playback_observation']['advancing'])
        self.assertTrue(result['checks']['playback_observation']['expected_stream'])
        self.assertEqual(result['checks']['plex_audio_read']['outcome'],'success')
        audio.read.assert_called_once_with(4096)
        self.plex.media_request.assert_called_once_with('/private-audio','bytes=0-4095')
        self.assertNotIn('audio-private',json.dumps(result));self.assertNotIn(SECRET,json.dumps(result))

    def test_version_and_unknown_room_validation(self):
        self.assertEqual(version(SECRET),'not_reported')
        with self.assertRaises(ValueError):self.runner.run('http://evil')
        self.renderer.command.assert_not_called()

class SupportHttpTests(unittest.TestCase):
    from test_diagnostics import DiagnosticsHttpTests as Fixture
    setUp=Fixture.setUp
    close=Fixture.close
    get=Fixture.get
    def post(self,authenticated=True,origin=True):
        from http.client import HTTPConnection
        base='http://127.0.0.1:'+str(self.server.server_port)
        self.app.plex=NS(public_base=base)
        headers={'Content-Type':'application/x-www-form-urlencoded'}
        if authenticated:headers['Cookie']=self.cookie
        if origin:headers['Origin']=base
        conn=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request('POST','/diagnostics/run','requester=room',headers)
        response=conn.getresponse();status=response.status;response.read();conn.close();return status
    def test_diagnosis_requires_authentication_and_same_origin(self):
        self.app.support_checks=Mock()
        self.assertEqual(self.post(authenticated=False),303)
        self.assertEqual(self.post(origin=False),403)
        self.app.support_checks.run.assert_not_called()
        self.assertEqual(self.post(),303)
        self.app.support_checks.run.assert_called_once_with('room')
    def test_get_cannot_run_diagnosis(self):
        self.app.support_checks=Mock()
        self.assertEqual(self.get('/diagnostics/run')[0],404)
        self.app.support_checks.run.assert_not_called()
