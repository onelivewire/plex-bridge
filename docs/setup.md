# Setup, from first launch to music

## Installing on Unraid

Plex Bridge runs alongside your existing Plex server and makes your WiiMs
available as Plexamp playback destinations. Browse music in Plexamp, choose a
room, and use the bridge dashboard for grouping, volume, and Sleep. Symfonium
is an optional controller to add after normal playback works.

This is a **guided beta installation**, not a tested Unraid-specific package or
one-click Community Applications template. Linux amd64 Docker acceptance has
passed; independent installation on Unraid remains pending. The following
adapts the normal setup to Unraid without claiming platform validation.

### Prepare Docker, networking, and the release files

1. Enable Docker in Unraid. Install a compatible
   [Compose Manager plugin](https://github.com/mstrhakr/compose_plugin) following
   its instructions; it supplies the Compose CLI and stack management UI.
   Check `docker compose version` in the Unraid terminal. Unraid's
   [Docker overview](https://docs.unraid.net/unraid-os/using-unraid-to/run-docker-containers/overview/)
   explains its storage conventions and lack of native Compose support.
2. Download and extract the complete reviewed release source archive into a
   persistent directory, for example `/mnt/user/appdata/plex-bridge/source`.
   Keep the Dockerfile, package, Compose file, and guides together. A pasted
   `compose.yaml` alone is insufficient because the images build from source.
   Treat the directory containing that file as the stack's working directory.
3. Copy `deployment.env.example` to `.env` and set `BRIDGE_HOST` to the Unraid
   server's reserved LAN IPv4 address. Keep the exact release version pinned.
   Reserve the Plex server and WiiM addresses too. Keep `network_mode: host`;
   ports **8793, 8794, and 8795** must be free. Do not switch the stack to a
   custom container IP or add port mappings to work around a conflict.
4. Manage the three services as one Compose stack. Keep the same project name
   for every start, stop, restore, and update. Leave automatic image updates and
   automatic Git deployments disabled for this beta.

### Decide how to preserve the installation before first launch

The supplied stack uses six Docker-managed named volumes: `setup-private`,
`playback-config`, `topology-config`, `handoff`, `playback-state`, and
`topology-state`. **Putting the source in appdata does not put these volumes
there.** An appdata-only backup may miss the installation's credentials, room
identities, and state. Docker manages named-volume storage separately; see
[Docker's volume documentation](https://docs.docker.com/engine/storage/volumes/).

For the supplied layout, arrange a backup of all six actual volumes, your
private `.env`, and the matching source/images. Stop all three services for the
backup and preserve numeric ownership and permissions. Follow
[backup and restore](maintenance.md). Do not delete/recreate Unraid's Docker
storage or remove these volumes on the assumption that appdata holds a copy.
Confirm what your chosen backup tool actually includes before relying on it.

An appdata bind-mount layout is a possible operator adaptation, but this release
does not include a validated Unraid override. It must preserve all six distinct
stores, shared mounts, read-only boundaries, and access for UID/GID **10001:10001**.
Simply replacing volume names with empty host directories can break permissions
and initialization. Do not apply that change to an existing installation without
a stopped backup, a deliberate migration, and restore testing. If you want the
supplied layout, retain its named volumes and back them up explicitly.

### Start the stack and complete setup

From the extracted source directory in the Unraid terminal:

```sh
docker compose build
docker compose up -d
docker compose ps
docker compose exec setup python -c 'import json; from pathlib import Path; print(json.loads(Path("/private/setup-code.json").read_text())["code"])'
```

Alternatively, use the exact release's tested amd64 image download and its
load/retag instructions, then start with `docker compose up -d --no-build`.
The setup code printed above is private. Open `http://UNRAID-IP:8795` and continue
with **1. Claim the installation** below. Use the Plex server's reachable LAN URL
when prompted; Plex can run on Unraid or another machine.

Begin with one room and confirm sound, pause/resume, volume, Sleep, and amplifier
wake. Then test grouping and presets. Add [Symfonium and Last.fm](symfonium.md)
last, including the single completed-scrobble check. After setup, the dashboard
is at `http://UNRAID-IP:8794/dashboard`. Use [Diagnose playback and reviewed
logs](support.md) for problems; container health alone does not prove sound.

### Updates and the installation handoff

Name an installation owner and schedule a weekly check of releases and security
notices. Use notifications rather than automatic deployment. Apply approved
versions during a quiet window after a stopped backup; retain the old images,
source, `.env`, and volume set for rollback. Keep all three services on the same
version and preserve the Compose project name. Follow the
[update and recovery procedure](maintenance.md), then repeat room playback and
any Symfonium/Last.fm checks. Configure failed backup/update notifications in the
owner's existing Unraid or scheduler notification workflow; this stack does not
install that notification service for them.

Record Unraid, Compose, Plex, and WiiM versions and complete the
[beta checklist](beta-testing.md). Keep that evidence distinct from the existing
Linux container tests: an Unraid installation is not accepted until its own
storage, restart/recovery, and physical playback checks succeed.

## 1. Claim the installation and create an administrator

Follow the README's Docker commands. At port 8795, enter the setup code printed
by the local command. Create a username and a unique password of at least 12
characters. Store that password in your password manager. It is the dashboard
login, not your Plex password. The bridge stores a password hash.

## 2. Select your server and rooms

Power on your WiiMs and Plex server. Choose **Find WiiMs and Plex servers on my
LAN**, then explicitly select your server and rooms. Discovery is read-only;
it does not start music. Discovery may miss a device, so manual entry remains
available. Use **Name, IP address**, one room per line; the first is the default.

Confirm these addresses before saving:

| Field | Meaning |
| --- | --- |
| Bridge URL | Docker host's LAN address with port 8794, not setup port 8795 |
| Plex server URL | Reachable Plex server address and port, usually 32400 |
| Room IP | Each physical WiiM's reserved LAN address, not a Plex endpoint ID |

Example addresses in the source are documentation-only and cannot discover your
hardware. Set DHCP reservations in your router for the Docker host, server,
and every WiiM. Presets identify the requesting WiiM by its source IP; unstable
addresses can break them. Plex may run on another host from the bridge.

Choose **Save and review players**, review names/addresses, then **Finish local
setup**. Configuration and credentials are handed to the runtime services
automatically. The setup code is invalidated after completion. Closing/reopening
an unfinished setup resumes saved progress.

## 3. Continue on the bridge

Follow **Continue setup** to port 8794 and sign in with the administrator you
just created. Use the cards across the top to move through connection checks,
Plex authorization, server access, and playback verification. Per-room progress
persists; an interrupted check can be retried. Check the room named in each card
before taking an action.

## 4. Link each room to Plex

For each selected room, get or resume its authorization attempt, then choose
**Open Plex authorization** and approve it with the Plex account that can access
your server. Return to the bridge and choose **Check Plex approval**. Merely
obtaining a code does not authorize the room. Pending authorization, expiry, and
publication failure are separate outcomes; follow the displayed retry guidance.
Each room uses its own stable endpoint identity and may require its own approval.

Check Plex server access using the linked account. Then open Plexamp's player
selector and confirm the selected room appears. Cloud registration cannot prove
what Plexamp currently displays, so that confirmation is intentionally manual.

## 5. Verify actual playback

Arm the selected room's playback check, choose that exact endpoint in Plexamp,
and start a track at a comfortable volume. Return to the bridge and check stream
progress, then confirm you can hear music. The check looks for the bridge's
stream and advancing position; unrelated audio is not a successful test.
Repeat for the remaining rooms. No check should start music merely by opening
a page. Finish setup only when the required checks are complete.

## 6. Set up optional room actions

Open the dashboard. Configure which rooms may lead automatic joins, then follow
[preset setup](presets.md) if you want hardware shortcuts. Test volume, pause,
resume, group join/leave, and Sleep separately. A reachable badge means a device
answered a status query; it is not proof every command works on its firmware.

## 7. Save the support instructions

Bookmark `http://YOUR-LAN-IP:8794/dashboard` and keep your bridge administrator
login available. Read the [step-by-step playback support guide](support.md),
included in this installation's `docs` directory. After successful test playback,
practice finding the room's **Diagnose playback** button, reviewing its results,
and downloading the reviewed JSON. No issue submission is needed for this practice.

If playback later fails, that guide explains what to record, where to click,
what each result means, how to review and attach the file, and what to do if the
dashboard or diagnosis is unavailable. The dashboard's **Diagnostics** page also
provides the collection checklist. Collect evidence before restarting when practical.

## Optional Symfonium and Last.fm

After normal playback verification, follow the [Symfonium guide](symfonium.md)
for the dedicated provider, bridge output selection, and a single-scrobble test.
Plexamp-only installations do not need that provider. Last.fm is optional; link
it in the Plex account if wanted and verify a completed listen appears once.

## If an address changes

This beta does not have a supported general in-place readdressing workflow.
Reserve addresses before installing. The least disruptive repair is to restore
the previous reservation on your router. Do not edit one runtime's config alone:
the services share a validated handoff. Changing the bridge host address also
changes advertised endpoint and preset URLs. Back up the installation before
attempting a planned migration; ask for help if you need to preserve identities.
