# Setup, from first launch to music

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
