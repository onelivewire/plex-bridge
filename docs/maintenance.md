# Backup, restore, upgrade, and removal

All six named volumes form one installation. Preserve them together:
`setup-private`, `playback-config`, `topology-config`, `handoff`,
`playback-state`, and `topology-state`. Names have the Compose project prefix;
inspect your own containers rather than assuming a global volume name.

## Private backup

Schedule a quiet period: stopping playback interrupts music. Keep the release
source/archive and image IDs with the backup, plus a private copy of `.env`.
Do not render or share resolved credentials/configuration.

1. Run `docker compose stop` from your installation directory.
2. Find named volumes with `docker volume ls` and verify their Compose labels.
3. Archive each of the six volumes using a Docker volume-backup tool while all
   three containers remain stopped. Preserve numeric UID/GID 10001 and file
   modes. Follow Docker's [volume backup/restore procedure](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes).
4. Store the six archives and `.env` together with restrictive permissions on
   encrypted/private storage. They contain Plex tokens and signing secrets.
5. Run `docker compose start` and verify dashboard access and endpoint identity.

A live filesystem copy is not a consistent backup. A support download is not a
backup. Never upload either the volumes or private backups to a public issue.

## Restore / move host

Restore into empty replacement volumes using the same release's images. Keep
the old installation stopped so two copies never advertise identical endpoints.
Restore all six archives with original numeric ownership/modes, preserve `.env`
and the reserved host address, then start Compose. Confirm administrator login,
room identities, account linking, and actual playback. Do not merge old and new
state. If the address must change, see the limitations in [setup](setup.md).

The automated isolated test exercises a same-version volume-copy restore and
compares identity/handoff fingerprints. It does not establish cross-version
migration compatibility or validate your particular backup tool.

## Upgrade and rollback

No automatic upgrades or schema migration promises exist in this first beta.
Do not assume any later image can read earlier state. Follow the release's
specific compatibility notes. Before upgrading, create the stopped backup above
and retain the prior source/images. Keep setup/playback/topology on the same
release version. Build all three before replacing containers.

For rollback, stop the new containers and restore the entire pre-upgrade volume
set with the matching older release. Do not start an old image against state
that a newer version modified unless compatibility was explicitly tested.

## Uninstall

`docker compose down` removes the containers and retains the named volumes.
Keep those volumes if you may reinstall. To permanently erase the installation,
first retain any needed private backup, then deliberately remove only its six
verified volumes (`docker compose down -v` is destructive). Cloud Plex device
registrations are separate: remove this bridge's verified endpoint registrations
in Plex Authorized Devices. Do not remove your Plex server or unrelated clients.

## Update ownership and schedule

The installation owner is responsible for updates. Check this repository's
Releases and Security notices weekly and before a planned maintenance window.
Keep an exact reviewed beta version pinned; release detection is not permission
to install it. There is no bundled unattended updater, scheduler, or email
service. If you use Semaphore or another scheduler, make the weekly job a
notification/check job and send failed checks to your normal administrator
email channel. Apply releases only during an approved quiet window.

For an approved compatible release, retain the old images and stopped backup,
extract the new reviewed source into a new directory, copy your private `.env`
with the new exact version, and preserve the existing Compose project name
(`docker compose -p YOUR-EXISTING-PROJECT ...`) so the six volumes are reused.
Build all three images, then use that same project name to recreate services.
Never run two copies against the same installation. Check container health,
dashboard login, room identities, Plexamp playback, and optional Symfonium/Last.fm
acceptance after the update. On failure, stop and use the full rollback procedure
above; report the failed update through your administrator notification channel.

Symfonium, Plex, WiiM firmware, Docker, and the host OS have separate update
owners/workflows. Repeat the single-scrobble test after relevant client/server
updates; upstream behavior may change independently of this bridge release.
