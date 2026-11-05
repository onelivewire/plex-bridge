# 0.1.0-beta.2 validation record

This is an initial tester beta. The package includes normal Plexamp setup and
optional Symfonium control with selective listen-report suppression. It is not
certified across all WiiM hardware, Plex accounts, or Android TV interfaces.

## Source validation

On 2026-11-05 UTC, a fresh allowlisted export passed 318 tests, including all
three real-Chromium DOM tests and four HTTP integration tests against an isolated
Plex substitute. No tests were skipped. The source scanner (Gitleaks 8.30.1,
recursive decoding enabled) found no secrets. The export includes no private
installation history, operational runbooks, credentials, or runtime state.
Import-closure validation guards against omitting runtime modules from the
release list. SHA256SUMS identifies the exact distributed source files.

The browser backend and HTTP Plex server are synthetic; these tests do not
prove audible playback. Household acceptance of the Companion relay and
filtered provider confirmed audible playback, pause/resume, track transition,
and one completed Last.fm entry. That household test predates the beta's added
privacy-safe proxy diagnostic events; it is not an independent install.

## Container release gate

The repository's **beta checks** workflow builds all three images from the exact
export, exercises fresh setup, credential isolation, restart and same-version
restore in network-isolated containers, scans source and every image layer with
Gitleaks, scans each image for HIGH/CRITICAL vulnerabilities and secrets with
Trivy, and produces CycloneDX SBOMs. Publication requires a successful run for
the release commit. See the release notes and that commit's Actions run for the
actual outcome; older candidate results are not evidence for this build.

The optional image archive contains the three tested Linux amd64 images named
`pb-acceptance-ci-setup`, `pb-acceptance-ci-topology`, and
`pb-acceptance-ci-playback`. Standard installation builds from source; see the
release notes for loading and retagging the optional images. No rolling registry
tag or unattended image update is supplied.

## Still pending

- An independent household completing the [beta checklist](beta-testing.md),
  including amplifier wake after Sleep for its model/firmware.
- Broader hardware/platform coverage (including ARM), offline-cloud continuity,
  cross-version migration/rollback, and controlled Focus-failure hardware trials.
- Independent security review. LAN-only Companion trust remains a documented
  design constraint.
- Shared/managed-account and multi-user Last.fm attribution acceptance, plus
  broader Symfonium authentication and Android TV input/accessibility coverage.

Scans are snapshots tied to the pinned image and scanner database at test time.
Rescan before later releases. A passing scan does not exclude lower-severity or
undisclosed vulnerabilities. No private scan logs, test state, backups, or live
configuration belong in the release assets.
