# Security and privacy

This beta is for a trusted household LAN. It is not safe to publish its HTTP
ports on the internet, attach it to an untrusted shared network, or assume the
administrator password protects every Plex-compatible route.

## Trust boundaries

- Setup requires a local claim code, validates Host/Origin, and invalidates the
  code after provisioning. Dashboard actions require an administrator session.
- Plex Companion compatibility routes use client/target identifiers rather
  than cryptographically verified controller authentication. LAN clients must
  be trusted. The topology API uses a generated bearer credential.
- HTTP is used locally. WiiM HTTPS uses device self-signed certificates without
  server identity verification. Keep network access restricted to trusted hosts.
- Source-IP preset checks and signed media URLs add restrictions but do not turn
  an untrusted LAN into a secure deployment. Do not put a public proxy in front.
- Docker administrative access gives access to stored credentials. Containers
  run unprivileged with read-only roots; six named volumes hold durable state.

## What is private

Room names, addresses, Plex tokens, media/session data, password hashes, signing
keys, and installation IDs are runtime data. They do not belong in the source
archive. Private backups contain these data and must never be posted as support
attachments. The app necessarily exchanges information with Plex for linking,
registration, and playback; it does not automatically upload support bundles.

Ordinary application logs use allowlisted structured fields. Support downloads
require login and a preview; they contain bounded playback-process diagnostic
events, not a complete cross-service dump. Timestamps/outcomes can still reveal
usage times. Review the exact preview before sharing.

## Reporting a vulnerability

Use this repository's **Security → Report a vulnerability** private reporting
form once the public repository exists. Do not include credentials in a public
issue. The maintainer must enable private reporting before inviting beta users;
if the button is absent, ask for a private contact without posting exploit or
secret details. No response-time commitment is offered during the beta.

Only the current beta candidate is under active review. Security fixes will be
released explicitly; there is no automatic update service. This document and
passing tests are not an independent security assessment.

## Optional Symfonium connection

`/symfonium/` forwards only to the Plex origin configured during setup. It never
supplies the bridge's stored Plex credential. Except for `/identity`, requests
must carry a non-placeholder caller Plex token. That is a presence check, not
independent token validation: Plex enforces access on forwarded requests. Do not
configure Plex to trust unauthenticated clients from the bridge host/network.
Listen reports are intentionally acknowledged locally and never sent upstream.

The bridge dashboard password does not protect this namespace. Plex credentials
and library/control traffic traverse local HTTP, and callers may use the Plex
API permissions their account has. Restrict the port to trusted LAN hosts. It is
not a general-purpose proxy or an internet access solution. Cookies/hop headers
are removed, external redirects are not followed, and local redirects remain
inside the dedicated namespace. WebSocket upgrades are not supported. See the
[Symfonium guide](docs/symfonium.md) for authentication and reporting limitations.
