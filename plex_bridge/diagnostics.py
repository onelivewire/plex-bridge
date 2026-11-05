"""Allowlisted diagnostics: raw messages, arguments and tracebacks never serialize."""
from collections import deque, OrderedDict
from datetime import datetime, timezone
import html
import contextvars
from contextlib import contextmanager
from functools import wraps
import hashlib
import hmac
import os
from pathlib import Path
import platform
import re
import secrets
import urllib.error
import json
import logging
import sys
import threading
import time
import uuid

from plex_bridge import __version__

VERSION = __version__
EVENTS = frozenset({"service_start", "request", "operation", "unhandled_error",
                    "setup_wait", "setup_ready", "play_media", "topology_unavailable",
                    "timeline_failed", "registration_failed", "registration_refreshed", "monitor_failed",
                    "storage_failed", "legacy_event", "queue_fallback", "play_accepted",
                    "command_rejected", "renderer_failed", "rollback_failed", "action_rejected", "restart_fallback",
                    "playback_unavailable", "playback_recovered", "timeline_ignored", "renderer_request_failed", "attempt_started", "attempt_finished",
                    "stage_finished", "renderer_observed", "diagnosis_finished", "selection_resolved",
                    "proxy_auth_required", "proxy_media_token_repaired", "proxy_report_suppressed",
                    "proxy_forward_failed"})
ERRORS = frozenset({"OSError", "PermissionError", "FileNotFoundError", "TimeoutError",
                    "ConnectionError", "ConnectionRefusedError", "BrokenPipeError",
                    "ConnectionResetError", "ValueError", "TypeError", "KeyError",
                    "RuntimeError", "ConfigError", "FocusError", "HTTPError", "URLError"})
TEMPLATES = {
    "Plex Media Server ignored timeline session=%s item=%s": "timeline_ignored",
    "listening on %s:%s": "service_start",
    "listening on %s:%s for %d WiiMs": "service_start",
    "received Plex playMedia target=%s offset=%s viewOffset=%s": "play_media",
    "using fail-open individual Plex endpoints; topology unavailable": "topology_unavailable",
    "Plex Media Server timeline report failed": "timeline_failed",
    "could not refresh Plex endpoint registration": "registration_failed",
    "dynamic Plex endpoint monitor failed": "monitor_failed",
    "Plex renderer monitor recovered from an unexpected error": "monitor_failed",
    "could not persist event history": "storage_failed",
    "WiiM native PlayQueue rejected the Plex queue; using direct track playback": "queue_fallback",
    "Plex queue refresh at track completion failed; using cached queue": "queue_fallback",
    "accepted Plex playMedia queue=%s item=%s canonical_index=%s renderer_index=0": "play_accepted",
    "Plex playMedia rejected: %s fields=%s": "command_rejected",
    "Plex playback command rejected: %s path=%s endpoint=%s": "command_rejected",
    "could not stop former Plex room renderer %s": "renderer_failed",
    "deferred WiiM track change failed; restoring prior timeline": "renderer_failed",
    "coalesced WiiM volume update failed": "renderer_failed",
    "could not publish dynamic Plex endpoint %s": "registration_failed",
    "Plex focus rollback failed": "rollback_failed",
    "focus rollback failed": "rollback_failed",
    "could not load WiiM group state: %s": "topology_unavailable",
    "could not load group decision history: %s": "topology_unavailable",
    "could not load Plex endpoint group states": "topology_unavailable",
}
def failure_reason(exc):
    # Inspect exception types, never serialize exception text or request URLs.
    current = exc
    for _ in range(8):
        if isinstance(current, urllib.error.HTTPError):
            return 'http_error'
        if isinstance(current, (ValueError, TypeError)):
            return 'invalid_response'
        if isinstance(current, TimeoutError):
            return 'timeout'
        if isinstance(current, ConnectionError):
            return 'connection_error'
        current = getattr(current, '__cause__', None) or getattr(current, 'reason', None)
        if current is None:
            break
    return 'other'


