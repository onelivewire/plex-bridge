"""Bounded, read-only playback probes and manually requested restart tracking."""
import logging
import threading
import time


class RoomHealth:
    INTERVAL = 10
    RESTART_TIMEOUT = 120
    RECOVERY_NOTICE_SECONDS = 30

    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}
        self.locks = {}

    def room_lock(self, ident):
        with self.lock:
            return self.locks.setdefault(ident, threading.RLock())

    def _room(self, ident):
        return self.rooms.setdefault(ident, dict(checked=None, failures=0, successes=0,
            playback_ok=None, restart_at=None, restart_pending=False, saw_offline=False, restart_result='', recovered_at=None))

    def check(self, device, reachable, renderer):
        # Dashboard serializes snapshots; restart actions share this lock.
        with self.room_lock(device.id):
            health = self._room(device.id)
            now = time.monotonic()
            if health['checked'] is None or now - health['checked'] >= self.INTERVAL:
                ok = False
                if reachable:
                    try:
                        root = renderer.av_transport(device, 'GetTransportInfo', [('InstanceID', 0)])
                        ok = any(n.tag.split('}')[-1] == 'CurrentTransportState' and n.text
                                 for n in root.iter())
                    except (RuntimeError, OSError, ValueError, TypeError):
                        pass
                was_degraded = health['failures'] >= 2
                health['checked'] = time.monotonic()
                health['playback_ok'] = ok
                health['failures'] = 0 if ok else health['failures'] + 1
                health['successes'] = health['successes'] + 1 if ok else 0
                degraded = health['failures'] >= 2
                if degraded != was_degraded:
                    logging.getLogger('wiim-focus-control').log(
                        logging.WARNING if degraded else logging.INFO,
                        'room playback health changed',
                        extra={'event': 'playback_unavailable' if degraded else 'playback_recovered'})
                if health['restart_pending'] or health['restart_result'].startswith(('Recovery was not', 'WiiM did not')):
                    health['saw_offline'] |= not reachable
                    elapsed = time.monotonic() - health['restart_at']
                    if elapsed >= 15 and reachable and health['successes'] >= 2:
                        health['restart_pending'] = False
                        health['recovered_at'] = time.monotonic()
                        health['restart_result'] = ('Restart verified. Both services are responding. Select this room in Plexamp to resume.'
                            if health['saw_offline'] else
                            'Both services are responding. A reboot was not independently confirmed. Select this room in Plexamp to resume.')
                    elif elapsed >= self.RESTART_TIMEOUT:
                        health['restart_pending'] = False
                        health['restart_result'] = 'Recovery was not verified. Power-cycle this WiiM and check again.'
            return self._view(health, reachable)

    def _view(self, health, reachable):
        if (health['recovered_at'] is not None and
                time.monotonic() - health['recovered_at'] >= self.RECOVERY_NOTICE_SECONDS):
            health['restart_result'] = ''
            health['recovered_at'] = None
        pending = health['restart_pending']
        cooldown = (health['restart_at'] is not None and
                    time.monotonic() - health['restart_at'] < self.RESTART_TIMEOUT)
        if pending:
            message = 'Restart requested; checking device and playback service. Monitoring continues in the background.'
        elif health['restart_result']:
            message = health['restart_result']
        elif not reachable:
            message = 'Device is unreachable. Check its power and network connection.'
        elif health['failures'] >= 2:
            message = 'WiiM is online, but its playback service is not responding. Restarting this WiiM may help.'
        elif health['playback_ok'] is False:
            message = 'Playback service did not respond. Checking again before recommending a restart.'
        else:
            message = ''
        # A later failure supersedes an earlier recovery notice.
        if not pending and health['restart_result'].startswith(('Restart verified.', 'Both services')) and (not reachable or health['failures'] >= 2):
            health['restart_result'] = ''
            health['recovered_at'] = None
            return self._view(health, reachable)
        return dict(playback_ok=health['playback_ok'], degraded=health['failures'] >= 2,
                    restarting=pending, restart_allowed=reachable and not cooldown, message=message)

    def begin_restart(self, ident):
        with self.room_lock(ident):
            health = self._room(ident)
            now = time.monotonic()
            if health['restart_at'] is not None and now - health['restart_at'] < self.RESTART_TIMEOUT:
                raise ValueError('A restart was already requested. Wait for the recovery check before retrying')
            health.update(recovered_at=None, restart_at=now, restart_pending=True, saw_offline=False, restart_result='', checked=None,
                          successes=0)

    def rejected(self, ident):
        with self.room_lock(ident):
            self._room(ident)['restart_pending'] = False
            self._room(ident)['restart_result'] = 'WiiM did not acknowledge the restart. Power-cycle it if playback remains unavailable.'
