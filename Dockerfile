# M3Undle Provider Simulator -- a deterministic IPTV provider and
# stream-failure fixture for proxy integration testing. Synthetic media,
# declarative provider scenarios, connection failures, and three synthetic
# MPEG-TS packet-level faults (continuity-counter errors, delayed PAT/PMT,
# malformed packets). No FFmpeg or TSDuck bundled -- everything here is a
# self-contained, dependency-free Python engine.
#
# Build context is this directory (provider-simulator/), not a parent repo:
#   docker build -t provider-simulator:dev .
#
# Standalone, no arguments needed -- runs a baked-in example scenario:
#   docker run --rm -p 19001:19001 provider-simulator:dev --bind 0.0.0.0
#   curl http://127.0.0.1:19001/playlist.m3u
#
# Or point it at your own scenario/fixture, mounted in:
#   docker run --rm -p 19001:19001 \
#     -v $(pwd)/my-scenarios:/app/scenarios:ro \
#     -v $(pwd)/my-fixtures:/app/fixtures:ro \
#     provider-simulator:dev --bind 0.0.0.0 --scenario scenarios/my-scenario.yaml
#
# Self-verify a scenario with no external client at all (see RUN_MODE.md):
#   docker run --rm provider-simulator:dev --run scenarios/core/baseline-01-clean-continuous-live-stream.yaml

FROM python:3.12-slim

ARG ENGINE_VERSION=0.2.0
LABEL org.opencontainers.image.title="M3Undle Provider Simulator" \
      org.opencontainers.image.description="Deterministic IPTV provider and stream-failure simulator for proxy integration testing." \
      org.opencontainers.image.version="${ENGINE_VERSION}" \
      org.opencontainers.image.licenses="MIT"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app

COPY src/ src/
COPY schemas/ schemas/
COPY scenarios/core/ scenarios/core/
COPY fixtures/synthetic/ fixtures/synthetic/

ENV SIMULATOR_PORT=19001

RUN useradd --create-home --uid 10001 simulator \
    && mkdir -p /app/fixtures /app/scenarios \
    && chown -R simulator:simulator /app
USER simulator

EXPOSE 19001

HEALTHCHECK --interval=5s --timeout=2s --start-period=5s --retries=6 \
    CMD python3 -c "\
import os, sys, urllib.request; \
port = os.environ.get('SIMULATOR_PORT', '19001'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2).status == 200 else 1)"

ENTRYPOINT ["python3", "src/provider_sim.py"]
# Local mode's default (--bind 127.0.0.1) isn't reachable from outside the
# container network namespace; a bare `docker run <image>` with no other
# args still needs --bind 0.0.0.0 to be reachable from the host.
CMD ["--bind", "0.0.0.0"]
