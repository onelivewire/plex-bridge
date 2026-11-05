"""Supervise setup, topology and playback as one unprivileged installation."""
import fcntl
import ipaddress
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from plex_bridge.diagnostics import configure

SERVICES = ('setup', 'topology', 'playback')


def environments(environ=None):
    base = dict(os.environ if environ is None else environ)
    host = str(ipaddress.IPv4Address(base['BRIDGE_HOST']))
    listen = str(ipaddress.IPv4Address(base.get('BRIDGE_LISTEN_HOST', host)))
    root = Path(base.get('BRIDGE_DATA_PATH_INTERNAL', '/data'))
    if not root.is_absolute():
        raise ValueError('Data path must be absolute')
    # Per-service paths must not inherit overrides for a different packaging mode.
    for key in ('CONFIG_PATH', 'CREDENTIALS_PATH', 'RUNTIME_PATH', 'SETUP_PATH',
                'RUNTIME_EXPORTS', 'WAITING_PATH', 'STATE_PATH'):
        base.pop(key, None)
    base.update(HANDOFF_PATH=str(root / 'handoff'), BRIDGE_SERVICE='setup')
    setup = dict(base, SETUP_PATH=str(root / 'setup'), SETUP_HOST=listen,
                 SETUP_PORT='8795', SETUP_ORIGIN=f'http://{listen}:8795',
                 EXPECTED_BRIDGE_ORIGIN=f'http://{host}:8794', RUNTIME_EXPORTS=str(root / 'exports'))
    result = {'setup': setup}
    for service, port in (('topology', '8793'), ('playback', '8794')):
        result[service] = dict(base, BRIDGE_SERVICE=service,
                              RUNTIME_PATH=str(root / 'exports' / service),
                              WAITING_PATH=f'/tmp/bridge-waiting-{service}.json',
                              LISTEN_HOST=listen, LISTEN_PORT=port)
    result['topology'].update(HISTORY_PATH=str(root / 'topology/history.json'),
                              POLICY_PATH=str(root / 'topology/leadership.json'),
                              TRIGGER_STANDBY_ENABLED='0')
    result['playback'].update(STATE_PATH=str(root / 'playback'),
                              GROUP_API_BASE=f'http://{listen}:8793', PLEX_GROUP_ENDPOINT_MODE='local')
    return result


def supervise(commands, envs, stop_requested, grace=8):
    """An exited child stops the whole bundle so Docker can restart it coherently."""
    children = []
    try:
        for service in SERVICES:
            if stop_requested():
                return 0
            children.append(subprocess.Popen(commands[service], env=envs[service], start_new_session=True))
        while not stop_requested():
            if any(child.poll() is not None for child in children):
                logging.getLogger('plex_bridge').error('', extra={'event': 'unhandled_error'})
                return 1
            time.sleep(0.1)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + grace
        for child in children:
            try:
                child.wait(timeout=max(0, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def main():
    configure('runtime')
    os.umask(0o077)
    envs = environments()
    root = Path(envs['setup']['SETUP_PATH']).parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Retained for this process lifetime; a second bundle must not own this state.
    with (root / '.supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for directory in ('setup', 'exports/playback', 'exports/topology', 'handoff', 'playback', 'topology'):
            (root / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        for service in ('topology', 'playback'):
            Path(envs[service]['WAITING_PATH']).unlink(missing_ok=True)
        stopping = False
        def stop(signum, frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        commands = {s: [sys.executable, '-m', 'plex_bridge.setup' if s == 'setup' else 'plex_bridge.runtime']
                    for s in SERVICES}
        return supervise(commands, envs, lambda: stopping)


if __name__ == '__main__':
    sys.exit(main())
