# WiiM preset setup

Open **Set up WiiM presets** in the bridge dashboard. Its copy buttons supply
your actual host address. These shared URLs work for every enrolled WiiM:

| Action | URL pattern | Result |
| --- | --- | --- |
| Join | `http://YOUR-LAN-IP:8793/preset/join` | Join the one eligible playing group |
| Focus | `http://YOUR-LAN-IP:8794/preset/focus` | Move recoverable playback here and sleep former rooms |
| Disband | `http://YOUR-LAN-IP:8794/preset/disband` | Dissolve the group and sleep its rooms |

In WiiM Home, select the physical room, open its network-stream URL entry,
paste one URL, and save it to a preset slot with a clear name. Repeat for the
other actions and rooms. WiiM Home menu wording varies by version; the dashboard
links to the manufacturer's network-stream instructions. The bridge cannot
program preset slots for you.

Use the WiiM preset itself to test. Clicking the URL in a laptop browser cannot
identify which room you meant: the request's source IP must belong to an enrolled
WiiM. Keep DHCP reservations and host networking enabled. Avoid proxies/NAT
between WiiMs and these routes.

Join requires exactly one eligible playing leader. Focus is for a queue owned
by this bridge, not native WiiM Home Plex playback. A stream preset can replace
the requesting room's source; test with disposable playback first. There is no
individual Sleep/Leave stream preset: use the dashboard for those actions.
