# Troubleshooting

| Symptom | Check |
| --- | --- |
| Containers healthy, no music | Health is process liveness. Finish per-room Plex linking and playback checks. |
| Setup page unavailable | Verify BRIDGE_HOST is assigned to the Docker host, ports are free, and containers are running. Use port 8795. |
| Discovery misses devices | Same LAN, device power, multicast/firewall settings; use manual IP entry. |
| Open configured bridge URL / HTTP 403 | Use exactly the configured LAN address and port, not a hostname/proxy alias. |
| Authorization stays pending | Open Plex authorization, approve it, return and check approval. Expired attempts need renewal. |
| Room missing in Plexamp | Check approval/publication, reopen the selector, confirm the correct Plex account and LAN reachability. |
| Old duplicate Plex rooms | Retired installations can leave account registrations. Stop the old bridge; remove only verified obsolete registrations in Plex Authorized Devices. |
| Reachable, action fails | Reachability is not a capability check. Read the action's specific error; record model/firmware. |
| Connection interrupted | Action may have reached the device. Check status before retrying; reload after updating frontend code. |
| Sleep fails | Playback must stop before trigger-off. Check trigger cable, amplifier trigger mode, model support, and reported verification result. |
| Another room's playback changes | A follower's transport controls its leader; the bridge has one active Plex queue. |
| Preset Join does nothing | Run it from a WiiM, confirm enrolled source IP, and choose exactly one eligible playing leader. |
| Password lost | No self-service recovery is provided. Preserve a private backup; do not delete volumes blindly. A fresh install requires new linking. |

For support, choose **Diagnose playback** on the affected room, then review and
download the JSON from Diagnostics. Follow the [support workflow](support.md)
for reproduction steps, privacy boundaries, retention limits, and Plexamp logs.

Incident history persists across playback-container restarts when state storage
is available. A healthy process does not establish audible playback. If an
operation partly completed, verify physical topology before more group commands.
