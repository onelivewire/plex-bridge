# Beta validation and tester checklist

## Scope

Start with one room on a trusted LAN and a disposable playback queue. Initial
hardware experience comes from one household, including WiiM Pro devices; this
is not universal model/firmware certification. Linux amd64 is the initial image
acceptance target. ARM, Docker Desktop, separate VLANs, reverse proxies,
independent simultaneous queues, gapless and bit-perfect playback are unverified.

## Automated checks

Run from this package root:

```sh
PYTHONPATH=. python3 -m unittest discover -s tests
BRIDGE_BROWSER=/absolute/path/to/chrome PYTHONPATH=. python3 -m unittest discover -s tests -p test_dashboard_browser.py
```

Browser tests use the real DOM with a mocked backend. They catch form submission
bugs but do not prove a physical device changed state. Container acceptance is
opt-in, uses fresh resources and `--network none`, and retains stopped resources
for inspection:

```sh
python3 tests/container_acceptance.py . pb-acceptance-unique-run
```

Use a new prefix each time. This builds three images, performs synthetic setup,
checks credential isolation and support-download privacy, restarts the bundle,
and restores all six stopped volumes into fresh volumes to verify persisted
identity/handoff. It never contacts a real room or Plex account. Run it on a
Docker-capable Linux host; local Docker permissions are required.

## Independent household acceptance — pending

A maintainer cannot claim this passed without another tester's observations.
Record host OS/architecture, Docker/Compose, Plex server/Plexamp, and each WiiM's
model/firmware using generic room labels. Follow only the public README:

- Fresh install, claim code, administrator, discovery/manual entry, review.
- Each room's Plex authorization, picker visibility and audible playback.
- Pause/resume, next/previous, per-room volume/mute; inspect actual device state.
- Join/leave, follower transport, leader confirmation, disband with/without Sleep.
- Sleep stops playback and powers off a connected amplifier; later playback
  wakes the amplifier normally. Check both outcomes on each trigger-capable model.
- Plex Focus preserves the intended queue and position; unsupported sources
  produce a useful refusal. Test a controlled unavailable-room failure.
- Restart and same-version backup/restore preserve identity, login and linking.
- Device/server offline cases produce useful status and recover after reconnection.
- Reload/back navigation, phone screen and keyboard-only setup are usable.
- Review a support preview and confirm it contains no room/account/media details.

Record failures with the sanitized support download. No offline-cloud continuity,
cross-version upgrade/rollback, or broad hardware compatibility claim is made
until those specific trials are recorded. Beta release notes must keep unresolved
failures visible rather than treating unit tests as hardware acceptance.

## Optional Symfonium acceptance

Follow [the complete integration guide](symfonium.md). Record app/platform and
Plex versions and account type without credentials. Verify manual provider sync,
restart persistence, the Plex bridge output (with UPnP hidden if desired),
audible playback, advancing position, pause/resume, and next-track transition.
With Plex's Last.fm link enabled and other scrobblers excluded, finish a fresh
track and record one completed Last.fm entry. Verify ordinary Plexamp playback
still works. Local Symfonium playback through the filtered provider does not
submit server listen reports; use a separate direct provider for that purpose.
