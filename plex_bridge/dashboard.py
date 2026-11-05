"""Room dashboard: read-only status collection and explicit authenticated actions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import html
import json
import logging
from pathlib import Path
from plex_bridge.provisioning import atomic_write
from plex_bridge.room_health import RoomHealth
from plex_bridge.diagnostics import dashboard_traced
import threading
import time


def esc(value):
    return html.escape(str(value), quote=True)


def metadata_label(value):
    # WiiM uses this misspelled placeholder for inputs without track metadata.
    return 'Unknown' if isinstance(value, str) and value.strip().casefold() == 'unknow' else value


SOURCES = {'0': 'No source', '1': 'AirPlay', '2': 'DLNA', '10': 'Network playlist',
           '11': 'USB', '16': 'SD card', '31': 'Spotify Connect', '32': 'TIDAL Connect',
           '40': 'Line input', '41': 'Bluetooth', '42': 'Storage', '43': 'Optical input'}
TRANSPORT_MODES = {'1', '2', '10', '11', '16', '31', '32', '41', '42'}
SKIP_MODES = {'10', '11', '16', '31', '32', '42'}


class Dashboard:
    def __init__(self, app):
        self.app = app
        self.cache_lock = threading.Lock()
        self.cached = None
        self.cached_at = 0
        self.health = RoomHealth()
        state_path = getattr(getattr(app.plex, 'store', None), 'state_path', None)
        self.mute_path = state_path.with_name('dashboard-volume.json') if isinstance(state_path, Path) else None
        self.mute_volumes = {}
        if self.mute_path and self.mute_path.exists():
            try:
                saved = json.loads(self.mute_path.read_text())
                self.mute_volumes = {k:v for k,v in saved.items() if k in app.coordinator.by_id and type(v) is int and 0 < v <= 100}
            except (OSError, ValueError, AttributeError):
                pass

    @property
    def coordinator(self):
        return self.app.coordinator

    def monitor_once(self):
        # Independent of topology, metadata, and browser polling.
        c = self.coordinator
        def read(device):
            try:
                raw = c.plex.renderer.command(device, 'getPlayerStatus')
                reachable = isinstance(raw, dict) and 'status' in raw
            except (RuntimeError, OSError, ValueError, TypeError):
                reachable = False
            with self.app.diagnostics.room_scope(device.id):
                self.health.check(device, reachable, c.plex.renderer)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(read, c.devices))

    def monitor_forever(self):
        while True:
            try:
                self.monitor_once()
            except Exception:
                logging.getLogger('wiim-focus-control').exception(
                    'room health monitor failed', extra={'event': 'monitor_failed'})
            time.sleep(self.health.INTERVAL)

    def snapshot(self, fresh=False):
        with self.cache_lock:
            if not fresh and self.cached is not None and time.monotonic() - self.cached_at < 3:
                return self.cached
            c = self.coordinator
            try:
                players = c.groups.players()
                topology_ok = True
            except (RuntimeError, OSError, ValueError, KeyError, TypeError):
                players, topology_ok = [], False
            by_id = {p['device']['id']: p for p in players}
            saved_session = c.plex.store.snapshot()

            def read(device):
                state = dict(by_id.get(device.id, {}))
                state['device'] = {'id': device.id, 'name': device.name, 'host': device.host}
                state['topology_ok'] = topology_ok and device.id in by_id
                state['trigger'] = None
                try:
                    trigger = c.plex.renderer.command(device, 'getTriggeroutStatus')
                    if isinstance(trigger, dict) and str(trigger.get('status')) in {'0', '1'}:
                        state['trigger'] = int(trigger['status'])
                except (RuntimeError, OSError, ValueError, TypeError):
                    pass
                try:
                    raw = c.plex.renderer.command(device, 'getPlayerStatus')
                    if not isinstance(raw, dict) or 'status' not in raw:
                        raise ValueError('Invalid status')
                    state.update(raw=raw, reachable=True)
                    if saved_session and saved_session.target_device_id == device.id:
                        try:
                            state['source_url'] = c.plex.renderer.current_track(device)[0]
                        except (RuntimeError, OSError, ValueError):
                            state['source_url'] = ''
                    try:
                        metadata = c.plex.renderer.command(device, 'getMetaInfo')
                        state['metadata'] = metadata if isinstance(metadata, dict) else {}
                    except (RuntimeError, OSError, ValueError):
                        state['metadata'] = {}
                except (RuntimeError, OSError, ValueError, TypeError):
                    state.update(raw={}, reachable=False)
                state['health'] = self.health.check(device, state['reachable'], c.plex.renderer)
                return state
            with ThreadPoolExecutor(max_workers=6) as pool:
                rooms = list(pool.map(read, c.devices))
            self.cached = rooms
            self.cached_at = time.monotonic()
            return rooms

    def context(self, room, rooms):
        if not room.get('topology_ok'):
            return None, [], 'Group status unavailable'
        host = room['device']['host']
        if room.get('role') == 'follower':
            leader = next((r for r in rooms if r['device']['host'] == room.get('master_host')), None)
            if leader is None:
                return None, [], 'Group leader unavailable'
        else:
            leader = room
        members = [r for r in rooms if r is leader or
                   (r.get('role') == 'follower' and r.get('master_host') == leader['device']['host'])]
        label = ('Following ' + leader['device']['name'] if leader is not room else
                 'Leading group' if len(members) > 1 else 'Standalone')
        return leader, members, label

    def plex_session(self, leader):
        if leader is None or not leader.get('reachable'):
            return None
        session = self.coordinator.plex.store.snapshot()
        # Never label an old saved session as the current source. The bridge's
        # signed media path must still be physically loaded on this renderer.
        if session and session.target_device_id == leader['device']['id']:
            raw = leader['raw']
            source = leader.get('source_url', '')
            if session.renderer_session_id and f'/plex/media/{session.renderer_session_id}/' in source:
                return session
        return None

    @dashboard_traced
    def act(self, form):
        c = self.coordinator
        get = lambda key: form.get(key, [''])[0]
        requester, action = get('requester'), get('action')
        if requester not in c.by_id:
            raise ValueError('Unknown room')
        if not c.operation_lock.acquire(blocking=False):
            raise ValueError('Another room action is running; wait for it to finish')
        try:
            if action == 'restart':
                if get('confirm') != 'yes':
                    raise ValueError('Confirm restarting this WiiM; playback and any group it leads may be interrupted')
                self.health.begin_restart(requester)
                # One attempt only. A disconnect can mean the reboot was accepted.
                try:
                    result = c.plex.renderer.command(c.by_id[requester], 'reboot')
                    ack = result.get('status') if isinstance(result, dict) else result
                    if isinstance(ack, str) and ack.strip().lower() == 'unknown command':
                        logging.getLogger('wiim-focus-control').warning(
                            'restart command unsupported', extra={'event': 'restart_fallback'})
                        result = c.plex.renderer.command(c.by_id[requester], 'StartRebootTime:1')
                except (RuntimeError, OSError, ValueError):
                    result = None
                if result is not None:
                    ack = result.get('status') if isinstance(result, dict) else result
                    if str(ack).strip().upper() != 'OK':
                        self.health.rejected(requester)
                        raise ValueError('WiiM did not acknowledge the restart; check room status')
                c.history.record('restart', c.by_id[requester].name, 'requested', 'Manual WiiM restart requested; recovery pending')
                return 'Restart requested. Checking device and playback service; playback will not be started automatically.'
            rooms = self.snapshot(fresh=True)
            room = next(r for r in rooms if r['device']['id'] == requester)
            if not room['reachable'] or not room['topology_ok']:
                raise ValueError('Room or group status unavailable; check the connection')
            leader, members, _ = self.context(room, rooms)
            device = c.by_id[requester]
            if action in {'volume', 'mute', 'unmute'}:
                if action == 'volume':
                    value = int(get('volume'))
                elif action == 'mute':
                    current = int(room['raw'].get('vol', -1))
                    if not 0 < current <= 100:
                        raise ValueError('Room is already silent or its volume is unavailable')
                    self.mute_volumes[requester] = current
                    if self.mute_path:
                        atomic_write(self.mute_path, self.mute_volumes)
                    value = 0
                else:
                    if requester not in self.mute_volumes or str(room['raw'].get('vol')) != '0':
                        raise ValueError('No dashboard mute to restore')
                    value = self.mute_volumes[requester]
                if not 0 <= value <= 100:
                    raise ValueError('Volume must be between 0 and 100')
                c.plex.renderer.rendering_control(device, 'SetVolume',
                    [('InstanceID', 0), ('Channel', 'Single'), ('DesiredVolume', value)])
                verified = c.plex.renderer.rendering_control(device, 'GetVolume',
                    [('InstanceID', 0), ('Channel', 'Single')])
                observed = next((e.text for e in verified.iter() if e.tag.split('}')[-1] == 'CurrentVolume'), None)
                if observed != str(value):
                    raise ValueError('Volume command sent, but the new level could not be verified')
                if action != 'mute':
                    self.mute_volumes.pop(requester, None)
                    if self.mute_path:
                        atomic_write(self.mute_path, self.mute_volumes)
            elif action in {'play', 'pause', 'previous', 'next'}:
                if leader is None or not leader['reachable']:
                    raise ValueError('Group leader unavailable')
                mode = str(leader['raw'].get('mode', ''))
                session = self.plex_session(leader)
                allowed = SKIP_MODES if action in {'previous', 'next'} else TRANSPORT_MODES
                if not session and mode not in allowed:
                    raise ValueError('This source must be controlled in its source app')
                target = c.by_id[leader['device']['id']]
                if session:
                    command = {'previous': 'skipPrevious', 'next': 'skipNext'}.get(action, action)
                    c.plex.playback_command(command, {}, c.plex.endpoint_identifier(target.id))
                else:
                    command = {'play': 'resume', 'pause': 'pause', 'previous': 'prev', 'next': 'next'}[action]
                    result = c.plex.renderer.command(target, 'setPlayerCmd:' + command)
                    if str(result).strip().upper() != 'OK':
                        raise ValueError('WiiM did not acknowledge the playback command')
            elif action in {'sleep', 'leave', 'disband'}:
                if leader is None:
                    raise ValueError('Group status unavailable')
                if action in {'sleep', 'leave'} and leader is room and len(members) > 1 and get('confirm') != 'yes':
                    raise ValueError('This room leads a group. Confirm dissolving the group first')
                if action == 'sleep':
                    c.sleep(device)
                elif action == 'leave':
                    c.groups.leave(requester)
                elif get('sleep_after') == 'yes':
                    c.disband(device)
                else:
                    c.groups.disband(requester)
            elif action == 'join':
                c.groups.auto_join(requester)
            elif action == 'focus':
                if not c.plex.recoverable():
                    if not c.spotify.authorized or not (c.spotify.playback() or {}).get('item'):
                        raise ValueError('No recoverable Plex or Spotify playback to move')
                c.focus(device)
                event = c.history.snapshot()[0]
                return event.get('detail', 'Move completed; check the room status')
            else:
                raise ValueError('Unknown action')
            c.history.record(action, device.name, 'completed', 'Dashboard action completed')
            return f'{device.name}: {action.capitalize()} completed'
        finally:
            self.cached_at = 0
            c.operation_lock.release()

    def card(self, room, rooms):
        device, raw = room['device'], room['raw']
        ident = esc(device['id'])
        leader, members, group = self.context(room, rooms)
        available = room['reachable'] and room['topology_ok']
        source_raw = leader['raw'] if leader and leader['reachable'] else {}
        mode = str(source_raw.get('mode', ''))
        session = self.plex_session(leader)
        state = {'play': 'Playing', 'pause': 'Paused', 'stop': 'Idle', 'load': 'Loading', 'loading': 'Loading'}.get(str(source_raw.get('status')), 'Unknown')
        source = 'Plex' if session else SOURCES.get(mode, 'Source unavailable')
        metadata = leader.get('metadata', {}) if leader else {}
        metadata = metadata.get('metaData', metadata)
        if not isinstance(metadata, dict):
            metadata = {}
        title = session.current.title if session else metadata_label(metadata.get('title', ''))
        artist = session.current.artist if session else metadata_label(metadata.get('artist', ''))
        lead_group = leader is room and len(members) > 1

        def form(action, label, enabled=True, extra='', confirmation=False):
            warning = ' data-confirm="This room leads the group. Continuing will dissolve the group and may interrupt the other rooms."' if confirmation else ''
            return (f'<form class="room-action" method="post" action="/dashboard/action"{warning}>'
                    f'<input type="hidden" name="requester" value="{ident}">'
                    f'<input type="hidden" name="action" value="{action}">{extra}'
                    f'<button {"" if enabled and available else "disabled"}>{label}</button></form>')

        volume = raw.get('vol', '')
        try:
            volume = max(0, min(100, int(volume)))
        except (ValueError, TypeError):
            volume = None
        volume_ui = form('volume', 'Set volume', volume is not None,
            f'<label>Room volume <output>{volume if volume is not None else "—"}</output>'
            f'<input aria-label="Volume for {esc(device["name"])}" type="range" name="volume" min="0" max="100" value="{volume if volume is not None else 0}"></label>')
        muted = device['id'] in self.mute_volumes and volume == 0
        volume_ui += form('unmute' if muted else 'mute', 'Unmute' if muted else 'Mute', muted or (volume is not None and volume > 0))
        transport = bool(session) or mode in TRANSPORT_MODES
        skip = bool(session) or mode in SKIP_MODES
        controls = form('previous', 'Previous', skip) + form('pause' if state == 'Playing' else 'play', 'Pause' if state == 'Playing' else 'Play', transport) + form('next', 'Next', skip)
        group_controls = form('join', 'Join playing group', any(r is not room and r.get('reachable') and r.get('allow_smart_leadership') and r['raw'].get('status') == 'play' and r.get('role') != 'follower' and r not in members for r in rooms))
        group_controls += form('focus', 'Move playback here', self.coordinator.plex.recoverable() or self.coordinator.spotify.authorized)
        if len(members) > 1:
            group_controls += form('leave', 'Leave group', confirmation=lead_group)
        asleep = (available and room.get('trigger') == 0
                  and raw.get('status') == 'stop' and len(members) == 1)
        group_controls += form('sleep', 'Asleep' if asleep else 'Sleep',
                               not asleep, confirmation=lead_group)
        disband = form('disband', 'Disband group', len(members) > 1,
            '<label class="check"><input type="checkbox" name="sleep_after" value="yes">Put rooms to sleep afterward</label>')
        endpoint = self.coordinator.plex.endpoint_identifier(device['id'])
        linked = bool(self.app.plex_cloud and self.app.plex_cloud.linked(endpoint))
        health = room.get('health', {})
        status = ('Restarting' if health.get('restarting') else 'Playback unavailable'
                  if room['reachable'] and health.get('degraded') else
                  'Checking playback' if room['reachable'] and health.get('playback_ok') is False else
                  'Online' if room['reachable'] else 'Unreachable')
        health_message = f'<p class="health-message" role="status">{esc(health.get("message", ""))}</p>'
        diagnose = (f'<form method="post" action="/diagnostics/run">'
                    f'<input type="hidden" name="requester" value="{ident}">'
                    '<button>Diagnose playback</button></form>')
        restart_warning = ('Restart this WiiM? Playback will stop. '
                           + ('This room leads a group; other rooms may be interrupted. ' if lead_group else '')
                           + ('Group status is unavailable; other rooms may be interrupted. ' if not room['topology_ok'] else '')
                           + 'Settings will be preserved. Playback will not resume automatically.')
        restart = (f'<form class="room-action" method="post" action="/dashboard/action" data-confirm="{esc(restart_warning)}">'
                   f'<input type="hidden" name="requester" value="{ident}">'
                   '<input type="hidden" name="action" value="restart">'
                   f'<button {"" if health.get("restart_allowed", room["reachable"]) else "disabled"}>Restart WiiM</button></form>')
        return f'''<article class="room-card" id="room-{ident}" data-room="{ident}">
<header><h2>{esc(device['name'])}</h2><span class="badge {'online' if room['reachable'] and status == 'Online' else 'offline'}">{status}</span></header>
{health_message}{restart if health.get("degraded") else ""}{diagnose}
<div class="room-state"><strong>{state}</strong><span>{esc(source)}</span></div>
<p class="track">{esc(title or ('No playback' if state == 'Idle' else 'Track information unavailable'))}<small>{esc(artist)}</small></p>
<p class="group-label">{esc(group)}</p><p class="muted">{esc(', '.join(r['device']['name'] for r in members) if len(members) > 1 else 'This room plays independently.')}</p>
<div class="transport actions">{controls}</div><p class="muted">{'Playback controls affect the group.' if len(members)>1 else 'Playback controls affect this room.'} {'Use the source app for unavailable controls.' if not skip or not transport else ''}</p>
<div class="volume actions">{volume_ui}</div><p class="muted">Volume and mute affect only this WiiM. Mute saves the level, then sets it to zero.</p>
<div class="actions grouping">{group_controls}</div><p class="room-result" role="status" aria-live="polite"></p>
<details><summary>Room details &amp; group options · Plex {'linked' if linked else 'needs attention'}</summary>
<p>Address: {esc(device['host'])}</p><p>Move playback here transfers recoverable playback from the current source.</p>
<p>{'Sleeping or removing this leader dissolves its group.' if lead_group else 'Sleep leaves the group, stops playback, and turns off the amplifier trigger.'}</p>
{disband}{restart if not health.get("degraded") else ""}<p><a href="/onboarding?step=accounts#link-{esc(endpoint)}">Manage Plex link</a></p></details></article>'''

    def preset_guide(self):
        urls = [
            ('Join playing group', self.coordinator.groups.base + '/preset/join', 'Joins the one eligible playing session. If several sessions are playing, choose a group explicitly in WiiM Home.'),
            ('Move playback here', self.app.plex.public_base + '/preset/focus', 'Recovers supported Plex or Spotify playback here and sleeps the former group.'),
            ('Disband and sleep', self.app.plex.public_base + '/preset/disband', 'Dissolves the requesting room’s group and puts its rooms to sleep.'),
        ]
        fields = ''.join(f'<p><strong>{esc(name)}</strong> — {esc(detail)}</p><label>Stream URL<input class="preset-url" readonly value="{esc(url)}" aria-label="{esc(name)} URL"></label><button type="button" class="copy-url">Copy URL</button>' for name, url, detail in urls)
        return f'''<details class="settings" id="preset-guide"><summary>Set up WiiM presets</summary>
<p>Existing presets using these URLs keep working. The same URLs work on every enrolled WiiM; the bridge identifies the room by its reserved IP address.</p>
<ol><li>Select the WiiM you want to configure in WiiM Home.</li>
<li>Open <strong>Browse → Open Network Stream → Add Stream</strong>.</li>
<li>Give the stream one of the names below, paste its URL, and save it. “Test Stream” actually runs the action—use it only when ready.</li>
<li>Open the stream’s three-dot menu, choose <strong>Preset</strong>, and select your preferred preset slot. Keep existing slot assignments if you already use them.</li>
<li>Repeat for the other actions and rooms. Use the matching preset button on supported WiiM hardware or remotes.</li></ol>
{fields}<p class="copy-result" role="status"></p>
<p>To try Join, start music in one eligible room, then press the preset in another. Allow a few seconds for the group to settle. Living Room and Theater Room should stay excluded from automatic leadership if their TV audio must not attract other rooms.</p>
<p>Use this dashboard for Leave and Sleep. A network-stream preset replaces the current source before it reaches the bridge, so a Leave preset can interrupt the whole group.</p>
<p><a href="https://faq.wiimhome.com/en/support/solutions/articles/72000636011-tutorial-using-the-open-network-stream-service" target="_blank" rel="noopener">WiiM’s illustrated network-stream guide</a></p></details>'''

    def page(self, message=''):
        rooms = self.snapshot()
        complete = self.app.onboarding and self.app.onboarding.is_complete()
        setup = '<span class="badge online">Setup complete</span> <a href="/onboarding?step=connections">Connection checks</a>' if complete else '<a href="/onboarding">Finish setup and check connections</a>'
        cards = ''.join(self.card(r, rooms) for r in rooms)
        leadership = ''.join(f'<label class="check"><input type="checkbox" name="enabled" value="{esc(r["device"]["id"])}" {"checked" if r.get("allow_smart_leadership") else ""}>{esc(r["device"]["name"])}</label>' for r in rooms)
        events = ''.join(f'<li>{esc(e.get("timestamp", ""))} · {esc(e.get("requester", ""))} · {esc(e.get("action", ""))}: {esc(e.get("outcome", ""))} — {esc(e.get("detail", ""))}</li>' for e in self.coordinator.history.snapshot()[:20])
        now = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
        return STYLE + f'''<main><header class="dash-header"><div><p class="eyebrow">WHOLE-HOME AUDIO</p><h1>Your rooms</h1></div><nav>{setup} · <a href="#preset-guide">Set up WiiM presets</a> · <a href="/diagnostics">Diagnostics</a></nav></header>
<p class="notice" role="status">{esc(message)}</p><div id="room-dashboard"><p id="refresh-status" role="status">Checked {now} · updates automatically</p>
<div class="room-grid" id="plex-endpoints">{cards}</div></div>
{self.preset_guide()}
<details class="settings"><summary>Settings &amp; connections</summary><h2>Smart leadership</h2><p>Checked rooms can lead “Join playing group.” Explicit grouping and moving playback are unaffected.</p><form class="policy" method="post" action="/dashboard/leadership">{leadership}<button>Save leadership</button></form>
<h2>Spotify</h2><p>{'Authorized' if self.coordinator.spotify.authorized else 'Not authorized'}</p><form method="post" action="/spotify/start"><button>Authorize Spotify</button></form>
<p>Preset URLs: <code>{esc(self.app.plex.public_base)}/preset/focus</code> and <code>{esc(self.app.plex.public_base)}/preset/disband</code>.</p></details>
<details class="settings"><summary>Recent activity</summary><ul>{events or '<li>No actions recorded.</li>'}</ul></details></main>''' + SCRIPT


STYLE = '''<style>
.preset-url{display:block;width:min(100%,36rem);box-sizing:border-box}body{max-width:86rem;background:#111820;color:#e9eff6}h1{font-size:2.2rem;margin:.3rem 0 1.4rem}h2{font-size:1.25rem;margin:0}button{background:#263a4d;border-color:#45627a;min-height:44px}button:hover{background:#34516b}button:disabled{opacity:.4;cursor:not-allowed}button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #8ecbff;outline-offset:3px}.dash-header,.room-card header{display:flex;justify-content:space-between;gap:1rem;align-items:center}.dash-header{flex-wrap:wrap}.eyebrow{font-size:.75rem;letter-spacing:.15em;color:#9bb8d1}.room-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,23rem),1fr));gap:1rem}.room-card{background:#1b2632;border:1px solid #3a4e61;border-radius:1rem;padding:1.25rem;min-width:0}.badge{font-size:.8rem;border-radius:2rem;padding:.3rem .65rem;white-space:nowrap}.online{color:#b5efcf;background:#203f35}.offline{color:#ffd6c6;background:#59392e}.room-state{display:flex;justify-content:space-between;margin-top:1.4rem;gap:1rem}.room-state strong{font-size:1.25rem}.track{min-height:3rem;overflow-wrap:anywhere}.track small{display:block;color:#abc0d0;margin-top:.4rem}.muted,#refresh-status{font-size:.85rem;color:#aec0d0}.group-label{margin-bottom:.3rem}.volume{border-block:1px solid #3a4e61;padding:.7rem 0;align-items:center}.volume label{display:block}.volume input[type=range]{width:13rem;max-width:100%;accent-color:#92ccff}.volume output{font-variant-numeric:tabular-nums}.grouping{margin-top:1rem}.room-card details{border-top:1px solid #3a4e61;padding-top:1rem;margin-top:1rem;font-size:.9rem}summary{cursor:pointer;min-height:32px}.settings{margin-top:1rem;border:1px solid #3a4e61;padding:1rem;border-radius:.7rem}.room-result{min-height:1.2em;font-size:.9rem;color:#b8dcff}.room-result.busy:before{content:'';display:inline-block;width:.8rem;height:.8rem;border:2px solid #7993aa;border-top-color:transparent;border-radius:50%;margin-right:.5rem;animation:spin .8s linear infinite}.notice:empty,.health-message:empty{display:none}.health-message{padding:.7rem;background:#493e28;border-radius:.4rem}.stale .badge{background:#594b25;color:#ffecb3}@keyframes spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.room-result.busy:before{animation:none}}@media(max-width:540px){body{padding:.7rem;margin:0}.room-card{padding:1rem}.dash-header nav{line-height:2}}
</style>'''

SCRIPT = '''<script>
(() => {
  let pending = false, refreshing = false, generation = 0;
  const root = () => document.querySelector('#room-dashboard');
  const results = new Map();
  async function refresh() {
    if (pending || refreshing || document.hidden) return;
    refreshing = true;
    const version = generation;
    try {
      const response = await fetch('/dashboard', {cache:'no-store'});
      if (!response.ok) throw Error('Status check failed');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const fresh = page.querySelector('#room-dashboard');
      if (!fresh) throw Error('Sign in again to check status');
      if (pending || version !== generation) return;
      root().querySelectorAll('.room-card').forEach(card => {
        const replacement = [...fresh.querySelectorAll('.room-card')].find(r => r.dataset.room === card.dataset.room);
        if (!replacement || card.contains(document.activeElement) || card.dataset.dirty === 'true') return;
        replacement.querySelector('details').open = card.querySelector('details').open;
        replacement.querySelector('.room-result').textContent = results.get(card.dataset.room) || '';
        card.replaceWith(replacement);
      });
      root().classList.remove('stale');
      document.querySelector('#refresh-status').textContent = fresh.querySelector('#refresh-status').textContent;
    } catch (error) {
      root().classList.add('stale');
      document.querySelector('#refresh-status').textContent = 'Status is stale — ' + error.message + '. Retrying…';
      root().querySelectorAll('.badge').forEach(b => b.textContent = 'Status stale');
    } finally { refreshing = false; }
  }
  document.addEventListener('click', async event => {
    if (event.target.matches('a[href="#preset-guide"]')) document.querySelector('#preset-guide').open = true;
    if (!event.target.matches('.copy-url')) return;
    const input = event.target.previousElementSibling.querySelector('input');
    input.select();
    let copied = false;
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(input.value); copied = true; }
      else copied = document.execCommand('copy');
    } catch (_) {}
    document.querySelector('.copy-result').textContent = copied ? 'URL copied.' : 'URL selected. Use Copy on your device.';
  });
  document.addEventListener('input', event => {
    if (event.target.matches('.room-card input[type=range]')) {
      event.target.closest('label').querySelector('output').value = event.target.value;
      event.target.closest('.room-card').dataset.dirty = 'true';
    }
  });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('.room-action')) return;
    event.preventDefault();
    if (pending) return;
    const data = new URLSearchParams(new FormData(form));
    if (form.dataset.confirm) {
      if (!confirm(form.dataset.confirm)) return;
      data.set('confirm', 'yes');
    }
    pending = true; generation++;
    const card = form.closest('.room-card'), status = card.querySelector('.room-result');
    const label = event.submitter?.textContent || 'Action';
    status.classList.add('busy'); status.textContent = label + ' — working…';
    card.setAttribute('aria-busy','true');
    const enabled = [...root().querySelectorAll('button:not(:disabled)')];
    enabled.forEach(b => b.disabled = true);
    try {
      // A named control (our hidden `action` input) masks form.action.
      const response = await fetch(form.getAttribute('action'), {method:'POST',headers:{'Accept':'application/json'},body:data});
      if (response.redirected || response.status === 401) {
        status.textContent = 'Your dashboard session expired. Sign in again before using controls.';
      } else if (response.headers.get('Content-Type')?.includes('application/json')) {
        const result = await response.json();
        status.textContent = result.message;
      } else {
        status.textContent = 'The bridge returned HTTP ' + response.status + ' instead of an action result. Refresh the dashboard and check the room status before retrying.';
      }
    } catch (_) {
      status.textContent = 'Connection interrupted. The action may have completed; check status before retrying.';
    } finally {
      results.set(card.dataset.room, status.textContent);
      status.classList.remove('busy'); card.removeAttribute('aria-busy');
      card.dataset.dirty = 'false'; enabled.forEach(b => b.disabled = false);
      document.activeElement?.blur(); pending = false;
      await refresh();
    }
  });
  setInterval(refresh, 5000);
})();
</script>'''
