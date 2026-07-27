# Synthetic Media Fixtures

Every file here is 100% synthetic — generated from FFmpeg's built-in
`testsrc2`/`sine` lavfi sources, no third-party or real-provider content
embedded anywhere. Every fixture records its generation command, generator
version, container digest, and SHA-256 hash below, so a clean checkout can
verify every one independently — see `src/verify_fixture_hashes.py`.

The older commands below record historical provenance and are not the final
public reproduction workflow where they reference `m3undle:branch-main` or a
locally cached image. Public reproduction currently uses the guarded local
FFmpeg worker and the local-build TSDuck worker recipe (the same recipe the
project's separately named TSDuck image will use once published), never a
M3Undle product image or host executable. See [`../../README.md`](../../README.md)
and [`../../LICENSING.md`](../../LICENSING.md).

The engine's own scanner-oriented tests mostly use the minimal synthetic TS
packet sequence `provider_sim.py` generates procedurally per-request
(`payload_mode: ts`), not a stored file — see the `_TS_SEQUENCE` comments in
that module for its PAT/PMT/PID layout. **Know what that synthetic sequence
is and isn't:** it is transport-layer test data — structurally valid TS
packets with a fixed PAT/PMT/PID cycle — but it carries no PTS/PCR
timestamps and its section CRCs are zeroed. It exercises packet/PID/
safe-start scanning (its purpose); it is not decodable media, and tools
like `ffprobe` or VLC will reject or complain about it. A scenario that
needs a real, probeable, decodable stream should use `payload_mode: file`
with one of the fixtures below. The files here back `payload_mode:
file` scenarios that need a real, FFmpeg-probeable stream.

## Playback-contract assets

| File | Container and streams | SHA-256 |
|---|---|---|
| `playback-movie.mkv` | Matroska; MPEG-4 video and Opus audio; 2.008 s | `96e82e36a84bc11e97956f831ba4df814521185c5318d7961459664d0e299c63` |
| `playback-episode.mkv` | Matroska; MPEG-4 video and Opus audio; 2.008 s | `5a404a8a868fe44794f001196110a9fc90e93a057a6099482e02aed675c62d91` |

Generated with FFmpeg 6.1.1 from the `testsrc2`/`sine` lavfi sources. The
movie uses a 440 Hz tone; the episode uses an 880 Hz tone and horizontally
flipped video. Used by `playback_contract.py`'s built-in VOD demo mapping
(`movie:30001`/`movie:30002`/`series:50001`/`series:50002`).

## `clean-relay-loop-long.ts`

Used by `baseline-02-continuous-timestamp-file-loop.yaml` and
`baseline-07-slow-stream-below-nominal-bitrate.yaml` — any scenario that
needs a real, FFmpeg-probeable MPEG-TS stream longer than a couple of
seconds.

Current standalone reproduction. Writes to a `_repro/` scratch subdirectory
rather than the tracked fixture path — compare the resulting hash against the
table below instead of overwriting the committed file directly, and pass your
host UID/GID so the container doesn't leave root-owned output in your
checkout:

```bash
mkdir -p _repro
docker compose --profile workers build media-worker
docker compose --profile workers run --rm --user "$(id -u):$(id -g)" media-worker -y \
  -f lavfi -i testsrc2=size=640x360:rate=25 \
  -f lavfi -i sine=frequency=1000:sample_rate=48000 \
  -t 30 -c:v libx264 -preset veryfast -pix_fmt yuv420p -g 50 \
  -c:a aac -b:a 128k -f mpegts /work/_repro/clean-relay-loop-long.ts
sha256sum _repro/clean-relay-loop-long.ts
```

Historical provenance command:

```
docker run --rm -v <repo>/fixtures/synthetic:/out --entrypoint ffmpeg \
  m3undle:branch-main -y \
  -f lavfi -i testsrc2=size=640x360:rate=25 \
  -f lavfi -i sine=frequency=1000:sample_rate=48000 \
  -t 30 -c:v libx264 -preset veryfast -pix_fmt yuv420p -g 50 \
  -c:a aac -b:a 128k -f mpegts /out/clean-relay-loop-long.ts
```

| File | Container and streams | Duration | Bitrate | SHA-256 |
|---|---|---|---|---|
| `clean-relay-loop-long.ts` | MPEG-TS; H.264 (25fps, GOP 50) and AAC; 1000 Hz tone | 30.021 s (ffprobe) | ~928 kbps (ffprobe) | `ef9d231a7408ea1d978d1a4a1673046ce435126fcc8dbe476837611ee01c6fce` |

FFmpeg version: 6.1.1-3ubuntu5. Container digest at generation time:
`sha256:31430990f916e3c4fab706f8d6a1a2c71a7125c487a6286c366d5b6400123eff`
(a local dev build tag, not a published reference — revisit once a
versioned public build image exists). PID layout: PAT on PID 0, PMT on PID
4096 (`0x1000`), video on PID 256 (`0x100`), audio on PID 257 (`0x101`) —
standard FFmpeg mpegts-muxer defaults, same as every other fixture here.
Expected timestamp range: 1.4587s → 31.48s (`ffprobe` `start_time` +
`duration`).

Sized so that no baseline scenario's `chunk_count` loops this file even
once within a single connection — a scenario that *wants* a restart-from-
zero loop uses `timestamp-rewind-loop.ts` below instead, deliberately.

## `clean-relay-timestamp-discontinuity.ts`

A real, TSDuck-produced PCR/PTS/DTS fault: `clean-relay-loop-long.ts`
spliced with itself at packet 6000 (byte offset 1,128,000 — exact TS
packet boundary; the file is a clean multiple of 188 bytes), with the tail
half's video PID (256) PTS/DTS shifted backward 5 seconds using TSDuck's
`pcredit` plugin. PCR is deliberately left untouched, so the discontinuity
is unsignaled — no `discontinuity_indicator` flag, nothing at the transport
layer announcing it — the "lying provider" case the plan's Q2 media-fault
domain review flagged as the real-world proxy-killer, distinct from a
spec-compliant signaled reset.

Compose pulls the separately named TSDuck worker once published and builds the
same transparent recipe locally until then or when an explicit fallback is
needed.

Writes to the same `_repro/` scratch subdirectory — compare the resulting
hash rather than overwriting the committed fixture:

```
mkdir -p _repro
docker compose --profile workers build ts-worker
dd if=clean-relay-loop-long.ts of=_repro/head.ts bs=188 count=6000
dd if=clean-relay-loop-long.ts of=_repro/tail-original.ts bs=188 skip=6000
docker compose --profile workers run --rm --user "$(id -u):$(id -g)" ts-worker \
  -I file /work/_repro/tail-original.ts \
  -P pcredit --pid 256 --add-pts -5000 --add-dts -5000 --unit millisecond \
  -O file /work/_repro/tail-mutated.ts
cat _repro/head.ts _repro/tail-mutated.ts > _repro/clean-relay-timestamp-discontinuity.ts
sha256sum _repro/clean-relay-timestamp-discontinuity.ts
```

| File | Container and streams | Duration | SHA-256 |
|---|---|---|---|
| `clean-relay-timestamp-discontinuity.ts` | MPEG-TS; H.264 (25fps, GOP 50) and AAC; 1000 Hz tone — identical media to `clean-relay-loop-long.ts`, PTS/DTS discontinuity spliced in | 30.021 s | `fb47e9153da27e3d003b3e264f20cb4b017103168e90a1ccd85525b664bb11d2` |

TSDuck version: 3.44-4676 (`docker/tsduck-worker/Dockerfile`, pinned against
the pinned release selected at generation time and verified against the GitHub release before
building — not guessed). Container digest at generation time:
`sha256:aa7ad240d32c1245c92784206ceb8bb7b47c906cffa6fa0b197ab686fd9b5f23`.

**Verified with TSDuck itself, not just asserted:** `ffprobe` shows video
PTS/DTS at 11.16s immediately before packet 6000 and 6.24s immediately
after (a genuine ~4.92s backward jump, matching the requested 5000ms within
normal frame-timing granularity). `pcrextract` shows PCR on the same PID
continuing its steady ~2,160,000-tick (27MHz clock) cadence straight
through the same boundary with no discontinuity at all — confirming the
splice affected only PTS/DTS, not PCR, i.e. genuinely unsignaled at the
transport layer.

Same FFmpeg 6.1.1-3ubuntu5 build, same `testsrc2`/`sine` lavfi sources, same
container digest as above. Generated with (per-file `-f lavfi`/output
arguments shown in the table). Current standalone template:

```bash
mkdir -p _repro
docker compose --profile workers build media-worker
docker compose --profile workers run --rm --user "$(id -u):$(id -g)" media-worker -y \
  -f lavfi -i testsrc2=size=<WxH>:rate=25 \
  -f lavfi -i sine=frequency=<HZ>:sample_rate=48000 \
  -t <SECONDS> [-vf drawtext=...] \
  -c:v libx264 -preset veryfast -pix_fmt yuv420p -g <GOP> \
  -c:a aac -b:a 128k -f mpegts /work/_repro/<name>.ts
sha256sum _repro/<name>.ts
```

Historical provenance template:

```
docker run --rm --user "$(id -u):$(id -g)" -v <repo>/fixtures/synthetic:/out \
  --entrypoint ffmpeg m3undle:branch-main -y \
  -f lavfi -i testsrc2=size=<WxH>:rate=25 \
  -f lavfi -i sine=frequency=<HZ>:sample_rate=48000 \
  -t <SECONDS> [-vf drawtext=...] \
  -c:v libx264 -preset veryfast -pix_fmt yuv420p -g <GOP> \
  -c:a aac -b:a 128k -f mpegts /out/<name>.ts
```

PID layout for all of these: PAT on PID 0, PMT on PID 4096 (`0x1000`),
video (when present) on PID 256 (`0x100`), audio on PID 257 (`0x101`) when
both streams are present, or PID 256 when audio is the only stream
(`constant-tone.ts`).

| File | Used by | Container and streams | Duration | Expected timestamp range | SHA-256 |
|---|---|---|---|---|---|
| `numbered-frames.ts` | (available, not yet wired into a baseline scenario) | MPEG-TS; H.264 (25fps, GOP 50) with `drawtext` frame-index overlay top-left, plus AAC 1000 Hz tone | 5.021 s | 1.4587s → 6.4800s | `3a9042de20fa71322b3299570007c16231632d3d5cb19a7510400c1539031040` |
| `constant-tone.ts` | (available, not yet wired into a baseline scenario) | MPEG-TS; AAC only, unvarying 1000 Hz sine, no video stream | 4.971 s | 1.4000s → 6.3707s | `01010fe8781a795909122abfe859ab5f55f15f69b93aacb49d0017dd9ccf5639` |
| `video-only.ts` | (available, not yet wired into a baseline scenario) | MPEG-TS; H.264 (25fps, GOP 50) only, no audio stream | 5.000 s | 1.4800s → 6.4800s | `ea6bb42c8271f9c791a56ae945d3e614b646932d73beb1af352313f79b299130` |
| `short-gop.ts` | (available, not yet wired into a baseline scenario) | MPEG-TS; H.264 (25fps, **GOP 5**) and AAC 1000 Hz tone — I-frame roughly every 0.2 s, contrast with `clean-relay-loop-long.ts`'s GOP 50 | 5.021 s | 1.4587s → 6.4800s | `476447bce0fdc5377b5b80390be4c99564653323350a9c6bc465f1019cb6ac53` |
| `resolution-360p.ts` | `baseline-13-midstream-source-replacement.yaml` | MPEG-TS; H.264 640x360 (25fps, GOP 50) and AAC 1000 Hz tone | 5.021 s | 1.4587s → 6.4800s | `8a76ae2c3695eee30a04dd2d6f89c2399c04e2994295719f75bb38b965042b80` |
| `resolution-720p.ts` | `baseline-13-midstream-source-replacement.yaml` | MPEG-TS; H.264 1280x720 (25fps, GOP 50) and AAC 1000 Hz tone — otherwise identical settings to `resolution-360p.ts` so a scenario can switch source mid-stream with only resolution changing | 5.021 s | 1.4587s → 6.4800s | `82ab0a888020f69078c043b39c3a1664d0725142b3c887166b4aa3c0db1c45a9` |
| `timestamp-rewind-loop.ts` | `baseline-03-restart-from-zero-timestamp-loop.yaml` | MPEG-TS; H.264 640x360 (25fps, GOP 50) and AAC 500 Hz tone | 3.021 s | 1.4587s → 4.4800s | `d7647965af2de981796839c8f6bff6c11ee4312b0c7a4c2c01c25284e108524a` |

**On `timestamp-rewind-loop.ts` specifically:** this file is *deliberately*
shorter than `baseline-03`'s `chunk_count`-driven connection duration, so
the engine's byte-modulo wraparound restarts it from byte zero mid-
connection on purpose — a genuine "provider restarted from byte zero"
timestamp rewind, as an intentional fault rather than an accident. Do not
point a clean/fault-free scenario at this file — use
`clean-relay-loop-long.ts` for that.
