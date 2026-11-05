import threading
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace as NS
from unittest.mock import Mock

from plex_bridge.dashboard import Dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.devices = [NS(id='leader', name='Living <Room>', host='192.0.2.1'), NS(id='follower', name='Bedroom', host='192.0.2.2')]
        self.renderer = Mock()
        self.renderer.command.return_value = 'OK'
        self.volumes = {'leader': 30, 'follower': 20}
        def rendering(device, action, args):
            if action == 'SetVolume': self.volumes[device.id] = dict(args)['DesiredVolume']
            return ET.fromstring(f'<response><CurrentVolume>{self.volumes[device.id]}</CurrentVolume></response>')
        self.renderer.rendering_control.side_effect = rendering
        self.plex = NS(renderer=self.renderer, store=Mock(), endpoint_identifier=lambda x: 'endpoint-' + x,
                       playback_command=Mock(), recoverable=Mock(return_value=False), public_base='http://192.0.2.10:8794')
        self.plex.store.snapshot.return_value = None
        self.c = NS(devices=self.devices, by_id={d.id:d for d in self.devices}, plex=self.plex,
                    groups=Mock(base='http://192.0.2.10:8793'), operation_lock=threading.Lock(),
                    history=Mock(), focus=Mock(), sleep=Mock(), disband=Mock(), spotify=NS(authorized=False))
        self.c.history.snapshot.return_value = []
        self.app = NS(coordinator=self.c, plex=self.plex, plex_cloud=Mock(), onboarding=Mock())
        self.ui = Dashboard(self.app)
        self.rooms = [dict(device=vars(d), raw={'mode':'31','status':'play','vol':'0','mute':'0'},
                           reachable=True, topology_ok=True, online=True, role='follower' if d.id=='follower' else 'standalone/leader',
                           master_host='192.0.2.1', allow_smart_leadership=True) for d in self.devices]
        self.ui.snapshot = Mock(return_value=self.rooms)

    def act(self, action, requester='follower', **extra):
        return self.ui.act({k:[v] for k,v in dict(action=action,requester=requester,**extra).items()})

    def test_follower_transport_targets_leader(self):
        self.act('pause')
        self.renderer.command.assert_called_once_with(self.devices[0], 'setPlayerCmd:pause')

    def test_volume_and_mute_target_single_room_channel(self):
        self.act('volume',volume='0')
        self.renderer.rendering_control.assert_any_call(self.devices[1], 'SetVolume', [('InstanceID',0),('Channel','Single'),('DesiredVolume',0)])
        self.rooms[0]['raw']['vol'] = '30'
        self.act('mute',requester='leader')
        self.renderer.rendering_control.assert_any_call(self.devices[0], 'SetVolume', [('InstanceID',0),('Channel','Single'),('DesiredVolume',0)])
        self.rooms[0]['raw']['vol'] = '0'
        self.act('unmute',requester='leader')
        self.assertEqual(self.volumes['leader'],30)
        self.assertNotIn('leader',self.ui.mute_volumes)

    def test_reject_invalid_volume_without_command(self):
        for value in ('-1','101','vol:80',''):
            with self.assertRaises(ValueError): self.act('volume',volume=value)
        self.renderer.rendering_control.assert_not_called()

    def test_sleep_follower_leaves_then_sleeps(self):
        self.act('sleep')
        self.c.sleep.assert_called_once_with(self.devices[1])
        self.c.disband.assert_not_called()

    def test_sleep_leader_requires_confirmation(self):
        with self.assertRaises(ValueError): self.act('sleep',requester='leader')
        self.c.sleep.assert_not_called()
        self.act('sleep',requester='leader',confirm='yes')
        self.c.sleep.assert_called_once_with(self.devices[0])

    def test_sleep_button_uses_observed_trigger_and_playback(self):
        room = self.rooms[1]
        room.update(role='standalone/leader', master_host='', trigger=0)
        room['raw']['status'] = 'stop'
        self.assertIn('<button disabled>Asleep</button>', self.ui.card(room, self.rooms))
        for trigger, status in [(None, 'stop'), (1, 'stop'), (0, 'play')]:
            room['trigger'], room['raw']['status'] = trigger, status
            card = self.ui.card(room, self.rooms)
            self.assertIn('>Sleep</button>', card)
            self.assertNotIn('>Asleep</button>', card)

    def test_consolidated_disband_respects_sleep_option(self):
        self.act('disband')
        self.c.groups.disband.assert_called_once_with('follower')
        self.c.disband.assert_not_called()
        self.act('disband',sleep_after='yes')
        self.c.disband.assert_called_once_with(self.devices[1])

    def test_source_without_transport_cannot_be_controlled(self):
        self.rooms[0]['raw']['mode']='43'
        with self.assertRaises(ValueError): self.act('next')
        self.renderer.command.assert_not_called()

    def test_old_plex_session_not_used_for_other_source(self):
        self.plex.store.snapshot.return_value=NS(target_device_id='leader', renderer_session_id='old')
        self.act('next')
        self.plex.playback_command.assert_not_called()
        self.renderer.command.assert_called_once_with(self.devices[0], 'setPlayerCmd:next')

    def test_verified_plex_uses_queue_control(self):
        self.plex.store.snapshot.return_value=NS(target_device_id='leader', renderer_session_id='current')
        self.rooms[0]['source_url']='http://bridge/plex/media/current/track'
        self.act('next')
        self.plex.playback_command.assert_called_once_with('skipNext', {}, 'endpoint-leader')
        self.renderer.command.assert_not_called()

    def test_unavailable_or_busy_does_not_send_commands(self):
        self.rooms[1]['reachable']=False
        with self.assertRaises(ValueError): self.act('play')
        self.c.operation_lock.acquire()
        try:
            with self.assertRaises(ValueError): self.act('play')
        finally: self.c.operation_lock.release()
        self.renderer.command.assert_not_called()

    def test_restart_requires_confirmation_and_targets_only_requested_room(self):
        with self.assertRaises(ValueError): self.act('restart')
        self.renderer.command.assert_not_called()
        self.act('restart', confirm='yes')
        self.renderer.command.assert_called_once_with(self.devices[1], 'reboot')
        with self.assertRaises(ValueError): self.act('restart', confirm='yes')
        self.assertEqual(self.renderer.command.call_count, 1)
        self.c.groups.disband.assert_not_called()
        self.plex.playback_command.assert_not_called()

    def test_restart_does_not_require_working_topology(self):
        self.rooms[1]['topology_ok'] = False
        self.act('restart', confirm='yes')
        self.renderer.command.assert_called_once_with(self.devices[1], 'reboot')
        self.assertIn('Group status is unavailable', self.ui.page())

    def test_restart_disconnect_is_uncertain_and_not_retried(self):
        self.renderer.command.side_effect = RuntimeError('connection closed')
        self.assertIn('Restart requested', self.act('restart', confirm='yes'))
        with self.assertRaises(ValueError): self.act('restart', confirm='yes')
        self.assertEqual(self.renderer.command.call_count, 1)

    def test_restart_accepts_json_ack_and_rejects_failure(self):
        self.renderer.command.return_value = {'status': 'OK'}
        self.act('restart', confirm='yes')
        self.renderer.command.return_value = {'status': 'Failed'}
        with self.assertRaises(ValueError): self.act('restart', requester='leader', confirm='yes')

    def test_degraded_card_shows_warning_and_restart(self):
        self.rooms[0]['health'] = dict(degraded=True, playback_ok=False,
            restart_allowed=True, message='Playback service is not responding')
        page = self.ui.page()
        self.assertIn('Playback unavailable', page)
        self.assertIn('Playback service is not responding', page)
        self.assertEqual(page.count('>Restart WiiM</button>'), 2)
        self.assertIn('This room leads a group', page)

    def test_cards_escape_names_show_zero_and_preset_guide(self):
        page=self.ui.page()
        self.assertEqual(page.count('class="room-card"'),2)
        self.assertIn('Living &lt;Room&gt;',page)
        self.assertIn('<output>0</output>',page)
        self.assertIn('Following Living &lt;Room&gt;',page)
        self.assertIn('http://192.0.2.10:8793/preset/join',page)
        self.assertIn('id="preset-guide"',page)
        self.assertNotIn('Full disband',page)
        self.assertIn('Setup complete',page)

if __name__ == '__main__': unittest.main()
