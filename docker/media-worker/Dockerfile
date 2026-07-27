# Locally built FFmpeg worker. This recipe is distributed; the resulting
# binary image is built and retained by the user or CI runner, not published
# by the M3Undle Provider Simulator project.
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ARG FFMPEG_PACKAGE_VERSION=7:6.1.1-3ubuntu5

# The live Ubuntu archive only indexes the current version of a package per
# pocket, so pinning ffmpeg= above is not enough by itself -- once noble ships
# a security update, ${FFMPEG_PACKAGE_VERSION} disappears from the Packages
# index and this build starts failing with "Version ... was not found".
# Pointing at a fixed snapshot.ubuntu.com timestamp keeps the archive itself
# reproducible regardless of later updates. The timestamp must postdate
# whatever version of gcc-14-base is already baked into the pinned base image
# above (checked at pin time: 14.2.0-4ubuntu2~24.04.1, published 2026-02-26 --
# see Launchpad's binary publishing history for gcc-14-base in ubuntu/noble)
# -- apt will not downgrade an already-installed package, so an older
# snapshot than that fails with "unmet dependencies" several layers down from
# ffmpeg itself (via libgomp1 -> gcc-14-base). ffmpeg=${FFMPEG_PACKAGE_VERSION}
# itself has been unchanged/unsuperseded in noble since its 2024-04-06
# publish, so any snapshot at or after the gcc-14-base date below still
# serves it. snapshot.ubuntu.com is HTTPS-only, so ca-certificates has to
# come from the image's default (unpinned) sources first -- apt itself has
# no CA bundle yet at this point.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && printf '%s\n' \
        'Types: deb' \
        'URIs: https://snapshot.ubuntu.com/ubuntu/20260720T000000Z' \
        'Suites: noble noble-updates noble-security' \
        'Components: main universe' \
        'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "ffmpeg=${FFMPEG_PACKAGE_VERSION}" \
    && test -s /usr/share/doc/ffmpeg/copyright \
    && ! ffmpeg -buildconf 2>&1 | grep -q -- "--enable-nonfree" \
    && ffmpeg -buildconf > /usr/share/doc/ffmpeg/BUILD-VERSION.txt \
    && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.title="M3Undle Provider Simulator FFmpeg worker" \
      org.opencontainers.image.description="Locally built FFmpeg worker; not a project-distributed binary image." \
      org.opencontainers.image.source="https://ffmpeg.org/" \
      org.opencontainers.image.licenses="GPL-2.0-or-later" \
      org.m3undle.corresponding-source="https://launchpad.net/ubuntu/+source/ffmpeg/7:6.1.1-3ubuntu5"

WORKDIR /work

ENTRYPOINT ["ffmpeg"]
CMD ["-version"]
