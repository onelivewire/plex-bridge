# Backup, restore, upgrade, and removal

The default single-container installation stores everything in **`/data`**.
Preserve the complete mount: `setup`, `exports`, `handoff`, `playback`, `topology`,
and any other contents. These include credentials, Plex links, identities and
history. With the default Compose file this is a named `bridge-data` volume;
with `BRIDGE_DATA_PATH` set it is your chosen host directory. Verify the actual
mount in your container configuration. The source folder is not a data backup.

Existing three-container installations must use [split-layout instructions](split-layout.md).
Do not point a new single-container service at just one of the old volumes.

## Private backup

1. Schedule a quiet period and run `docker compose stop`. This interrupts music.
2. Back up the complete data mount while the container is stopped. For a named
   volume, use Docker's [volume backup procedure](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes).
   For a bind directory, use a filesystem archive/backup preserving numeric
   UID/GID **10001:10001** and file modes. Never back up by copying live state.
3. Retain the private `.env`, exact release source and image ID with the data
   backup on encrypted/private storage. Do not upload these as support files.
4. Run `docker compose start`. Verify login, existing room identities and sound.

A diagnostic download is not a backup. Test a restore before relying on your
backup tool. Backup jobs must stop the entire container and notify the operator
on failure; an inventory entry or scheduled copy alone does not establish this.

## Restore / move host

Keep the old installation stopped to avoid two copies advertising the same
identities. Restore the entire backup into an empty replacement volume or bind
directory using the same release image, numeric ownership and modes. Preserve
`.env`, the data mount selection, Compose project name, and reserved host IP.
Start Compose and check administrator login, room identities, Plex authorization,
actual playback, and optional Symfonium/Last.fm. Do not merge two state trees.
Address changes remain subject to [setup limitations](setup.md#if-an-address-changes).

Automated tests exercise a same-version stopped volume restore and a Linux bind
mount restore. They do not establish cross-version migration or certify your
backup tool, Unraid filesystem, or physical hardware.

## Update ownership, schedule, and rollback

The installation owner checks this repository's Releases and Security notices
weekly. Use scheduled notifications if desired, with failures sent to the
owner's existing email/notification channel. No scheduler or email service is
bundled. Leave unattended image updates and Git deployments disabled. A release
notification is not authorization to install it.

For an explicitly compatible reviewed release:

1. Take the stopped backup and retain the current image and source.
2. Extract the new reviewed source into a new directory. Copy the private `.env`,
   update its exact version, and preserve `BRIDGE_HOST` and `BRIDGE_DATA_PATH`.
3. Use the same Compose project name (`docker compose -p YOUR-EXISTING-PROJECT`)
   for every command so the existing named volume is selected. Build the new
   image before replacement, then start it with that same project name and mount.
4. Check container health, dashboard login, stable identities and actual room
   playback, including Symfonium/Last.fm if configured.
5. If it fails, stop the new container, restore the complete pre-update data
   backup, and start the matching older image. Do not let an old image read
   newer state unless that compatibility was explicitly validated. Notify the
   installation owner of the failure and retain reviewed diagnostic evidence.

There is no general schema-migration promise in this beta. Moving from the old
three-container layout is a separate storage migration, not a normal update;
see [split layout](split-layout.md). Plex, Symfonium, WiiM firmware, Compose
Manager, Docker and the host OS retain their separate update workflows. Repeat
playback and single-scrobble acceptance after relevant upstream updates.

## Health and recovery

The bundle supervises all three services. A child exit stops its siblings and
exits the container with failure so `restart: unless-stopped` can recover it.
SIGTERM stops all children with a bounded grace period. HTTP health probes cover
setup plus both runtimes; before provisioning, runtime waiting is healthy only
while the corresponding process lives. A health probe failure marks the
container unhealthy but does not itself trigger Docker's restart policy.
Collect [diagnostics](support.md) before manually restarting a hung service.

## Uninstall

`docker compose down` removes the container and retains its named volume/bind
directory. Keep data if you may reinstall. `docker compose down -v` deletes named
volumes; deleting a bind directory requires a separate deliberate filesystem
operation. Do neither until you have retained any needed private backup. Remove
only this installation's verified registrations in Plex Authorized Devices;
never remove the Plex server or unrelated clients.
