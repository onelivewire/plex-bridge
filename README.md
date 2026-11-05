# plex-bridge — beta candidate

Play Plex music on WiiM players and manage rooms from a local dashboard.
Plexamp supplies the library and queue; the bridge supplies room endpoints,
playback controls, native WiiM grouping, and amplifier-off Sleep.
[Symfonium](docs/symfonium.md) is an optional controller, with a dedicated
connection for avoiding duplicate Last.fm reports.

**Version: 0.1.0-beta.2.** This is a candidate for a small, supervised beta,
not a broadly certified release. See [validation results](docs/validation.md) and the [tester checklist](docs/beta-testing.md).
No published image registry is required: build the three images from this source.

## Before installing

- A Linux Docker Engine host with Docker Compose v2, a reserved LAN IPv4 address,
  and ports **8793, 8794, 8795** available. Host networking is required.
- A Plex music server, a Plex account with access to it, Plexamp, and at least
  one WiiM on the same trusted LAN. Reserve their addresses in your router.
- Internet access for the initial image build and Plex account linking.
- Permission to control the selected rooms during testing.

Plex and the bridge can run on different machines. This release supports
**one active Plex queue per installation**, even though each room appears as
an endpoint. Selecting another room can move/replace that session; this is not
independent simultaneous Plex playback per room. Native WiiM groups share audio.

Do not expose these ports to the internet. Plex Companion controls trust LAN
clients and do not authenticate them using the dashboard password. External reverse
proxies, Docker Desktop, routed VLAN discovery, and IPv6 deployment are not
accepted beta targets. Read [security and privacy](SECURITY.md).

## Install

Start in the directory containing this README and `compose.yaml` from the
reviewed release archive. Do not copy an existing installation's private data.

```sh
cp deployment.env.example .env
```

Edit `.env`: replace the documentation-only `BRIDGE_HOST=192.0.2.10` with
this Docker host's reserved LAN IPv4 address. Leave the release version intact.
Run Docker commands with the permissions required on your host.

```sh
docker compose build
docker compose up -d
docker compose ps
docker compose exec setup python -c 'import json; from pathlib import Path; print(json.loads(Path("/private/setup-code.json").read_text())["code"])'
```

The last command displays your private setup code. It prevents someone else on
the LAN from claiming the installation before you. Do not post it in an issue.
Open `http://YOUR-LAN-IP:8795` using the address you configured, enter that code,
and follow [the complete setup guide](docs/setup.md), including its final step
to bookmark the dashboard and practice collecting a support download. Keep the
included [step-by-step playback support instructions](docs/support.md) with your
installation. They cover failures, reviewing results, submitting a report, and
what to do when the dashboard is unavailable.

The setup, playback, and topology containers start together. Until local setup
finishes, the latter two wait for configuration; a healthy container alone does
not mean Plex linking or room verification is complete.

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
