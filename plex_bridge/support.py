"""Explicit, bounded read-only support probes. Never export upstream payloads."""
import json
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from plex_bridge.diagnostics import failure_reason, utc


MODELS = {
    'Muzo_Mini': 'WiiM Mini', 'WiiM_Mini': 'WiiM Mini',
    'WiiM_Pro': 'WiiM Pro', 'WiiM_Pro_with_gc4a': 'WiiM Pro', 'WiiM_Pro_Plus': 'WiiM Pro Plus',
    'WiiM_ProPlus': 'WiiM Pro Plus', 'WiiM_Amp': 'WiiM Amp',
    'WiiM_Amp_Pro': 'WiiM Amp Pro', 'WiiM_Ultra': 'WiiM Ultra',
    'WiiM_Amp_Ultra': 'WiiM Amp Ultra',
}


def version(value):
    return value if type(value) is str and re.fullmatch(r'(?:Linkplay\.)?\d+(?:[.-]\d+){1,5}(?:-[0-9a-f]{5,16})?', value) else 'not_reported'


class SupportChecks:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.last_run = None

    def run(self, requester):
        c = self.app.coordinator
        if requester not in c.by_id:
            raise ValueError('Select an enrolled room')
        if not self.lock.acquire(blocking=False):
            raise ValueError('A diagnosis is already running')
        try:
            if self.last_run is not None and time.monotonic()-self.last_run < 30:
                raise ValueError('Wait 30 seconds before running another diagnosis')
            self.last_run = time.monotonic()
            diagnostics = self.app.diagnostics
            device = c.by_id[requester]
            with diagnostics.attempt('diagnose', requester):
                report = {'captured_at': utc(), 'room_id': diagnostics.opaque('room:'+requester),
                          'read_only': True, 'checks': {}, 'device': {}}
                checks = report['checks']
                renderer = c.plex.renderer

                def probe(name, operation, summarize):
                    start = time.monotonic()
                    try:
                        result = summarize(operation())
                        checks[name] = dict(outcome='success', elapsed_ms=int((time.monotonic()-start)*1000), **result)
                    except Exception as exc:
                        checks[name] = {'outcome': 'failed', 'reason': failure_reason(exc),
                                        'elapsed_ms': int((time.monotonic()-start)*1000)}
                        code = getattr(exc, 'code', None)
                        if type(code) is int and 100 <= code <= 599:
                            checks[name]['http_status'] = code

                def information(raw):
                    if not isinstance(raw, dict):
                        raise ValueError('invalid device response')
                    model = MODELS.get(raw.get('project'), 'unknown')
                    report['device'] = {'model': model, 'firmware': version(raw.get('firmware')),
                                        'model_recognized': model != 'unknown'}
                    return {}

                probe('device_status', lambda: renderer.command(device, 'getStatusEx'), information)

                def transport(root):
                    state = next((n.text for n in root.iter() if n.tag.split('}')[-1]=='CurrentTransportState'), '')
                    if state not in {'PLAYING','STOPPED','PAUSED_PLAYBACK','TRANSITIONING','NO_MEDIA_PRESENT'}:
                        raise ValueError('invalid transport response')
                    return {'state': state}
                probe('playback_control', lambda: renderer.av_transport(device, 'GetTransportInfo', [('InstanceID',0)]), transport)

                def trigger(raw):
                    if not isinstance(raw, dict) or str(raw.get('status')) not in {'0','1'}:
                        return {'telemetry_supported': False}
                    return {'telemetry_supported': True, 'enabled': str(raw['status'])=='1'}
                probe('amplifier_trigger', lambda: renderer.command(device, 'getTriggeroutStatus'), trigger)

                def topology():
                    request = urllib.request.Request(c.groups.base+'/api/v1/players', headers=c.groups.headers)
                    with urllib.request.urlopen(request, timeout=3) as response:
                        raw = json.loads(response.read(256*1024))
                    selected = next(p for p in raw['players'] if p['device']['id']==requester)
                    role = selected.get('role')
                    return {'role': role if role in {'follower','standalone/leader'} else 'unknown'}
                probe('topology', topology, lambda x:x)

                def identity():
                    with urllib.request.urlopen(c.plex.allowed_server_base+'/identity', timeout=3) as response:
                        root = ET.fromstring(response.read(65536))
                    return {'server_version': version(root.get('version'))}
                probe('plex_server', identity, lambda x:x)

                session = c.plex.store.snapshot()
                if session and session.target_device_id == requester:
                    report['track_id'] = diagnostics.opaque('track:'+session.current.metadata_key)
                    def observed():
                        first = renderer.snapshot(device)
                        time.sleep(1)
                        second = renderer.snapshot(device)
                        return {'state': second.state if second.state in {'playing','paused','stopped','buffering'} else 'unknown',
                                'expected_stream': c.plex.media_path(session, session.current) in second.source_url,
                                'advancing': second.position_ms > first.position_ms,
                                'sample_interval_ms': 1000}
                    probe('playback_observation', observed, lambda x:x)
                    def media():
                        if session.server_base.rstrip('/') != c.plex.allowed_server_base:
                            raise ValueError('unapproved server origin')
                        request = c.plex.media_request(c.plex.media_path(session, session.current), 'bytes=0-4095')
                        with urllib.request.urlopen(request, timeout=5) as response:
                            body = response.read(4096)
                            if response.status not in (200,206) or not body:
                                raise ValueError('empty audio response')
                            return {'http_status': response.status, 'bytes_read': len(body)}
                    probe('plex_audio_read', media, lambda x:x)
                else:
                    checks['playback_observation'] = {'outcome':'not_checked', 'reason':'no_saved_session_for_room'}
                    checks['plex_audio_read'] = {'outcome':'not_checked', 'reason':'no_saved_session_for_room'}
                with diagnostics.lock:
                    diagnostics.last_checks = report
                return report
        finally:
            self.lock.release()
