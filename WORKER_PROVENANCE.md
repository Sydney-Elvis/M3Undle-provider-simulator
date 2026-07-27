# Worker image provenance

## TSDuck worker

- Distribution model: project-published worker image, with local-build fallback.
- Source project: <https://github.com/tsduck/tsduck>
- Version: `3.44-4676`
- Package source: the release `.deb` from the matching upstream GitHub tag,
  verified against architecture-specific SHA-256 values independently
  calculated and pinned in the recipe.
- License: BSD 2-Clause.
- License location in image: `/usr/share/licenses/tsduck/LICENSE.txt`.
- Architectures targeted by the recipe: `linux/amd64` and `linux/arm64`;
  `linux/amd64` is currently validated.
- Intended image: `ghcr.io/sydney-elvis/m3undle-provider-simulator-tsduck:3.44-4676`.

Publishing is a separate permission-gated action. Until that image exists,
Compose builds the same recipe locally.

## FFmpeg worker

- Distribution model: local build only.
- Base image: `ubuntu:24.04` pinned to
  `sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90`.
- Package: Ubuntu `ffmpeg=7:6.1.1-3ubuntu5`.
- Package archive: pinned to the `snapshot.ubuntu.com` timestamp
  `20260720T000000Z` so the build stays reproducible after the live archive
  moves on. `ffmpeg=7:6.1.1-3ubuntu5` has been unchanged/unsuperseded in
  `noble` since its 2024-04-06T21:32:16Z publish (per Launchpad's publishing
  history for source package `ffmpeg`), but the *pinned base image* already
  has `gcc-14-base 14.2.0-4ubuntu2~24.04.1` installed (published
  2026-02-26T17:46:57Z); apt won't downgrade an already-installed package, so
  the snapshot timestamp must postdate that gcc-14-base publish, not just the
  ffmpeg one. Re-verify both facts against Launchpad before moving this
  timestamp earlier.
- Corresponding source (Ubuntu source package, for the GPL components this
  build enables): <https://launchpad.net/ubuntu/+source/ffmpeg/7:6.1.1-3ubuntu5>.
- Upstream source and legal guidance: <https://ffmpeg.org/> and
  <https://ffmpeg.org/legal.html>.
- Package copyright metadata in image: `/usr/share/doc/ffmpeg/copyright`.
- Recorded build output: `/usr/share/doc/ffmpeg/BUILD-VERSION.txt`.
- Architectures: those for which the pinned Ubuntu base and exact package are
  available; currently validated on `linux/amd64`.

The Dockerfile fails if `ffmpeg -version` reports `--enable-nonfree`. The
Ubuntu build enables GPL components, so its effective license is recorded as
GPL rather than LGPL. The project distributes the recipe, not this resulting
binary image.

## Rejected upstream FFmpeg images

The following candidates were reviewed on 2026-07-26 and rejected:

- `lscr.io/linuxserver/ffmpeg:8.1.2`, manifest digest
  `sha256:595345f99ed5ecbe773388553baa9a0c0cd8f097aa76d3dc89a6b5b7dbee9c4b`.
  Runtime `ffmpeg -version` reports `--enable-nonfree`.
- `ghcr.io/jrottenberg/ffmpeg` was not selected because its published build
  documentation also shows `--enable-nonfree` configurations and does not
  provide sufficient assurance for this project's conservative policy.

Do not replace the local FFmpeg worker with an upstream image until its exact
digest and runtime configure flags have been reviewed and recorded here.
