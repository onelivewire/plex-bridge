# Start listening

You need a Linux x86-64 computer/NAS with Docker, a Plex music library,
Plexamp and a WiiM on the same trusted home network. Plex may run on another
computer. Reserve the Docker host, Plex and WiiM addresses in your router.
This beta supports one active Plex queue per installation. ARM and Docker
Desktop are not accepted targets.

## Unraid

The included [Unraid template](../unraid/plex-bridge.xml) removes source builds,
Compose Manager and file-permission commands. It is a **tester template**;
independent Unraid acceptance and Community Applications listing are pending.
It will not yet appear in the Apps search results.

1. [Download plex-bridge.xml](https://github.com/onelivewire/plex-bridge/releases/download/v0.1.0-beta.5/plex-bridge.xml). Through your existing Unraid flash-share access,
   save it as `config/plugins/dockerMan/templates-user/my-plex-bridge.xml`.
   Keep existing templates. Alternatively, use Docker → Add Container and copy
   the repository, host network, environment fields and Extra Parameters from
   the XML. [Unraid documents saved template locations](https://docs.unraid.net/community-applications/).
2. In Docker → Add Container, select `plex-bridge`. Enter your Unraid server's
   LAN IPv4 address and a private setup code generated in your password manager
   (32–256 characters, no spaces). Leave the other settings at their defaults.
3. Apply, then click the container icon → WebUI. Enter that same setup code.
   Create your administrator login, discover/select your Plex server and WiiMs,
   and follow the on-screen Plex authorization and playback checks.

The template uses the private Docker volume **`plex-bridge-data`**. It survives
container replacement, but it is **not in appdata**. Appdata-only backups miss
it; replacing/deleting Docker storage may destroy it. Set up a stopped volume
backup using the procedure below before relying on this installation.
For additional independent installations, change both container name and volume
name and use another host; the three fixed listening ports cannot be shared.

## Other Linux Docker hosts

[Download compose.yaml](https://github.com/onelivewire/plex-bridge/releases/download/v0.1.0-beta.5/compose.yaml)
and [deployment.env.example](https://github.com/onelivewire/plex-bridge/releases/download/v0.1.0-beta.5/deployment.env.example)
into a new folder. Copy `deployment.env.example` to `.env`, set
`BRIDGE_HOST` to this Docker host's reserved LAN IPv4 address, and optionally
set `BRIDGE_SETUP_CODE` to a private 32–256 character password-manager value.
Keep the supplied exact version. From that folder run:

```sh
docker compose up -d
```

Open `http://YOUR-SERVER-IP:8795`. Enter your chosen code. If you left it blank,
retrieve the generated code once:

```sh
docker compose exec bridge python -m plex_bridge.setup_code
```

Follow the wizard to create your administrator, discover/select devices, approve
Plex access for each room, and test audible playback. No source download, local
image build or Git installation is required. The default Compose project is
`plex-bridge`; its data volume is normally `plex-bridge_bridge-data`.

## First music, then extras

In Plexamp, select your new room and start a track at a comfortable volume.
Confirm sound in the wizard. Bookmark `http://YOUR-SERVER-IP:8794/dashboard`.
Add [presets](presets.md) and [Symfonium](symfonium.md) when you want them.
[Detailed setup](setup.md#1-claim-the-installation-and-create-an-administrator)
and [help](troubleshooting.md) are available if a check fails. Do not expose
these ports to the internet. No music starts merely from discovering devices.

## Protect settings and keep current

The installation owner checks [Releases](https://github.com/onelivewire/plex-bridge/releases)
and security notices weekly. Pinned image versions do not advance automatically;
Unraid's same-tag image check does not detect a new version tag. Disable
unattended updates. Use a quiet window for reviewed upgrades and configure your
existing backup/scheduler notifications to report failure to the owner.

For a private stopped-volume backup, create a private backup directory and use
the following commands in the host terminal. Substitute the verified container
and volume names: Unraid uses `plex-bridge` / `plex-bridge-data`; Compose usually
uses `plex-bridge-bridge-1` / `plex-bridge_bridge-data`. Keep the backup directory
on persistent storage outside Docker storage and restrict access to it.

```sh
docker stop YOUR-CONTAINER
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 --mount type=volume,source=YOUR-VOLUME,target=/data,readonly --entrypoint tar ghcr.io/onelivewire/plex-bridge:0.1.0-beta.5 -C /data -czf - . > YOUR-PRIVATE-BACKUP-DIRECTORY/bridge-backup.tar.gz
docker start YOUR-CONTAINER
```

Use a new dated filename each time. Verify the volume name with Docker Inspect
first: a misspelling can create an empty volume. Check command success and
archive contents; an empty archive is not a successful backup. Retain the
private install settings and previous exact image too. Test an isolated restore
before relying on your backup; see [recovery](maintenance.md).

For an approved compatible update, take that backup, select the new exact image
version in Unraid's Repository field or Compose's `.env`, then Apply in Unraid
or run `docker compose pull && docker compose up -d`. Keep the same data volume,
project name and host address. Check health, administrator login, existing room
identities and audible playback. If it fails, stop the container and restore
**both** the prior full data backup and matching image. Do not feed newer state
to an older image without verified compatibility. [Full maintenance guide](maintenance.md).
