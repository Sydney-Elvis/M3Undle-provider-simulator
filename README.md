# M3Undle Provider Simulator

[![CI](https://github.com/Sydney-Elvis/M3Undle-provider-simulator/actions/workflows/ci.yml/badge.svg)](https://github.com/Sydney-Elvis/M3Undle-provider-simulator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A fake IPTV provider that fails on purpose.

Real providers stall, drop connections mid-stream, return `503`/`429`,
replay stale timestamps on reconnect, or hand you malformed MPEG-TS. This
simulator reproduces those failures on demand and deterministically, so you
can test a proxy or player's recovery behavior directly instead of waiting
for it to happen in production.

Not a real IPTV server — no reseller panel, playlist editor, or DVR. It
speaks enough of the Xtream Codes API (auth, `player_api.php`, `get.php` M3U,
`/live/{user}/{pass}/{id}` stream paths) to test against, but it's not a
complete implementation. It exists to break in specific, repeatable ways.

Runs anywhere Docker runs. No host Python, FFmpeg, or TSDuck required.

## Quickstart

```bash
docker compose up --build
curl http://127.0.0.1:19001/health
curl http://127.0.0.1:19001/playlist.m3u
```

Or as a Python package (editable install from a checkout):

```bash
pip install -e .
provider-simulator --scenario scenarios/core/baseline-01-clean-continuous-live-stream.yaml
```

## Scenarios

A scenario is YAML: provider/channel config plus an optional `sequence` of
triggers (`after: seconds` / `after: connections`) and actions
(`set_channel_behavior`, `wait`, ...) that mutate the provider's behavior
mid-run. No `sequence` at all just makes it a static fixture. See
`scenarios/core/` for the baseline set — clean streams, stalls, aborts, HTTP
faults, TS-layer faults, multiple clients sharing a stream, source swaps,
auth — and `schemas/provider-scenario.schema.json` for the format.

Every scenario checks itself:

```bash
provider-simulator --run scenarios/core/baseline-08-temporary-http-503.yaml --result-file /tmp/result.json
```

gives you a pass/fail result with which expected events actually fired, no
external client needed.

## Workers

The simulator image never bundles FFmpeg or TSDuck. When a scenario needs
real media mutation (`generate:` on a channel, e.g. shifting an audio PID's
timestamps), `run-scenario.sh` builds it once via a separate worker
container and caches the result by content hash — before the simulator
starts, never live or per-connection:

```bash
docker compose --profile workers build
./run-scenario.sh scenarios/generated-examples/audio-pid-timestamp-offset-generated.yaml -- \
  --run scenarios/generated-examples/audio-pid-timestamp-offset-generated.yaml --result-file /tmp/result.json
```

`scenarios/core/` stays worker-free by design — it has to load with zero
external tools. Anything needing a worker lives under
`scenarios/generated-examples/` instead.

Why the split, and the exact FFmpeg/TSDuck licensing stance: see
[`LICENSING.md`](LICENSING.md).

## Layout

```text
src/                            engine (provider_sim.py) and support modules
schemas/                        scenario JSON Schema
scenarios/core/                 baseline scenarios, no external tools needed
scenarios/generated-examples/   scenarios needing a `generate:` worker pass
fixtures/synthetic/             hash-verified static media fixtures
docker/                         worker Dockerfiles (media-worker, ts-worker)
tests/                          python -m unittest discover -s tests
```

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Security issues:
[`SECURITY.md`](SECURITY.md). Licensing and worker provenance:
[`LICENSING.md`](LICENSING.md), [`THIRD_PARTY.md`](THIRD_PARTY.md),
[`WORKER_PROVENANCE.md`](WORKER_PROVENANCE.md).