TRACE = contextvars.ContextVar('support_trace', default={})


def timed(stage):
    """Instrument a bounded stage without serializing its arguments or result."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            started = time.monotonic()
            outcome, reason = 'success', None
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                outcome, reason = 'failed', failure_reason(exc)
                raise
            finally:
                # Background polling is intentionally not retained as incidents.
                if TRACE.get().get('attempt_id') and (not TRACE.get().get('_background') or outcome == 'failed'):
                    operation = args[2] if stage in {'wiim_transport', 'wiim_queue', 'wiim_http'} and len(args) > 2 else None
                    if type(operation) is str:
                        operation = operation.split(':', 1)[0]
                    logging.getLogger('wiim-focus-control').info('', extra={
                        'event': 'stage_finished', 'stage': stage, 'outcome': outcome,
                        'reason': reason, 'operation': operation,
                        'elapsed_ms': min(3600000, int((time.monotonic()-started)*1000))})
        return wrapped
    return decorate


def traced(action):
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            diagnostics = getattr(self, 'diagnostics', None)
            if diagnostics is None:
                return fn(self, *args, **kwargs)
            command = args[0] if action == 'command' and args else action
            params = args[1] if action == 'command' and len(args) > 1 else (args[0] if args else {})
            current = self.store.snapshot()
            room = current.target_device_id if current else ''
            endpoint = kwargs.get('endpoint_identifier', '')
            if not endpoint and action == 'play_media' and len(args) > 1:
                endpoint = args[1]
            if endpoint:
                try:
                    room = self.target_device(endpoint).id
                except (RuntimeError, ValueError, KeyError):
                    pass
            track = params.get('key', [''])[0] if isinstance(params, dict) else ''
            if not track and current:
                track = current.current.metadata_key
            with diagnostics.attempt(command, room, track):
                return fn(self, *args, **kwargs)
        return wrapped
    return decorate


def dashboard_traced(fn):
    @wraps(fn)
    def wrapped(self, form):
        diagnostics = getattr(self.app, 'diagnostics', None)
        if diagnostics is None:
            return fn(self, form)
        with diagnostics.attempt(form.get('action', [''])[0], form.get('requester', [''])[0]):
            return fn(self, form)
    return wrapped


def session_traced(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        diagnostics = getattr(self, 'diagnostics', None)
        if diagnostics is None:
            return fn(self, *args, **kwargs)
        with diagnostics.session_scope(self.store.snapshot()):
            return fn(self, *args, **kwargs)
    return wrapped


def media_traced(fn):
    @wraps(fn)
    def wrapped(self, path):
        session = self.app.plex.store.snapshot()
        if session and session.renderer_session_id not in path:
            session = None
        with self.app.diagnostics.session_scope(session):
            return fn(self, path)
    return wrapped


ENUMS = {
    "stage": {"wiim_http", "wiim_transport", "wiim_queue", "plex_queue", "renderer_start", "plex_media"},
    "playback_state": {"playing", "paused", "stopped", "buffering", "unknown"},
    "reason": {"timeout", "connection_error", "http_error", "invalid_response", "other", "busy", "confirmation_required",
               "restart_cooldown", "restart_rejected"},
    "operation": {"GetTransportInfo", "SetAVTransportURI", "CreateQueue", "PlayQueueWithIndex",
                  "SetQueueLoopMode", "GetPositionInfo", "Play", "Stop", "Pause", "Seek",
                  "getPlayerStatus", "getMetaInfo", "getStatusEx", "getTriggeroutStatus",
                  "setPlayerCmd", "reboot", "StartRebootTime"},
    "action": {"focus", "disband", "join", "leave", "sleep", "stop", "leadership",
               "leave_sleep", "standby_leave", "connections", "server", "link", "finish",
               "seen", "arm", "verify", "heard", "restart", "play_media", "play", "pause",
               "skipTo", "skipNext", "skipPrevious", "seekTo", "stepBack", "stepForward",
               "refreshPlayQueue", "setParameters", "diagnose"},
    "outcome": {"success", "failed", "waiting", "joined", "slept", "stopped", "enabled",
                "disabled", "ignored", "restored", "conflict", "no_active", "ambiguous",
                "left", "disbanded", "focused", "noop", "accepted"},
    "method": {"GET", "POST", "OPTIONS", "HEAD", "PUT", "DELETE"},
}


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Diagnostics:
    def __init__(self, component):
        self.component = component if component in {"playback", "topology", "setup", "runtime"} else "unknown"
        self.lock = threading.RLock()
        self.events = deque(maxlen=250)
        self.bundles = OrderedDict()
        self.incidents = deque(maxlen=500)
        self.salt = secrets.token_bytes(32)
        self.persistence_path = None
        self.persistence_status = 'disabled'
        self.session_contexts = OrderedDict()
        self.observations = OrderedDict()
        self.last_checks = None
        self.write_lock = threading.Lock()
        self.write_pending = threading.Event()
        self.write_stop = threading.Event()
        self.writer = None
        self.archive = None
        self.archive_pending = deque(maxlen=10000)
        self.archive_dropped = 0

    def record(self, record):
        # Never call getMessage(), str(exception), formatException(), or repr(args).
        event = getattr(record, "event", None)
        if type(event) is not str or event not in EVENTS:
            event = TEMPLATES.get(record.msg, "legacy_event") if type(record.msg) is str else "legacy_event"
        row = {"timestamp": utc(), "severity": logging.getLevelName(record.levelno)
               if record.levelno in (10, 20, 30, 40, 50) else "INFO",
               "component": self.component, "event": event, "version": VERSION}
        for field, values in ENUMS.items():
            value = getattr(record, field, None)
            if type(value) is str and value in values:
                row[field] = value
        status = getattr(record, "status", None)
        if type(status) is int and 100 <= status <= 599:
            row["status"] = status
        if record.exc_info and record.exc_info[0]:
            name = record.exc_info[0].__name__
            row["error_class"] = name if name in ERRORS else "OtherError"
        for field, value in TRACE.get().items():
            if field == 'attempt_id' and TRACE.get().get('_background') and event == 'stage_finished':
                continue
            if field in {'attempt_id', 'room_id', 'track_id'} and type(value) is str and re.fullmatch('[0-9a-f]{32}', value):
                row[field] = value
        for field in ('elapsed_ms', 'position_ms'):
            value = getattr(record, field, None)
            if type(value) is int and 0 <= value <= 86400000:
                row[field] = value
        value = getattr(record, 'stream_matches', None)
        if type(value) is bool:
            row['stream_matches'] = value
        with self.lock:
            self._retain(row)
            self.events.append((time.monotonic(), row))
        return json.dumps(row, separators=(",", ":"))

    def opaque(self, value):
        return hmac.new(self.salt, str(value).encode(), hashlib.sha256).hexdigest()[:32]

    @contextmanager
    def room_scope(self, room):
        token = TRACE.set({'room_id': self.opaque('room:' + room)})
        try:
            yield
        finally:
            TRACE.reset(token)

    @contextmanager
    def attempt(self, action, room='', track=''):
        context = {'attempt_id': uuid.uuid4().hex}
        if room:
            context['room_id'] = self.opaque('room:' + room)
        if track:
            context['track_id'] = self.opaque('track:' + track)
        token = TRACE.set(context)
        started = time.monotonic()
        logger = logging.getLogger('wiim-focus-control')
        logger.info('', extra={'event': 'attempt_started', 'action': action})
        try:
            yield context
        except Exception as exc:
            logger.warning('', extra={'event': 'attempt_finished', 'action': action,
                           'outcome': 'failed', 'reason': failure_reason(exc),
                           'elapsed_ms': int((time.monotonic()-started)*1000)})
            raise
        else:
            logger.info('', extra={'event': 'attempt_finished', 'action': action,
                        'outcome': 'accepted', 'elapsed_ms': int((time.monotonic()-started)*1000)})
        finally:
            TRACE.reset(token)

    def bind_session(self, session):
        if TRACE.get():
            TRACE.set(dict(TRACE.get(), room_id=self.opaque('room:'+session.target_device_id),
                           track_id=self.opaque('track:'+session.current.metadata_key)))
            logging.getLogger('wiim-focus-control').info('', extra={'event':'selection_resolved'})
        with self.lock:
            self.session_contexts[session.renderer_session_id] = dict(TRACE.get())
            while len(self.session_contexts) > 16:
                self.session_contexts.popitem(last=False)

    @contextmanager
    def session_scope(self, session):
        with self.lock:
            context = dict(self.session_contexts.get(session.renderer_session_id, {})) if session else {}
        if session and context:
            context['track_id'] = self.opaque('track:' + session.current.metadata_key)
            context['_background'] = True
        token = TRACE.set(context)
        try:
            yield
        finally:
            TRACE.reset(token)

    def observe(self, session, observed, matches):
        if not TRACE.get():
            return
        key = (session.renderer_session_id, session.current.play_queue_item_id)
        with self.lock:
            prior = self.observations.get(key)
            advancing = bool(prior and observed.position_ms > prior[0])
            # Once advancement is established, identical playback state is not
            # a new incident merely because two fast polls share a position.
            if (prior and prior[1][:2] == (observed.state, matches)
                    and prior[1][2] and observed.state == 'playing' and matches):
                advancing = True
            signature = (observed.state, matches, advancing)
            self.observations[key] = (observed.position_ms, signature)
            while len(self.observations) > 32:
                self.observations.popitem(last=False)
            if prior and prior[1] == signature:
                return
        logging.getLogger('wiim-focus-control').info('', extra={
            'event': 'renderer_observed', 'playback_state': observed.state,
            'position_ms': observed.position_ms, 'stream_matches': matches,
            'outcome': 'success' if advancing and matches and observed.state == 'playing' else 'waiting'})

    @staticmethod
    def _restored_row(raw):
        # Treat a stored diagnostics file as untrusted input as well.
        if not isinstance(raw, dict) or raw.get('event') not in EVENTS:
            return None
        stamp = raw.get('timestamp', '')
        if type(stamp) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00', stamp):
            return None
        try:
            age = time.time() - datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            return None
        from plex_bridge.history import HistoryArchive
        days = HistoryArchive.LIMITS[HistoryArchive.lane(raw)][0]
        if not -300 <= age <= days*86400:
            return None
        saved_version = raw.get('version', '')
        row = {'timestamp': stamp, 'event': raw['event'], 'version': saved_version if type(saved_version) is str and re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z0-9.]+)?', saved_version) else 'unknown'}
        for key, allowed in dict(ENUMS, severity={'INFO','WARNING','ERROR','CRITICAL','DEBUG'},
                                  component={'playback','topology','runtime','setup','unknown'},
                                  error_class=ERRORS | {'OtherError'}).items():
            value = raw.get(key)
            if type(value) is str and value in allowed:
                row[key] = value
        for key in ('attempt_id','room_id','track_id'):
            value = raw.get(key)
            if type(value) is str and re.fullmatch('[0-9a-f]{32}', value):
                row[key] = value
        for key in ('status','elapsed_ms','position_ms','repeat_count'):
            value = raw.get(key)
            if type(value) is int and 0 <= value <= 86400000:
                row[key] = value
        if type(raw.get('stream_matches')) is bool:
            row['stream_matches'] = raw['stream_matches']
        return row

    def enable_persistence(self, path):
        with self.lock:
            self.persistence_path = Path(path)
            try:
                if self.persistence_path.exists():
                    if self.persistence_path.stat().st_size > 1024*1024:
                        raise ValueError('oversized diagnostics')
                    data = json.loads(self.persistence_path.read_text())
                    salt = data.get('salt', '')
                    if type(salt) is str and re.fullmatch('[0-9a-f]{64}', salt):
                        self.salt = bytes.fromhex(salt)
                    self.incidents.clear()
                    for raw in data.get('incidents', [])[-500:]:
                        row = self._restored_row(raw)
                        if row:
                            self.incidents.append(row)
                from plex_bridge.history import HistoryArchive
                directory = self.persistence_path.with_suffix('.history')
                migrate = not directory.exists() or not any(directory.glob("*.jsonl"))
                self.archive = HistoryArchive(directory, self._restored_row)
                if migrate:
                    self.archive_pending.extend(dict(r) for r in self.incidents)
                self.archive.prune()
            except (OSError, ValueError, TypeError, AttributeError):
                self.persistence_status = 'unavailable'
                return
        self.flush()
        if self.writer is None:
            self.writer = threading.Thread(target=self._writer_loop, daemon=True)
            self.writer.start()
            import atexit
            atexit.register(self.close)

    def _persist(self):
        if self.persistence_path is not None:
            self.write_pending.set()

    def _writer_loop(self):
        last_flush = time.monotonic()
        while not self.write_stop.is_set():
            self.write_pending.wait(1)
            if self.write_pending.is_set() or time.monotonic() - last_flush >= 3600:
                self.write_pending.clear()
                self.write_stop.wait(0.25)  # coalesce bursts without delaying commands
                self.flush()
                last_flush = time.monotonic()
        if self.write_pending.is_set():
            self.flush()

    def flush(self):
        # Used by the background writer and explicit graceful shutdown only.
        # Never perform filesystem I/O while holding a playback operation lock.
        if self.persistence_path is None:
            return
        with self.write_lock:
            with self.lock:
                payload = {'schema_version': 1, 'salt': self.salt.hex(),
                           'incidents': [dict(r) for r in self.incidents]}
                path = self.persistence_path
                pending = list(self.archive_pending)
                archive_written = False
                self.archive_pending.clear()
            try:
                from plex_bridge.provisioning import atomic_write
                if self.archive is not None:
                    self.archive.append(pending)
                    archive_written = True
                atomic_write(path, payload)
                self.persistence_status = 'enabled'
            except (OSError, ValueError, TypeError):
                with self.lock:
                    for row in reversed([] if archive_written else pending):
                        if len(self.archive_pending) == self.archive_pending.maxlen:
                            self.archive_dropped += 1
                            break
                        self.archive_pending.appendleft(row)
                self.persistence_status = 'unavailable'

    def close(self):
        self.write_stop.set()
        self.write_pending.set()
        if self.writer and self.writer is not threading.current_thread():
            self.writer.join(timeout=2)

    def _retain(self, row):
        # Routine HTTP traffic cannot evict incident evidence. Coalesce recurring
        # uncorrelated warnings into one entry per minute.
        if (row['event'] == 'request' and row.get('status', 200) < 400) or row['severity'] == 'DEBUG':
            return
        cutoff = time.time() - 30*86400
        while self.incidents and datetime.fromisoformat(self.incidents[0]['timestamp']).timestamp() < cutoff:
            self.incidents.popleft()
        if 'attempt_id' not in row:
            key = tuple(row.get(k) for k in ('event','reason','operation','room_id'))
            for previous in reversed(self.incidents):
                if time.time()-datetime.fromisoformat(previous['timestamp']).timestamp() > 60:
                    break
                if tuple(previous.get(k) for k in ('event','reason','operation','room_id')) == key:
                    previous['repeat_count'] = min(previous.get('repeat_count',1)+1, 86400000)
                    # Persist the summary with the next distinct event or
                    # hourly maintenance, rather than rewriting on every poll.
                    return
        retained = dict(row)
        self.incidents.append(retained)
        if len(self.archive_pending) == self.archive_pending.maxlen:
            self.archive_dropped += 1
        self.archive_pending.append(retained)
        self._persist()

    def environment(self):
        system = platform.system()
        machine = platform.machine()
        digest = hashlib.sha256()
        for source in sorted(Path(__file__).parent.glob('*.py')):
            try:
                digest.update(source.name.encode() + b'\0')
                digest.update(source.read_bytes())
            except OSError:
                pass
        return {'bridge_version': VERSION, 'build_fingerprint': digest.hexdigest(),
                'python': '.'.join(str(x) for x in sys.version_info[:3]),
                'os': system if system in {'Linux','Darwin','Windows'} else 'other',
                'architecture': machine if machine in {'x86_64','AMD64','aarch64','arm64','armv7l'} else 'other'}

    def preview(self, start='', end=''):
        """Frozen bounded snapshot; no dependency requests or private configuration reads."""
        archived = None
        if start or end:
            if not start or not end:
                raise ValueError('Enter both start and end times in UTC.')
            try:
                first = datetime.strptime(start, '%Y-%m-%dT%H:%M').replace(tzinfo=timezone.utc)
                last = datetime.strptime(end, '%Y-%m-%dT%H:%M').replace(tzinfo=timezone.utc)
            except ValueError:
                raise ValueError('Enter valid start and end times in UTC.') from None
            if first > last:
                raise ValueError('Start must be before end.')
            if self.archive is None:
                raise ValueError('The history archive is unavailable.')
            with self.write_lock:
                archived = self.archive.read(first.isoformat(timespec='seconds'),
                                             last.isoformat(timespec='seconds'))
        with self.lock:
            now = time.monotonic()
            self._expire(now)
            incident = uuid.uuid4().hex
            payload = {"schema_version": 2, "application_version": VERSION,
                       "incident_id": incident, "created_at": utc(), "component": self.component,
                       "scope": "Playback process. Recent events: 250/one hour. Preview: latest 500 retained events. Disk archive: details seven days/80 MiB, incidents thirty days/20 MiB; size caps can shorten retention. Successful background polls excluded.",
                       "persistence": self.persistence_status, "environment": self.environment(),
                       "archive": {"enabled": self.archive is not None, "dropped_events": self.archive_dropped,
                                   "details_days": 7, "incident_days": 30, "max_bytes": 100*1024*1024},
                       "incidents": [dict(row) for row in self.incidents if self._restored_row(row)],
                       "diagnosis": self.last_checks,
                       "health": {"http_process": "responding", "devices": "not_checked",
                                  "plex": "not_checked", "topology": "not_collected"},
                       "capabilities": {"raw_debug_logging": False, "automatic_upload": False},
                       "events": [dict(row) for stamp, row in self.events if now - stamp <= 3600]}
            if archived is not None:
                payload['incidents'] = archived['events']
                payload['history_window'] = {'start_utc': start, 'end_utc': end,
                                             'matched': archived['matched'],
                                             'truncated': archived['truncated'], 'limit': 2000}
                payload['events'] = []
            body = (json.dumps(payload, indent=2) + "\n").encode()
            self.bundles[incident] = (now, body)
            while len(self.bundles) > 8:
                self.bundles.popitem(last=False)
            return incident, body

    def _expire(self, now):
        for key, (stamp, _) in list(self.bundles.items()):
            if now - stamp > 600:
                del self.bundles[key]
        while self.events and now - self.events[0][0] > 3600:
            self.events.popleft()

    def download(self, incident):
        with self.lock:
            self._expire(time.monotonic())
            entry = self.bundles.get(incident)
            return entry[1] if entry else None

    def page(self, start='', end=''):
        form = ("<form method='get' action='/diagnostics'><h2>Earlier activity</h2>"
                "<p>Choose a UTC time window. Exports include at most the latest 2,000 matching events; "
                "use a narrower window if the result is truncated.</p>"
                "<label>Start (UTC) <input type='datetime-local' name='start' required></label> "
                "<label>End (UTC) <input type='datetime-local' name='end' required></label> "
                "<button type='submit'>Review history</button></form>")
        try:
            incident, body = self.preview(start, end)
        except (ValueError, OSError):
            return "<h1>Diagnostics</h1><p>History unavailable or invalid time window. Enter both times, with start before end.</p>" + form

        return ("<h1>Diagnostics</h1><p><a href='/dashboard'>Back to dashboard</a></p>"
                + form + "<h2>When playback fails</h2><ol>"
                "<li>Note the time and timezone, Plexamp platform, affected room/group, what you selected, "
                "what Plexamp displayed, and whether you heard audio. Capture evidence before restarting if practical.</li>"
                "<li>Return to the dashboard, find the affected room, and choose <strong>Diagnose playback</strong> once. "
                "Allow roughly 30 seconds. It checks services without changing playback. Opening this page alone does not run it.</li>"
                "<li>Review the JSON below. <code>diagnosis</code> shows the latest room check; <code>incidents</code> "
                "contains earlier events. Failed or not-checked results are useful; successful checks do not prove audible sound.</li>"
                "<li>Choose <strong>Download this reviewed JSON</strong> below. Save it within ten minutes. "
                "If the download expires, reopen Diagnostics and review the new preview.</li>"
                "<li>Open a playback issue in the public repository where you obtained the release, or use your beta support channel. "
                "Attach the reviewed file, symptom/time, Plexamp version/platform, grouping and any recovery steps. "
                "Do not attach credentials, configuration directories or unreviewed logs.</li>"
                "<li>If the room check fails, download the available history here and report that failure too. "
                "The installation includes the full instructions and fallback steps in <code>docs/support.md</code>.</li></ol>"
                "<p>Review the exact JSON below before sharing. It includes event times and outcomes, "
                "including anonymous request, room and track IDs, software versions and device capability checks. "
                "It excludes credentials, addresses, room names, media titles and raw errors. Nothing is uploaded.</p>"
                "<p>Use Diagnose playback on a room card for read-only checks. Recent events reset on restart; "
                "incident history persists for up to seven days when storage is available. "
                "This preview expires after 10 minutes or eight newer previews.</p>"
                f"<p>Incident ID: <code>{incident}</code></p>"
                f"<p><a href='/diagnostics/download/{incident}'>Download this reviewed JSON</a></p>"
                f"<pre style='white-space:pre-wrap;overflow-wrap:anywhere'>{html.escape(body.decode())}</pre>")


class SafeHandler(logging.StreamHandler):
    def __init__(self, diagnostics, stream=None):
        super().__init__(stream)
        self.diagnostics = diagnostics

    def emit(self, record):
        try:
            self.stream.write(self.diagnostics.record(record) + "\n")
            self.flush()
        except Exception:
            # logging.handleError itself dumps unsafe record arguments to stderr.
            pass


def configure(component):
    diagnostics = Diagnostics(component)
    logging.basicConfig(level=logging.INFO, handlers=[SafeHandler(diagnostics)], force=True)
    logging.captureWarnings(True)
    def uncaught(kind, value, traceback):
        logging.getLogger("plex_bridge").error("", extra={"event": "unhandled_error"},
                                              exc_info=(kind, value, traceback))
    sys.excepthook = uncaught
    threading.excepthook = lambda args: uncaught(args.exc_type, args.exc_value, args.exc_traceback)
    return diagnostics


class SafeServerErrors:
    def handle_error(self, request, client_address):
        logging.getLogger("plex_bridge").error("", extra={"event": "unhandled_error"}, exc_info=True)


def request_event(logger, method, args):
    # BaseHTTPRequestHandler uses different argument shapes for malformed input.
    status = args[1] if len(args) > 1 else None
    if type(status) is str and len(status) == 3 and status.isascii() and status.isdigit():
        status = int(status)
    logger.info("", extra={"event": "request", "method": method, "status": status})
