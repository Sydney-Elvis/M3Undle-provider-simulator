# Scenario run/verify mode

This document covers the current Python engine. The public product boundary,
worker-container architecture, clean-host requirement, and third-party
distribution policy are defined in [`README.md`](README.md) and
[`LICENSING.md`](LICENSING.md). Host-installed FFmpeg/TSDuck and cached
M3Undle images are not supported dependencies.

The engine's self-verification mode: `--run <scenario>` executes a scenario
to completion against the engine's own built-in client -- no external test
harness, no `curl` script -- evaluates `expected_simulator_events`
as a real contract, and exits 0 (passed) or 1 (failed). This is what public
CI invokes to prove every example in `scenarios/core/` still behaves as
documented.

```
python3 src/provider_sim.py --run scenarios/core/baseline-05-abrupt-connection-close.yaml
```

Every public scenario in `scenarios/core/` passes under this mode; that's
its exit gate. A scenario whose `expected_simulator_events` lists a type
that never fires produces a failing result artifact and exit code 1 --
`--run` cannot silently pass a scenario that regressed.

## How it drives the scenario

1. Binds an OS-assigned ephemeral port and starts the server.
2. Starts the `ScenarioSequenceRunner` if the scenario has a `sequence:`.
3. Fetches `/playlist.m3u` -- using `provider.authentication`'s configured
   credentials when set, the same way a real downstream client would
   already be configured, not by special-casing authenticated scenarios --
   then reads every listed stream URL to completion in parallel. HTTP
   errors and dropped/reset connections while reading are expected client
   behavior for many baselines (a 503, an abrupt close) and are swallowed;
   whether the scenario passed is decided entirely from the emitted event
   log against `expected_simulator_events`, never from what the driver
   observed on the wire.
4. Waits for the sequence runner and driven client to settle, then sleeps
   `--settle-seconds` (default 2s) so trailing async events land.
5. Matches `expected_simulator_events` against the emitted log as an
   ordered subsequence by event type, logs `scenario_completed`, tears the
   server down, and writes the result.

Each entry in `expected_simulator_events` may be a bare event-type string
(the original shape, action_id unconstrained) or an object that also pins
the matched event's `action_id`:

```yaml
expected_simulator_events:
  - stream_opened
  - event: source_switched
    action_id: swap-1
```

A type match whose `action_id` doesn't equal the requested value does not
stop the scan -- matching keeps looking forward for a later event of the
same type that does carry the right id.

## Scenario-level `run:` settings (optional)

```yaml
run:
  max_duration_s: 30      # overall watchdog for the driven-client + sequence
                           # phase; default 90s
  trigger_timeout_s: 10   # overrides ScenarioSequenceRunner's default 60s
                           # metric-trigger timeout for this scenario
```

## Result artifact

Printed to stdout and, with `--result-file PATH`, also written there:

```json
{
  "run_id": "...",
  "scenario_id": "...",
  "scenario_path": "...",
  "engine_version": "0.2.0",
  "supported_schema_versions": [1],
  "python_version": "3.12.13",
  "dependencies": {"pyyaml": "...", "jsonschema": "..."},
  "passed": true,
  "sequence_aborted": false,
  "failure_reason": null,
  "expected_events": [
    {"event": "stream_opened", "matched": true, "sequence": 2, "virtual_time_ms": 23, "action_id": null}
  ],
  "counters": {
    "chunks_sent_by_channel": {"...": 0},
    "bytes_sent_by_channel": {"...": 0},
    "total_opened": 0, "total_closed": 0, "total_rejected": 0, "total_broken": 0
  },
  "connection_timeline": {
    "<connection_id>": {
      "channel": "...", "first_chunk_index": 0, "last_chunk_index": 0,
      "bytes": 0, "wraparound_detected": false
    }
  }
}
```

`--events-file PATH` additionally appends every emitted event as a JSONL
line (server mode and `--run` mode both support this), so verification and
external adapters don't have to depend on the bounded 200-event
`/debug/state` window. `run_id` is engine-generated unless overridden with
`--run-id` (e.g. for CI correlation), and is present in every event's
envelope, not just `--run`'s.

## Documented determinism tolerances

These are the actual guarantees, not aspirational ones:

- **Per-connection event ordering is strict**: for a single `connection_id`,
  events (`stream_opened` -> ... -> `stream_closed`) are emitted in the
  order they occurred.
- **The global `sequence` number is lock-ordered** across the whole engine
  instance (protected by `ProviderState.lock`), so it is monotonic but
  interleaves across connections/channels in real wall-clock order, not
  per-channel order.
- **Wall-clock pacing drifts by up to one `chunk_interval_ms` per chunk** --
  `time.sleep()` between chunks is not a hard real-time guarantee.
- **Trigger polling granularity is 100ms** -- a `connections`/`chunks`/`bytes`
  trigger can fire up to ~100ms after its target is actually reached.
- **Trigger timeout is 60s by default**, overridable per scenario via
  `run.trigger_timeout_s`. A trigger that never resolves aborts the
  remaining sequence (`scenario_sequence_aborted`) rather than applying its
  action at a meaningless moment.
- **`expected_simulator_events` matching is by event type, ordered
  subsequence** (not exact adjacency). An entry may additionally pin the
  matched event's `action_id`; a type match with the wrong `action_id` does
  not stop the scan, it keeps looking forward for a later event of the same
  type that does carry the right id. Other events may interleave freely
  between expected ones.

## Not yet built

- Stall-then-resume on the same connection (a stall always ends the
  connection today).
- Request-time / live / mid-connection worker-driven mutation -- faults that
  would need rewriting an in-flight stream while a connection is open.
  Scenario-*load-time* worker orchestration (materializing a scenario's
  `generate:`-declared fixture once, before the server starts) is built --
  see the top-level `README.md`'s "Worker orchestration" section -- but
  nothing here mutates a stream while it is being served, and the published
  simulator image still never contains or invokes `ffmpeg`/`tsp` itself.
