# 0.1.0-beta.5 installation validation

Beta.5 replaces the beginner source-build path with a versioned registry image,
adds an Unraid tester template, and accepts an optional private installation
code without resetting existing state. Source builds remain available through
`compose.source.yaml`.

Local validation ran 334 tests: 331 passed and the three Chrome tests were
skipped because no local Chromium executable was configured. CI runs those
browser tests plus both container-layout install/restart/restore checks and the
existing source/image security gates. Publication requires the exact versioned
CI run linked in the release notes. Registry publication reuses that run's
scanned image artifact, then verifies anonymous pull and fresh startup.

The first scan detected fixed Alpine dependency vulnerabilities in the old
pinned base. The candidate now pins official Python 3.12.16 on Alpine 3.24
by digest; the normal security gate remains mandatory.

Unraid template structure, storage, security options and version consistency
are checked automatically. An independent Unraid install, first-user usability
trial, real-device playback, cross-version migration and Community Applications
listing remain pending. The default template uses a Docker named volume, which
must be backed up separately from appdata.

## Historical beta.4 validation


Beta.4 adds verified in-place synchronization of rolling Plex queues with the
WiiM native queue. The preceding source commit passed 330 tests including real
Chrome, fresh setup/restart/restore for both layouts, source and image-layer
secret scans, and HIGH/CRITICAL image vulnerability gates. The exact beta.4
versioned build must pass the same CI gates before its assets are published;
the release notes link that run and the downloadable checksums/SBOMs.

Household acceptance verified uninterrupted append and played-history removal,
then a fresh Symfonium cast and two consecutive physical WiiM Next actions with
native queue readback after each. Last.fm controller-report suppression passed;
a new scrobble count was not repeated for this patch. Arbitrary rearrangement
of played history remains unsupported. Independent hardware/Unraid, ARM,
cross-version migrations and independent security review remain pending.

## Prior beta.3 validation


This initial tester beta defaults to one container supervising setup, topology,
and playback, with one persistent `/data` mount. The optional split layout
retains the three-container/six-volume model. It is not certified across all
WiiM hardware, Unraid versions, Plex accounts, or Android TV interfaces.

## Source validation

On 2026-11-05 UTC, 324 tests passed locally with no skips, including three
real-Chromium DOM checks, HTTP proxy integration, child-exit cleanup, bounded
shutdown, spawn-failure cleanup, and multi-service health checks. The release
workflow repeats these tests against the exact exported source. Import-closure
and SHA256SUMS checks guard the export boundary. No private installation history,
credentials, runtime state, or operational runbooks are in the public export.

The earlier household acceptance of the unchanged Companion relay/listen filter
confirmed audible playback, pause/resume, track transition and one completed
Last.fm entry. That is not hardware acceptance of the new container packaging.
The household runtime has not been automatically replaced by this release.

## Container release gate

Publication requires a successful **beta checks** run for the release commit.
See release notes for its exact run and results. The workflow checks:

- Source secrets with Gitleaks and all unit/HTTP/browser tests.
- Existing split-container fresh setup, credential mount isolation, restart,
  diagnostic privacy and six-volume stopped restore.
- Single-container HTTP setup, both provisioning waiters, all three listeners,
  diagnostic privacy, restart, forced child death, whole-bundle failure, clean
  shutdown, one-volume stopped restore and a Linux host-directory restore.
- Gitleaks scanning of every saved image layer; Trivy HIGH/CRITICAL vulnerability
  and secret gates for the bundle and all three split targets; CycloneDX SBOMs.

Container tests use synthetic devices, no published ports, and disabled external
networking. They do not contact real Plex accounts or WiiMs. The appdata-style
bind-mount test is on Linux Docker, not an Unraid host. The published single-image
archive contains the tested Linux amd64 `pb-acceptance-ci-bundle:latest` image;
release notes explain loading and retagging it for historical releases. Beta.5
uses the versioned registry image by default; source builds remain optional.
No rolling registry tag or unattended updater is supplied.

## Still pending

- An independent household and Unraid host completing the [beta checklist](beta-testing.md),
  including real playback, amplifier wake, storage backup and recovery.
- ARM, other platforms/firmware, offline-cloud continuity, gapless/bit-perfect
  claims, cross-version migration/rollback, and split-to-bundle state migration.
- Independent security review. The default bundle shares its filesystem/UID
  between processes; LAN-only Companion trust remains a design constraint.
- Shared/managed-account and multi-user Last.fm attribution, broader Symfonium
  authentication and Android TV accessibility coverage.

Scans are snapshots tied to the exact image and scanner database at test time.
Rescan before future releases. Passing scans do not exclude lower-severity or
undisclosed vulnerabilities. Private test state/logs never belong in assets.
