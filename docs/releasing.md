# Release procedure

This package is exported from a private installation repository using an explicit
source allowlist. Never publish the installation repository or import its Git
history. Only initialize a fresh public repository from the reviewed export.

1. Keep application version, Dockerfile default and Compose defaults aligned.
2. Run all unit/integration tests and the opt-in real-browser tests.
3. Run `python3 tools/check_release.py .` and a current Gitleaks directory scan
   with `--redact --max-decode-depth 3`. Review findings; do not suppress whole
   directories to make the scan pass.
4. Build the bundle and three optional split images from the exact export. Run isolated fresh-install,
   restart, child-failure/shutdown and same-version restore acceptance. Save the exact source manifest and
   image IDs with the release evidence.
5. Save each image with Docker and scan every layer, not just the merged rootfs.
   Use Trivy (secret/vulnerability scanners) and retain an SBOM. Review any
   findings against the actual build. Document unresolved release blockers.
6. Complete the independent household checklist or label the release narrowly
   as an initial tester candidate with that gate explicitly pending.
7. Create a fresh public repository, enable GitHub private vulnerability
   reporting, and verify the Security reporting link works. Publishing the
   repository/images is a separate operator action; CI does not auto-publish.
8. Publish the reviewed archive/checksums and matching image set together with
   release notes and validation limits. Do not include private scan logs,
   acceptance state, backups, or operational reports in release assets.

The supplied CI runs tests, browser checks, builds/isolated restore, and scans
source/images. Security tools may need network access to update their databases.
A scanner pass is bounded evidence, not a guarantee that no personal data exists.
Keep any base-image license files and attach SBOMs when distributing images.

The Gitleaks configuration extends the standard rules and makes one narrow
exception: the exact public CPython release-signing fingerprint used in the
[official Python Dockerfile](https://github.com/docker-library/python/blob/master/3.12/alpine3.24/Dockerfile).
Docker image history repeats `GPG_KEY`; this is a public fingerprint, not a
private key. No directory or class of credentials is globally excluded.

`SHA256SUMS` belongs to a particular export. After intentional source changes,
regenerate the export and checksums before release validation. Do not silently
edit a previously validated release archive.

## Versioned registry image and install templates

The default Compose and Unraid template use the public
`ghcr.io/onelivewire/plex-bridge` bundle. Keep their exact version, package
version, Dockerfile, source Compose and deployment example aligned. Do not
replace an already released version tag; ship a new version instead.

After review and all normal release gates, push the matching `vVERSION` tag.
Run **beta checks** manually on that exact tag with `publish_image=true`.
Only the publish job has `packages: write`. It downloads the bundle artifact
from its own successful verification job, loads and tags that tested image,
and pushes it without rebuilding. Ordinary pushes, PRs and workflow runs never
publish. It checks anonymous manifest access afterward. On first publication,
the package owner may need to set the GHCR package visibility to Public in
GitHub's package settings; no user should need registry credentials to install.
Re-run publication only for the same reviewed source if visibility verification
failed. Record the registry digest and verify a clean anonymous pull/start and
same-version restore before declaring the distribution accepted.

Attach the two small install files and Unraid XML to the reviewed release.
Unraid template import is separate from Community Applications submission;
do not claim an Apps listing or independent hardware acceptance until verified.
The template intentionally uses a Docker named volume, initialized by the image
with ownership 10001:10001. Appdata backups alone do not protect it. Keep this
storage limitation prominent in the template and quick-start.
