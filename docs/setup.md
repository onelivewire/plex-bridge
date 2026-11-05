# Setup, from first launch to music

## Installing on Unraid

Plex Bridge runs alongside Plex and makes your WiiMs available as Plexamp
playback destinations. This release runs setup, playback and grouping in **one
container**, with **one persistent `/data` mount**. The dashboard controls rooms;
Plexamp browses music. Add Symfonium only after ordinary playback works.

This remains a guided beta installation. The single-container package and a
host-directory restore are tested on Linux Docker; independent Unraid and WiiM
hardware acceptance is pending. No Community Applications template is supplied.

### Prepare the release and Compose

Enable Docker and install a compatible
[Compose Manager plugin](https://github.com/mstrhakr/compose_plugin), then verify
`docker compose version` in the terminal. Unraid's
[Docker overview](https://docs.unraid.net/unraid-os/using-unraid-to/run-docker-containers/overview/)
explains its appdata conventions and lack of native Compose support. The plugin
is a deployment convenience; only one application container is created.

Extract the entire reviewed source archive into a persistent directory, such as
`/mnt/user/appdata/plex-bridge/source`. Keep its Dockerfile, package and Compose
file together; pasting Compose alone does not supply the source build context.
From that directory, copy `deployment.env.example` to `.env`. Set `BRIDGE_HOST`
to Unraid's reserved LAN IPv4 address and keep the release version pinned.
Reserve Plex/WiiM addresses too. Keep host networking; ports 8793–8795 must be
free. Do not substitute a custom container IP or port mappings.

### Set up one appdata directory

For a **new installation**, create an empty data directory owned by the
container's numeric UID/GID, using the Unraid administrator terminal:

```sh
install -d -m 700 -o 10001 -g 10001 /mnt/user/appdata/plex-bridge/data
```

Add this to `.env`:

```ini
BRIDGE_DATA_PATH=/mnt/user/appdata/plex-bridge/data
```

The Compose file mounts that directory at `/data`. It holds setup credentials,
Plex links, room identities, both runtimes' configuration and state, and history.
Do not use the source directory as the data directory. Do not use a broad
permission-reset operation on appdata: the bridge runs as **10001:10001**, not
Unraid's usual application account. It does not use PUID/PGID variables or run
as root to repair ownership. Prepare ownership before first launch.

If you omit `BRIDGE_DATA_PATH`, Docker creates the named volume `bridge-data`
(with the Compose project prefix). That volume is not automatically under
appdata. An appdata-only backup may miss it. See
[Docker volumes](https://docs.docker.com/engine/storage/volumes/).

Stop the bridge before backing up the complete data directory/volume, preserve
numeric ownership and modes, and retain `.env` and the matching release/images.
Test recovery before relying on the backup. Do not delete Unraid Docker storage
assuming that a named volume has an appdata copy. Existing six-volume installs
must follow [the split-layout guidance](split-layout.md); changing mounts alone
would create a different installation.

### Start and verify

From the extracted source directory:

```sh
docker compose build
docker compose up -d
docker compose ps
docker compose exec bridge python -c 'import json; from pathlib import Path; print(json.loads(Path("/data/setup/setup-code.json").read_text())["code"])'
```

Alternatively, load/tag the exact release's tested amd64 image as its release
notes describe and start with `docker compose up -d --no-build`. Keep the setup
code private. Open `http://UNRAID-IP:8795` and continue at step 1 below. Plex can
run on Unraid or another host; enter its reachable LAN URL.

Start with one room: confirm sound, pause/resume, volume, Sleep and amplifier
wake. Then test grouping and presets, and finally optional
[Symfonium/Last.fm](symfonium.md). The dashboard is
`http://UNRAID-IP:8794/dashboard`. [Diagnose playback and logs](support.md) help
with failures; container health is not proof of sound.

### Updates and recovery

Assign an installation owner to check releases/security notices weekly. Leave
automatic image updates and automatic Git deployments disabled. Apply approved
versions during a quiet window after a stopped backup, preserving the same
Compose project name and data mount. Retain the previous image and full data
snapshot for rollback. Configure failed backup/update notifications through the
owner's existing Unraid or scheduler notification workflow; this stack does not
install that service. Follow [maintenance](maintenance.md), and repeat playback
and optional Last.fm checks afterward. Manage Compose Manager updates separately.

Record Unraid, Compose, Plex and WiiM versions and complete the
[beta checklist](beta-testing.md), including restart, backup/restore and actual
hardware playback. Linux packaging tests do not replace this acceptance.

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
