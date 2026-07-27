# Third-party tooling and distribution policy

This document records the project's technical distribution policy. It is not
legal advice. Re-check upstream license texts and obtain qualified legal review
before changing the distribution model.

## Project policy

The published simulator image contains neither FFmpeg nor TSDuck. The project
may publish a separate TSDuck worker under its own image name with the complete
BSD notice. It does not publish, mirror, or attach an FFmpeg worker image.

The public repository may provide:

- a Dockerfile that the user or CI uses to build a worker locally;
- a Compose definition that builds that Dockerfile locally; or
- a Compose definition that pulls a reviewed upstream image directly by
  immutable digest.

Locally built worker images remain in the user's or CI runner's container
store. They are not project release artifacts. Merely placing a binary in a
separate image does not avoid its license if the project distributes that
image. Only the separately named TSDuck worker is approved for the project
registry. FFmpeg and any other worker remain local-build-only unless this
policy is explicitly revised after a new review.

## TSDuck

TSDuck is licensed under the BSD 2-Clause License. That license permits source
and binary redistribution, with conditions including retention/reproduction
of its copyright notice, license conditions, and disclaimer.

The approved model permits a separately named project TSDuck worker because
the BSD conditions are straightforward, while retaining the Dockerfile as a
local-build fallback. The TSDuck worker
Dockerfile must:

- pin the TSDuck version and supported platform;
- verify the downloaded package against the architecture-specific SHA-256
  values pinned and recorded by this project (and prefer an upstream signature
  or checksum when the release provides one);
- retain the TSDuck license text and copyright notice in the built worker;
- record `tsversion` in run artifacts; and
- be published only as the separately named TSDuck worker with the complete
  BSD notice and recorded provenance.

Official references:

- <https://github.com/tsduck/tsduck/blob/master/LICENSE.txt>
- <https://tsduck.io/docs/tsduck.html>

## FFmpeg

FFmpeg is primarily LGPL 2.1-or-later. Enabling optional GPL components makes
the resulting build GPL; enabling `--enable-nonfree` can make the resulting
binary unredistributable. External libraries and codec choices can also change
the applicable obligations. A container image containing an FFmpeg binary is
binary distribution by whoever publishes that image.

This project therefore does not publish an FFmpeg worker image. An FFmpeg
worker Dockerfile must:

- build from a pinned official FFmpeg source release, or install a pinned
  distribution package from a pinned base-image digest;
- avoid `--enable-nonfree`;
- explicitly record the configure flags, external libraries, source version,
  source URL, and `ffmpeg -version` output;
- preserve the applicable FFmpeg license texts and notices in the locally
  built worker;
- provide the corresponding-source location and build recipe needed by the
  applicable license;
- be reviewed again if GPL components such as `libx264` are enabled; and
- never be published or mirrored as a project-owned image.

Official references:

- <https://ffmpeg.org/legal.html>
- <https://ffmpeg.org/doxygen/trunk/md_LICENSE.html>
- <https://ffmpeg.org/download.html>

## Upstream images

Pulling a third-party image directly avoids this project republishing that
binary, but it does not make provenance or security irrelevant. Before an
upstream worker image is placed in Compose, record:

- its publisher and source repository;
- immutable image digest and supported architectures;
- FFmpeg/TSDuck version and build configuration;
- included license notices and corresponding-source location;
- update and vulnerability-response policy; and
- the review date and reviewer, so a stale approval is easy to spot.

An upstream image recorded here is still not a project release artifact:
Compose references it by digest, but this project neither builds nor
publishes it.
