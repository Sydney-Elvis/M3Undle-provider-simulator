# Locally buildable TSDuck worker (Stage A8 Public Asset). Installs
# TSDuck's official .deb package -- see plan's Dependency and Container
# Strategy: distribute the build recipe, not a prebuilt copy of the
# dependency.
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

# Pinned to the release verified when this recipe was created. Version updates
# are intentional maintenance changes; do not describe this pin as perpetually current:
# https://github.com/tsduck/tsduck/releases/tag/v3.44-4676
ARG TSDUCK_VERSION=3.44-4676
ARG TARGETARCH
RUN if [ -z "${TARGETARCH}" ]; then \
        echo "TARGETARCH is unset -- build with BuildKit (DOCKER_BUILDKIT=1 or docker buildx)" >&2; \
        exit 1; \
    fi
ARG TSDUCK_DEB_SHA256_AMD64=03a983a5147c5f733ef89aafd23f2d83e19a9e987ae4a575a7ee62ab4b16986e
ARG TSDUCK_DEB_SHA256_ARM64=09e728567c9e1eac619a440dfd22393eaaf04c0ab09711498cd5f787a7c7cae2
# The v3.44-4676 tag's underlying commit, resolved once via the GitHub API
# (repos/tsduck/tsduck/git/refs/tags/v3.44-4676 -> annotated tag object ->
# commit) and pinned here so the LICENSE.txt fetch below survives the tag
# ref ever being force-moved, and is checksummed like the .deb already is.
ARG TSDUCK_LICENSE_COMMIT=7ab046df6760151e0d5bfe029599406f438763cf
ARG TSDUCK_LICENSE_SHA256=58be33f8bd6df6371a95d310ea0ea405d9d46c5186bd8fb438020ea09d685353
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates && \
    curl -fsSL -o /tmp/tsduck.deb \
        "https://github.com/tsduck/tsduck/releases/download/v${TSDUCK_VERSION}/tsduck_${TSDUCK_VERSION}.ubuntu24_${TARGETARCH}.deb" && \
    case "${TARGETARCH}" in \
        amd64) echo "${TSDUCK_DEB_SHA256_AMD64}  /tmp/tsduck.deb" ;; \
        arm64) echo "${TSDUCK_DEB_SHA256_ARM64}  /tmp/tsduck.deb" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac | sha256sum -c - && \
    apt-get install -y --no-install-recommends /tmp/tsduck.deb && \
    mkdir -p /usr/share/licenses/tsduck && \
    curl -fsSL -o /usr/share/licenses/tsduck/LICENSE.txt \
        "https://raw.githubusercontent.com/tsduck/tsduck/${TSDUCK_LICENSE_COMMIT}/LICENSE.txt" && \
    echo "${TSDUCK_LICENSE_SHA256}  /usr/share/licenses/tsduck/LICENSE.txt" | sha256sum -c - && \
    rm /tmp/tsduck.deb && \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.title="M3Undle Provider Simulator TSDuck worker" \
      org.opencontainers.image.description="Separately distributed TSDuck worker with retained BSD notice." \
      org.opencontainers.image.source="https://github.com/tsduck/tsduck" \
      org.opencontainers.image.licenses="BSD-2-Clause" \
      org.opencontainers.image.version="${TSDUCK_VERSION}"

WORKDIR /work

ENTRYPOINT ["tsp"]
