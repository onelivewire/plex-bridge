# plex-bridge — beta candidate

Play Plex music on WiiM players and manage rooms from a local dashboard.
Plexamp supplies the library and queue; the bridge supplies room endpoints,
playback controls, native WiiM grouping, and amplifier-off Sleep.
[Symfonium](docs/symfonium.md) is an optional controller, with a dedicated
connection for avoiding duplicate Last.fm reports.

**Version: 0.1.0-beta.5.** Guided beta for Linux x86-64 Docker hosts.
One container, one private data volume, and a browser setup wizard.

## Install and start listening

Follow the **[quick-start](docs/quick-start.md)**:

- **Unraid:** use the supplied [install template](unraid/plex-bridge.xml), enter
  your server address and private setup code, then open WebUI. Independent
  Unraid acceptance and Community Applications listing are pending.
- **Other Linux Docker hosts:** download the two small configuration files, set
  your server address, and run `docker compose up -d`. Docker downloads the
  versioned prebuilt image; no source build is required.

The wizard discovers devices, creates your administrator login, guides Plex
linking, and checks actual playback. Plex can run on another machine. You need
Plexamp, a Plex music library and at least one WiiM on the same trusted LAN.
Reserve their addresses in your router. Ports **8793–8795** must be available.

This beta supports **one active Plex queue per installation**, rather than
independent simultaneous Plex queues per room. Do not expose it to the internet.
ARM, Docker Desktop and routed VLAN discovery are not accepted targets.
See [validation](docs/validation.md) and [security](SECURITY.md).

For a source build, use `docker compose -f compose.source.yaml build` followed
by `docker compose -f compose.source.yaml up -d` with the included `.env`.
Existing split installations should follow [split-layout guidance](docs/split-layout.md);
changing Compose files does not migrate their data.

## Use

Open `http://YOUR-LAN-IP:8794/dashboard` and sign in with your new administrator.
Each room shows its current playback/source and group membership. Transport
controls on a follower operate the group leader; volume affects that room.
Mute saves its volume, sets it to zero, and Unmute restores the saved level.

Sleep detaches the selected room as needed, stops playback, and explicitly
turns its 12 V trigger off after verifying playback stopped. That turns off a
trigger-connected amplifier; it does **not** prove the WiiM entered the remote's
standby mode. A model without trigger telemetry receives a shutdown request.
Sleeping a group leader requires confirmation because the group is dissolved.
Disband offers a single optional “sleep afterward” checkbox.

Use **Set up WiiM presets** in the dashboard for your installation's exact URLs,
or follow [the preset guide](docs/presets.md). Join needs an eligible playing
leader; enable only the rooms you want as automatic join targets in dashboard
settings. Focus requires recoverable bridge-owned Plex playback. Spotify Focus
and standby-trigger automation are experimental and are not configured by the
beta installer. Native WiiM Plex playback is not a bridge-owned queue.

## Feature guide

The [complete usage guide](docs/using-the-bridge.md) explains every dashboard
control, its effect on grouped rooms, and the beta's experimental controls.

| What you want to do | Instructions |
| --- | --- |
| Install, discover devices, link Plex, and verify sound | [Setup](docs/setup.md) |
| Start music, read room status, control transport/volume/mute | [Using the bridge](docs/using-the-bridge.md#choose-music-and-read-room-status) |
| Join rooms, choose automatic leaders, or move playback | [Grouping and Focus](docs/using-the-bridge.md#join-a-playing-group-and-choose-eligible-leaders) |
| Leave/disband groups, Sleep, or restart a WiiM | [Room actions](docs/using-the-bridge.md#leave-disband-and-sleep) |
| Assign WiiM hardware/remote presets | [Presets](docs/presets.md) |
| Browse with Symfonium and manage Last.fm | [Symfonium and Last.fm](docs/symfonium.md) |
| Understand diagnostics, history, and logs | [Support](docs/support.md) |
| Back up, restore, update, roll back, or uninstall | [Maintenance](docs/maintenance.md) |
| Understand experimental controls and unsupported setups | [Feature limits](docs/using-the-bridge.md#experimental-and-unsupported-features) |

## Help and maintenance

- [Setup and network addresses](docs/setup.md)
- [Optional Symfonium setup and Last.fm](docs/symfonium.md)
- [Step-by-step playback problem reporting](docs/support.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Backup, restore, upgrade, rollback, and uninstall](docs/maintenance.md)
- [Beta test checklist and limitations](docs/beta-testing.md)
- [Security reporting](SECURITY.md)
- [Release process](docs/releasing.md)

Plex and WiiM are third-party products; this project is not affiliated with or
endorsed by their manufacturers. Application source is distributed under the [MIT license](LICENSE); see
[provenance and third-party components](NOTICE).

For playback problems, use **Diagnose playback** on the affected room and follow
the [support workflow](docs/support.md). Review the exact diagnostic JSON before
sharing; the bridge never uploads it automatically.
