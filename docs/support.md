# Getting help with playback

## Before you need help

During installation, bookmark `http://YOUR-LAN-IP:8794/dashboard`, replacing
`YOUR-LAN-IP` with the bridge host address you configured. Sign in with the bridge
administrator created during setup, not your Plex account. Keep the downloaded
release's `docs` folder: these instructions ship with it and can be read offline.
The dashboard's **Diagnostics** link also contains a short step-by-step checklist.

After your first successful playback test, practice steps 3–6 below. You do not
need to create an issue or send anyone that practice download.

## When playback fails or takes too long

1. **Record the symptom and time.** Write down the local time and timezone,
   Plexamp platform (Mac, phone, etc.), selected room, whether it was grouped,
   and what you clicked. Describe what Plexamp displayed and what you heard.
   For example: “At 8:10 PM Mountain time, selecting a later track in Mac
   Plexamp woke the amplifier, but Plexamp stayed paused and there was no audio.”
   Use generic room/track labels if you do not want to share their names.
2. **Keep the evidence.** If practical, reproduce once. Avoid repeated retries,
   restarting the bridge or WiiM, changing rooms, or replacing the queue before
   collecting the bundle. If you already did those things, note them; still
   collect a bundle. You do not need to reproduce a disruptive failure again.
3. **Open the bridge dashboard.** In a browser on the same LAN, open
   `http://YOUR-LAN-IP:8794/dashboard` and sign in with your bridge administrator.
   Find the physical room where playback failed. If it was a group follower,
   record the leader too; the saved-queue checks may apply only to that leader.
4. **Run the room check.** On that room's card, select **Diagnose playback** once.
   Wait for the Diagnostics page; allow roughly 30 seconds if services time out.
   The check does not play, pause, stop, regroup, reboot, change volume, or change
   the amplifier trigger. If another diagnosis is running, let it finish; if
   asked to wait, allow 30 seconds before trying again.
5. **Review the result.** The page contains the exact JSON that will be downloaded.
   You do not need to interpret every field. The `diagnosis` section describes
   the room check; `incidents` contains earlier events. Check that the timestamp
   corresponds to your attempt. Read the privacy notes below and decide whether
   you are comfortable sharing the timestamps, versions and playback information.
   No credentials, addresses, room names or media titles should appear.
6. **Save the reviewed file.** Choose **Download this reviewed JSON** and save the
   `.json` file from your browser's downloads. Do this within ten minutes of the
   preview and before creating eight newer previews. If the link has expired,
   open **Diagnostics** again, review the new preview, then download it. Nothing
   has been sent to support yet.
7. **Submit a playback issue.** Open the Issues page of the public repository
   from which you obtained this release, choose **New issue**, and select its
   playback report template. For a supervised beta, use the support channel
   supplied with your invitation. Attach only the reviewed JSON. Include the
   symptom/time from step 1, expected behavior, actual behavior, Plexamp
   version/platform, grouping, and any restart/retry you performed. State whether
   audio was audible, and whether another client worked. Include Docker/Compose
   versions if available (`docker --version` and `docker compose version` on the
   bridge host). Do not paste configuration or credentials.
8. **Describe recovery separately.** After saving the evidence, you can try the
   relevant troubleshooting steps. If a restart or another app restores playback,
   add exactly what worked and when to the report. A successful recovery does
   not explain the original failure by itself.

## If a step does not work

| What you see | What to do |
| --- | --- |
| Dashboard will not open | Confirm the bridge host address and port 8794, and that you are on its LAN. On the Docker host, run `docker compose ps` from the installation directory. Report which services are stopped/unhealthy; do not share raw configuration or unreviewed logs. |
| No Diagnose playback button | Confirm you opened the bridge dashboard, not Plexamp or WiiM Home. Check your installed release; older versions may require an upgrade using the maintenance guide. |
| Diagnosis fails or does not finish | Open the dashboard's **Diagnostics** link (or `/diagnostics` at the same host/port), then review and download the available history. State that the room check failed. If that page also fails, report the symptom and service status without a bundle. |
| `failed` in a check | Keep the result: partial failures are useful evidence. Other checks may still have succeeded. |
| `not_checked` or no diagnosis | Opening Diagnostics alone does not run a room check. A room without the saved queue cannot receive its audio/progression checks. Explain the room and grouping rather than starting unrelated music just to fill these fields. |
| All checks succeed but no sound | Report the missing sound anyway. Stream access and advancing position cannot prove that speakers produced audio. |
| `persistence` is `unavailable` | Download current evidence before restarting. Mention this status; earlier events may not survive a restart. |

See [troubleshooting](troubleshooting.md) for symptom-specific checks and
[maintenance](maintenance.md) for upgrades, backups and rollback.

## What the bundle includes

- Bridge release, a source build fingerprint, Python version, OS family and
  CPU architecture. The fingerprint distinguishes local patches with the same
  release label; it is not a machine identifier.
