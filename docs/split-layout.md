# Existing three-container installations

Beta.3 defaults to one `bridge` container and one `/data` mount for new installs.
`compose.split.yaml` retains setup, playback and topology as three containers,
with six named volumes and separate credential mounts. Existing installations
must not blindly run the new default Compose file: that creates new storage and
can conflict on ports while the old stack is still active.

To retain the split layout, use `docker compose -f compose.split.yaml` for every
command, with the **same project name** and existing `.env` as before. Build all
three targets from one reviewed version. Version compatibility still needs
review and a stopped backup; retaining the layout is not a cross-version schema
migration guarantee. Standard port numbers and per-room linking are unchanged.
The setup-code command is:

```sh
docker compose -f compose.split.yaml exec setup python -c 'import json; from pathlib import Path; print(json.loads(Path("/private/setup-code.json").read_text())["code"])'
```

## Backup and rollback

Stop all three containers together and back up these six volumes as one set:
`setup-private`, `playback-config`, `topology-config`, `handoff`, `playback-state`,
and `topology-state`. Actual names have the original Compose project prefix.
Retain `.env`, the Compose file, source version and image IDs. Preserve numeric
UID/GID 10001 and file modes. Backups contain credentials and must stay private.

Restore all six into empty replacement volumes, keep the old stack stopped,
and start the matching version with the same addresses/project name. Check
login, identities, room linking and playback. For rollback after an update,
restore the whole pre-update set and matching old images, not selected files.

## Moving to one container

There is **no automated in-place migration** in this release. Continue using the
split layout until a stopped, verified migration can be performed. The logical
mapping for a future migration is:

| Old volume | New path under the single data mount |
| --- | --- |
| setup-private | setup/ |
| playback-config | exports/playback/ |
| topology-config | exports/topology/ |
| handoff | handoff/ |
| playback-state | playback/ |
| topology-state | topology/ |

This mapping documents layout, not acceptance of a cross-version migration.
A migration must copy **all contents** while stopped, preserve ownership/modes,
validate handoff hashes and identities, and verify playback with the same host
address. Keep the six originals and prior images for rollback. Do not run both
layouts simultaneously, merge state from another installation, or re-run setup
against a partial migration. The one-container layout shares a UID/filesystem;
it intentionally gives up inter-container credential-mount isolation.
