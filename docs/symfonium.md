# Symfonium controller and Last.fm

Symfonium is optional. Complete [normal setup](setup.md) and verify audible
Plexamp playback first. The bridge does not install, license, or update Symfonium.
Use its official distribution and keep it updated separately. Shield use requires
an Android app installation and may need a mouse or keyboard: this beta does not
promise a fully remote-accessible Android TV interface. See Symfonium's
[official installation options](https://docs.symfonium.app/wiki/other/f-droid-repository/)
and [provider guide](https://docs.symfonium.app/wiki/providers/plex-media-provider-configuration/).

## What the integration does

The bridge registers linked rooms with Plex Media Server's Companion relay so
controllers using the server's client list can find them. A room appears with
its bridge endpoint name. The existing room identities and Plexamp workflow
remain in use; no separate relay container or private installation script is
required.

A second feature, the built-in `/symfonium/` connection, forwards library and
control requests to the Plex server selected during normal setup. It discards
Symfonium's `/:/timeline` and `/:/scrobble` listen reports. The bridge continues
to report its own playback to Plex. This avoids the two independent playback
reports that produced duplicate Last.fm scrobbles in household testing.

This is a dedicated controller connection, not a global Plex scrobbling switch.
It affects every user of that URL, and only requests actually sent through it.
Other Plex clients and direct server connections are unaffected.

## Add the dedicated provider

1. In Symfonium, open **Settings → Manage media providers → Add media provider →
   Plex**. Choose manual/local server entry rather than automatic Plex account
   server discovery. Labels may vary with the app version.
2. Enter `http://YOUR-BRIDGE-LAN-IP:8794/symfonium/`. This is the bridge address,
   not the Plex server address or setup port. If the form splits the URL, use
   HTTP, the bridge host, port `8794`, and subdirectory `/symfonium/`.
3. Authenticate with your **Plex account**. “Login” means the Plex sign-in
   identifier (username or account email); it is not the bridge dashboard
   administrator. Enter your password only in the app. The proxy never lends
   the bridge's stored Plex token to the controller.
4. For social sign-in accounts without a Plex password, or accounts with 2FA,
   follow the authentication sections of the official provider guide above.
   Do not disable 2FA or publish a token to work around authentication errors.
5. Name the provider something distinct, such as **Plex Bridge controller**,
   and let its music library finish syncing.
6. In Symfonium's provider/source filter, select this provider alone for bridge
   listening. If you already added the same server automatically, leave that
   separate provider excluded. Existing queue entries may still refer to the
   old provider: start a fresh queue from the filtered library.
7. Restart Symfonium and verify the saved URL, selected provider, and library
   still work. Automatic server discovery can bypass the proxy; a successful
   login alone does not prove reporting uses the filtered connection.

If this manual authentication flow does not work for your account type, use the
normal Plexamp setup and report the limitation privately. Managed accounts,
shared-server accounts, and every 2FA/social-login combination have not been
accepted independently. Do not weaken Plex authentication to make them work.

## Choose the bridge room, then hide confusing UPnP entries

Open the cast/output picker and choose the room identified as a **Plex bridge
endpoint**, not its physical WiiM **UPnP** entry. A UPnP selection bypasses the
bridge's queue and reporting workflow even if it wakes the amplifier.

Use Symfonium's renderer visibility controls (the eye/hide control in the
renderer list, depending on version) to hide unwanted UPnP entries. This is a
local display preference; it does not disable UPnP on the WiiM. Do not hide the
Plex bridge endpoint with the similar room name. If only UPnP appears, check the
room's Plex authorization and server access in the bridge dashboard, confirm
Symfonium uses that same server/account, and reopen its output list.

## Choose one Last.fm reporting path

For this integration, let **Plex handle Last.fm**:

1. Sign into the Plex account linked to the room and connect Last.fm from Plex's
   [Other Services](https://plex.tv/users/other-services) account page.
2. Disable any separate Android scrobbler's monitoring of Symfonium, including
   notification-based Last.fm/Pano Scrobbler equivalents. No Last.fm key or
   password belongs in the bridge configuration.
3. Use the dedicated provider and bridge endpoint described above. Symfonium
   reports playback to its server and can broadcast to local scrobblers; see
   the [developer's explanation](https://support.symfonium.app/t/scrobbling-to-last-fm/520).
   Merely disabling an Android scrobbler does not suppress the server reports.
4. Play a fresh, distinctive track all the way through once. Confirm audible
   sound, advancing position, pause/resume, and transition to the next track.
   Check Last.fm's recent tracks after it finishes; allow for delivery delay.
   Count completed listens, not the temporary “now playing” display. Expect
   **one** entry for that listen. Record the timestamp and result.
5. If there are two entries, verify the provider used by the actual queue and
   check for another scrobbler or server-side reporting integration. If there
   are none, verify Plex's account link and repeat with Plexamp to separate a
   Plex/Last.fm problem from a Symfonium connection problem.

**Local playback limitation:** playing on the Shield/phone itself through the
filtered provider also has its server listen reports suppressed. The bridge is
not playing in that case and cannot supply the missing report. For local
listening, use a separate direct Plex provider and deliberately choose a single
reporting path. Do not use that direct provider for bridge sessions while
expecting duplicate suppression. Resume/play-history updates from the filtered
controller may also be absent; the bridge's actual playback owns those reports.

The successful household test used one Plex account and a bridge-owned session.
Multi-user Last.fm attribution and independent households remain beta checks;
the proxy does not select a Last.fm account or guarantee third-party delivery.

## Troubleshooting and removal

- **Library works, playback immediately stops:** check the selected endpoint,
  actual queue provider, and Plex authentication. A missing or placeholder
  token is rejected. Reauthenticate the manual provider if needed.
- **Only UPnP appears:** check room linking/server reachability; the library
  proxy alone does not register an unlinked room.
- **“Nothing is playing”:** distinguish the controller display from audible
  sound and bridge state. Run the affected room's **Diagnose playback**, then
  save a reviewed support download before restarting.
- **401 / 502:** 401 can mean missing controller authentication or upstream
  refusal; 502 means the bridge could not complete the Plex HTTP request.
  Use the [support guide](support.md) for the privacy-safe proxy events.
- **Undo:** choose your original direct provider or Plexamp, remove/exclude the
  controller provider, and unhide renderers if desired. Direct-provider
  Symfonium bridge playback can again produce duplicate listen reports.

The endpoint is HTTP on a trusted LAN, not a remote-access gateway. See the
[security boundaries](../SECURITY.md) before using it.

## Native WiiM Next and rolling queues

Current source synchronizes Symfonium's rolling current/next queue with the
WiiM queue. Physical Next then advances to the upcoming track instead of
restarting the only track left on the renderer. Updates edit upcoming entries
and remove discarded played history without issuing Play or Seek; the bridge
reads the queue back after each edit. The controller UI may update after the
audio transition.

This fix is included in beta.4 and later image assets; beta.3 images do not
contain it.
Arbitrary reordering of already-played history is not supported by this in-place
update; it is rejected without editing the native queue. A physical transition
racing a queue edit can require the controller to retry its refresh.

After a bridge restart, check the cast picker: Symfonium may have fallen back
to Local playback. Select the Plex Bridge endpoint again before testing. To
verify the queue fix, start an album and press the WiiM's physical Next twice,
allowing the controller to receive each transition and supply the next track.
