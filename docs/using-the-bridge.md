# Using the bridge

Complete [setup and per-room playback verification](setup.md) first. Open
`http://YOUR-LAN-IP:8794/dashboard` and sign in with your bridge administrator.
Keep Plexamp or the optional [Symfonium controller](symfonium.md) available for
browsing music and creating queues. The dashboard is for room control; it is not
a music-library browser.

## Choose music and read room status

In Plexamp's player selector, choose the intended bridge room and start music.
There is one active Plex queue for the installation: selecting a second room
can move or replace it. For several rooms playing the same music, form a native
WiiM group. Independent simultaneous Plex queues are not supported.

Room cards refresh automatically. They show source, playback state, available
track information, group membership, and device health. **Online** means the
WiiM answered a status request. **Checking playback** or **Playback unavailable**
means playback checks need attention even if device HTTP is reachable.
**Unreachable** is a connection problem; **Restarting** indicates a requested
restart is being monitored. A stale page or missing metadata is not proof that
music stopped. Verify what is audible before repeating an interrupted action.

## Transport, volume, and mute

Use **Play/Pause**, **Previous**, and **Next** on the room card. Transport on any
group member acts on its group leader and therefore affects the shared music.
Some source types do not support these commands; disabled controls mean you
should use that source's own app. Start a new library selection in Plexamp or
Symfonium, not by expecting Play to create a new queue.

Move the **Room volume** slider and choose **Set volume**. This changes that
physical WiiM only. **Mute** saves the room's current volume and sets it to zero;
**Unmute** restores the saved value. It does not pause the shared music. This
saved mute level is process memory, so do not rely on it surviving a bridge
restart; set the volume explicitly if needed.

## Join a playing group and choose eligible leaders

1. Start music in the room that should lead.
2. Open **Settings & connections → Smart leadership**. Check the rooms allowed
   to attract automatic joins, then choose **Save leadership**. Exclude rooms
   whose TV or other audio should not attract another room automatically.
3. On the room you want to add, choose **Join playing group**. The bridge needs
   exactly one eligible playing leader. Wait for the membership display to
   update and confirm the shared audio.

If there are multiple eligible sessions, the bridge refuses to guess. Choose
the desired group explicitly in WiiM Home, or stop the unwanted session and
retry. Smart leadership affects automatic Join only; it does not prevent
explicit grouping or moving playback.

## Move playback here (Focus)

Choose **Move playback here** on the destination room to recover the active
bridge-owned Plex queue there and put the former rooms to sleep. This is a
transfer, not a way to add a second independent queue. Check the destination's
sound, track, and position and verify the former group stopped.

Focus needs recoverable playback. Native WiiM Home Plex playback and arbitrary
external sources are not the same as a bridge-owned queue. If the action fails
partway, inspect the actual room/group state and collect diagnostics before
issuing more group commands. Automatic recovery is best-effort and does not
prove physical device state.

## Leave, disband, and Sleep

| Control | How to use it | Effect |
| --- | --- | --- |
| Leave group | Use the room card's **Leave group** button. | Removes a follower. Leaving as leader requires confirmation because its group dissolves. It is not an amplifier-off request. |
| Disband group | Expand **Room details & group options**, then choose **Disband group**. | Dissolves that group. The optional **Put rooms to sleep afterward** checkbox also stops/sleeps its rooms. |
| Sleep | Choose **Sleep** on the room card. | Detaches as needed, stops playback, verifies the stop, then requests amplifier trigger-off. Sleeping a leader requires group-dissolution confirmation. |

Sleep concerns the WiiM's 12 V trigger and a connected amplifier. It does not
prove the WiiM entered its remote-control standby mode. Models without trigger
telemetry receive the request without that verification. **Asleep** describes
observed stopped, ungrouped, trigger-off state. To resume listening, select the
room and start playback again; confirm amplifier wake on your model during
initial acceptance.

## Restart a WiiM

Use **Restart WiiM** in **Room details & group options**. It may also appear
prominently when playback health is degraded. Collect a diagnostic snapshot
first when practical. Confirm the warning: restarting stops playback and may
interrupt a group the room leads. Device settings are preserved, but playback
will not resume automatically. Wait for room health to recover, then deliberately
start playback. This restarts the WiiM, not the bridge container or Plex server.

## Plex linking, presets, and controller setup

- **Manage Plex link** in the room details returns to that room's authorization
  step. Follow [setup](setup.md) to approve and verify it. Top-of-page
  **Connection checks** revisits reachability without reinstalling the bridge.
- **Set up WiiM presets** provides copyable URLs. Follow [preset setup](presets.md)
  for Join, Focus, and Disband-and-sleep. Testing a stream URL runs its action;
  use the WiiM itself so source-IP room identification works.
- For Shield/Android browsing and casting, follow [Symfonium and Last.fm](symfonium.md).
  It covers credentials, choosing the dedicated provider and Plex endpoint,
  hiding UPnP entries, and checking for exactly one completed scrobble.

## Recent activity, diagnostics, and logs

**Recent activity** lists the latest room actions and outcomes. It is a quick
local history, not proof of audible playback and not a privacy-reviewed support
bundle. **Diagnose playback** on a room performs read-only checks. Then open
**Diagnostics**, review the JSON, and download the exact reviewed file. Earlier
activity can be selected by a UTC time window. See [support](support.md) for
retention, interpreting events, live Docker log commands, and safe sharing.

For an error or timeout, record the time and actual result before retrying. Some
commands may have reached a device despite an interrupted response. Follow the
[troubleshooting table](troubleshooting.md) and [maintenance guide](maintenance.md)
for service recovery, backups, upgrades, rollback, or removal.

## Experimental and unsupported features

The dashboard exposes **Authorize Spotify**, and the implementation contains
Spotify Focus support, but the public beta installer does not configure the
required Spotify application credentials. This is not a supported one-click
Spotify setup; leave it unused for the normal Plex installation. Standby-trigger
automation is also outside the public installer. Neither is required for Plexamp
or Symfonium. There is no public general-purpose automation/API integration guide
or supported in-place readdressing workflow in this beta.

See the [beta coverage and pending checks](beta-testing.md) before relying on
unverified firmware behavior, gapless/bit-perfect playback, offline-cloud use,
or other deployment platforms. New hardware should complete those checks with
a disposable queue at a comfortable volume.
