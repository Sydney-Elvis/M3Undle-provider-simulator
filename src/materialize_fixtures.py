#!/usr/bin/env python3
"""
Scenario-driven fixture materializer.

Reads a scenario YAML's per-channel `generate:` block and, if the target
isn't already cached, invokes the corresponding worker via `docker compose
--profile workers run` to produce it under fixtures/synthetic/_generated/.

Scope, stated explicitly (this project documents tolerances rather than
glossing over them): this is scenario-LOAD-TIME materialization only. It
runs once, before the simulator starts serving, and is never invoked again
for the lifetime of that process -- it is not live per-connection mutation.
The published simulator image still never invokes ffmpeg/tsp itself; this
script is host-side tooling, excluded from that image (see .dockerignore).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_DIR = REPO_ROOT / "fixtures" / "synthetic"
GENERATED_DIR = SYNTHETIC_DIR / "_generated"

WORKER_SERVICE_NAMES = {"media": "media-worker", "ts": "ts-worker"}


class MaterializationError(RuntimeError):
    """Fail-closed for any of: missing docker/compose, base fixture not
    found, unknown worker, or worker invocation failure. Always carries an
    actionable message, never a bare traceback."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cache_key(worker: str, base_hash: str, args: list[str]) -> str:
    """Content-addressed: changes if the worker, the exact args, or the
    base fixture's own bytes change -- never if only the base fixture's
    name or mtime changes."""
    digest_input = "\x00".join([worker, base_hash, *args])
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]


def find_generate_blocks(scenario_config: dict[str, Any]) -> list[dict[str, Any]]:
    channels = (scenario_config.get("provider") or {}).get("channels") or {}
    blocks = []
    for channel_id, channel_config in channels.items():
        if isinstance(channel_config, dict) and "generate" in channel_config:
            blocks.append({"channel": channel_id, **channel_config["generate"]})
    return blocks


def target_path(worker: str, base: str, args: list[str]) -> Path:
    base_path = SYNTHETIC_DIR / base
    if not base_path.is_file():
        raise MaterializationError(f"generate: base fixture not found: {base_path}")
    key = cache_key(worker, sha256_file(base_path), args)
    return GENERATED_DIR / f"{key}.ts"


def is_cached(target: Path) -> bool:
    """Idempotent: the target's filename IS the hash of its inputs, so
    existence alone proves correctness -- no separate sidecar hash file
    needed (unlike the hand-committed fixtures in fixtures/synthetic/,
    whose filenames are human-chosen, not content-addressed)."""
    return target.is_file()


def _require_docker_cli() -> None:
    if shutil.which("docker") is None:
        raise MaterializationError(
            "generate: requires the `docker` CLI, which was not found on PATH. "
            "Install Docker and Docker Compose to materialize this scenario's fixtures."
        )


def materialize_one(worker: str, base: str, args: list[str]) -> Path:
    target = target_path(worker, base, args)
    if is_cached(target):
        print(f"  cached: {target.relative_to(REPO_ROOT)}", flush=True)
        return target

    _require_docker_cli()
    service = WORKER_SERVICE_NAMES.get(worker)
    if service is None:
        raise MaterializationError(
            f"generate.worker must be one of {sorted(WORKER_SERVICE_NAMES)}, got {worker!r}"
        )

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    container_in = f"/work/{base}"
    container_out = f"/work/_generated/{target.name}"
    substituted = [arg.replace("{IN}", container_in).replace("{OUT}", container_out) for arg in args]

    command = [
        "docker", "compose", "--profile", "workers", "run", "--rm",
        "--user", f"{os.getuid()}:{os.getgid()}",
        service, *substituted,
    ]
    print(f"  materializing: {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=str(REPO_ROOT), text=True, capture_output=True)
    if result.returncode != 0:
        raise MaterializationError(
            f"docker compose run --rm {service} failed (exit {result.returncode}):\n{result.stderr}"
        )
    if not target.is_file():
        raise MaterializationError(
            f"worker {service!r} exited 0 but did not produce {target} -- "
            f"check the {{OUT}} placeholder is used in generate.args"
        )
    return target


def materialize_scenario(scenario_path: Path) -> int:
    scenario_config = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    blocks = find_generate_blocks(scenario_config or {})
    if not blocks:
        print(f"{scenario_path}: no generate: blocks found, nothing to do")
        return 0
    for block in blocks:
        materialize_one(block["worker"], block["base"], block["args"])
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: materialize_fixtures.py <scenario.yaml>", file=sys.stderr)
        return 2
    try:
        return materialize_scenario(Path(argv[0]))
    except MaterializationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