- Anonymous room, track and playback-attempt IDs. IDs are keyed locally; names,
  Plex metadata IDs, addresses and signed URLs are not included. IDs remain
  stable within the installation across restarts while its diagnostics state
  file is retained, allowing incidents to be compared.
- Correlated command, queue-loading and device-control stages, durations,
  allowlisted failure reasons, HTTP status where available, and observed state,
  stream match and progression. An accepted command is distinct from verified
  advancing playback. Deferred handoffs retain the initiating attempt ID.
- The default preview contains up to 500 recent retained events. A separate
  private archive targets seven days of playback details (80 MiB) and thirty
  days of incidents (20 MiB). Size limits can shorten those windows. Successful
  background polls are excluded and repeated warnings are coalesced. Recent
  process events are also included (250 events, at most one hour).
- **Earlier activity** on Diagnostics lets you select a UTC start/end window.
  Its frozen preview/download includes at most the latest 2,000 matching archive
  events, with total matches and an explicit truncation flag. Narrow the window
  if truncated. This archive lives in the existing playback state volume.
- The most recent explicit diagnosis in this process: recognized WiiM model,
  firmware when valid, status/control responses, trigger telemetry capability,
  topology availability, Plex reachability/version, and—when this room owns the
  saved queue—a brief stream/progression observation and up to 4 KiB audio read.

A successful audio read proves that the bridge can read those bytes from Plex;
it does not prove the WiiM fetched them or that speakers produced sound. A paused
or stopped track is not expected to advance. No active saved queue means the
media check is marked not checked. Unknown hardware identifiers are reported as
unknown rather than exporting arbitrary firmware response fields.

Incident history is written asynchronously and survives bridge restarts. An abrupt
process kill can lose the newest unwritten burst (normally under a second). Live probe results, per-process
correlation mappings and frozen download previews do not. Persistent storage
failure is reported as `unavailable`, and diagnostics continue in memory without
blocking playback. Snapshots and exports are bounded; this is not full distributed
tracing of Plexamp, Plex Media Server, every service, or the network.

## Privacy and additional logs

The bundle excludes credentials, tokens, cookies, IP addresses, hostnames, room
names, media titles, raw exception messages, raw upstream responses and audio.
It does include timestamps, hardware/software versions, anonymous relationships,
playback positions and outcomes; review before sharing. The local persistence
file also contains the anonymous-ID key. Share the reviewed export, not that
file, configuration directories, `.env`, state backups or container inspections.

If the bundle does not explain the client behavior, support may request a narrow
Plexamp log excerpt around the incident. On macOS the logs are normally under
`~/Library/Logs/Plexamp/`; on other platforms use Plexamp's log-export feature
if available. Review those logs separately: unlike the bridge's bundle they may
contain account, device, media, network or authentication details. Do not attach
unreviewed logs publicly. Describe what you can hear; software cannot confirm it.

## Symfonium and Last.fm checks

Include the Symfonium version/platform, whether the selected output was Plex or
UPnP, whether the queue came from the dedicated controller provider, and the
completed Last.fm entry count. Do not include the provider password or token.
The [integration guide](symfonium.md) has the full setup and isolation procedure.

The playback service emits these allowlisted events:

| Event | Meaning / next check |
| --- | --- |
| `proxy_auth_required` | The controller supplied no usable Plex token; authenticate the manual provider. |
| `proxy_media_token_repaired` | A cast command's missing/placeholder media token was replaced with that same caller's Plex token. This is not a playback-success event. |
| `proxy_report_suppressed` | A listen report reached the dedicated connection and was discarded. It does not prove Plex submitted a scrobble. |
| `proxy_forward_failed` | Plex returned an HTTP error or the upstream exchange failed; status/reason are retained without the request URL. |
| `registration_failed` / `timeline_failed` | Check Plex reachability and room authorization; these can also describe the Companion relay. |

Repeated events may be coalesced in retained history, so event counts are not
scrobble counts. A controller bypassing the proxy produces no suppression event.
The bridge cannot inspect your Last.fm account or Android scrobbler settings.

For live local inspection from the installation directory:

```sh
docker compose ps
docker compose logs --since 10m --tail 200 bridge
```

Logs are structured event records, not a raw protocol transcript. They retain
safe event names, status codes, timings, and anonymous identifiers; they omit
request URLs, tokens, media titles, and exception messages. There is no supported
switch that dumps all raw traffic. The reviewed dashboard JSON is the preferred
support attachment. Container health only proves the configured health check,
not audible sound or successful Last.fm delivery.

If a maintainer needs Symfonium or Plex's own debug logs, use those products'
private support procedures and inspect/redact the files first. They do not share
this bridge's allowlist and may contain credentials, paths, and listening data.
Do not attach them unreviewed to a public issue.

For the retained split layout, use `docker compose -f compose.split.yaml logs
--since 10m --tail 200 playback`. The single container combines records from all
services; each record identifies its component. An unhealthy container does not
automatically restart just because an HTTP probe failed. Child-process exits
stop the bundle and its restart policy restarts it; a hung process requires
inspection and an operator restart after collecting diagnostics.
