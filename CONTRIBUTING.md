# Contributing

## Running the test suite

```bash
pip install -e .
python -m unittest discover -s tests -v
```

No `pytest` or other test runner is required — everything is plain
`unittest`, and the only third-party dependencies are `pyyaml` and
`jsonschema` (see `requirements.txt`/`pyproject.toml`).

## Validating a scenario file

```bash
python src/provider_sim.py --validate scenarios/core/your-scenario.yaml
```

This runs the same fail-closed structural (JSON Schema) and semantic
validation the server performs at startup, without binding a port.

## Self-verifying a scenario end to end

```bash
python src/provider_sim.py --run scenarios/core/your-scenario.yaml --result-file /tmp/result.json
```

Drives the scenario to completion against the engine's own built-in client
and checks `expected_simulator_events` as a real contract — see
[`RUN_MODE.md`](RUN_MODE.md) for the full design.

## Adding a scenario

- New public example scenarios go under `scenarios/core/` and must load and
  `--run` cleanly with **zero external tools** — `tests/test_scenario_validation.py`'s
  `test_valid_scenario_loads_cleanly` enforces this by loading every file in
  that directory.
- A scenario that requires materializing a fixture via the optional
  `media-worker`/`ts-worker` Docker images (see `generate:` in
  `schemas/provider-scenario.schema.json` and `src/materialize_fixtures.py`)
  belongs under `scenarios/generated-examples/` instead, since it can't
  satisfy the Docker-free requirement above.
- Every fixture under `fixtures/synthetic/` must record its generation
  command, generator version, and a SHA-256 hash in
  `fixtures/synthetic/README.md` — verified by
  `src/verify_fixture_hashes.py`.

## Code style

- No comments explaining *what* code does — only *why*, when the reason
  isn't obvious from the code itself (a hidden constraint, a workaround, a
  subtle invariant).
- Keep the engine (`src/provider_sim.py` and friends) free of any
  M3Undle-lab-specific or otherwise external orchestration coupling — it
  must run standalone from any working directory with no special
  environment variables.
- Fail closed: an invalid or ambiguous scenario should refuse to start
  (`SystemExit`), not start half-configured.
